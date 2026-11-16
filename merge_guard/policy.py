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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
#: The forges a policy can name. There is no default: see :func:`require_forge`.
FORGES = ("github", "forgejo")
#: The creator the gate trusts for its own status, as (login, type). On GitHub
#: it is the identity that a workflow using the repository token writes as. A
#: Forgejo adapter reports a status written by its own account this way.
GATE_PUBLISHER = ("github-actions[bot]", "Bot")
# A review and the commit it names are timestamped by different clocks: the
# review's server timestamp by the forge, the commit's own committer date by
# whoever authored it. Clock skew between them must not reject a genuine
# approval, and 300 s allows limited clock skew and nothing more. It is a
# plausibility check, not a proof: a head timestamped inside the tolerance, or
# one whose date was written to look older, still reads as approved
# (issue #20).
REVIEW_ASSOCIATION_TOLERANCE_SECONDS = 300


class Unusable(ValueError):
    """A complete policy decision cannot be established from these inputs."""


def unknown_fields(data: Any) -> list[str]:
    """Fields the schema does not accept, as dotted paths.

    A missing or mistyped known section is left to :func:`load_policy`, which
    reports it as ``policy_missing_field``; this function only reports the
    fields nothing reads.
    """
    if not isinstance(data, dict):
        raise Unusable("policy_not_a_table")
    found = [key for key in data if key not in KNOWN_FIELDS]
    for section, allowed in KNOWN_FIELDS.items():
        value = data.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            continue
        found += [f"{section}.{key}" for key in value if key not in allowed]
    return found


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
    # Defaults on so an existing consumer keeps the behaviour it pinned. A
    # repository that must bind the approval to the commit itself turns it off
    # and then a new commit always needs a new approval.
    carry_unchanged_diff: bool
    # The forge the policy names, or None when the file names none. A file
    # written before the key existed still loads; whatever opens the forge then
    # has to name it (require_forge), because there is no default forge.
    forge: str | None = None


#: Every field the schema accepts. An unknown field is a refusal, not a warning:
#: a typo would otherwise silently change which rule runs. ``docs/cli.md`` owns
#: the prose and ``tests/test_contract.py`` pins these sets against it.
KNOWN_FIELDS: dict[str, frozenset[str]] = {
    "repository": frozenset({"name", "base_branch", "forge"}),
    "review": frozenset({
        "reviewer_id", "reviewer_login", "reviewer_type", "required_checks",
        "gate_context", "review_workflow", "merge_workflow", "carry_unchanged_diff",
    }),
    "paths": frozenset({"ordinary", "dependency_update_exemption"}),
}
#: Accepted today, removed when the clean-refresh rule replaces it.
DEPRECATED_FIELDS = frozenset({"review.carry_unchanged_diff"})


def load_policy(data: Any) -> Policy:
    unknown = unknown_fields(data)
    if unknown:
        raise Unusable("policy_unknown_field: " + ", ".join(sorted(unknown)))
    try:
        repo = data["repository"]
        review = data["review"]
        paths = data.get("paths", {})
        repository = repo["name"]
        base_branch = repo["base_branch"]
        forge = repo.get("forge")
        reviewer_id = review["reviewer_id"]
        reviewer_login = review["reviewer_login"]
        reviewer_type = review.get("reviewer_type", "Bot")
        checks = review["required_checks"]
        gate = review.get("gate_context", "review")
        ordinary = paths.get("ordinary", [])
        exemption = bool(paths.get("dependency_update_exemption", False))
        review_workflow = review.get("review_workflow", "merge-guard-review.yml")
        merge_workflow = review.get("merge_workflow", "merge-guard-merge.yml")
        carry = review.get("carry_unchanged_diff", True)
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
    if type(carry) is not bool:
        raise Unusable("policy_carry_unchanged_diff_invalid")
    if forge is not None and forge not in FORGES:
        raise Unusable("policy_forge_invalid")
    return Policy(repository, base_branch, reviewer_id, reviewer_login, reviewer_type,
                  tuple(checks), gate, tuple(ordinary), exemption,
                  review_workflow, merge_workflow, carry, forge)


def require_forge(policy: Policy, named: str | None = None) -> Policy:
    """The policy with its forge named, or a refusal: there is no default forge.

    ``named`` is a forge named outside the file, for example by the composite
    action's run line. The two must agree when both are present, so a surface
    cannot silently retarget a policy written for the other forge.
    """
    if named is not None and named not in FORGES:
        raise Unusable("policy_forge_invalid")
    if policy.forge is None and named is None:
        raise Unusable("policy_forge_missing")
    if policy.forge is not None and named is not None and named != policy.forge:
        raise Unusable("policy_forge_conflict")
    return replace(policy, forge=policy.forge or named)


