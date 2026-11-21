"""Merge-candidate mark in the automerge queue.

With `merge_candidate_policy: label-v1` the queue marks the PR it is about to
merge with `gate/candidate`, waits for the full run an up-to-date head gets
from the `labeled` event, and clears the mark when it parks the PR. A stale-base
head is refreshed first and marked only after the refresh succeeds.
A PR whose narrowed run is still running or red is not marked. The gate side
has its own tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _in_progress,
    _queue_inspect,
    _queue_store,
    merge_open_prs,
)

WRITES = {"mark_candidate", "refresh", "review", "merge"}


class CandidateGate(FakeGateApplication):
    """The fake gate, with the executor's mark route."""

    def mark_candidate(self, repository, pr, candidate):
        self.calls.append(("mark_candidate", repository, pr, candidate))
        return self.responses.get(("mark_candidate", repository, pr, candidate),
                                  {"ok": True, "candidate": candidate, "label_changed": True})


def _repository(tmp_path, policy="label-v1"):
    return merge_open_prs.Repository("core-repo", tmp_path, "forgejo", "acme/core-repo",
                                     merge_candidate_policy=policy)


def _writes(gate):
    return [call for call in gate.calls if call[0] in WRITES]


def _pass(repository, gate, store):
    return merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]


def test_an_up_to_date_green_head_is_marked_and_waits_for_its_full_run(tmp_path):
    """The narrowed run is green; the merge waits for the full run."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(merge_candidate=False),
    })

    first = _pass(repository, gate, store)

    assert first["results"] == [{"pr": 1, "outcome": "retained",
                                 "reason": "PR_CANDIDATE_MARKED"}]
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1

    # The `labeled` event's full run is running: the queue waits, unmarked twice.
    gate.calls.clear()
    gate.responses[("inspect", "core-repo", 1)] = _in_progress(merge_candidate=True, run=12)
    second = _pass(repository, gate, store)
    assert second["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "CI_RUN_IN_PROGRESS"}]
    assert _writes(gate) == []

    # The full run is green: the queue merges without marking again.
    gate.calls.clear()
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(merge_candidate=True, run=12)
    third = _pass(repository, gate, store)
    assert third["results"] == [{"pr": 1, "outcome": "merged"}]
    assert _writes(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


def test_a_stale_base_head_is_marked_after_its_refresh(tmp_path):
    """the label's full run starts on the refreshed head, not the old one."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False),
    })

    result = _pass(repository, gate, store)

    assert result["results"][0]["outcome"] == "refreshed"
    assert _writes(gate) == [
        ("refresh", "core-repo", 1, "a" * 40, "b" * 40),
        ("mark_candidate", "core-repo", 1, True),
    ]


def test_a_head_already_green_on_the_full_profile_merges_unmarked(tmp_path):
    """a run that classify already escalated to full c1 (a CI-trust
    or unmapped path) is the evidence the merge needs; marking would only
    start the same full run again."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            merge_candidate=False, ci_accepted_profile="checks/c1"),
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "merged"}]
    assert _writes(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


def test_a_head_green_on_the_narrowed_profile_is_still_marked(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            merge_candidate=False, ci_accepted_profile="checks/c2"),
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_MARKED"}]
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]


def test_a_doc_only_green_head_merges_unmarked(tmp_path):
    """the gate says a merge accepts this head's narrowed run, so no
    candidate mark and no full run: the exact-pair run is the merge evidence."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            merge_candidate=False, ci_accepted_profile="checks/c2",
            ci_merge_profile="checks/c2"),
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "merged"}]
    assert _writes(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


@pytest.mark.parametrize("extra", [
    {},  # a record without the field (an older gate): fail closed
    {"ci_merge_profile": "checks/c1"},
    {"ci_merge_profile": "checks/c3"},
    {"ci_merge_profile": None},
    {"ci_merge_profile": "unknown"},
])
def test_a_record_without_a_narrowed_merge_profile_is_still_marked(tmp_path, extra):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            merge_candidate=False, ci_accepted_profile="checks/c2", **extra),
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_MARKED"}]
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]


def test_a_doc_only_head_is_unmarked_only_while_green_or_pending(tmp_path):
    """A pending narrowed run is the doc-only head's whole CI; a red read or an
    unknown state keeps the mark."""
    repository = _repository(tmp_path)
    inspected = _in_progress(merge_candidate=False, ci_merge_profile="checks/c2")
    assert merge_open_prs._mark_needed(repository, inspected) is False
    green = _queue_inspect(merge_candidate=False, ci_merge_profile="checks/c2")
    assert merge_open_prs._mark_needed(repository, green) is False
    for state in ("red", "unknown", None):
        assert merge_open_prs._mark_needed(
            repository, {**green, "ci_state": state}) is True
    assert merge_open_prs._mark_needed(
        _repository(tmp_path, policy=""), inspected) is False


def test_a_stale_doc_only_head_is_refreshed_but_not_marked(tmp_path):
    """a doc-only head the gate merges on `checks/c2` is refreshed and
    left unmarked, so its pending post-refresh narrowed run is the whole CI."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False, ci_merge_profile="checks/c2"),
    })

    result = _pass(repository, gate, store)

    assert result["results"][0]["outcome"] == "refreshed"
    assert [call[0] for call in _writes(gate)] == ["refresh"]


def test_a_full_profile_head_behind_main_is_still_marked_after_its_refresh(tmp_path):
    """The refreshed head is a new run: the mark makes that one full."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            blocking_reason="PR_BASE_STALE", next_action="refresh_task",
            merge_candidate=False, ci_accepted_profile="checks/c1"),
    })

    result = _pass(repository, gate, store)

    assert result["results"][0]["outcome"] == "refreshed"
    assert _writes(gate) == [
        ("refresh", "core-repo", 1, "a" * 40, "b" * 40),
        ("mark_candidate", "core-repo", 1, True),
    ]


class LabelAwareGate(CandidateGate):
    """The gate's reads of one PR once the mark is on.

    The label starts a full run of the head at once, so every read after the
    mark sees that run in progress as the newest one, the refresh route's own
    locked read included: a refresh attempted after the mark is refused
    `PR_REFRESH_NOT_ELIGIBLE`.
    """

    def __init__(self):
        super().__init__({"core-repo": [1]})
        self.labelled = False

    def _row(self):
        if self.labelled:
            return _in_progress(merge_candidate=True, run=12)
        return _queue_inspect(blocking_reason="PR_BASE_STALE", next_action="refresh_task",
                              merge_candidate=False)

    def inspect(self, repository, pr):
        self.calls.append(("inspect", repository, pr))
        return self._row()

    def mark_candidate(self, repository, pr, candidate):
        self.calls.append(("mark_candidate", repository, pr, candidate))
        changed, self.labelled = self.labelled != candidate, candidate
        return {"ok": True, "candidate": candidate, "label_changed": changed}

    def refresh(self, repository, pr, head, base):
        self.calls.append(("refresh", repository, pr, head, base))
        row = self._row()
        if (row["blocking_reason"] not in {"CI_BASE_STALE", "PR_BASE_STALE"}
                or row["ci_run_conclusion"] != "success"):
            return {"ok": False, "code": "PR_REFRESH_NOT_ELIGIBLE"}
        return {"ok": True, "code": "PR_REFRESHED", "head_sha": "c" * 40}


