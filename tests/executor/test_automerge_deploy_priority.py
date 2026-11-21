"""Deploy intent finishes one review and yields the gate without parking its PR."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import deploy_chain.state as deploy_state
import pytest
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _core_repo,
    _queue_inspect,
    _queue_store,
    merge_open_prs,
)


@pytest.fixture
def deploy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "deploy.lock"
    path.write_text("")
    path.chmod(0o644)
    monkeypatch.setattr(deploy_state, "DEPLOY_LOCK_PATH", path)
    return path


def _hold(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


def test_waiting_deploy_yields_at_start_without_a_candidate_action(
    tmp_path: Path, deploy_path: Path,
) -> None:
    gate = FakeGateApplication({"core-repo": [1, 2]})
    store = _queue_store(tmp_path)
    fd = _hold(deploy_path)
    try:
        summary = merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store,
            executor_run_prefix="systemd:merge-guard-automerge:test",
        )[0]
    finally:
        os.close(fd)
    assert summary["ok"] is True
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert summary["executor_run"] == "systemd:merge-guard-automerge:test"
    assert not any(call[0] in {"inspect", "review", "merge", "refresh"} for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["parked"] == {}
    assert gate.statuses[-1][1] == {1: "queue=held reason=deploy_in_progress owner=operator pos=-",
                                     2: "queue=held reason=deploy_in_progress owner=operator pos=-"}


@pytest.mark.parametrize("ahead", [False, True])
def test_review_publication_finishes_before_yield_and_no_second_action_starts(
    tmp_path: Path, deploy_path: Path, ahead: bool,
) -> None:
    target = 2 if ahead else 1
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending" if ahead else "green", review="missing", conclusion=None,
        ),
        ("inspect", "core-repo", 2): _queue_inspect(review="missing"),
    })
    store = _queue_store(tmp_path)
    holders: list[int] = []
    published: list[int] = []

    def review(repository: str, pr: int, head: str, base: str,
               executor_run: str, retry_of: str | None = None) -> dict:
        gate.calls.append(("review", repository, pr, head, base, executor_run, retry_of))
        holders.append(_hold(deploy_path))
        published.append(pr)  # The gate's .review returns only after publication.
        return {"ok": True, "dispatched": True, "verdict": "APPROVED"}

    gate.review = review
    try:
        summary = merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store,
            max_reviews=3, max_reviews_per_repo=1,
        )[0]
    finally:
        for fd in holders:
            os.close(fd)
    assert published == [target]
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert not any(call[0] in {"merge", "refresh"} for call in gate.calls)
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1
    assert row["parked"] == {}
    # A held write after unwind proves _publish_hold reacquired the real
    # executor lock instead of recursively locking the active pass.
    assert gate.statuses[-1][1][target] == "queue=held reason=deploy_in_progress owner=operator pos=-"


def test_deploy_intent_after_one_refresh_blocks_the_next_repository(
    tmp_path: Path, deploy_path: Path,
) -> None:
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [2]}, responses={
        ("inspect", name, pr): _queue_inspect(ci="pending", blocking_reason="CI_BASE_STALE")
        for name, pr in (("core-repo", 1), ("docs-repo", 2))
    })
    store = _queue_store(tmp_path)
    original_refresh = gate.refresh
    holders: list[int] = []

    def refresh(repository: str, pr: int, head: str, base: str) -> dict:
        result = original_refresh(repository, pr, head, base)
        holders.append(_hold(deploy_path))
        return result

    gate.refresh = refresh
    try:
        summaries = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store)
    finally:
        for fd in holders:
            os.close(fd)
    assert holders, "the first refresh must announce a real deploy intent"
    assert [call[1:3] for call in gate.calls if call[0] == "refresh"] == [("core-repo", 1)]
    assert ("inspect", "docs-repo", 2) not in gate.calls
    assert all(summary["hold"]["reason"] == "DEPLOY_WAITING" for summary in summaries)
    assert all(row["parked"] == {} for row in store.read()["repositories"].values())


def test_new_deploy_intent_during_preparation_prevents_every_hold_release(
    tmp_path: Path, deploy_path: Path,
) -> None:
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    gate = FakeGateApplication(
        {"core-repo": [1], "docs-repo": [2]},
        head_refs={"core-repo": {1: "task/x"}, "docs-repo": {2: "renovate/x"}},
        labels={"core-repo": {1: []}, "docs-repo": {2: ["renovate", "premerge"]}},
    )
    store = _queue_store(tmp_path)
    merge_open_prs._publish_hold(repositories, {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    gate.statuses.clear()
    original_inspect = gate.inspect
    holders: list[int] = []

    def inspect(repository: str, pr: int) -> dict:
        result = original_inspect(repository, pr)
        if repository == "docs-repo":
            holders.append(_hold(deploy_path))
        return result

    gate.inspect = inspect
    try:
        summaries = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store)
    finally:
        for fd in holders:
            os.close(fd)
    assert holders, "the final repository's preparation must encounter a real deploy intent"
    assert all(summary["hold"]["reason"] == "DEPLOY_WAITING" for summary in summaries)
    assert gate.statuses == [], "no prior hold may be released after deployment announces intent"
    assert not any(call[0] in {"review", "merge", "refresh"} for call in gate.calls)
    assert all(row["held"] == "-" for row in store.read()["repositories"].values())


@pytest.mark.parametrize("action", ["merge", "refresh", "train_build", "train_member_merge", "train_close"])
def test_each_gate_mutation_rechecks_deploy_intent(action: str) -> None:
    calls: list[str] = []

    class Application:
        def __getattr__(self, name: str):
            return lambda *args: calls.append(name)

    budget = merge_open_prs._ReviewBudget(Application(), None)

    def yield_pass() -> None:
        raise merge_open_prs._DeployYield("DEPLOY_WAITING", "held", [])

    budget.checkpoint = yield_pass
    with pytest.raises(merge_open_prs._DeployYield):
        getattr(budget, action)("core-repo", 1)
    assert calls == []


def test_unusable_deploy_probe_refuses_the_pass_without_parking(
    tmp_path: Path, deploy_path: Path,
) -> None:
    deploy_path.chmod(0o666)
    gate = FakeGateApplication({"core-repo": [1]})
    store = _queue_store(tmp_path)
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert summary["ok"] is False
    assert summary["hold"]["reason"] == "DEPLOY_LOCK_UNAVAILABLE"
    assert summary["error"] == "DEPLOY_LOCK_UNAVAILABLE"
    assert gate.calls == []
    assert store.read()["repositories"] == {}


def test_deploy_intent_during_review_status_prevents_dispatch(
    tmp_path: Path, deploy_path: Path,
) -> None:
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="missing"),
    })
    store = _queue_store(tmp_path)
    original_status = gate.queue_status
    holders: list[int] = []

    def publish_status(repository, descriptions, published):
        if not holders and any("reason=reviewing" in value[0] for value in descriptions.values()):
            holders.append(_hold(deploy_path))
        return original_status(repository, descriptions, published)

    gate.queue_status = publish_status
    try:
        summary = merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        )[0]
    finally:
        for fd in holders:
            os.close(fd)
    assert holders, "the status callback must exercise the deployment arrival"
    assert not any(call[0] == "review" for call in gate.calls)
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert store.read()["repositories"]["core-repo"]["parked"] == {}


def test_deploy_intent_during_review_ahead_yields_at_the_next_repository(
    tmp_path: Path, deploy_path: Path,
) -> None:
    """Review ahead only inspects between reviews, and an inspection is no
    gate mutation, so the boundary between its repositories must probe."""
    repositories = [_core_repo(tmp_path), merge_open_prs.Repository(
        "docs-repo", tmp_path / "docs-repo", "forgejo", "acme/docs-repo")]
    waiting = _queue_inspect(ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS")
    gate = FakeGateApplication({"core-repo": [1, 2], "docs-repo": [5, 6]}, responses={
        ("inspect", name, pr): waiting
        for name, pr in (("core-repo", 1), ("core-repo", 2), ("docs-repo", 5))
    } | {("inspect", "docs-repo", 6): _queue_inspect(review="missing")})
    store = _queue_store(tmp_path)
    original_inspect = gate.inspect
    holders: list[int] = []

    def inspect(repository: str, pr: int) -> dict:
        if (repository, pr) == ("core-repo", 2):  # Reached only by review ahead.
            holders.append(_hold(deploy_path))
        return original_inspect(repository, pr)

    gate.inspect = inspect
    try:
        summaries = merge_open_prs.run_automerge_pass(
            repositories, gate, queue_store=store, max_reviews=2, max_reviews_per_repo=1)
    finally:
        for fd in holders:
            os.close(fd)
    assert holders, "review ahead must reach the second core-repo PR"
    assert ("inspect", "docs-repo", 6) not in gate.calls
    assert not any(call[0] == "review" for call in gate.calls)
    assert all(summary["hold"]["reason"] == "DEPLOY_WAITING" for summary in summaries)
    assert all(row["parked"] == {} for row in store.read()["repositories"].values())


def test_a_mid_pass_deploy_yield_stamps_the_end_of_its_work(
    tmp_path: Path, deploy_path: Path,
) -> None:
    """The next pass measures executor downtime from `last_pass`; the work
    done before a yield is not downtime and must not be re-based as one."""
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="missing"),
    })
    store = _queue_store(tmp_path)
    holders: list[int] = []
    stamps: list[float] = []

    def review(repository: str, pr: int, head: str, base: str,
               executor_run: str, retry_of: str | None = None) -> dict:
        gate.calls.append(("review", repository, pr, head, base, executor_run, retry_of))
        entry = store.read()["last_pass"]
        deadline = time.monotonic() + 5
        while time.time() <= entry and time.monotonic() < deadline:
            time.sleep(0.001)
        stamps.extend([entry, time.time()])
        holders.append(_hold(deploy_path))
        return {"ok": True, "dispatched": True, "verdict": "APPROVED"}

    gate.review = review
    try:
        summary = merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store)[0]
    finally:
        for fd in holders:
            os.close(fd)
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    entry, reviewed = stamps
    assert entry < reviewed, "the host clock must advance past the entry stamp"
    assert store.read()["last_pass"] >= reviewed


def test_a_yield_before_any_work_keeps_the_gap_for_the_next_pass(
    tmp_path: Path, deploy_path: Path,
) -> None:
    """A pass that yields at its first probe did no work, so the downtime
    before it is still owed a re-base by the next pass that runs."""
    store = _queue_store(tmp_path)
    gap_start = time.time() - 12 * 3600
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": {"pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": gap_start - 30, "started": True},
            "parked": {}}
        state["last_pass"] = gap_start
        store.save(state)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS")})
    fd = _hold(deploy_path)
    try:
        held = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    finally:
        os.close(fd)
    assert held["hold"]["reason"] == "DEPLOY_WAITING"
    assert store.read()["last_pass"] == gap_start

    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert summary["results"] == [{"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert summary["head_wait_s"] == pytest.approx(30, abs=2)


def _iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat(
        timespec="seconds")


def _write_marker(**fields: object) -> None:
    marker = {"schema": "deploy-marker.v1", "origin": "origin-host", "plan_id": "0123abcd",
              "state": "running", "started_at": _iso(3600), "claimed_at": _iso(60),
              "applied_at": _iso(30), **fields}
    path = merge_open_prs.DEPLOY_MARKER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: value for key, value in marker.items()
                                if value is not None}))


def _pass_under_held_lock(tmp_path: Path, deploy_path: Path) -> tuple[dict, list]:
    gate = FakeGateApplication({"core-repo": [1]})
    fd = _hold(deploy_path)
    try:
        summary = merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path))[0]
    finally:
        os.close(fd)
    return summary, gate.calls


def test_a_held_outer_lock_does_not_yield_once_its_deploy_has_applied_source(
    tmp_path: Path, deploy_path: Path,
) -> None:
    """The holder released the executor lock after source apply; its remaining
    steps leave the executor's code alone, so the pass goes on."""
    _write_marker()
    summary, calls = _pass_under_held_lock(tmp_path, deploy_path)
    assert "hold" not in summary
    assert any(call[0] == "inspect" for call in calls)


