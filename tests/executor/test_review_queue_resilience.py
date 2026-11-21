"""Claim repair and review progress at the existing queue/status boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from gate_estate_fixtures import _status_row
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _core_repo,
    _in_progress,
    _queue_inspect,
    _queue_store,
    merge_open_prs,
)


def _claim_park(store, *, claim_sha=None):
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"2": {
            "reason": "claim_unavailable", "head": "a" * 40, "base": "b" * 40,
            "run": [11, 1], "review": [None, "none"], "claim_sha": claim_sha,
        }}}
        store.save(state)


@pytest.mark.parametrize("active_waits", [False, True])
def test_valid_claim_releases_park_without_head_run_or_hash_change(tmp_path, active_waits):
    store = _queue_store(tmp_path)
    _claim_park(store, claim_sha="c" * 64)
    gate = FakeGateApplication({"core-repo": [1, 2] if active_waits else [2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(
            review="not_approved", claim_sha="c" * 64, claim_error=None),
    })
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)[0]
    assert [call[2] for call in gate.calls if call[0] == "review"] == [2]
    row = store.read()["repositories"]["core-repo"]
    assert "2" not in row["parked"]
    if active_waits:
        assert row["active"]["pr"] == 1
        assert result["results"][-1] == {"pr": 2, "outcome": "reviewed_ahead"}
        assert not any(call[0] in {"merge", "refresh"} for call in gate.calls)


def test_unchanged_invalid_claim_is_inspected_but_never_reviewed_ahead(tmp_path):
    store = _queue_store(tmp_path)
    _claim_park(store)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(
            review="not_approved", claim_sha=None, claim_error="PR_CLAIM_MISSING"),
    })
    for _ in range(2):
        merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    # selection ranks PR 2's park too, so a pass may read it twice.
    inspected = [call[2] for call in gate.calls if call[0] == "inspect"]
    assert set(inspected) == {1, 2} and inspected.count(2) >= 2
    assert not any(call[0] == "review" for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["parked"]["2"]["reason"] == "claim_unavailable"


@pytest.mark.parametrize("ahead", [False, True])
def test_review_progress_is_published_before_blocked_reviewer_returns(tmp_path, ahead):
    entered, release = Event(), Event()

    class BlockingReview(FakeGateApplication):
        def review(self, *args, **kwargs):
            result = super().review(*args, **kwargs)
            entered.set()
            assert release.wait(10), "test did not release reviewer"
            return result

    target = 2 if ahead else 1
    gate = BlockingReview({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress() if ahead else _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
    })
    store = _queue_store(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(merge_open_prs.run_automerge_pass, [_core_repo(tmp_path)], gate,
                                  queue_store=store, max_reviews=1)
        try:
            assert entered.wait(10), "review did not start"
            expected = ("queue=eligible reason=reviewing owner=queue pos=2" if ahead
                        else "queue=active reason=reviewing owner=queue pos=1")
            assert gate.statuses and gate.statuses[-1][1][target] == expected
            assert gate.parked_heads[-1][target] == "a" * 40
        finally:
            release.set()
            running.result(timeout=15)


def test_budget_exhaustion_reports_waiting_review_without_dispatch(tmp_path):
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                    queue_store=_queue_store(tmp_path), max_reviews=0)
    assert not any(call[0] == "review" for call in gate.calls)
    assert gate.statuses[-1][1][1] == "queue=active reason=waiting_review owner=queue pos=1"


@pytest.mark.parametrize(("extra", "action", "owner"), [
    ({"claim_error": None}, "wait_for_queue", "queue"),
    ({"claim_error": None, "blocking_reason": "PR_REVIEW_CHANGES_REQUESTED",
      "next_action": "address_review_findings"}, "address_review_findings", "author"),
    ({"claim_error": None, "blocking_reason": "PR_NOT_MERGEABLE",
      "next_action": "resolve_conflict"}, "resolve_conflict", "author"),
    ({"claim_sha": None, "claim_error": "PR_CLAIM_OUTDATED_HEAD"}, "correct_pr_claim", "author"),
    ({}, "correct_pr_claim", "author"),
    ({"claim_error": "UNKNOWN"}, "correct_pr_claim", "author"),
    ({"head_sha": "d" * 40, "claim_error": None}, "wait_for_queue", "queue"),
    ({"claim_error": None, "blocking_reason": "CI_RUN_IN_PROGRESS",
      "next_action": "wait_for_trusted_ci"}, "wait_for_trusted_ci", "queue"),
    ({"claim_error": None, "blocking_reason": "UNKNOWN_BLOCKER",
      "next_action": "operator_recovery"}, "operator_recovery", "operator"),
])
def test_stale_claim_park_uses_current_validation_and_preserves_author_findings(
        monkeypatch, extra, action, owner):
    row = _status_row(monkeypatch, {
        "ci_state": "green", "blocking_reason": "PR_REVIEW_NOT_APPROVED",
        "next_action": "review", **extra,
    }, queue_status="queue=parked reason=claim_unavailable owner=author pos=-")
    assert (row["next_action"], row["action_owner"]) == (action, owner)
    if action == "wait_for_queue":
        assert row["scheduling"]["state"] == "eligible"
        assert row["scheduling"]["owner"] == "queue"


@pytest.mark.parametrize("ahead", [False, True])
def test_review_refusal_reconciles_progress_to_waiting_review(tmp_path, ahead):
    target = 2 if ahead else 1
    gate = FakeGateApplication({"core-repo": [1, 2] if ahead else [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress() if ahead else _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("review", "core-repo", target): {"ok": False, "code": "REVIEW_ATTEMPT_BUSY"},
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                    queue_store=_queue_store(tmp_path), max_reviews=1)
    state = "eligible" if ahead else "active"
    assert gate.statuses[0][1][target] == f"queue={state} reason=reviewing owner=queue pos={target}"
    assert gate.statuses[-1][1][target] == f"queue={state} reason=waiting_review owner=queue pos={target}"


@pytest.mark.parametrize("ahead", [False, True])
def test_status_write_exception_does_not_block_or_repeat_paid_review(tmp_path, ahead):
    publications_before_review = []

    class FailingStatus(FakeGateApplication):
        def queue_status(self, repository, descriptions, published):
            super().queue_status(repository, descriptions, published)
            raise TimeoutError("advisory status transport failed")

        def review(self, *args, **kwargs):
            publications_before_review.append(len(self.statuses))
            return super().review(*args, **kwargs)

    target = 2 if ahead else 1
    gate = FailingStatus({"core-repo": [1, 2] if ahead else [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress() if ahead else _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("review", "core-repo", target): {"ok": False, "code": "REVIEW_INCOMPLETE"},
    })
    store = _queue_store(tmp_path)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    assert [call[2] for call in gate.calls if call[0] == "review"] == [target]
    assert publications_before_review == [1]
    assert store.read()["repositories"]["core-repo"].get("published", {}) == {}
    gate.responses[("inspect", "core-repo", target)] = _queue_inspect(review="approved")
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    assert [call[2] for call in gate.calls if call[0] == "review"] == [target]


@pytest.mark.parametrize("interruption", [TimeoutError, KeyboardInterrupt])
def test_interrupted_review_restart_reconciles_current_approval_without_redispatch(tmp_path, interruption):
    class InterruptedReview(FakeGateApplication):
        def review(self, *args, **kwargs):
            super().review(*args, **kwargs)
            # The real snapshot owner may finish publication before losing
            # its response; next-pass inspection/receipts own that decision.
            self.responses[("inspect", "core-repo", 1)] = _queue_inspect(review="approved")
            raise interruption("lost review response")

    gate = InterruptedReview({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
    })
    store = _queue_store(tmp_path)
    if interruption is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                            queue_store=store, max_reviews=1)
    else:
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                        queue_store=store, max_reviews=1)
        assert "reason=waiting_review" in gate.statuses[-1][1][1]
    assert "reason=reviewing" in gate.statuses[0][1][1]
    second = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                             queue_store=store, max_reviews=1)[0]
    assert second["results"] == [{"pr": 1, "outcome": "merged"}]
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1]
    assert gate.statuses[-1][1][1] == "queue=done reason=- owner=- pos=-"


def test_authority_refusal_stops_post_refusal_status_write_and_restart_uses_live_verdict(tmp_path):
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 1): {"ok": False, "code": "REVIEW_SUPERVISOR_UNAVAILABLE"},
    })
    store = _queue_store(tmp_path)
    result = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                             queue_store=store, max_reviews=1)[0]
    assert result["authority_integrity_failed"] is True
    assert len(gate.statuses) == 1  # pre-dispatch only; no writes after refusal
    assert "reason=reviewing" in gate.statuses[0][1][1]
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(review="approved")
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1]
    assert gate.statuses[-1][1][1] == "queue=done reason=- owner=- pos=-"


def test_repaired_claim_with_current_conflict_releases_park_without_paid_review(tmp_path):
    store = _queue_store(tmp_path)
    _claim_park(store)
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(
            review="not_approved", claim_error=None, claim_sha="c" * 64, mergeable=False,
            blocking_reason="PR_NOT_MERGEABLE"),
        ("inspect", "core-repo", 3): _queue_inspect(review="not_approved"),
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    assert [call[2] for call in gate.calls if call[0] == "review"] == [3]
    assert "2" not in store.read()["repositories"]["core-repo"]["parked"]