def test_a_stale_base_head_is_refreshed_before_the_mark_starts_a_run_that_blocks_it(tmp_path):
    """marked first, the label's full run on the obsolete head makes
    the refresh refuse (PR_REFRESH_NOT_ELIGIBLE) and the queue pays for two
    full runs. Refresh first, then mark, in one pass."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = LabelAwareGate()

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert _writes(gate) == [
        ("refresh", "core-repo", 1, "a" * 40, "b" * 40),
        ("mark_candidate", "core-repo", 1, True),
    ]
    assert gate.labelled


def test_a_refused_mark_after_a_refresh_keeps_the_refresh_for_the_next_pass(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False),
        ("mark_candidate", "core-repo", 1, True): {"ok": False,
                                                   "code": "PR_CANDIDATE_UNCONFIRMED"},
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


@pytest.mark.parametrize("code", ["INTERNAL_ERROR", "GATE_LOCK_DIR_UNSAFE",
                                  "REPOS_YAML_UNREADABLE"])
def test_an_authority_or_integrity_mark_failure_after_a_refresh_stops_the_pass(tmp_path, code):
    """The refresh stays recorded and active, and the pass stops."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False),
        ("inspect", "core-repo", 2): _queue_inspect(head="d", review="not_approved",
                                                    merge_candidate=False),
        ("mark_candidate", "core-repo", 1, True): {"ok": False, "code": code},
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40,
                                  "reason": code, "authority_integrity_failed": True}]
    assert result["authority_integrity_failed"] is True
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    assert [call[0] for call in _writes(gate)] == ["refresh", "mark_candidate"]


def test_an_integrity_stop_after_a_renovate_refresh_keeps_the_retry_spent(tmp_path):
    """The stop still records the executor-made head, so the next
    pass parks its red run instead of refreshing it a second time."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
                         labels={"core-repo": {1: ["renovate"]}}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False),
        ("mark_candidate", "core-repo", 1, True): {"ok": False, "code": "INTERNAL_ERROR"},
    })

    first = _pass(repository, gate, store)

    assert first["authority_integrity_failed"] is True
    assert store.read()["repositories"]["core-repo"]["active"]["refreshed_head"] == "c" * 40

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        head="c", base="e", ci="red", conclusion="failure", blocking_reason="CI_BASE_STALE",
        ci_reason="CI_BASE_STALE", merge_candidate=False)
    gate.calls.clear()
    _pass(repository, gate, store)

    assert not [call for call in _writes(gate) if call[0] == "refresh"]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_ci_failed")


def test_a_deploy_yield_at_the_post_refresh_mark_keeps_the_retry_spent(tmp_path, monkeypatch):
    """A deployment that takes the outer lock right after a
    Renovate refresh yields the pass at the mark's checkpoint. The yield still
    saves the executor-made head, so the next pass parks its red run instead
    of refreshing it a second time."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
                         labels={"core-repo": {1: ["renovate"]}}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False),
    })
    monkeypatch.setattr(merge_open_prs, "probe_deploy_lock",
                        lambda: ("held", "deploy") if any(
                            call[0] == "refresh" for call in gate.calls) else ("free", ""))
    monkeypatch.setattr(merge_open_prs, "_deploy_source_applied", lambda: False)

    first = _pass(repository, gate, store)

    assert first["hold"]["reason"] == "DEPLOY_WAITING"
    assert [call[0] for call in _writes(gate)] == ["refresh"]
    assert store.read()["repositories"]["core-repo"]["active"]["refreshed_head"] == "c" * 40

    monkeypatch.setattr(merge_open_prs, "probe_deploy_lock", lambda: ("free", ""))
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        head="c", base="e", ci="red", conclusion="failure", blocking_reason="CI_BASE_STALE",
        ci_reason="CI_BASE_STALE", merge_candidate=False)
    gate.calls.clear()
    _pass(repository, gate, store)

    assert not [call for call in _writes(gate) if call[0] == "refresh"]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_ci_failed")


