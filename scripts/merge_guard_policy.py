"""Policy decisions for merge-guard, adapted from ci-burst's review/merge gate.

One owner for the rules a trusted workflow applies. The caller supplies a
snapshot of GitHub state; this module decides, and it never performs I/O, so
every rule is directly testable.

Adapted from mikomiel/ci-burst `scripts/pr_policy.py` (same estate, same
allowlist model). The changes are the ones the shared engine needs: the rules
are data (`.github/merge-guard.toml`) instead of constants, the gate status
carries the reviewed head, and dependency-update exemption is opt-in.

Native `reviewDecision` is never an input. It is a derived convenience that
drops reviews on a new commit in ways this gate cannot verify.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

ACTIONS_APP_ID = 15368
DECISIVE = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
REVIEW_STATES = DECISIVE | {"COMMENTED", "PENDING"}
DEPENDENCY_FILES = frozenset({
    "requirements.txt", "requirements-dev.txt", "uv.lock", "poetry.lock",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum", "Cargo.lock",
})
SEVERITY = {"success": 0, "pending": 1, "failure": 2}


class Unusable(ValueError):
    """A complete policy decision cannot be established from these inputs."""


@dataclass(frozen=True)
class Policy:
    """Everything the gate needs to know about one consumer repository."""

    repository: str
    base_branch: str
    reviewer_login: str
    reviewer_type: str
    trusted_associations: frozenset[str]
    required_checks: tuple[str, ...]
    gate_context: str
    ordinary_paths: tuple[str, ...]
    dependency_update_exemption: bool


def load_policy(data: dict[str, Any]) -> Policy:
    """Validate the consumer file once, refusing anything ambiguous."""
    try:
        repository = data["repository"]["name"]
        base_branch = data["repository"]["base_branch"]
        reviewer = data["review"]["reviewer_login"]
        reviewer_type = data["review"].get("reviewer_type", "Bot")
        associations = tuple(data["review"].get("trusted_associations", []))
        checks = tuple(data["review"]["required_checks"])
        gate = data["review"].get("gate_context", "review")
        ordinary = tuple(data.get("paths", {}).get("ordinary", []))
        exemption = bool(data.get("paths", {}).get("dependency_update_exemption", False))
    except (KeyError, TypeError, AttributeError) as exc:
        raise Unusable(f"policy_missing_field: {exc}") from exc
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name := str(repository)):
        raise Unusable("policy_repository_invalid")
    if not all(isinstance(x, str) and x for x in (base_branch, reviewer, reviewer_type, gate)):
        raise Unusable("policy_identity_invalid")
    if not checks or not all(isinstance(x, str) and x for x in checks):
        raise Unusable("policy_required_checks_invalid")
    if not all(isinstance(x, str) and x for x in associations):
        raise Unusable("policy_associations_invalid")
    if not all(isinstance(x, str) and x for x in ordinary):
        raise Unusable("policy_ordinary_paths_invalid")
    return Policy(
        repository=name,
        base_branch=base_branch,
        reviewer_login=reviewer,
        reviewer_type=reviewer_type,
        trusted_associations=frozenset(associations),
        required_checks=checks,
        gate_context=gate,
        ordinary_paths=ordinary,
        dependency_update_exemption=exemption,
    )


def valid_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _trusted(item: dict[str, Any], policy: Policy) -> bool:
    user = item["user"]
    if (user.get("login"), user.get("type")) == (policy.reviewer_login, policy.reviewer_type):
        return True
    association = item.get("author_association") or user.get("association")
    return association in policy.trusted_associations


def review_verdict(reviews: Any, head: str, author: str, policy: Policy) -> tuple[str, str]:
    """Latest decisive state per verified identity; any change request blocks.

    A review counts only when its identity is the configured reviewer or carries
    a trusted association, and never when it is the pull request's author.
    """
    if not valid_sha(head) or not isinstance(reviews, list) or not author:
        raise Unusable("invalid_review_snapshot")
    latest: dict[int, dict[str, Any]] = {}
    for item in reviews:
        if not isinstance(item, dict):
            raise Unusable("invalid_review")
        user = item.get("user")
        if not isinstance(user, dict) or type(user.get("id")) is not int:
            raise Unusable("invalid_review_identity")
        if type(item.get("id")) is not int or item["id"] < 1:
            raise Unusable("invalid_review_id")
        state = item.get("state")
        if not isinstance(state, str) or state not in REVIEW_STATES:
            raise Unusable("unknown_review_state")
        if not _trusted(item, policy) or user.get("login") == author or state not in DECISIVE:
            continue
        if not valid_sha(item.get("commit_id")):
            raise Unusable("invalid_review_commit")
        previous = latest.get(user["id"])
        if previous is None or item["id"] > previous["id"]:
            latest[user["id"]] = item
    if any(x["state"] == "CHANGES_REQUESTED" for x in latest.values()):
        return "failure", "changes_requested"
    approved_at_head = [
        item for item in latest.values()
        if item["state"] == "APPROVED" and item["commit_id"] == head
    ]
    if any((item["user"].get("login"), item["user"].get("type"))
           == (policy.reviewer_login, policy.reviewer_type)
           for item in approved_at_head):
        return "success", "approved_at_head"
    if approved_at_head:
        return "pending", "reviewer_approval_required"
    return "pending", "current_head_approval_required"


def pull_scope(pr: Any, policy: Policy) -> bool:
    """The pull request belongs to this repository's base branch and is shaped."""
    if not isinstance(pr, dict):
        return False
    base, head, user = pr.get("base"), pr.get("head"), pr.get("user")
    if not all(isinstance(value, dict) for value in (base, head, user)):
        return False
    if not isinstance(base.get("repo"), dict):
        return False
    return (
        pr.get("state") == "open"
        and type(pr.get("number")) is int
        and type(pr.get("draft")) is bool
        and base.get("ref") == policy.base_branch
        and (base.get("repo") or {}).get("full_name") == policy.repository
        and valid_sha(base.get("sha"))
        and valid_sha(head.get("sha"))
        and isinstance(user.get("login"), str) and bool(user["login"])
    )


