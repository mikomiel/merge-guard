"""`merge_open_prs.py --reopen-review`: the operator reopen of exhausted review chains.

The review owner decides eligibility and moves the chain (the gate reopen
tests own that). Pinned here is what the queue CLI owns: the executor lock,
the selection, the arguments it hands the owner, and that only an applied
reopen ends a `review_execution_failed` park.
"""

from __future__ import annotations

import json
import subprocess

import gate_server_estate
import pytest
from merge_open_prs_fixtures import merge_open_prs

HEAD = "a" * 40
BASE = "b" * 40
REASON = "The reviewer exited 1 under the unit sandbox"


def _park(reason: str) -> dict:
    return {"reason": reason, "head": HEAD, "run": [11, 1], "review": [None, "none"]}


class Application:
    def __init__(self, inspected: dict | None = None):
        self.inspected = inspected
        self.calls: list[tuple[str, int]] = []

    def inspect(self, repository, pr):
        self.calls.append((repository, pr))
        return self.inspected or {"ok": True, "pr_state": "open", "head_sha": HEAD,
                                  "base_sha": BASE}


@pytest.fixture
def queue(monkeypatch, tmp_path):
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    store = merge_open_prs.QueueStore(repositories=list(repositories))
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {
            "1": _park("review_execution_failed"), "2": _park("conflict"),
            "3": _park("review_execution_failed")}}
        state["repositories"]["docs-repo"] = {"active": None, "parked": {
            "9": _park("review_execution_failed")}}
        store.save(state)
    application = Application()
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    monkeypatch.setenv("SUDO_USER", "operator-user")
    bridge: list[list[str]] = []
    answers: dict[str, dict] = {}

    def reopen(arguments):
        bridge.append(arguments)
        applied = arguments[-1] == "1"
        return answers.get(arguments[1], {
            "ok": True, "code": "REVIEW_REOPENED" if applied else "REVIEW_REOPEN_ELIGIBLE",
            **({"audit_record": f"/archive/{arguments[1]}.audit.json"} if applied else {})})

    monkeypatch.setattr(merge_open_prs, "private_reopen_review", reopen)
    return store, application, bridge, answers


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)["repositories"][0]


def test_a_dry_run_reports_and_changes_nothing(queue, capsys):
    store, application, bridge, _answers = queue
    before = store.path.read_bytes()

    assert merge_open_prs.main(["--reopen-review", "1", "--reason", REASON, "core-repo"]) == 0

    assert bridge == [["core-repo", "1", HEAD, BASE, REASON, "operator-user", "0", "0"]]
    assert application.calls == [("core-repo", 1)]
    out = _out(capsys)
    assert (out["applied"], out["unparked"]) == (False, {})
    assert out["reopen"]["1"] == {"ok": True, "code": "REVIEW_REOPEN_ELIGIBLE",
                                  "park": "review_execution_failed"}
    assert store.path.read_bytes() == before


def test_an_applied_reopen_ends_only_a_review_execution_failed_park(queue, capsys):
    store, application, bridge, _answers = queue

    assert merge_open_prs.main(["--reopen-review", "1", "--reopen-review", "2", "--yes",
                                "--allow-paid-attempts", "--reason", REASON, "core"]) == 1

    assert bridge == [["core-repo", "1", HEAD, BASE, REASON, "operator-user", "1", "1"]]
    out = _out(capsys)
    assert out["unparked"] == {"1": _park("review_execution_failed")}
    assert out["reopen"]["1"]["audit_record"] == "/archive/1.audit.json"
    # A conflict park is the author's: its chain is not what holds it, so the
    # owner is never asked and the one reopen of its key is not spent.
    assert out["reopen"]["2"] == {"ok": False, "code": "REVIEW_REOPEN_PARKED_OTHER",
                                  "park": "conflict"}
    assert application.calls == [("core-repo", 1)]
    rows = store.read()["repositories"]
    assert rows["core-repo"]["parked"] == {"2": _park("conflict"),
                                           "3": _park("review_execution_failed")}
    assert rows["docs-repo"]["parked"] == {"9": _park("review_execution_failed")}


