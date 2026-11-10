"""Policy decisions for merge-guard: a parameterised port of ci-burst's gate.

Ported from mikomiel/ci-burst `scripts/pr_policy.py` at main 31a0fefe. The
logic is deliberately kept: same snapshot checks, same review precedence, same
allowlist model. What changed is only where the rules live — a consumer's
`.github/merge-guard.toml` instead of module constants — plus the fixes the
shared engine needs (reviewer id, segment-aware globbing, root-level dependency
manifests).

Native `reviewDecision` is never an input: it is derived by GitHub and drops
approvals on a new commit in ways this gate cannot verify from a snapshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

ACTIONS_APP_ID = 15368
DECISIVE = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
REVIEW_STATES = DECISIVE | {"COMMENTED", "PENDING"}
DEPENDENCY_MANIFESTS = frozenset({
    "requirements.txt", "requirements-dev.txt", "uv.lock", "poetry.lock",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum", "Cargo.lock",
})
SEVERITY = {"success": 0, "pending": 1, "failure": 2}


class Unusable(ValueError):
    """A complete policy decision cannot be established from these inputs."""


@dataclass(frozen=True)
class Policy:
    repository: str
    base_branch: str
    reviewer_id: int
    reviewer_login: str
    reviewer_type: str
    required_checks: tuple[str, ...]
    gate_context: str
    ordinary_paths: tuple[str, ...]
    dependency_update_exemption: bool
    review_workflow: str
    merge_workflow: str


def load_policy(data: Any) -> Policy:
    try:
        repo = data["repository"]
        review = data["review"]
        paths = data.get("paths", {})
        repository = repo["name"]
        base_branch = repo["base_branch"]
        reviewer_id = review["reviewer_id"]
        reviewer_login = review["reviewer_login"]
        reviewer_type = review.get("reviewer_type", "Bot")
        checks = review["required_checks"]
        gate = review.get("gate_context", "review")
        ordinary = paths.get("ordinary", [])
        exemption = bool(paths.get("dependency_update_exemption", False))
        review_workflow = review.get("review_workflow", "merge-guard-review.yml")
        merge_workflow = review.get("merge_workflow", "merge-guard-merge.yml")
    except (KeyError, TypeError, AttributeError) as exc:
        raise Unusable(f"policy_missing_field: {exc}") from exc
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise Unusable("policy_repository_invalid")
    if not all(isinstance(x, str) and x for x in (base_branch, reviewer_login, reviewer_type, gate,
                                                  review_workflow, merge_workflow)):
        raise Unusable("policy_identity_invalid")
    if type(reviewer_id) is not int or reviewer_id < 1:
        raise Unusable("policy_reviewer_id_invalid")
    if not isinstance(checks, (list, tuple)) or not checks or not all(
            isinstance(x, str) and x for x in checks):
        raise Unusable("policy_required_checks_invalid")
    if not isinstance(ordinary, (list, tuple)) or not all(
            isinstance(x, str) and bool(x) for x in ordinary):
        raise Unusable("policy_ordinary_paths_invalid")
    if not isinstance(data.get("paths", {}).get("dependency_update_exemption", False), bool):
        raise Unusable("policy_dependency_exemption_invalid")
    return Policy(repository, base_branch, reviewer_id, reviewer_login, reviewer_type,
                  tuple(checks), gate, tuple(ordinary), exemption,
                  review_workflow, merge_workflow)


def valid_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def is_configured_reviewer(user: dict[str, Any], policy: Policy) -> bool:
    return (user.get("id"), user.get("login"), user.get("type")) == (
        policy.reviewer_id, policy.reviewer_login, policy.reviewer_type)


def review_verdict(reviews: Any, head: str, author: str, maintainers: Any, policy: Policy,
                   carried: Any = frozenset()) -> tuple[str, str]:
    """Latest decisive state per verified identity; any change request blocks.

    ``maintainers`` holds numeric user ids the API verified as write/maintain/
    admin. Association labels alone never grant that authority, and a review by
    the pull request author never counts.
    """
    if not valid_sha(head) or not isinstance(reviews, list) or not author:
        raise Unusable("invalid_review_snapshot")
    if not isinstance(maintainers, (set, frozenset)):
        raise Unusable("invalid_maintainer_set")
    if not isinstance(carried, (set, frozenset)):
        raise Unusable("invalid_carried_set")
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
        trusted = is_configured_reviewer(user, policy) or (
            user.get("type") == "User" and user["id"] in maintainers)
        if not trusted or user.get("login") == author or state not in DECISIVE:
            continue
        if not valid_sha(item.get("commit_id")):
            raise Unusable("invalid_review_commit")
        previous = latest.get(user["id"])
        if previous is None or item["id"] > previous["id"]:
            latest[user["id"]] = item
    if any(x["state"] == "CHANGES_REQUESTED" for x in latest.values()):
        return "failure", "changes_requested"
    for item in latest.values():
        if item["state"] != "APPROVED":
            continue
        if item["commit_id"] == head:
            return "success", "approved_at_head"
        if item["commit_id"] in carried:
            return "success", f"approved at {item['commit_id'][:12]} (carried, unchanged diff)"
    return "pending", "current_head_approval_required"


def pull_scope(pr: Any, policy: Policy) -> bool:
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
    """One verdict per SHA, worst open-PR verdict winning for a shared head."""
    if not isinstance(prs, list):
        raise Unusable("invalid_pull_snapshot")
    result: dict[str, tuple[str, str]] = {}
    for pr in prs:
        if not pull_scope(pr, policy):
            raise Unusable("invalid_pull_scope")
        head = pr["head"]["sha"]
        state, reason = review_verdict(
            pr.get("reviews"), head, pr["user"]["login"], set(pr.get("maintainers", ())),
            policy, frozenset(pr.get("carried_heads", ())))
        if pr["draft"]:
            state, reason = "pending", "pull_request_is_draft"
        if head not in result or SEVERITY[state] > SEVERITY[result[head][0]]:
            result[head] = (state, reason)
    return result


def checks_green(runs: Any, statuses: Any, head: str, policy: Policy) -> tuple[bool, str]:
    """Positive exact-head CI plus the trusted gate status; absence never passes.

    The gate status must be published by the Actions app itself
    (`github-actions[bot]`), which is the identity a workflow using the
    repository token gets. Only the default-branch workflows hold
    `statuses: write`, and `trusted_context` pins the workflow ref.
    """
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
    """Only the immutable comparison of this complete, current-base diff."""
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


def _segment_match(pattern: str, path: str) -> bool:
    """Glob that never lets `*` cross a `/` boundary."""
    pattern_parts = pattern.split("/")
    path_parts = path.split("/")
    if len(pattern_parts) != len(path_parts):
        return False
    for want, got in zip(pattern_parts, path_parts):
        if want == "*" or want == got:
            continue
        if "*" in want:
            import fnmatch
            if fnmatch.fnmatchcase(got, want):
                continue
        return False
    return True


def ordinary(path: str, policy: Policy) -> bool:
    """An allowlist, not a denylist: anything unmatched is human-only."""
    for pattern in policy.ordinary_paths:
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if path == prefix or path.startswith(prefix + "/"):
                return True
        elif _segment_match(pattern, path):
            return True
    return False


def dependency_update_only(paths: list[str], policy: Policy) -> bool:
    """A root-manifest bump only, and only when the consumer opted in."""
    return (
        policy.dependency_update_exemption is True
        and bool(paths)
        and all("/" not in path and PurePosixPath(path).name in DEPENDENCY_MANIFESTS for path in paths)
    )


def merge_decision(pr: Any, reviews: Any, runs: Any, statuses: Any, comparison: Any,
                   maintainers: Any, policy: Policy,
                   carried: Any = frozenset()) -> tuple[bool, str]:
    try:
        if not pull_scope(pr, policy) or pr["draft"]:
            return False, "pull_not_ready"
        if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
            return False, "mergeability_not_clean"
        state, reason = review_verdict(reviews, pr["head"]["sha"], pr["user"]["login"],
                                       maintainers, policy, carried)
        if state != "success":
            return False, reason
        # A verified maintainer can approve, but unattended ordinary-area merges
        # still require the configured reviewer at this head.
        if review_verdict(reviews, pr["head"]["sha"], pr["user"]["login"], set(), policy,
                          carried)[0] != "success":
            return False, "configured_reviewer_required_for_automerge"
        green, reason = checks_green(runs, statuses, pr["head"]["sha"], policy)
        if not green:
            return False, reason
        paths = changed_paths(comparison, pr.get("changed_files"), pr["base"]["sha"])
        if dependency_update_only(paths, policy):
            return True, "eligible_dependency_update"
        if any(not ordinary(path, policy) for path in paths):
            return False, "sensitive_paths"
    except Unusable as exc:
        return False, str(exc)
    return True, "eligible"


def same_refs(before: Any, after: Any, policy: Policy) -> bool:
    return (pull_scope(before, policy) and pull_scope(after, policy)
            and before["draft"] == after["draft"]
            and before["number"] == after["number"]
            and before["head"]["sha"] == after["head"]["sha"]
            and before["base"]["sha"] == after["base"]["sha"])