def test_a_red_renovate_refresh_is_not_followed_by_a_mark(tmp_path):
    """The refresh of a red Renovate head is the one retry; its run is not
    worth a full run, so the head stays unmarked."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
                         labels={"core-repo": {1: ["renovate"]}}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="red", conclusion="failure", blocking_reason="CI_BASE_STALE",
            ci_reason="CI_BASE_STALE", merge_candidate=False),
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert _writes(gate) == [("refresh", "core-repo", 1, "a" * 40, "b" * 40)]


def test_a_head_whose_narrowed_run_is_still_running_is_not_marked(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress(merge_candidate=False),
    })
    result = _pass(repository, gate, store)
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "CI_RUN_IN_PROGRESS"}]
    assert _writes(gate) == []


def test_a_red_narrowed_head_is_parked_unmarked(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="red", conclusion="failure", blocking_reason="CI_RUN_FAILED",
            merge_candidate=False),
    })
    _pass(repository, gate, store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    assert _writes(gate) == []


def _insufficient(conclusion, **extra):
    """A gate-C1-class head: the gate reads its narrowed run's profile first.

    A successful run is the shape the snapshot review accepts, and
    `ci_review_accepted` carries that answer; a failed one is refused.
    """
    extra.setdefault("ci_review_accepted", conclusion == "success")
    return _queue_inspect(
        ci="unavailable", conclusion=conclusion, review="not_approved",
        blocking_reason="CI_PROFILE_INSUFFICIENT", ci_reason="CI_PROFILE_INSUFFICIENT",
        merge_candidate=False, **extra)


def test_a_red_insufficient_profile_head_is_not_marked_and_is_parked(tmp_path):
    """the gate answers CI_PROFILE_INSUFFICIENT before it looks at the
    conclusion, so a failed narrowed run must not buy a full run that fails
    the same way, nor a review."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _insufficient("failure"),
    })

    first = _pass(repository, gate, store)

    assert first["results"] == [{"pr": 1, "outcome": "retained",
                                 "reason": "CI_PROFILE_INSUFFICIENT"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    assert _writes(gate) == []

    # Nothing new on the head: the next pass leaves it parked, with no write.
    _pass(repository, gate, store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    assert _writes(gate) == []


def test_a_red_insufficient_profile_renovate_head_is_parked_for_the_operator(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
                         labels={"core-repo": {1: ["renovate"]}}, responses={
        ("inspect", "core-repo", 1): _insufficient("failure"),
    })

    _pass(repository, gate, store)

    assert (store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"]
            == "renovate_ci_failed")
    assert _writes(gate) == []


def test_a_successful_narrowed_run_is_reviewed_before_the_full_run(tmp_path):
    """the snapshot review accepts a successful narrowed `c2` run, so the
    mark that starts the full run waits for the approval instead of preceding it."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = ApprovingGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _insufficient("success"),
    })

    result = _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["review", "mark_candidate"]
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_MARKED"}]


def test_a_head_the_review_would_refuse_is_marked_before_its_review(tmp_path):
    """The fallback: a head the review cannot accept -- here a run that is not the
    narrowed `c2` shape -- still needs its full run first, as before."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _insufficient("success", ci_review_accepted=False),
    })

    result = _pass(repository, gate, store)

    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_MARKED"}]


def test_a_refreshed_unapproved_head_is_not_marked(tmp_path):
    """the refresh path marks only an approval that survived the refresh.

    An unapproved head is reviewed on its new narrowed run, and the next pass
    decides from its own read whether the review may accept it."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", review="not_approved", blocking_reason="CI_BASE_STALE",
            ci_reason="CI_BASE_STALE", ci_required_profile="checks/c1"),
    })

    _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["refresh"]


def _marked_then_unreadable(
        tmp_path: Path, *, merge_candidate: bool = True, narrowed_seconds: int = 0) -> tuple:
    """Promote PR 1, optionally after waiting on its narrowed run, then lose the CI read."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    clock = [1_000_000.0]
    # With a narrowed run the queue holds only PR 1: the fixture scripts no read
    # for PR 2, so leaving it as a candidate would let the readiness election
    # mark it instead and the helper would never reach the marked-PR-1 state.
    gate = CandidateGate({"core-repo": [1] if narrowed_seconds else [1, 2]}, responses={
        ("inspect", "core-repo", 1): (_in_progress(merge_candidate=False)
                                      if narrowed_seconds else _queue_inspect(merge_candidate=False)),
    })
    if narrowed_seconds:
        _next_pass(repository, gate, store, clock)
        clock[0] += narrowed_seconds
        gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(merge_candidate=False)
    _next_pass(repository, gate, store, clock)
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]
    gate.calls.clear()
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        ci="unavailable", conclusion=None, run=None, attempt=None,
        blocking_reason="CI_RESPONSE_INVALID", ci_reason="CI_RESPONSE_INVALID",
        merge_candidate=merge_candidate)
    return repository, store, gate, clock


def _next_pass(repository, gate, store, clock):
    return merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                             now=lambda: clock[0])[0]


@pytest.mark.parametrize("narrowed_seconds", [0, 1260], ids=["immediate", "after_21_minutes"])
def test_a_marked_pr_reading_unavailable_evidence_in_the_start_window_is_retained(
        tmp_path: Path, narrowed_seconds: int) -> None:
    """the run list is in transition right after a mark. Parking the
    PR clears the mark and selects the next one while the first one's full run
    keeps running, to be discarded when main moves.

    The after_21_minutes case models a single-candidate queue: past the start
    window the fixture's unscripted PR 2 would otherwise read green and win the
    readiness election, so only PR 1 is a candidate there."""
    repository, store, gate, clock = _marked_then_unreadable(
        tmp_path, narrowed_seconds=narrowed_seconds)
    clock[0] += 30

    result = _next_pass(repository, gate, store, clock)

    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "CI_RESPONSE_INVALID"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {} and row["active"]["pr"] == 1
    assert _writes(gate) == []
    assert ("inspect", "core-repo", 2) not in gate.calls


@pytest.mark.parametrize("narrowed_seconds", [0, 1260], ids=["immediate", "after_21_minutes"])
def test_a_marked_pr_still_unreadable_past_the_start_window_is_parked(
        tmp_path: Path, narrowed_seconds: int) -> None:
    # The after_21_minutes case models a single-candidate queue so PR 1 is the
    # one marked after its long narrowed run (see the helper).
    repository, store, gate, clock = _marked_then_unreadable(
        tmp_path, narrowed_seconds=narrowed_seconds)
    clock[0] += 600
    _next_pass(repository, gate, store, clock)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    assert _writes(gate) == []
    clock[0] += 601

    result = _next_pass(repository, gate, store, clock)

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "evidence_unavailable"
    assert result["results"][0] == {"pr": 1, "outcome": "retained",
                                    "reason": "CI_RESPONSE_INVALID"}
    assert _writes(gate)[0] == ("mark_candidate", "core-repo", 1, False)


@pytest.mark.parametrize("mark_result", [
    pytest.param({"ok": False, "code": "PR_CANDIDATE_UNCONFIRMED"}, id="refused"),
    pytest.param({"ok": True, "label_changed": False}, id="already_present"),
])
def test_a_mark_that_starts_no_run_does_not_restart_the_wait(
        tmp_path: Path, mark_result: dict[str, object]) -> None:
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    clock = [1_000_000.0]
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress(merge_candidate=False),
        ("mark_candidate", "core-repo", 1, True): mark_result,
    })
    _next_pass(repository, gate, store, clock)
    clock[0] += 1260
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        merge_candidate=False, mergeable=None)

    _next_pass(repository, gate, store, clock)

    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["wait_since"] == 1_000_000.0
    assert active["started"] is True
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]


def test_an_unmarked_pr_reading_unavailable_evidence_is_parked_at_once(tmp_path):
    """The wait is for the full run a mark started, not for any broken read."""
    repository, store, gate, clock = _marked_then_unreadable(tmp_path, merge_candidate=False)
    clock[0] += 300

    _next_pass(repository, gate, store, clock)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "evidence_unavailable")


def _stale_c1_class(**extra):
    """A gate-C1-class head whose narrowed run succeeded on a now-stale base:
    the gate answers CI_BASE_STALE before it reads the profile."""
    return _queue_inspect(
        ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
        review="not_approved", recorded_base_sha="d" * 40,
        **{"merge_candidate": False, "ci_required_profile": "checks/c1", **extra})


