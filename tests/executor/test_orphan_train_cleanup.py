"""Retry abandoned train closes through the existing forge listing and gate."""

from __future__ import annotations

import pytest
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _core_repo,
    _in_progress,
    _queue_store,
    _train_repository,
    merge_open_prs,
)


class ClosingGate(FakeGateApplication):
    def train_close(self, repository, train_pr, reason):
        self.calls.append(("train_close", repository, train_pr, reason))
        result = self.responses.get(("train_close", repository, train_pr), {"ok": True})
        if result.get("ok"):
            self.by_repository[repository].remove(train_pr)
        return result


def test_failed_orphan_close_retries_from_next_listing_without_stalling_queue(tmp_path):
    gate = ClosingGate({"core-repo": [1, 9]}, head_refs={"core-repo": {9: "train/bbbbbbbbbbbb-1"}},
                       responses={("inspect", "core-repo", 1): _in_progress(),
                                  ("train_close", "core-repo", 9): {"ok": False, "code": "GATE_LOCK_BUSY"}})
    store = _queue_store(tmp_path)
    first = merge_open_prs.run_automerge_pass([_train_repository(tmp_path)], gate, queue_store=store)[0]
    assert first["train_cleanup"] == {"pr": 9, "outcome": "close_failed", "reason": "GATE_LOCK_BUSY"}
    assert first["results"][0]["reason"] == "CI_RUN_IN_PROGRESS"
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    gate.responses[("train_close", "core-repo", 9)] = {"ok": True}
    second = merge_open_prs.run_automerge_pass([_train_repository(tmp_path)], gate, queue_store=store)[0]
    assert second["train_cleanup"] == {"pr": 9, "outcome": "closed"}
    assert [call for call in gate.calls if call[0] == "train_close"] == [
        ("train_close", "core-repo", 9, "TRAIN_ORPHANED"),
        ("train_close", "core-repo", 9, "TRAIN_ORPHANED"),
    ]
    assert not any(call[:3] == ("inspect", "core-repo", 9) for call in gate.calls)


def test_cleanup_is_one_attempt_per_pass_across_rotating_repositories(tmp_path):
    docs_repo = merge_open_prs.Repository("docs-repo", tmp_path, "forgejo", "acme/docs-repo",
                                         merge_batch_policy="train-v1")
    gate = ClosingGate({"core-repo": [9, 10], "docs-repo": [8]},
                       head_refs={"core-repo": {9: "train/bbbbbbbbbbbb-1", 10: "train/cccccccccccc-2"},
                                  "docs-repo": {8: "train/dddddddddddd-3"}})
    store = _queue_store(tmp_path)
    for _ in range(2):
        merge_open_prs.run_automerge_pass([_train_repository(tmp_path), docs_repo], gate, queue_store=store)
    assert [call[1:3] for call in gate.calls if call[0] == "train_close"] == [
        ("core-repo", 9), ("docs-repo", 8),
    ]


def test_refused_malformed_orphan_does_not_starve_a_later_orphan(tmp_path):
    """`train_close` refuses a non-gate `train/` head as TRAIN_INVALID before
    any write, on every pass; that refusal spends no close attempt."""
    gate = ClosingGate({"core-repo": [9, 10]},
                       head_refs={"core-repo": {9: "train/not-a-train", 10: "train/cccccccccccc-2"}},
                       responses={("train_close", "core-repo", 9): {"ok": False, "code": "TRAIN_INVALID"}})
    result = merge_open_prs.run_automerge_pass([_train_repository(tmp_path)], gate,
                                             queue_store=_queue_store(tmp_path))[0]
    assert [call[1:3] for call in gate.calls if call[0] == "train_close"] == [
        ("core-repo", 9), ("core-repo", 10),
    ]
    assert result["train_cleanup"] == {"pr": 10, "outcome": "closed"}
    assert gate.by_repository["core-repo"] == [9]


def test_cleanup_protects_active_train_and_its_member_prs(tmp_path):
    store = _queue_store(tmp_path)
    prefixes = [
        {"position": 1, "pr": 8, "branch": "train/bbbbbbbbbbbb-1-prefix-1", "candidate": "c" * 40},
        {"position": 2, "pr": 9, "branch": "train/bbbbbbbbbbbb-1", "candidate": "e" * 40},
    ]
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": {
            "pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
            "wait_since": 1000, "started": True,
            "train": {"pr": 9, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40,
                      "started": 1000, "prefixes": prefixes, "members": [
                          {"pr": 1, "head": "a" * 40, "candidate": "c" * 40},
                          {"pr": 2, "head": "d" * 40, "candidate": "e" * 40},
                      ]},
        }, "parked": {}}
        store.save(state)

    class ActiveTrain(ClosingGate):
        def train_inspect(self, repository, train_pr):
            return {"ok": True, "pr_state": "open", "head_sha": "e" * 40, "base_sha": "b" * 40,
                    "members": [{"pr": 1, "head": "a" * 40}, {"pr": 2, "head": "d" * 40}],
                    "prefixes": [{**prefix, "ci_state": "pending", "ci_run_id": 11,
                                  "ci_run_attempt": 1} for prefix in prefixes],
                    "ci_state": "pending", "ci_run_id": 11, "ci_run_attempt": 1}

    gate = ActiveTrain({"core-repo": [1, 2, 8, 9, 10]}, head_refs={"core-repo": {
        1: "task/first", 2: "task/second", 9: "train/bbbbbbbbbbbb-1",
        8: "train/bbbbbbbbbbbb-1-prefix-1",
        10: "train/dddddddddddd-3",
    }})
    merge_open_prs.run_automerge_pass([_train_repository(tmp_path)], gate, queue_store=store, now=lambda: 1000)
    assert [call[2] for call in gate.calls if call[0] == "train_close"] == [10]
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 9


@pytest.mark.parametrize("code", ["EXECUTOR_HOST_REQUIRED", "INTERNAL_ERROR"])
def test_orphan_close_authority_or_integrity_refusal_stops_later_mutations(tmp_path, code):
    gate = ClosingGate({"core-repo": [1, 9]}, head_refs={"core-repo": {9: "train/bbbbbbbbbbbb-1"}},
                       responses={("train_close", "core-repo", 9): {"ok": False, "code": code}})
    result = merge_open_prs.run_automerge_pass([_train_repository(tmp_path)], gate,
                                             queue_store=_queue_store(tmp_path))[0]
    assert result["authority_integrity_failed"] is True
    assert not any(call[0] in {"inspect", "review", "merge", "refresh"} for call in gate.calls)
    assert not gate.statuses


def test_cleanup_without_train_policy_excludes_train_but_does_not_close_it(tmp_path):
    gate = ClosingGate({"core-repo": [1, 9]}, head_refs={"core-repo": {9: "train/bbbbbbbbbbbb-1"}})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path))
    assert [call[2] for call in gate.calls if call[0] == "merge"] == [1]
    assert not any(call[0] == "train_close" for call in gate.calls)