@pytest.mark.parametrize("fields", [
    None, {"applied_at": None}, {"applied_at": "soon"},
    {"applied_at": "2020-01-01T00:30:00"}, {"state": "failed"},
    {"claimed_at": _iso(deploy_state.DEPLOY_MARKER_TTL_SECONDS + 60)},
    {"claimed_at": None}, {"schema": "deploy-marker.v0"},
], ids=["absent", "no-applied-at", "not-iso", "no-offset", "failed-with-applied-at",
        "expired", "no-claimed-at", "other-schema"])
def test_a_held_outer_lock_still_yields_unless_a_fresh_running_marker_records_applied_source(
    tmp_path: Path, deploy_path: Path, fields: dict | None,
) -> None:
    """A new deploy may hold the outer lock while it waits for the executor."""
    if fields is not None:
        _write_marker(**fields)
    summary, calls = _pass_under_held_lock(tmp_path, deploy_path)
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert not any(call[0] in {"inspect", "review", "merge", "refresh"} for call in calls)


def test_an_unusable_probe_refuses_even_with_an_applied_marker(
    tmp_path: Path, deploy_path: Path,
) -> None:
    _write_marker()
    deploy_path.chmod(0o666)
    gate = FakeGateApplication({"core-repo": [1]})
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path))[0]
    assert summary["hold"]["reason"] == "DEPLOY_LOCK_UNAVAILABLE"
    assert gate.calls == []