def test_review_ahead_skips_an_unmarked_c1_class_head_on_a_stale_base(tmp_path):
    """The snapshot review refuses it PR_CI_NOT_GREEN until it is the candidate."""
    repository = _repository(tmp_path)

    assert merge_open_prs._review_ahead_base(repository, _stale_c1_class()) is None


@pytest.mark.parametrize(("row", "policy"), [
    pytest.param(_stale_c1_class(ci_required_profile="checks/c2"), "label-v1",
                 id="a_narrowed_class_head_is_reviewable"),
    pytest.param(_stale_c1_class(merge_candidate=True), "label-v1",
                 id="the_marked_candidate_is_reviewable"),
    pytest.param(_stale_c1_class(), "", id="without_the_policy_nothing_changes"),
])
def test_review_ahead_still_reviews_a_stale_base_head_the_gate_can_green(tmp_path, row, policy):
    repository = _repository(tmp_path, policy=policy)

    assert merge_open_prs._review_ahead_base(repository, row) == "d" * 40


def test_review_ahead_reviews_an_unmarked_c1_class_head_the_gate_accepts_on_a_stale_base(
        tmp_path):
    """the gate proved the narrowed run's base between the fork point and
    main's tip, so `ci_review_accepted` is true and the head is reviewed over its
    fork point instead of being skipped as `needs_full_run`."""
    repository = _repository(tmp_path)

    assert merge_open_prs._review_ahead_base(
        repository, _stale_c1_class(ci_review_accepted=True)) == "d" * 40


@pytest.mark.parametrize(("required", "reviewed"), [
    ("checks/c1", False),
    ("checks/c2", True),  # the control: review-ahead does reach PR 2
])
def test_the_pass_dispatches_no_review_ahead_for_a_stale_base_c1_class_head(
        tmp_path, required, reviewed):
    """The review-ahead rule through the executor: PR 1 waits on its full run, so the review
    slot goes to PR 2, whose narrowed run succeeded on a stale base."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(merge_candidate=True, run=12),
        ("inspect", "core-repo", 2): _stale_c1_class(ci_required_profile=required),
    })

    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store, max_reviews=1)

    assert ([call[2] for call in gate.calls if call[0] == "review"] == [2]) is reviewed


def test_review_ahead_still_reviews_a_green_c1_class_head_with_a_full_run(tmp_path):
    """The skip is for a head the gate cannot read green; this one already is."""
    repository = _repository(tmp_path)
    row = _queue_inspect(review="not_approved", merge_candidate=False,
                         ci_accepted_profile="checks/c1", ci_required_profile="checks/c1")

    assert merge_open_prs._review_ahead_base(repository, row) == "b" * 40


def _narrowed_accepted(**extra):
    """A gate-C1-class head on the current base whose narrowed `c2` run succeeded.

    The gate reads it CI_PROFILE_INSUFFICIENT, and `ci_review_accepted` says the
    snapshot review accepts that run, so review ahead may spend a slot on it.
    The forge's fork point is the current tip, so the head contains it.
    """
    return _insufficient("success", **{"ci_review_accepted": True,
                                       "recorded_base_sha": "b" * 40, **extra})


def _no_answer(row):
    """The read of a gate that does not send `ci_review_accepted`."""
    row.pop("ci_review_accepted", None)
    return row


@pytest.mark.parametrize("row", [
    pytest.param(_narrowed_accepted(), id="the_blocker_names_the_class"),
    pytest.param(_narrowed_accepted(ci_required_profile="checks/c1"),
                 id="the_row_carries_the_required_profile"),
])
def test_review_ahead_reviews_a_narrowed_c1_class_head_the_gate_accepts(tmp_path, row):
    """The full run needed for merge does not delay a review the gate accepts."""
    repository = _repository(tmp_path)

    assert merge_open_prs._review_ahead_base(repository, row) == "b" * 40


def _without(row, *keys):
    return {key: value for key, value in row.items() if key not in keys}


@pytest.mark.parametrize(("row", "base"), [
    pytest.param(_narrowed_accepted(), "b" * 40, id="the_head_forks_from_the_current_tip"),
    pytest.param(_narrowed_accepted(recorded_base_sha="d" * 40), "d" * 40,
                 id="the_head_forks_before_the_current_tip"),
    pytest.param(_narrowed_accepted(recorded_base_sha="invalid"), None,
                 id="the_fork_point_is_malformed"),
    pytest.param(_narrowed_accepted(recorded_base_sha=None), None, id="the_fork_point_is_null"),
    pytest.param(_without(_narrowed_accepted(), "recorded_base_sha"), None,
                 id="the_row_has_no_fork_point"),
])
def test_review_ahead_reviews_a_narrowed_head_only_over_a_tip_it_contains(tmp_path, row, base):
    """The fork-point guard holds: review the forge's fork point, not a tip the head
    does not contain. A missing or malformed fork point still refuses dispatch."""
    repository = _repository(tmp_path)

    assert merge_open_prs._review_ahead_base(repository, row) == base


@pytest.mark.parametrize("blocker", ["PR_BASE_STALE", "PR_BASE_UNVERIFIED"])
def test_review_ahead_skips_a_green_head_whose_base_the_gate_cannot_prove(tmp_path, blocker):
    """an unverified base is no better than a stale one, because the review
    owner refuses a base that is not an ancestor of the head all the same."""
    repository = _repository(tmp_path)
    row = _queue_inspect(review="not_approved", merge_candidate=False, blocking_reason=blocker)

    assert merge_open_prs._review_ahead_base(repository, row) is None


@pytest.mark.parametrize(("row", "reason"), [
    pytest.param({**_queue_inspect(review="not_approved"), "claim_error": "PR_CLAIM_MISSING"},
                 "claim_error:PR_CLAIM_MISSING", id="a_claim_error"),
    pytest.param(_queue_inspect(review="not_approved", blocking_reason="PR_BASE_STALE"),
                 "PR_BASE_STALE", id="a_green_head_behind_main"),
    pytest.param(_queue_inspect(review="not_approved", blocking_reason="PR_BASE_UNVERIFIED"),
                 "PR_BASE_UNVERIFIED", id="a_green_head_with_an_unproven_base"),
    pytest.param(_stale_c1_class(), "needs_full_run", id="an_unmarked_c1_class_head"),
    pytest.param(_narrowed_accepted(recorded_base_sha=None), "recorded_base_unverified",
                 id="a_narrowed_head_without_a_fork_point"),
    pytest.param(_queue_inspect(ci="pending", review="not_approved",
                                blocking_reason="CI_RUN_IN_PROGRESS"),
                 "CI_RUN_IN_PROGRESS", id="a_run_in_progress"),
    pytest.param(_queue_inspect(review="approved"), "approved", id="an_approved_head"),
    pytest.param(_queue_inspect(review="not_approved", pr_state="draft"), "pr_draft",
                 id="a_draft"),
    pytest.param(_queue_inspect(review="not_approved", mergeable=False), "not_mergeable",
                 id="a_conflict"),
    pytest.param(_queue_inspect(review="changes_requested"), "changes_requested",
                 id="a_standing_change_request"),
])
def test_review_ahead_logs_why_it_skips_a_head(tmp_path, caplog, row, reason):
    """the skip of a stale head was silent, so a thin review pipeline
    could not be told from an empty queue."""
    repository = _repository(tmp_path)

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        assert merge_open_prs._review_ahead_base(repository, row, pr=7) is None

    assert [record.getMessage() for record in caplog.records] == [
        f"automerge: review ahead skipped core-repo#7 {reason}"]


def test_review_ahead_logs_nothing_for_a_head_it_reviews(tmp_path, caplog):
    repository = _repository(tmp_path)

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        assert merge_open_prs._review_ahead_base(
            repository, _narrowed_accepted(), pr=7) == "b" * 40

    assert caplog.records == []


@pytest.mark.parametrize("row", [
    pytest.param(_narrowed_accepted(ci_review_accepted=False), id="the_gate_refuses_the_run"),
    pytest.param(_no_answer(_narrowed_accepted()), id="a_gate_that_sends_no_answer"),
    pytest.param(_narrowed_accepted(latest_delegated_review_state="APPROVED"),
                 id="a_delegated_review_approved_it"),
    pytest.param(_narrowed_accepted(review_state="approved"), id="the_head_is_approved"),
    pytest.param(_narrowed_accepted(mergeable=False), id="the_head_conflicts"),
    pytest.param({**_narrowed_accepted(), "blocking_reason": "PR_REVIEW_CHANGES_REQUESTED"},
                 id="a_change_request_stands"),
])
def test_review_ahead_skips_a_narrowed_head_unless_the_gate_accepts_it(tmp_path, row):
    repository = _repository(tmp_path)

    assert merge_open_prs._review_ahead_base(repository, row) is None


@pytest.mark.parametrize(("row", "reviewed"), [
    pytest.param(_narrowed_accepted(), True, id="the_gate_accepts_the_run"),
    pytest.param(_narrowed_accepted(ci_review_accepted=False), False, id="the_gate_refuses_it"),
    pytest.param(_no_answer(_narrowed_accepted()), False, id="a_gate_that_sends_no_answer"),
    pytest.param(_narrowed_accepted(recorded_base_sha="d" * 40), True,
                 id="the_head_forks_before_the_current_tip"),
])
def test_the_pass_reviews_ahead_a_narrowed_c1_class_head_the_gate_accepts(
        tmp_path, row, reviewed):
    """Through the executor: PR 1 waits on its full run, so the review slot
    goes to PR 2, a C1-class head that is not marked and not yet approved."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(merge_candidate=True, run=12),
        ("inspect", "core-repo", 2): row,
    })

    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store, max_reviews=1)

    assert [call[2:5] for call in gate.calls if call[0] == "review"] == (
        [(2, "a" * 40, row["recorded_base_sha"])] if reviewed else [])