def test_a_refused_reopen_keeps_the_park_and_exits_1(queue, capsys):
    store, _application, _bridge, answers = queue
    answers["1"] = {"ok": False, "code": "REVIEW_REOPEN_PAID_ATTEMPT", "tokens_total": 3000}

    assert merge_open_prs.main(["--reopen-review", "1", "--yes", "--reason", REASON,
                                "core-repo"]) == 1

    out = _out(capsys)
    assert out["unparked"] == {}
    assert out["reopen"]["1"]["code"] == "REVIEW_REOPEN_PAID_ATTEMPT"
    assert "1" in store.read()["repositories"]["core-repo"]["parked"]


def test_reopen_all_selects_only_review_execution_failed_parks_of_the_repository(
        queue, capsys):
    store, application, bridge, _answers = queue

    assert merge_open_prs.main(["--reopen-review-all", "--yes", "--reason", REASON,
                                "core-repo"]) == 0

    assert [arguments[1] for arguments in bridge] == ["1", "3"]
    assert application.calls == [("core-repo", 1), ("core-repo", 3)]
    assert set(_out(capsys)["unparked"]) == {"1", "3"}
    rows = store.read()["repositories"]
    assert rows["core-repo"]["parked"] == {"2": _park("conflict")}
    assert rows["docs-repo"]["parked"] == {"9": _park("review_execution_failed")}


def test_a_running_pass_makes_the_reopen_change_nothing(queue, capsys):
    store, application, bridge, _answers = queue
    before = store.path.read_bytes()

    with store.locked():
        assert merge_open_prs.main(["--reopen-review-all", "--yes", "--reason", REASON,
                                    "core-repo"]) == 1

    assert json.loads(capsys.readouterr().out) == {"repositories": [
        {"repository": "core-repo", "skipped": True, "reason": "AUTOMERGE_PASS_BUSY"}]}
    assert bridge == [] and application.calls == []
    assert store.path.read_bytes() == before


def test_an_unparked_pr_can_be_reopened(queue, capsys):
    store, _application, bridge, _answers = queue

    assert merge_open_prs.main(["--reopen-review", "7", "--yes", "--reason", REASON,
                                "core-repo"]) == 0

    assert [arguments[1] for arguments in bridge] == ["7"]
    out = _out(capsys)
    assert (out["reopen"]["7"]["code"], out["reopen"]["7"]["park"], out["unparked"]) == (
        "REVIEW_REOPENED", None, {})


@pytest.mark.parametrize("blocker", ["CI_BASE_STALE", "PR_BASE_STALE"])
def test_a_parked_head_that_succeeded_on_a_stale_base_is_released_without_the_owner(
        queue, capsys, blocker):
    """the review owner refuses such a PR PR_CI_NOT_GREEN, so the park
    is released instead and the queue's refresh makes the new head and chain."""
    store, application, bridge, _answers = queue
    application.inspected = {"ok": True, "pr_state": "open", "head_sha": HEAD,
                             "base_sha": BASE, "ci_state": "pending",
                             "blocking_reason": blocker, "ci_run_conclusion": "success"}

    assert merge_open_prs.main(["--reopen-review-all", "--yes", "--reason", REASON,
                                "core-repo"]) == 0

    assert bridge == []
    out = _out(capsys)
    assert set(out["unparked"]) == {"1", "3"}
    assert out["reopen"]["1"] == {"ok": True, "code": "REVIEW_REOPEN_STALE_BASE",
                                  "park": "review_execution_failed"}
    assert store.read()["repositories"]["core-repo"]["parked"] == {"2": _park("conflict")}