def valid_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def is_configured_reviewer(user: dict[str, Any], policy: Policy) -> bool:
    return (user.get("id"), user.get("login"), user.get("type")) == (
        policy.reviewer_id, policy.reviewer_login, policy.reviewer_type)


def _timestamp(value: Any, unusable: str) -> datetime:
    """An ISO 8601 instant with an explicit offset; a trailing ``Z`` is UTC.

    A timestamp without an offset cannot be ordered against the other side, so
    it counts as unusable rather than being guessed at.
    """
    if not isinstance(value, str) or not value:
        raise Unusable(unusable)
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise Unusable(unusable) from None
    if stamp.tzinfo is None:
        raise Unusable(unusable)
    return stamp


def review_verdict(reviews: Any, head: str, author: str, maintainers: Any, policy: Policy,
                   head_committed_at: str, carried: Any = frozenset()) -> tuple[str, str]:
    """Latest decisive state per verified identity; any change request blocks.

    ``maintainers`` holds numeric user ids the API verified as write/maintain/
    admin (or owner, on Forgejo). Association labels alone never grant that
    authority, and a review by the pull request author never counts.

    An approval of ``head`` counts only when the review could have seen it. The
    forge may reassign a review's ``commit_id`` to a commit that did not exist
    at submission time, and ``original_commit_id`` is null in observed data, so
    ``head_committed_at`` is required rather than optional: a caller that cannot
    supply it cannot get ``approved_at_head``.
    """
    if not valid_sha(head) or not isinstance(reviews, list) or not author:
        raise Unusable("invalid_review_snapshot")
    if not isinstance(maintainers, (set, frozenset)):
        raise Unusable("invalid_maintainer_set")
    if not isinstance(carried, (set, frozenset)):
        raise Unusable("invalid_carried_set")
    head_committed = _timestamp(head_committed_at, "invalid_head_commit_time")
    if not policy.carry_unchanged_diff:
        # The runner never supplies carry evidence under this policy. Zeroing it
        # here as well means a direct caller of this function cannot reintroduce
        # it, so the guarantee does not depend on one call site remembering.
        carried = frozenset()
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
            submitted = _timestamp(item.get("submitted_at"), "invalid_review_submitted_at")
            if submitted + timedelta(seconds=REVIEW_ASSOCIATION_TOLERANCE_SECONDS) < head_committed:
                # The commit did not exist when this approval was written, so
                # the approval says nothing about it. Let the carried branch and
                # the final refusal decide instead of inventing a success.
                continue
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
    """One verdict per SHA, worst open-PR verdict winning for a shared head.

    Each snapshot entry must carry ``head_committed_at``: the runner reads it
    from the head commit itself, and an entry without it is unusable rather
    than approved.
    """
    if not isinstance(prs, list):
        raise Unusable("invalid_pull_snapshot")
    result: dict[str, tuple[str, str]] = {}
    for pr in prs:
        if not pull_scope(pr, policy):
            raise Unusable("invalid_pull_scope")
        head = pr["head"]["sha"]
        state, reason = review_verdict(
            pr.get("reviews"), head, pr["user"]["login"], set(pr.get("maintainers", ())),
            policy, pr.get("head_committed_at"), frozenset(pr.get("carried_heads", ())))
        if pr["draft"]:
            state, reason = "pending", "pull_request_is_draft"
        if head not in result or SEVERITY[state] > SEVERITY[result[head][0]]:
            result[head] = (state, reason)
    return result


def latest_check_runs(runs: Any, head: str) -> dict[tuple[str, int], dict[str, Any]]:
    """The newest check run per (name, app) for this exact head.

    A snapshot about another commit is unusable, not evidence of absence: the
    missing-required-run state (#14) must never be inferred from a response that
    describes a different head.
    """
    if not isinstance(runs, list) or not valid_sha(head):
        raise Unusable("invalid_check_snapshot")
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for run in runs:
        if (not isinstance(run, dict) or type(run.get("id")) is not int
                or not isinstance(run.get("name"), str) or not run["name"]
                or not isinstance(run.get("app"), dict) or type(run["app"].get("id")) is not int):
            raise Unusable("invalid_check_run")
        if run.get("head_sha") != head:
            raise Unusable("check_head_mismatch")
        key = (run["name"], run["app"]["id"])
        if key not in latest or run["id"] > latest[key]["id"]:
            latest[key] = run
    return latest