@pytest.mark.parametrize(("row", "why"), [
    pytest.param(_narrowed_accepted(), "not_approved",
                 id="an_unapproved_narrowed_head_lacks_only_a_review"),
    pytest.param(_narrowed_accepted(latest_delegated_review_state="APPROVED"), None,
                 id="an_approved_narrowed_head_rides_a_train"),
    pytest.param(_narrowed_accepted(latest_delegated_review_state="APPROVED", mergeable=False),
                 "not_mergeable", id="a_conflict_is_named_before_the_narrowed_run"),
    pytest.param(_narrowed_accepted(ci_review_accepted=False), "ci_not_green",
                 id="a_narrowed_head_the_gate_refuses"),
    pytest.param(_no_answer(_narrowed_accepted()), "ci_not_green",
                 id="a_gate_that_sends_no_answer"),
])
def test_a_narrowed_head_the_gate_accepts_is_named_for_the_train_scan(tmp_path, row, why):
    """The lookahead may review an unapproved one; the gate's train build
    admits an approved one on the same rule, so it rides a train."""
    assert merge_open_prs._train_ineligibility(_repository(tmp_path), row) == why


def test_a_parked_candidate_has_its_mark_cleared(tmp_path):
    """The full run failed: the PR leaves the queue and the mark goes with it."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="red", conclusion="failure", blocking_reason="CI_RUN_FAILED",
            merge_candidate=True),
    })
    _pass(repository, gate, store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, False)]


def test_a_refresh_conflict_parks_without_a_mark_to_clear(tmp_path):
    """the refresh comes before the mark, so a refresh that conflicts
    leaves nothing to mark or clear."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", blocking_reason="CI_BASE_STALE", ci_reason="CI_BASE_STALE",
            merge_candidate=False),
        ("refresh", "core-repo", 1): {"ok": False, "code": "PR_REFRESH_CONFLICT"},
    })
    _pass(repository, gate, store)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "conflict"
    assert _writes(gate) == [("refresh", "core-repo", 1, "a" * 40, "b" * 40)]


def test_a_failed_clear_does_not_stop_the_park(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="red", conclusion="failure", blocking_reason="CI_RUN_FAILED",
            merge_candidate=True),
        ("inspect", "core-repo", 2): _queue_inspect(merge_candidate=True),
        ("mark_candidate", "core-repo", 1, False): {"ok": False,
                                                    "code": "PR_CANDIDATE_UNCONFIRMED"},
    })
    result = _pass(repository, gate, store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    # the green PR 2 is elected first; the red PR 1 parks after it.
    assert result["results"][0] == {"pr": 2, "outcome": "merged"}


def test_a_refused_mark_neither_refreshes_nor_merges(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(merge_candidate=False),
        ("mark_candidate", "core-repo", 1, True): {"ok": False,
                                                   "code": "PR_CANDIDATE_UNCONFIRMED"},
    })
    result = _pass(repository, gate, store)
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_UNCONFIRMED"}]
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]