def test_a_dry_run_reports_a_stale_base_release_and_changes_nothing(queue, capsys):
    store, application, bridge, _answers = queue
    application.inspected = {"ok": True, "pr_state": "open", "head_sha": HEAD,
                             "base_sha": BASE, "blocking_reason": "CI_BASE_STALE",
                             "ci_run_conclusion": "success"}
    before = store.path.read_bytes()

    assert merge_open_prs.main(["--reopen-review", "1", "--reason", REASON, "core-repo"]) == 0

    assert bridge == [] and _out(capsys)["unparked"] == {}
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("inspected", [
    pytest.param({"blocking_reason": "CI_BASE_STALE", "ci_run_conclusion": "failure"},
                 id="a_red_run_on_a_stale_base"),
    pytest.param({"blocking_reason": "CI_RUN_IN_PROGRESS", "ci_run_conclusion": None},
                 id="a_run_in_progress"),
    pytest.param({"blocking_reason": None, "ci_run_conclusion": "success",
                  "ci_state": "green"}, id="green_on_the_current_base"),
])
def test_any_other_ci_read_still_goes_to_the_review_owner(queue, capsys, inspected):
    _store, application, bridge, _answers = queue
    application.inspected = {"ok": True, "pr_state": "open", "head_sha": HEAD,
                             "base_sha": BASE, **inspected}

    assert merge_open_prs.main(["--reopen-review", "1", "--yes", "--reason", REASON,
                                "core-repo"]) == 0

    assert [arguments[1] for arguments in bridge] == ["1"]
    assert _out(capsys)["reopen"]["1"]["code"] == "REVIEW_REOPENED"


def test_an_unparked_stale_base_pr_is_not_released_by_the_reopen(queue, capsys):
    """No park to release: the owner decides, as before."""
    _store, application, bridge, _answers = queue
    application.inspected = {"ok": True, "pr_state": "open", "head_sha": HEAD,
                             "base_sha": BASE, "blocking_reason": "CI_BASE_STALE",
                             "ci_run_conclusion": "success"}

    merge_open_prs.main(["--reopen-review", "7", "--yes", "--reason", REASON, "core-repo"])

    assert [arguments[1] for arguments in bridge] == ["7"]


def test_an_uninspectable_pr_never_reaches_the_review_owner(queue, capsys):
    _store, application, bridge, _answers = queue
    application.inspected = {"ok": False, "code": "PR_SELECTION_MISMATCH"}

    assert merge_open_prs.main(["--reopen-review", "1", "--yes", "--reason", REASON,
                                "core-repo"]) == 1

    assert bridge == []
    assert _out(capsys)["reopen"]["1"]["code"] == "PR_SELECTION_MISMATCH"


@pytest.mark.parametrize("argv", [
    ["--reopen-review", "1", "core-repo"],
    ["--reopen-review", "1", "--reason", " ", "core-repo"],
    ["--reopen-review", "1", "--reason", REASON],
    ["--reopen-review", "1", "--reason", REASON, "core-repo", "docs-repo"],
    ["--reopen-review", "1", "--reopen-review-all", "--reason", REASON, "core-repo"],
    ["--reopen-review", "1", "--unpark", "2", "--reason", REASON, "core-repo"],
    ["--reopen-review-all", "--automerge", "--reason", REASON],
    ["--unpark", "1", "--yes", "core-repo"],
    ["--reason", REASON, "core-repo"],
    ["--allow-paid-attempts", "--queue-show"],
])
def test_reopen_flags_refuse_an_ambiguous_command(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(("stdout", "code"), [
    ('{"ok": false, "code": "USAGE"}', "REVIEW_REOPEN_UNAVAILABLE"),
    ("not json", "REVIEW_REOPEN_UNAVAILABLE"),
    ('{"ok": true, "code": "REVIEW_REOPENED"}', "REVIEW_REOPENED"),
])
def test_the_bridge_reads_an_undeployed_owner_as_unavailable(monkeypatch, stdout, code):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(gate_server_estate.subprocess, "run", run)

    assert gate_server_estate.private_reopen_review(["core-repo", "1"])["code"] == code
    assert seen == [([gate_server_estate.MCP_PYTHON, gate_server_estate.SNAPSHOT_REVIEW,
                      "reopen", "core-repo", "1"],
                     gate_server_estate.PRIVATE_REOPEN_TIMEOUT_SECONDS)]