def missing_required_check(runs: Any, head: str, policy: Policy) -> str | None:
    """The first required check with no run for this head, or None.

    Absence is a different state from a failed or pending run: the remedy is
    recovery (dispatch the workflow for the branch), not a code change (#14).
    """
    latest = latest_check_runs(runs, head)
    for name in policy.required_checks:
        if (name, ACTIONS_APP_ID) not in latest:
            return name
    return None


def checks_green(runs: Any, statuses: Any, head: str, policy: Policy) -> tuple[bool, str]:
    """Positive exact-head CI plus the trusted gate status; absence never passes.

    A required context with no run for this head is the named
    ``required_check_run_missing`` refusal; a run that exists but is not
    completed successfully stays ``required_check_not_success``. The gate status
    must be published by the Actions app itself (`github-actions[bot]`), which is
    the identity a workflow using the repository token gets. Only the
    default-branch workflows hold `statuses: write`, and `trusted_context` pins
    the workflow ref.
    """
    if not isinstance(statuses, list):
        raise Unusable("invalid_check_snapshot")
    latest_runs = latest_check_runs(runs, head)
    for name in policy.required_checks:
        run = latest_runs.get((name, ACTIONS_APP_ID))
        if run is None:
            return False, "required_check_run_missing"
        if run.get("status") != "completed" or run.get("conclusion") != "success":
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
            or (creator.get("login"), creator.get("type")) != GATE_PUBLISHER):
        return False, "untrusted_review_status"
    if any(s.get("state") != "success" for s in latest_statuses.values()):
        return False, "additional_status_not_green"
    return True, "checks_green"


#: The changed-file statuses that GitHub and Forgejo document, together.
CHANGED_FILE_STATUSES = frozenset({"added", "removed", "deleted", "modified", "changed",
                                   "renamed", "copied", "unchanged"})


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
        # A file whose status is unknown could be a rename that names no old
        # path, so only a status that a forge documents is read.
        status = item.get("status")
        if not isinstance(status, str) or status not in CHANGED_FILE_STATUSES:
            raise Unusable("invalid_changed_path")
        # A rename changes its old path as well, so a rename that does not name
        # that path would be judged on its new path alone.
        if status == "renamed" and item.get("previous_filename") is None:
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


def _comparison_head_commit_time(comparison: Any, head: str) -> str:
    """The head's own committer date, proven by the comparison's last commit.

    A compare response is ordered base..head, so its last entry is the head only
    when that entry says so. Anything else is a refusal, never a guess: a
    timestamp taken from the wrong commit would support a head nobody reviewed.
    """
    commits = comparison.get("commits") if isinstance(comparison, dict) else None
    if not isinstance(commits, list) or not commits:
        raise Unusable("invalid_comparison_commits")
    last = commits[-1]
    if not isinstance(last, dict) or last.get("sha") != head:
        raise Unusable("comparison_head_mismatch")
    commit = last.get("commit")
    committer = commit.get("committer") if isinstance(commit, dict) else None
    date = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(date, str) or not date:
        raise Unusable("invalid_head_commit_time")
    return date


def merge_decision(pr: Any, reviews: Any, runs: Any, statuses: Any, comparison: Any,
                   maintainers: Any, policy: Policy,
                   carried: Any = frozenset()) -> tuple[bool, str]:
    try:
        if not pull_scope(pr, policy) or pr["draft"]:
            return False, "pull_not_ready"
        if pr.get("mergeable") is not True:
            return False, "mergeability_not_clean"
        if pr.get("mergeable_state") == "blocked":
            # The motivating case for #14 — a required check with no run —
            # normally reads "blocked" here. Name that specific reason first;
            # a blocked head never becomes eligible.
            green, reason = checks_green(runs, statuses, pr["head"]["sha"], policy)
            if not green:
                return False, reason
            return False, "mergeability_not_clean"
        if pr.get("mergeable_state") != "clean":
            return False, "mergeability_not_clean"
        head_committed_at = _comparison_head_commit_time(comparison, pr["head"]["sha"])
        state, reason = review_verdict(reviews, pr["head"]["sha"], pr["user"]["login"],
                                       maintainers, policy, head_committed_at, carried)
        if state != "success":
            return False, reason
        # A verified maintainer can approve, but unattended ordinary-area merges
        # still require the configured reviewer at this head.
        if review_verdict(reviews, pr["head"]["sha"], pr["user"]["login"], set(), policy,
                          head_committed_at, carried)[0] != "success":
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