def test_without_the_policy_the_queue_never_marks(tmp_path):
    """docs-repo and every other repository keep the serial queue as is."""
    repository, store = _repository(tmp_path, policy=""), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(),
    })
    result = _pass(repository, gate, store)
    assert result["results"] == [{"pr": 1, "outcome": "merged"}]
    assert _writes(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


def test_the_policy_is_read_from_the_catalog(tmp_path):
    repos = merge_open_prs.load_repositories()
    assert repos["core-repo"].merge_candidate_policy == "label-v1"
    assert repos["docs-repo"].merge_candidate_policy == ""


class PromotedRunGate(CandidateGate):
    """A C2-class head with a narrowed success (run 10) and a promoted full run.

    While `gate/candidate` is on, the gate judges the head on its full run;
    once the park clears the mark it judges the class profile again, which
    the older narrowed success satisfies.
    """

    def __init__(self, full_run):
        super().__init__({"core-repo": [1]})
        self.labelled = True
        self.full_run = full_run
        self.unlabelled = _queue_inspect(run=10, merge_candidate=False,
                                         ci_accepted_profile="checks/c2")

    def inspect(self, repository, pr):
        self.calls.append(("inspect", repository, pr))
        return self.full_run if self.labelled else self.unlabelled

    def mark_candidate(self, repository, pr, candidate):
        self.calls.append(("mark_candidate", repository, pr, candidate))
        changed, self.labelled = self.labelled != candidate, candidate
        return {"ok": True, "candidate": candidate, "label_changed": changed}


UNJUDGED_FULL_RUNS = {
    "cancelled": "CI_RUN_CANCELED",
    "timed_out": "CI_RUN_TIMED_OUT",
    "action_required": "CI_RESPONSE_INVALID",
}


def _unjudged_full_run(conclusion):
    reason = UNJUDGED_FULL_RUNS[conclusion]
    return _queue_inspect(ci="unavailable", run=20, conclusion=conclusion,
                          blocking_reason=reason, ci_reason=reason, merge_candidate=True)


def _park_on_the_promoted_run(tmp_path, conclusion):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = PromotedRunGate(_unjudged_full_run(conclusion))
    _pass(repository, gate, store)
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert (parked["reason"], parked["run"][0]) == ("ci_failed", 20)
    assert _writes(gate) == [("mark_candidate", "core-repo", 1, False)]
    assert not gate.labelled
    gate.calls.clear()
    return repository, store, gate


@pytest.mark.parametrize("conclusion", sorted(UNJUDGED_FULL_RUNS))
def test_an_older_narrowed_success_does_not_release_a_promoted_run_park(tmp_path, conclusion):
    """Clearing the mark makes the gate read the older narrowed
    success again. That is no new evidence for the head, so the park holds
    and no new full run is started on the same head."""
    repository, store, gate = _park_on_the_promoted_run(tmp_path, conclusion)

    _pass(repository, gate, store)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["run"][0] == 20
    assert _writes(gate) == []
    assert not gate.labelled


@pytest.mark.parametrize("run", [20, 30], ids=["rerun_of_the_parked_run", "newer_run"])
def test_a_full_success_at_or_after_the_parked_run_releases_it(tmp_path, run):
    """A rerun of the parked run that succeeds, or a newer full run, is new
    evidence: the park ends and the head merges on it without a new mark."""
    repository, store, gate = _park_on_the_promoted_run(tmp_path, "cancelled")
    gate.unlabelled = _queue_inspect(run=run, merge_candidate=False,
                                     ci_accepted_profile="checks/c1")

    result = _pass(repository, gate, store)

    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]
    assert result["results"] == [{"pr": 1, "outcome": "merged"}]
    assert _writes(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


STALL_PARKS = ["ci_stalled", "evidence_unavailable"]


def _stall_parked_on_the_promoted_run(tmp_path, reason):
    """A park the queue made on the in-flight full run 20, mark already cleared."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"1": {
            "reason": reason, "head": "a" * 40, "base": "b" * 40, "run": [20, 1],
            "review": [None, "none"], "since": 0.0}}}
        store.save(state)
    gate = PromotedRunGate(_in_progress(run=20, merge_candidate=True))
    gate.labelled = False
    return repository, store, gate


@pytest.mark.parametrize("reason", STALL_PARKS)
def test_an_older_narrowed_success_does_not_release_a_stalled_promoted_run_park(
        tmp_path, reason):
    """A stall is no verdict, but going back to the older narrowed run is no
    recovery either: the park holds and the head is not marked again."""
    repository, store, gate = _stall_parked_on_the_promoted_run(tmp_path, reason)

    _pass(repository, gate, store)

    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert (parked["reason"], parked["run"][0]) == (reason, 20)
    assert _writes(gate) == []
    assert not gate.labelled


@pytest.mark.parametrize("run", [20, 30], ids=["rerun_of_the_parked_run", "newer_run"])
@pytest.mark.parametrize("reason", STALL_PARKS)
def test_a_full_success_at_or_after_a_stalled_run_releases_it(tmp_path, reason, run):
    repository, store, gate = _stall_parked_on_the_promoted_run(tmp_path, reason)
    gate.unlabelled = _queue_inspect(run=run, merge_candidate=False,
                                     ci_accepted_profile="checks/c1")

    result = _pass(repository, gate, store)

    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]
    assert result["results"] == [{"pr": 1, "outcome": "merged"}]
    assert _writes(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


# ------------------------------------------------ no full run before approval


class ApprovingGate(CandidateGate):
    """The review approves the head: the next read shows the approval."""

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        response = super().review(repository, pr, head, base, executor_run, retry_of)
        self.responses[("inspect", repository, pr)] = {
            **self.responses[("inspect", repository, pr)], "review_state": "approved"}
        return response


def test_an_unreviewed_head_is_reviewed_before_its_full_run(tmp_path):
    """the mark starts the full run, so it follows the approval."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = ApprovingGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="not_approved", merge_candidate=False, ci_accepted_profile="checks/c2",
            ci_required_profile="checks/c2"),
    })

    result = _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["review", "mark_candidate"]
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_MARKED"}]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


def test_a_rejected_head_gets_no_full_run(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="not_approved", merge_candidate=False, ci_required_profile="checks/c2"),
        ("review", "core-repo", 1): {"ok": True, "code": "REVIEW_PUBLISHED",
                                     "verdict": "CHANGES_REQUIRED"},
    })

    _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["review"]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "changes_requested")


def test_an_unreviewed_stale_base_head_is_refreshed_but_not_marked(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="not_approved", ci="pending", blocking_reason="CI_BASE_STALE",
            merge_candidate=False, ci_required_profile="checks/c2"),
    })

    _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["refresh"]


