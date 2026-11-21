"""Shared fixtures for the merge-open-PRs executor contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SCRIPT_PATH = REPO_ROOT / "tools-config" / "scripts" / "merge_open_prs.py"

def _load_module():
    spec = importlib.util.spec_from_file_location("merge_open_prs", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, not after: `@dataclass` resolves its own
    # class's module out of `sys.modules` while the decorator runs, and a
    # by-path load that skips this raises an AttributeError on None at import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

merge_open_prs = _load_module()

QUEUE_STORE_TYPE = merge_open_prs.QueueStore

@pytest.fixture(autouse=True)
def _private_automerge_state(monkeypatch, tmp_path):
    """CLI tests use the real state writer without touching executor state."""
    directory = tmp_path / "gate-state"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(merge_open_prs, "QueueStore",
                        lambda **kwargs: QUEUE_STORE_TYPE(directory / "automerge.json", **kwargs))
    # The deployment adapter's reads use their own imported constructor;
    # isolating only the CLI writer leaves status tests dependent on the real
    # executor queue.
    server_adapter = sys.modules.get("gate_server_estate")
    if server_adapter is not None:
        monkeypatch.setattr(server_adapter, "QueueStore",
                            lambda **kwargs: QUEUE_STORE_TYPE(directory / "automerge.json", **kwargs))
    # The deploy marker on the machine running the suite must never
    # pause a test's pass; the marker tests write their own here.
    monkeypatch.setattr(merge_open_prs, "DEPLOY_MARKER_PATH",
                        tmp_path / "deploy-state" / "marker.json")
    import deploy_chain.state as deploy_state
    monkeypatch.setattr(deploy_state, "DEPLOY_LOCK_PATH", tmp_path / "deploy.lock")
    from git_gate import review_followups
    original_status = review_followups.status
    private_journal = tmp_path / "followups"
    private_journal.mkdir(mode=0o700)
    monkeypatch.setattr(review_followups, "status", lambda repository, pr: original_status(
        repository, pr, root=private_journal))

class FakeGateApplication:
    def __init__(self, candidates, *, responses=None, head_refs=None, labels=None):
        self.by_repository = candidates
        # Listed head branches and labels per repository; None models an
        # application that returns only `pull_requests`.
        self.head_refs = head_refs
        self.labels = labels
        self.responses = responses or {}
        self.calls = []
        # Queue-status publications, kept apart from `calls` so the review and
        # merge sequences asserted elsewhere stay exact.
        self.statuses = []
        # The head each published parked PR was parked on, per publication.
        self.parked_heads = []
        self.heads = {}
        # The forge timeline: every label a listing showed on a PR stays in
        # its history after it is removed. `inspect` reports it as the gate
        # does, for a Renovate PR only and unless a response names its own.
        self.label_history = {}

    def retry_review_followups(self):
        return {"ok": True}

    def queue_status(self, repository, descriptions, published):
        """Model the gate: skip an unchanged [head, description] pair."""
        self.statuses.append((repository, {pr: description for pr, (description, _head)
                                           in descriptions.items()}, dict(published)))
        self.parked_heads.append({pr: head for pr, (_description, head) in descriptions.items()
                                  if head is not None})
        response = self.responses.get(("queue_status", repository))
        if response is not None:
            return response
        result = {}
        for pr, (description, _head) in descriptions.items():
            current = [self.heads.get((repository, pr), "a" * 40), description]
            result[str(pr)] = current
        return {"ok": True, "published": result}

    def candidates(self, repository):
        self.calls.append(("candidates", repository))
        listed = {"ok": True, "pull_requests": self.by_repository[repository]}
        if self.head_refs is not None:
            listed["head_refs"] = {str(pr): head for pr, head
                                   in self.head_refs.get(repository, {}).items()}
        if self.labels is not None:
            listed["labels"] = {str(pr): names for pr, names
                                in self.labels.get(repository, {}).items()}
            for pr, names in self.labels.get(repository, {}).items():
                if isinstance(names, list):
                    self.label_history.setdefault((repository, pr), set()).update(
                        name for name in names if isinstance(name, str))
        return listed

    def _with_label_history(self, repository, pr, response):
        history = self.label_history.get((repository, pr), set())
        head = (self.head_refs or {}).get(repository, {}).get(pr)
        renovate = "renovate" in history or (isinstance(head, str)
                                             and head.startswith("renovate/"))
        if not renovate or not response.get("ok") or "label_history" in response:
            return response
        return {**response, "label_history": sorted(history)}

    def inspect(self, repository, pr):
        self.calls.append(("inspect", repository, pr))
        response = self.responses.get(("inspect", repository, pr))
        if isinstance(response, list):
            return self._with_label_history(repository, pr, response.pop(0))
        if response:
            return self._with_label_history(repository, pr, response)
        return self._with_label_history(repository, pr, {
            "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "ci_state": "green", "review_state": "approved"})

    def consent_preserved(self, repository, pr, consented_head, head, base):
        return False

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        self.calls.append(
            ("review", repository, pr, head, base, executor_run, retry_of)
        )
        response = self.responses.get(("review", repository, pr))
        if isinstance(response, list):
            return response.pop(0)
        if response:
            return response
        return {"ok": True}

    def merge(self, repository, pr, head, base):
        self.calls.append(("merge", repository, pr, head, base))
        return self.responses.get(("merge", repository, pr), {"ok": True})

    def refresh(self, repository, pr, head, base):
        self.calls.append(("refresh", repository, pr, head, base))
        return self.responses.get(
            ("refresh", repository, pr),
            {"ok": True, "code": "PR_REFRESHED", "head_sha": "c" * 40},
        )

def _queue_store(tmp_path, repositories=("core-repo", "docs-repo")):
    directory = tmp_path / "persistent-queue"
    directory.mkdir(mode=0o700)
    return QUEUE_STORE_TYPE(directory / "automerge.json", repositories=repositories)

def _queue_inspect(*, head="a", base="b", ci="green", review="approved",
                   run=11, attempt=1, conclusion="success", **extra):
    row = {"ok": True, "head_sha": head * 40, "base_sha": base * 40,
           "ci_state": ci, "review_state": review, "ci_run_id": run,
           "ci_run_attempt": attempt, "ci_run_conclusion": conclusion,
           # the gate's own CI-evidence answer,
           # which the executor reads instead of re-deriving the rule. Only a
           # green read is accepted by default; a fixture that models the
           # accepted narrowed run overrides it.
           "ci_review_accepted": ci == "green"}
    # The gate projection reports either
    # rejection as this blocker ahead of any CI blocker.
    if (review == "changes_requested"
            or extra.get("latest_delegated_review_state") == "REQUEST_CHANGES"):
        row.update(blocking_reason="PR_REVIEW_CHANGES_REQUESTED",
                   next_action="address_review_findings")
    row = {**row, **extra}
    # the gate projects `ci_evidence_answer` next to
    # `ci_review_accepted`; an accepted read names its reason. A fixture that
    # models a rejected or unknown read sets the answer itself.
    if row["ci_review_accepted"] is True and "ci_evidence_answer" not in row:
        row.update(ci_evidence_answer="accepted",
                   ci_evidence_reason="GREEN" if row["ci_state"] == "green" else "NARROWED_RUN")
    return row

def _core_repo(tmp_path):
    return merge_open_prs.Repository("core-repo", tmp_path, "forgejo", "acme/core-repo")

def _in_progress(**extra):
    return _queue_inspect(ci="pending", conclusion=None,
                          blocking_reason="CI_RUN_IN_PROGRESS", **extra)

def _train_repository(tmp_path, policy="train-v1"):
    return merge_open_prs.Repository("core-repo", tmp_path, "forgejo", "acme/core-repo",
                                     merge_batch_policy=policy)
