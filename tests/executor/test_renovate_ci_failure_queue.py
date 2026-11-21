"""Confirmed bot CI failures retain their evidence and need operator recovery."""

from __future__ import annotations

import json

import pytest
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _core_repo,
    _in_progress,
    _queue_inspect,
    _queue_store,
    merge_open_prs,
)


def _failed_park(reason="renovate_ci_failed"):
    return {"reason": reason, "head": "c" * 40, "base": "b" * 40,
            "run": [12, 1], "review": [None, "none"], "claim_sha": "2" * 64,
            "since": 1000.0}


def _failed_inspect():
    return _queue_inspect(
        head="c", base="d", ci="pending", conclusion="failure", run=12,
        blocking_reason="CI_BASE_STALE", claim_sha="2" * 64)


@pytest.mark.parametrize("signal", ["branch", "label", "author"])
@pytest.mark.parametrize("active_wait", [False, True])
def test_old_failed_parks_keep_age_and_evidence_when_bot_ownership_is_established(
    tmp_path, signal, active_wait, caplog,
):
    store = _queue_store(tmp_path)
    old = _failed_park("ci_failed")
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": ({"pr": 2, "head": "a" * 40, "base": "b" * 40,
                        "run": [11, 1], "wait_since": 4990.0, "started": True}
                       if active_wait else None),
            "parked": {"1": old},
        }
        store.save(state)
    # A prior version-1 record remains readable before the normal pass migrates it.
    assert store.read()["repositories"]["core-repo"]["parked"]["1"] == old
    gate = FakeGateApplication(
        {"core-repo": [1, 2]},
        head_refs={"core-repo": {1: "renovate/x" if signal == "branch" else "task/x",
                                2: "task/y" if active_wait else "renovate/y"}},
        labels={"core-repo": {1: ["renovate"] if signal == "label" else [], 2: []}},
        responses={("inspect", "core-repo", 1): _failed_inspect(),
                   ("inspect", "core-repo", 2): (_in_progress() if active_wait
                                                else _queue_inspect())})
    caplog.set_level("INFO", logger=merge_open_prs.__name__)

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 5000.0)[0]

    reason = "ci_failed" if signal == "author" else "renovate_ci_failed"
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"] == old | {"reason": reason}
    assert summary["park_wait_s"] == (0 if signal == "author" else 4000)
    owner = "author" if signal == "author" else "operator"
    assert gate.statuses[-1][1][1] == f"queue=parked reason={reason} owner={owner} pos=-"
    if active_wait:
        assert row["active"]["pr"] == 2
        assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)
    else:
        assert {"pr": 2, "outcome": "merged"} in summary["results"]
        assert [call[2] for call in gate.calls if call[0] in {"refresh", "review", "merge"}] == [2]
    assert ("automerge park reclassified: core-repo#1" in caplog.text) is (signal != "author")


@pytest.mark.parametrize("change", ["head", "claim", "review", "missing_run"])
def test_only_new_head_or_ci_evidence_releases_a_confirmed_bot_failure(tmp_path, change):
    store = _queue_store(tmp_path)
    parked = _failed_park()
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"1": parked}}
        store.save(state)
    inspected = _failed_inspect()
    if change == "head":
        inspected["head_sha"] = "e" * 40
    elif change == "claim":
        inspected["claim_sha"] = "3" * 64
    elif change == "review":
        inspected["latest_delegated_review_id"] = 99
    else:
        inspected.update(ci_run_id=None, ci_run_attempt=None)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
        labels={"core-repo": {1: ["renovate"]}},
        responses={("inspect", "core-repo", 1): inspected,
                   ("refresh", "core-repo", 1): {"ok": True, "head_sha": "f" * 40}})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    mutations = [call for call in gate.calls if call[0] in {"refresh", "review", "merge"}]
    row = store.read()["repositories"]["core-repo"]
    if change == "head":
        assert mutations == [("refresh", "core-repo", 1, "e" * 40, "d" * 40)]
        assert row["parked"] == {}
    else:
        assert mutations == []
        assert row["parked"]["1"] == parked


@pytest.mark.parametrize("binding_rejection", [False, True])
def test_explicit_unpark_readmits_ci_failure_without_dismissing_review(
    tmp_path, monkeypatch, capsys, binding_rejection,
):
    repository = _core_repo(tmp_path)
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: {"core-repo": repository})
    store = merge_open_prs.QueueStore(repositories=["core-repo"])
    parked = _failed_park()
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"1": parked}}
        store.save(state)

    assert merge_open_prs.main(["--unpark", "1", "core-repo"]) == 0
    assert json.loads(capsys.readouterr().out)["repositories"][0]["unparked"] == {"1": parked}
    assert store.read()["repositories"]["core-repo"]["parked"] == {}
    inspected = _failed_inspect()
    if binding_rejection:
        inspected.update(blocking_reason="PR_REVIEW_CHANGES_REQUESTED",
                         review_state="changes_requested")
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
        labels={"core-repo": {1: ["renovate"]}},
        responses={("inspect", "core-repo", 1): inspected,
                   ("refresh", "core-repo", 1): {"ok": True, "head_sha": "f" * 40}})

    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    mutations = [call for call in gate.calls if call[0] in {"refresh", "review", "merge"}]
    if binding_rejection:
        assert mutations == []
        assert row["parked"]["1"]["reason"] == "renovate_changes_requested"
    else:
        assert mutations == [("refresh", "core-repo", 1, "c" * 40, "d" * 40)]
        assert row["active"]["refreshed_head"] == "f" * 40
        assert row["parked"] == {}