def test_a_delegated_approval_is_marked_after_its_refresh(tmp_path):
    """An approval of the head on an older base survives the clean refresh.

    The mark reads the refreshed head: the pre-refresh read names the old head,
    and the reuse proof is what carries the approval onto the new one."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(
                review="not_approved", latest_delegated_review_state="APPROVED", ci="pending",
                blocking_reason="CI_BASE_STALE", merge_candidate=False,
                ci_required_profile="checks/c2"),
            _queue_inspect(head="c", review="approved", ci="pending", merge_candidate=False,
                           ci_required_profile="checks/c2"),
        ],
    })

    _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["refresh", "mark_candidate"]


def test_a_refresh_without_a_surviving_approval_is_not_marked(tmp_path):
    """the pre-refresh head's delegated approval does not authorise the
    mark of the refreshed head. The reuse proof decides, and a range it cannot
    digest -- a binary change, for one -- refuses it, so the new head reads
    not_approved and must be reviewed before any full run."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(
                review="not_approved", latest_delegated_review_state="APPROVED", ci="pending",
                blocking_reason="CI_BASE_STALE", merge_candidate=False,
                ci_required_profile="checks/c2"),
            _queue_inspect(head="c", review="not_approved", ci="pending",
                           merge_candidate=False, ci_required_profile="checks/c2"),
        ],
    })

    _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["refresh"]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


def test_a_refresh_whose_new_head_cannot_be_read_is_not_marked(tmp_path):
    """A failed read of the refreshed head keeps the refresh and marks nothing."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(
                review="not_approved", latest_delegated_review_state="APPROVED", ci="pending",
                blocking_reason="CI_BASE_STALE", merge_candidate=False,
                ci_required_profile="checks/c2"),
            {"ok": False, "code": "TEA_REVIEW_PROBE_FAILED"},
        ],
    })

    _pass(repository, gate, store)

    assert [call[0] for call in _writes(gate)] == ["refresh"]


def test_a_refused_mark_after_the_approval_keeps_the_pr_active(tmp_path):
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = ApprovingGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="not_approved", merge_candidate=False, ci_required_profile="checks/c2"),
        ("mark_candidate", "core-repo", 1, True): {"ok": False,
                                                   "code": "PR_CANDIDATE_UNCONFIRMED"},
    })

    result = _pass(repository, gate, store)

    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_UNCONFIRMED"}]
    assert not [call for call in gate.calls if call[0] == "merge"]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


# ------------------------------------------------ one CI-evidence answer


def _unknown(reason, **extra):
    """The gate's answer to a read it cannot decide: not accepted, and not a refusal."""
    return {"ci_review_accepted": False, "ci_evidence_answer": "unknown",
            "ci_evidence_reason": reason, **extra}


class DelegatedApprovingGate(CandidateGate):
    """The review approves the head over its recorded base, not the exact pair.

    The next read keeps `review_state: not_approved` and shows the delegated
    approval, as for an unrefreshed head reviewed on an older base."""

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        response = super().review(repository, pr, head, base, executor_run, retry_of)
        self.responses[("inspect", repository, pr)] = {
            **self.responses[("inspect", repository, pr)],
            "latest_delegated_review_state": "APPROVED"}
        return response


def test_an_active_head_accepted_on_mains_tip_is_reviewed_over_its_fork_point(tmp_path):
    """The gate accepts a narrowed run on the exact (head, main's tip) pair of a head
    that forked before that tip. The active path reviews the forge's fork point,
    which the head contains, and the approval that review earns marks the head at
    once: the PR is neither parked nor reviewed again."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = DelegatedApprovingGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _narrowed_accepted(recorded_base_sha="d" * 40),
    })

    first = _pass(repository, gate, store)

    assert [call[:5] for call in _writes(gate)] == [
        ("review", "core-repo", 1, "a" * 40, "d" * 40),
        ("mark_candidate", "core-repo", 1, True)]
    assert first["results"] == [{"pr": 1, "outcome": "retained",
                                 "reason": "PR_CANDIDATE_MARKED"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1 and row["parked"] == {}

    # The `labeled` event's full run is running: no second review, no write.
    gate.calls.clear()
    gate.responses[("inspect", "core-repo", 1)] = _in_progress(
        merge_candidate=True, run=12, review="not_approved",
        latest_delegated_review_state="APPROVED", recorded_base_sha="d" * 40)
    _pass(repository, gate, store)
    assert _writes(gate) == []



class DelegatedApprovingUnavailableGate(DelegatedApprovingGate):
    """The review approves over the recorded base, but the read after it finds
    the CI listing failed: the approval stands, the CI answer is unknown."""

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        response = super().review(repository, pr, head, base, executor_run, retry_of)
        self.responses[("inspect", repository, pr)] = _queue_inspect(
            ci="unavailable", conclusion=None, review="not_approved",
            blocking_reason="CI_API_UNAVAILABLE", merge_candidate=False,
            latest_delegated_review_state="APPROVED", recorded_base_sha="d" * 40,
            **_unknown("CI_EVIDENCE_UNAVAILABLE"))
        return response


def test_an_approval_over_the_fork_point_does_not_mark_a_head_whose_ci_read_failed(tmp_path):
    """The approval earned over the recorded base marks the head only when the
    fresh read passes the guards of the pre-review mark; a CI listing that failed after the
    review waits, as any unmarked head with that blocker does."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = DelegatedApprovingUnavailableGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _narrowed_accepted(recorded_base_sha="d" * 40),
    })

    first = _pass(repository, gate, store)

    assert [call[:5] for call in _writes(gate)] == [
        ("review", "core-repo", 1, "a" * 40, "d" * 40)]
    assert first["results"] == [{"pr": 1, "outcome": "retained",
                                 "reason": "CI_API_UNAVAILABLE"}]



class ExactApprovingGate(CandidateGate):
    """The review approves the exact pair; the read after it is `after`."""

    def __init__(self, *args, after, **kwargs):
        super().__init__(*args, **kwargs)
        self.after = after

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        response = super().review(repository, pr, head, base, executor_run, retry_of)
        self.responses[("inspect", repository, pr)] = self.after
        return response