def review_plan(prs: Any, policy: Policy) -> dict[str, tuple[str, str]]:
    """One verdict per SHA, the worst open-pull verdict winning for a shared head."""
    if not isinstance(prs, list):
        raise Unusable("invalid_pull_snapshot")
    result: dict[str, tuple[str, str]] = {}
    for pr in prs:
        if not pull_scope(pr, policy):
            raise Unusable("invalid_pull_scope")
        head = pr["head"]["sha"]
        state, reason = review_verdict(pr.get("reviews"), head, pr["user"]["login"], policy)
        if pr["draft"]:
            state, reason = "pending", "pull_request_is_draft"
        if head not in result or SEVERITY[state] > SEVERITY[result[head][0]]:
            result[head] = (state, reason)
    return result


def same_refs(before: Any, after: Any, policy: Policy) -> bool:
    """Snapshot equality, not merge readiness: an unchanged draft is valid."""
    return (pull_scope(before, policy) and pull_scope(after, policy)
            and before["draft"] == after["draft"]
            and before["number"] == after["number"]
            and before["head"]["sha"] == after["head"]["sha"]
            and before["base"]["sha"] == after["base"]["sha"])


def checks_green(runs: Any, statuses: Any, head: str, policy: Policy) -> tuple[bool, str]:
    """Require positive exact-head CI and the trusted gate status; never absence."""
    if not isinstance(runs, list) or not isinstance(statuses, list) or not valid_sha(head):
        raise Unusable("invalid_check_snapshot")
    latest_runs: dict[tuple[str, int], dict[str, Any]] = {}
    for run in runs:
        if (not isinstance(run, dict) or type(run.get("id")) is not int
                or not isinstance(run.get("name"), str) or not run["name"]
                or not isinstance(run.get("app"), dict) or type(run["app"].get("id")) is not int):
            raise Unusable("invalid_check_run")
        if run.get("head_sha") != head:
            return False, "check_head_mismatch"
        key = (run["name"], run["app"]["id"])
        if key not in latest_runs or run["id"] > latest_runs[key]["id"]:
            latest_runs[key] = run
    for name in policy.required_checks:
        run = latest_runs.get((name, ACTIONS_APP_ID))
        if not run or run.get("status") != "completed" or run.get("conclusion") != "success":
            return False, "required_check_not_success"
    if any(r.get("status") != "completed" or r.get("conclusion") not in {"success", "neutral", "skipped"}
           for r in latest_runs.values()):
        return False, "additional_check_not_green"
    latest_statuses: dict[str, dict[str, Any]] = {}
    for status in statuses:
        if (not isinstance(status, dict) or type(status.get("id")) is not int
                or not isinstance(status.get("context"), str) or not status["context"]):
            raise Unusable("invalid_commit_status")
        key = status["context"]
        if key not in latest_statuses or status["id"] > latest_statuses[key]["id"]:
            latest_statuses[key] = status
    gate = latest_statuses.get(policy.gate_context)
    if not gate or gate.get("state") != "success":
        return False, "review_status_not_success"
    creator = gate.get("creator")
    if (not isinstance(creator, dict)
            or (creator.get("login"), creator.get("type")) != ("github-actions[bot]", "Bot")):
        return False, "untrusted_review_status"
    if any(s.get("state") != "success" for s in latest_statuses.values()):
        return False, "additional_status_not_green"
    return True, "checks_green"


