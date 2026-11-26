"""the queue executor reruns a CI job lost to infrastructure, once.

The gate recognizes the loss and owns the bound. Pinned here is what the
executor owns: it asks the gate once for a PR it was about to park
`ci_failed`, keeps that PR active while the rerun runs, and parks exactly as
before on every other answer. A deploy that arrives first still yields the
pass: the rerun is a forge write, like a refresh or a merge.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import deploy_chain.state as deploy_state
import pytest
from gate_server_estate import EstateGateOperations
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _core_repo,
    _in_progress,
    _queue_inspect,
    _queue_store,
    merge_open_prs,
)

JOBS = [{"id": 3, "name": "leaf-test-2", "previous_attempt": 1}]
STARTED = {"ok": True, "code": "RERUN_STARTED", "run_id": 11, "head_sha": "a" * 40,
           "rerun_jobs": JOBS, "comment_created": True}
HELD = {"pr": 1, "outcome": "retained", "reason": "CI_RERUN_LOST", "run_id": 11,
        "head_sha": "a" * 40, "rerun_jobs": JOBS}
# What the pass records for a PR it parks `ci_failed` today, with PR 2 elected after it.
PARKED_AS_BEFORE = [
    {"pr": 1, "outcome": "retained", "reason": "CI_RUN_FAILED"},
    {"pr": 2, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"},
]


class RerunGate(FakeGateApplication):
    """The fixture application plus the one extra operation these tests need."""

    def __init__(self, *args, answer, **kwargs):
        super().__init__(*args, **kwargs)
        self.answer = answer

    def rerun_lost(self, repository, pr):
        self.calls.append(("rerun_lost", repository, pr))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _red(**extra):
    return _queue_inspect(ci="red", conclusion="failure", run=11,
                          blocking_reason="CI_RUN_FAILED", **extra)


def _gate(answer, first=None, cls=RerunGate):
    responses = {("inspect", "core-repo", 1): first or _red(),
                 ("inspect", "core-repo", 2): _in_progress(run=12)}
    if cls is RerunGate:
        return RerunGate({"core-repo": [1, 2]}, answer=answer, responses=responses)
    return cls({"core-repo": [1, 2]}, responses=responses)


def _pass(gate, store, tmp_path, at=1000.0):
    return merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: at)[0]


def _asked(gate):
    return [call for call in gate.calls if call[0] == "rerun_lost"]


def _row(store):
    return store.read()["repositories"]["core-repo"]


def _lost_while_waiting(tmp_path, answer=STARTED):
    """PR 1 waits on a running run at t=1000 and reads red at t=2000: the job was lost."""
    store = _queue_store(tmp_path)
    gate = _gate(answer, first=_in_progress(run=11))
    waiting = _pass(gate, store, tmp_path)
    assert waiting["results"] == [{"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    gate.responses[("inspect", "core-repo", 1)] = _red()
    return store, gate


def test_a_lost_job_is_rerun_and_the_pr_keeps_its_slot(tmp_path):
    store, gate = _lost_while_waiting(tmp_path)

    summary = _pass(gate, store, tmp_path, at=2000.0)

    assert summary["results"] == [HELD]
    assert _asked(gate) == [("rerun_lost", "core-repo", 1)]
    row = _row(store)
    assert row["parked"] == {}
    assert row["active"]["pr"] == 1
    # The rerun starts a run the queue must wait for: the window runs from now.
    assert (row["active"]["wait_since"], row["active"]["started"]) == (2000.0, False)
    # A rerun is a CI wait, not a PR stuck on one reason: it starts no stuck count.
    assert "retained" not in row["active"]
    assert not any(call[0] in {"review", "merge", "refresh"} for call in gate.calls)


def test_the_rerun_that_turns_green_merges_without_a_second_ask(tmp_path):
    store, gate = _lost_while_waiting(tmp_path)
    _pass(gate, store, tmp_path, at=2000.0)

    gate.responses[("inspect", "core-repo", 1)] = _in_progress(run=11)
    running = _pass(gate, store, tmp_path, at=2060.0)
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(run=11)
    merged = _pass(gate, store, tmp_path, at=2120.0)

    assert running["results"] == [{"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    # The queue goes on to PR 2 in the same pass, as after any merge.
    assert merged["results"] == [{"pr": 1, "outcome": "merged"},
                                 {"pr": 2, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert _asked(gate) == [("rerun_lost", "core-repo", 1)]
    assert [call for call in gate.calls if call[0] == "merge"] == [
        ("merge", "core-repo", 1, "a" * 40, "b" * 40)]
    assert _row(store)["parked"] == {}


def test_an_allowlisted_flake_rerun_keeps_the_slot_like_a_lost_job(tmp_path):
    """the gate also starts a rerun for an allowlisted flake. The executor
    reads only the code, so it holds the PR exactly as for a lost job."""
    flake = {**STARTED, "basis": "allowlisted_flake", "flake_jobs": [
        {"id": 3, "name": "leaf-test-2", "tests": ["t.py::test_x"], "tickets": ["T-1"]}]}
    store, gate = _lost_while_waiting(tmp_path, answer=flake)

    summary = _pass(gate, store, tmp_path, at=2000.0)

    assert summary["results"] == [HELD]
    assert _row(store)["parked"] == {} and _row(store)["active"]["pr"] == 1


def test_a_second_red_after_the_rerun_parks_the_pr(tmp_path):
    store, gate = _lost_while_waiting(tmp_path)
    _pass(gate, store, tmp_path, at=2000.0)

    # The gate's own bound answers the second ask: the executor keeps none.
    gate.answer = {"ok": False, "code": "RERUN_LIMIT_REACHED"}
    gate.responses[("inspect", "core-repo", 1)] = _red(attempt=2)
    summary = _pass(gate, store, tmp_path, at=2060.0)

    assert [row["reason"] for row in summary["results"]] == ["CI_RUN_FAILED", "CI_RUN_IN_PROGRESS"]
    assert len(_asked(gate)) == 2
    row = _row(store)
    assert row["parked"]["1"]["reason"] == "ci_failed"
    assert row["active"]["pr"] == 2


# Every answer that is not a started rerun: the executor parks exactly as before.
DECLINED = [
    {"ok": False, "code": "RERUN_NOT_LOST"},
    {"ok": False, "code": "RERUN_LIMIT_REACHED"},
    {"ok": False, "code": "RERUN_NOT_RED"},
    {"ok": False, "code": "RERUN_NO_FAILED_JOBS"},
    {"ok": False, "code": "RERUN_JOBS_UNREADABLE"},
    {"ok": False, "code": "RERUN_REFUSED"},
    {"ok": False, "code": "EXECUTOR_HOST_REQUIRED"},
    {"ok": False, "code": "RERUN_STARTED"},
    {"ok": True, "code": "RERUN_NOT_LOST"},
    {"ok": True},
    RuntimeError("boom"),
    None,
    "RERUN_STARTED",
]


@pytest.mark.parametrize("answer", DECLINED, ids=repr)
def test_any_other_answer_parks_the_pr_as_before(tmp_path, answer):
    store = _queue_store(tmp_path)
    gate = _gate(answer)

    summary = _pass(gate, store, tmp_path)

    assert summary["results"] == PARKED_AS_BEFORE
    assert _asked(gate) == [("rerun_lost", "core-repo", 1)]
    row = _row(store)
    assert row["parked"]["1"]["reason"] == "ci_failed"
    assert row["active"]["pr"] == 2


def test_an_application_without_the_operation_parks_the_pr_as_before(tmp_path):
    store = _queue_store(tmp_path)

    summary = _pass(_gate(None, cls=FakeGateApplication), store, tmp_path)

    assert summary["results"] == PARKED_AS_BEFORE
    assert _row(store)["parked"]["1"]["reason"] == "ci_failed"


@pytest.mark.parametrize(("first", "park"), [
    # The reviewer's rejection outranks the red run: no rerun fixes it.
    (_queue_inspect(ci="red", conclusion="failure", run=11, review="changes_requested"),
     "changes_requested"),
    # A run on an old base is refreshed with the PR, not rerun on its old merge ref.
    (_queue_inspect(ci="pending", conclusion="failure", run=11, blocking_reason="CI_BASE_STALE",
                    ci_reason="CI_BASE_STALE"), "ci_failed"),
], ids=["changes_requested", "stale_base"])
def test_the_gate_is_never_asked_for_a_pr_parked_for_another_reason(tmp_path, first, park):
    store = _queue_store(tmp_path)
    gate = _gate(STARTED, first=first)

    _pass(gate, store, tmp_path)

    assert _asked(gate) == []
    assert _row(store)["parked"]["1"]["reason"] == park


@pytest.fixture
def deploy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "deploy.lock"
    path.write_text("")
    path.chmod(0o644)
    monkeypatch.setattr(deploy_state, "DEPLOY_LOCK_PATH", path)
    return path


def test_a_deploy_arriving_before_the_rerun_yields_the_pass_and_keeps_the_pr(tmp_path, deploy_path):
    store = _queue_store(tmp_path)
    gate = _gate(STARTED)
    read = gate.inspect
    holders: list[int] = []

    def inspect(repository, pr):
        answer = read(repository, pr)
        if pr == 1 and answer.get("ci_state") == "red" and not holders:
            holders.append(os.open(deploy_path, os.O_RDONLY))
            fcntl.flock(holders[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        return answer

    gate.inspect = inspect
    try:
        summary = _pass(gate, store, tmp_path)
    finally:
        for fd in holders:
            os.close(fd)

    assert holders, "the read must exercise the deployment arrival"
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert _asked(gate) == []
    assert _row(store)["parked"] == {}


# The review-ahead scan parks a red Renovate PR before its turn (`renovate_ci_failed`).
# It asks the gate for the same one rerun first.

AHEAD_HEADS = {1: "task/x", 2: "renovate/a", 3: "task/y", 4: "renovate/b"}


def _ahead_gate(answer, second=None, heads=None):
    """PR 1 is active and waits on CI, queued PR 2 reads red, PR 4 can be reviewed ahead."""
    heads = heads or AHEAD_HEADS
    labels = {pr: ["renovate"] if head.startswith("renovate/") else []
              for pr, head in heads.items()}
    return RerunGate(
        {"core-repo": [1, 2, 3, 4]}, answer=answer,
        head_refs={"core-repo": heads}, labels={"core-repo": labels},
        responses={("inspect", "core-repo", 1): _in_progress(),
                   ("inspect", "core-repo", 2): second or _red(),
                   ("inspect", "core-repo", 3): _in_progress(),
                   ("inspect", "core-repo", 4): _queue_inspect(head="e", base="f",
                                                               review="not_approved")})


def _pass_ahead(gate, store, tmp_path):
    return merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1, now=lambda: 1000.0)[0]


def _reviewed(gate):
    return [call[2] for call in gate.calls if call[0] == "review"]


def test_review_ahead_reruns_a_lost_renovate_job_instead_of_parking_it(tmp_path):
    store = _queue_store(tmp_path)
    gate = _ahead_gate(STARTED)

    summary = _pass_ahead(gate, store, tmp_path)

    row = _row(store)
    assert (row["active"]["pr"], row["parked"]) == (1, {})
    assert {**HELD, "pr": 2, "review_ahead": True} in summary["results"]
    assert _asked(gate) == [("rerun_lost", "core-repo", 2)]
    # The rerun spends no review: the next queued PR still gets the one review ahead.
    assert _reviewed(gate) == [4]
    assert not any(call[0] in {"refresh", "merge"} and call[2] == 2 for call in gate.calls)


@pytest.mark.parametrize("answer", DECLINED, ids=repr)
def test_review_ahead_parks_the_renovate_pr_on_any_other_answer(tmp_path, answer):
    store = _queue_store(tmp_path)
    gate = _ahead_gate(answer)

    summary = _pass_ahead(gate, store, tmp_path)

    assert {"pr": 2, "outcome": "retained", "reason": "CI_RUN_FAILED",
            "review_ahead": True} in summary["results"]
    assert _asked(gate) == [("rerun_lost", "core-repo", 2)]
    assert _row(store)["parked"]["2"]["reason"] == "renovate_ci_failed"
    assert _reviewed(gate) == [4]


@pytest.mark.parametrize(("second", "heads"), [
    # A red run on a moved base keeps its one catch-up refresh on the active path.
    (_queue_inspect(ci="red", conclusion="failure", run=11, review="not_approved",
                    blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
                    next_action="refresh_task"), None),
    # An agent PR is not parked early: it meets the active path's ask at its own turn.
    (_red(review="not_approved"), {**AHEAD_HEADS, 2: "task/z"}),
], ids=["renovate_on_a_moved_base", "agent_pr"])
def test_review_ahead_does_not_ask_for_a_pr_it_would_not_park(tmp_path, second, heads):
    store = _queue_store(tmp_path)
    gate = _ahead_gate(STARTED, second=second, heads=heads)

    _pass_ahead(gate, store, tmp_path)

    assert _asked(gate) == []
    assert _row(store)["parked"] == {}


def test_a_deploy_arriving_before_the_review_ahead_rerun_yields_the_pass(tmp_path, deploy_path):
    store = _queue_store(tmp_path)
    gate = _ahead_gate(STARTED)
    read = gate.inspect
    holders: list[int] = []
    reads_of_pr_2: list[int] = []

    def inspect(repository, pr):
        answer = read(repository, pr)
        if pr == 2:
            reads_of_pr_2.append(pr)
            # The selection reads PR 2 first; the review-ahead scan reads it again
            # and asks the gate next, with no other checkpoint between the two.
            if len(reads_of_pr_2) == 2:
                holders.append(os.open(deploy_path, os.O_RDONLY))
                fcntl.flock(holders[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        return answer

    gate.inspect = inspect
    try:
        summary = _pass_ahead(gate, store, tmp_path)
    finally:
        for fd in holders:
            os.close(fd)

    assert holders, "the read must exercise the deployment arrival"
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert _asked(gate) == []
    assert _row(store)["parked"] == {}


# The server adapter seam: the executor's application reaches the gate through this class.


def _operations(gate, authority=(True, ""), uid=0):
    return EstateGateOperations(gate_factory=lambda **kwargs: gate,
                                executor_authority=lambda: authority,
                                effective_uid=lambda: uid)


@pytest.mark.parametrize(("authority", "uid"), [((False, "not selected"), 0), ((True, ""), 1000)])
def test_rerun_lost_requires_the_same_mutation_authority_as_merge(authority, uid):
    operations = EstateGateOperations(
        gate_factory=lambda **kwargs: (_ for _ in ()).throw(AssertionError("gate called")),
        private_review=lambda argv: (_ for _ in ()).throw(AssertionError("review called")),
        executor_authority=lambda: authority,
        effective_uid=lambda: uid,
    )
    assert operations.rerun_lost("core-repo", 7) == {"ok": False, "code": "EXECUTOR_HOST_REQUIRED"}


def test_rerun_lost_dispatches_the_rerun_with_the_automatic_flag():
    captured = []
    gate = SimpleNamespace(execute=lambda *args, **kwargs: captured.append((args, kwargs)) or {
        "ok": True, "code": "RERUN_STARTED", "run_id": 11, "_internal": object()})

    result = _operations(gate).rerun_lost("core-repo", 7)

    assert result == {"ok": True, "code": "RERUN_STARTED", "run_id": 11}
    assert captured == [(("rerun-ci", "core-repo", "7"), {"rerun_lost": True})]


def test_rerun_lost_reports_a_gate_refusal_by_code_only():
    from git_gate.contracts import fail_with

    def refuse(*_args, **_kwargs):
        fail_with("RERUN_NOT_LOST", job_id=3)

    assert _operations(SimpleNamespace(execute=refuse)).rerun_lost("core-repo", 7) == {
        "ok": False, "code": "RERUN_NOT_LOST"}


@pytest.mark.parametrize(("authority", "uid"), [((False, "not selected"), 0), ((True, ""), 1000)])
def test_rerun_lost_main_requires_the_same_mutation_authority_as_merge(authority, uid):
    operations = EstateGateOperations(
        gate_factory=lambda **kwargs: (_ for _ in ()).throw(AssertionError("gate called")),
        private_review=lambda argv: (_ for _ in ()).throw(AssertionError("review called")),
        executor_authority=lambda: authority,
        effective_uid=lambda: uid,
    )
    assert operations.rerun_lost_main("core-repo") == {"ok": False, "code": "EXECUTOR_HOST_REQUIRED"}


def test_rerun_lost_main_dispatches_the_rerun_for_main_with_the_automatic_flag():
    captured = []
    record = {"ok": True, "code": "RERUN_STARTED", "run_id": 11, "head_sha": "a" * 40,
              "rerun_jobs": JOBS}
    gate = SimpleNamespace(execute=lambda *args, **kwargs: captured.append((args, kwargs)) or {
        **record, "_internal": object()})

    result = _operations(gate).rerun_lost_main("core-repo")

    # main has no PR audit comment: the caller gets the run, the head and the jobs.
    assert result == record
    assert captured == [(("rerun-ci", "core-repo", "main"), {"rerun_lost": True})]


def test_rerun_lost_main_reports_a_gate_refusal_by_code_only():
    from git_gate.contracts import GateError, fail_with

    def refuse(*_args, **_kwargs):
        fail_with("RERUN_NOT_LOST", job_id=3)

    def refuse_before_any_start(*_args, **_kwargs):
        raise GateError("RERUN_REFUSED", {"http_status": 403, "job_id": 3, "rerun_started": []})

    assert _operations(SimpleNamespace(execute=refuse)).rerun_lost_main("core-repo") == {
        "ok": False, "code": "RERUN_NOT_LOST"}
    assert _operations(SimpleNamespace(execute=refuse_before_any_start)).rerun_lost_main("core-repo") == {
        "ok": False, "code": "RERUN_REFUSED"}


def test_rerun_lost_main_names_the_jobs_a_refused_rerun_had_already_started():
    from git_gate.contracts import GateError

    def refuse_after_two_starts(*_args, **_kwargs):
        raise GateError("RERUN_REFUSED", {"http_status": 403, "job_id": 7, "rerun_started": [5, 6]})

    # No PR holds a partial audit comment for main, so the refusal is the only record.
    assert _operations(SimpleNamespace(execute=refuse_after_two_starts)).rerun_lost_main("core-repo") == {
        "ok": False, "code": "RERUN_REFUSED", "rerun_started": [5, 6]}