@pytest.mark.parametrize(("after", "writes", "outcome"), [
    pytest.param({**_queue_inspect(ci="unavailable", conclusion=None,
                                   blocking_reason="CI_API_UNAVAILABLE", merge_candidate=False,
                                   **_unknown("CI_EVIDENCE_UNAVAILABLE")),
                  "review_state": "approved"},
                 [], {"reason": "CI_API_UNAVAILABLE"}, id="a_failed_ci_read_waits"),
    pytest.param({**_insufficient("success", **_unknown("RANGE_UNCLASSIFIED")),
                  "review_state": "approved"},
                 [("mark_candidate", "core-repo", 1, True)],
                 {"reason": "PR_CANDIDATE_MARKED",
                  "detail": "ci_evidence_unknown:RANGE_UNCLASSIFIED"},
                 id="an_eligible_unknown_read_is_marked_and_named"),
])
def test_a_mark_after_an_exact_approval_needs_the_fresh_read_to_land_next(
        tmp_path, after, writes, outcome):
    """The read after the review decides the mark as the read before it did: a
    CI read that failed meanwhile waits, and a mark on an unknown answer names it."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = ExactApprovingGate({"core-repo": [1]}, after=after, responses={
        ("inspect", "core-repo", 1): _narrowed_accepted(),
    })

    result = _pass(repository, gate, store)

    assert [call[:5] for call in _writes(gate)] == [
        ("review", "core-repo", 1, "a" * 40, "b" * 40), *writes]
    assert result["results"] == [{"pr": 1, "outcome": "retained", **outcome}]


@pytest.mark.parametrize(("answer", "detail"), [
    pytest.param(_unknown("RECORDED_BASE_MISSING"),
                 {"detail": "ci_evidence_unknown:RECORDED_BASE_MISSING"},
                 id="an_unknown_answer_is_named"),
    pytest.param({"ci_review_accepted": False, "ci_evidence_answer": "rejected",
                  "ci_evidence_reason": "EVIDENCE_BINDING_MISMATCH"}, {},
                 id="a_rejected_answer_is_the_plain_mark"),
])
def test_a_mark_placed_on_an_unknown_ci_answer_names_it(tmp_path, answer, detail):
    """The queue still buys the full run for a head whose CI evidence the gate
    cannot decide, the conservative wait, but never as a reasonless mark."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _insufficient("success", **answer),
    })

    result = _pass(repository, gate, store)

    assert _writes(gate) == [("mark_candidate", "core-repo", 1, True)]
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_CANDIDATE_MARKED", **detail}]


def test_an_unknown_answer_from_a_failed_ci_listing_waits_and_is_not_marked(tmp_path):
    """An unknown answer lifts only the wait for approval; the guards of every
    mark still apply. A failed CI listing is not a narrowed run that succeeded,
    so the pass waits on it, as before the shared CI-evidence answer, and buys no full run."""
    repository, store = _repository(tmp_path), _queue_store(tmp_path)
    gate = CandidateGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="unavailable", conclusion=None, review="not_approved",
            blocking_reason="CI_API_UNAVAILABLE", merge_candidate=False,
            **_unknown("CI_EVIDENCE_UNAVAILABLE")),
    })

    result = _pass(repository, gate, store)

    assert _writes(gate) == []
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "CI_API_UNAVAILABLE"}]


def test_review_ahead_names_an_unknown_ci_answer_instead_of_a_full_run(tmp_path, caplog):
    repository = _repository(tmp_path)

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        assert merge_open_prs._review_ahead_base(
            repository, _stale_c1_class(**_unknown("MAIN_TIP_MISSING")), pr=7) is None

    assert [record.getMessage() for record in caplog.records] == [
        "automerge: review ahead skipped core-repo#7 ci_evidence_unknown:MAIN_TIP_MISSING"]


def test_review_ahead_names_an_unknown_answer_on_a_failed_ci_listing(tmp_path, caplog):
    """The CI-driven fallback skip names an unknown answer too, not only the
    full-profile skip: a `checks/c2` head in a repository without the
    candidate policy whose CI listing failed."""
    repository = merge_open_prs.Repository("core-repo", tmp_path, "forgejo", "acme/core-repo")
    row = _queue_inspect(ci="unavailable", conclusion=None, review="not_approved",
                         blocking_reason="CI_API_UNAVAILABLE", merge_candidate=False,
                         ci_required_profile="checks/c2",
                         **_unknown("CI_EVIDENCE_UNAVAILABLE"))

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        assert merge_open_prs._review_ahead_base(repository, row, pr=7) is None

    assert [record.getMessage() for record in caplog.records] == [
        "automerge: review ahead skipped core-repo#7 ci_evidence_unknown:CI_EVIDENCE_UNAVAILABLE"]


@pytest.mark.parametrize(("row", "why"), [
    pytest.param(_insufficient("success", **_unknown("RECORDED_BASE_MISSING")),
                 "ci_evidence_unknown:RECORDED_BASE_MISSING", id="an_unknown_answer_is_named"),
    pytest.param(_insufficient("success", ci_review_accepted=False,
                               ci_evidence_answer="rejected",
                               ci_evidence_reason="EVIDENCE_BINDING_MISMATCH"),
                 "ci_not_green", id="a_rejected_answer_is_not_green"),
    pytest.param(_stale_c1_class(**_unknown("MAIN_TIP_MISSING"),
                                 latest_delegated_review_state="APPROVED"),
                 None, id="a_refresh_eligible_head_still_rides"),
])
def test_the_train_scan_names_an_unknown_ci_answer_after_the_refresh_exemption(
        tmp_path, row, why):
    assert merge_open_prs._train_ineligibility(_repository(tmp_path), row) == why


def _train_v2(tmp_path):
    return merge_open_prs.Repository("core-repo", tmp_path, "forgejo", "acme/core-repo",
                                     merge_candidate_policy="label-v1",
                                     merge_batch_policy="train-v2")


@pytest.mark.parametrize(("answer", "why"), [
    pytest.param({"ci_evidence_answer": "rejected", "ci_evidence_reason": "RUN_BASE_OUTSIDE_RANGE",
                  "ci_review_accepted": False},
                 "ci_not_green", id="a_rejected_stale_success_does_not_ride"),
    pytest.param(_unknown("MAIN_TIP_MISSING"),
                 "ci_evidence_unknown:MAIN_TIP_MISSING", id="an_unknown_stale_success_is_named"),
    pytest.param({"ci_evidence_answer": "accepted", "ci_evidence_reason": "GREEN",
                  "ci_review_accepted": True},
                 None, id="an_accepted_stale_success_rides"),
    pytest.param({}, "ci_not_green", id="a_read_without_the_answer_fails_closed"),
])
def test_a_train_v2_rider_needs_the_gates_accepted_answer(tmp_path, answer, why):
    """train-v2's member refusal admits exactly what the record's
    CI-evidence answer accepts. The executor reads the same answer, so a
    refresh-eligible head (a success on an older base) the gate would refuse
    is not offered as a rider and no pass is spent on a refused build."""
    row = {key: value for key, value in _stale_c1_class(
        latest_delegated_review_state="APPROVED", **answer).items()
        if answer or key not in {"ci_evidence_answer", "ci_evidence_reason"}}
    assert merge_open_prs._refresh_eligible(_train_v2(tmp_path), row)
    assert merge_open_prs._train_ineligibility(_train_v2(tmp_path), row) == why