def changed_paths(comparison: Any, declared: Any, base: str) -> list[str]:
    """Only the immutable comparison of this complete, current-base diff.

    Both sides of a rename are returned: a rename can move a file out of a
    sensitive area, so comparing only the new name would wave it through.
    """
    if type(declared) is not int or declared < 1 or not isinstance(comparison, dict):
        raise Unusable("invalid_changed_file_count")
    origin, ancestor = comparison.get("base_commit"), comparison.get("merge_base_commit")
    if not isinstance(origin, dict) or origin.get("sha") != base:
        raise Unusable("comparison_base_mismatch")
    if not isinstance(ancestor, dict) or ancestor.get("sha") != base:
        raise Unusable("head_is_behind_base")
    if comparison.get("status") != "ahead":
        raise Unusable("comparison_not_ahead")
    files = comparison.get("files")
    if not isinstance(files, list) or len(files) != declared:
        raise Unusable("incomplete_changed_files")
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or not item.get("filename"):
            raise Unusable("invalid_changed_path")
        for key in ("filename", "previous_filename"):
            path = item.get(key)
            if path is None:
                continue
            if (not isinstance(path, str) or not path or path.startswith("/")
                    or "\\" in path or "\0" in path
                    or any(part in {".", "..", ""} for part in path.split("/"))):
                raise Unusable("invalid_changed_path")
            paths.append(path)
    return paths


def ordinary(path: str, policy: Policy) -> bool:
    """An allowlist, not a denylist: anything unmatched is human-only."""
    if path in policy.ordinary_paths:
        return True
    for pattern in policy.ordinary_paths:
        if pattern.endswith("/**") and path.startswith(pattern[:-2]):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _dependency_update_only(paths: list[str]) -> bool:
    return bool(paths) and all(PurePosixPath(p).name in DEPENDENCY_FILES for p in paths)


def merge_decision(pr: Any, reviews: Any, runs: Any, statuses: Any, comparison: Any,
                   policy: Policy) -> tuple[bool, str]:
    """A merge is allowed only when every condition holds on the same head."""
    try:
        if not pull_scope(pr, policy) or pr["draft"]:
            return False, "pull_not_ready"
        if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
            return False, "mergeability_not_clean"
        state, reason = review_verdict(reviews, pr["head"]["sha"], pr["user"]["login"], policy)
        if state != "success":
            return False, reason
        green, reason = checks_green(runs, statuses, pr["head"]["sha"], policy)
        if not green:
            return False, reason
        paths = changed_paths(comparison, pr.get("changed_files"), pr["base"]["sha"])
        if policy.dependency_update_exemption and _dependency_update_only(paths):
            return True, "eligible_dependency_update"
        if any(not ordinary(path, policy) for path in paths):
            return False, "sensitive_paths"
    except Unusable as exc:
        return False, str(exc)
    return True, "eligible"
