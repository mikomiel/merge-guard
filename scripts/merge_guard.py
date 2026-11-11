"""Run merge-guard for one repository: publish the review status, or merge.

Ported from mikomiel/ci-burst `scripts/github_pr_automation.py` at main
31a0fefe: the same trusted-context guard, listing/deep-collection split,
revocation ordering, unchanged-status dedup, merge postcondition and
one-merge-per-run, with the repository constants replaced by the consumer's
policy file.

Local execution is a dry run. Writes need `--apply` *and* a trusted workflow
context bound to the default branch, so a pull-request run cannot publish or
merge.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

try:  # imported as scripts.merge_guard by the tests
    from . import merge_guard_policy as mg
except ImportError:  # executed as a script by the composite action
    import merge_guard_policy as mg

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10
SAFE_LOGIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
MAINTAINER_PERMISSIONS = {"write", "maintain", "admin"}
BOT_CREATOR = ("github-actions[bot]", "Bot")


class ApiError(RuntimeError):
    """A GitHub operation is unavailable or its postcondition is unproven."""


class GitHub:
    def __init__(self, policy: mg.Policy, apply: bool = False) -> None:
        self.policy = policy
        self.apply = apply

    def request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        if method != "GET" and not self.apply:
            raise ApiError("write_disabled")
        command = ["gh", "api", "--hostname", "github.com", "--method", method,
                   f"repos/{self.policy.repository}/{path}"]
        if body is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(
                command, input=json.dumps(body) if body is not None else None,
                text=True, capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError("github_transport_unavailable") from exc
        if result.returncode != 0 or len(result.stdout.encode()) > MAX_RESPONSE_BYTES:
            raise ApiError("github_api_failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ApiError("github_response_unreadable") from exc

    def pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            chunk = self.request(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if not isinstance(chunk, list):
                raise ApiError("github_page_unreadable")
            items.extend(chunk)
            if len(chunk) < 100:
                return items
        raise ApiError("github_pagination_exhausted")

    def pulls(self) -> list[dict[str, Any]]:
        return self.pages(f"pulls?state=open&base={self.policy.base_branch}")

    def pull(self, number: int) -> dict[str, Any]:
        if type(number) is not int or number < 1:
            raise ApiError("pull_number_unusable")
        return self.request(f"pulls/{number}")

    def reviews(self, number: int) -> list[dict[str, Any]]:
        return self.pages(f"pulls/{number}/reviews")

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        if not mg.valid_sha(sha):
            raise ApiError("invalid_head")
        latest: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            data = self.request(
                f"commits/{sha}/check-runs?filter=latest&per_page=100&page={page}")
            if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
                raise ApiError("github_check_runs_unreadable")
            page_total = data.get("total_count")
            if type(page_total) is not int or page_total < 0:
                raise ApiError("github_check_runs_total_unusable")
            if total is None:
                total = page_total
            elif page_total != total:
                raise ApiError("github_check_runs_total_changed")
            latest.extend(data["check_runs"])
            if len(data["check_runs"]) < 100:
                break
        if total is None or len(latest) != total:
            raise ApiError("github_check_runs_incomplete")
        return latest

    def statuses(self, sha: str) -> list[dict[str, Any]]:
        return self.pages(f"commits/{sha}/statuses")

    def compare(self, base: str, head: str) -> dict[str, Any]:
        return self.request(f"compare/{base}...{head}")

    def commit_time(self, sha: str) -> str:
        """The committer date the commit itself carries, read from that commit.

        The date is part of the commit and cannot move, unlike a review's
        ``commit_id``, which the forge may reassign after the fact.
        """
        if not mg.valid_sha(sha):
            raise ApiError("invalid_head")
        data = self.request(f"commits/{sha}")
        if not isinstance(data, dict) or data.get("sha") != sha:
            raise ApiError("commit_time_unusable")
        commit = data.get("commit")
        committer = commit.get("committer") if isinstance(commit, dict) else None
        date = committer.get("date") if isinstance(committer, dict) else None
        if not isinstance(date, str) or not date:
            raise ApiError("commit_time_unusable")
        return date

    def branch_head(self, branch: str) -> str:
        return self.request(f"git/ref/heads/{branch}").get("object", {}).get("sha", "")

    def publish(self, sha: str, state: str, description: str) -> None:
        """Publish unless the newest gate status already says exactly this.

        Only this workflow's own bot identity counts as unchanged; a status
        written by anything else is replaced rather than trusted.
        """
        latest: dict[str, dict[str, Any]] = {}
        for status in self.statuses(sha):
            if (not isinstance(status, dict) or type(status.get("id")) is not int
                    or not isinstance(status.get("context"), str) or not status["context"]):
                raise ApiError("github_status_unusable")
            key = status["context"]
            if key not in latest or status["id"] > latest[key]["id"]:
                latest[key] = status
        current = latest.get(self.policy.gate_context)
        if current is not None:
            creator = current.get("creator") if isinstance(current.get("creator"), dict) else {}
            if (current.get("state") == state and current.get("description") == description[:140]
                    and (creator.get("login"), creator.get("type")) == BOT_CREATOR):
                return
        self.request(f"statuses/{sha}", "POST", {
            "state": state, "context": self.policy.gate_context, "description": description[:140],
        })

    def merge(self, number: int, sha: str) -> None:
        if not self.apply or type(number) is not int or not mg.valid_sha(sha):
            raise ApiError("merge_write_refused")
        result = self.request(f"pulls/{number}/merge", "PUT",
                              {"sha": sha, "merge_method": "squash"})
        if (not isinstance(result, dict) or result.get("merged") is not True
                or not mg.valid_sha(result.get("sha"))):
            raise ApiError("merge_not_confirmed")
        after = self.pull(number)
        if (after.get("merged") is not True or after.get("state") != "closed"
                or (after.get("head") or {}).get("sha") != sha
                or after.get("merge_commit_sha") != result["sha"]):
            raise ApiError("merge_postcondition_unproven")


def maintainer_ids(api: GitHub, reviews: Any, author: str) -> set[int]:
    """Numeric ids the API confirms have write, maintain or admin permission.

    Reviews are pre-filtered before the API call: human identity, not the
    author, a decisive state and a login usable as an API path segment. The
    permission payload must name the same user id, and each user is asked once.
    """
    if not isinstance(reviews, list):
        raise ApiError("reviews_unusable")
    ids: set[int] = set()
    seen: set[Any] = set()
    for item in reviews:
        if not isinstance(item, dict):
            raise ApiError("review_unusable")
        user = item.get("user")
        if not isinstance(user, dict):
            raise mg.Unusable("review_user_unusable")
        login = user.get("login")
        if (user.get("type") != "User" or login == author
                or item.get("state") not in mg.DECISIVE):
            continue
        if (not isinstance(login, str) or not login
                or any(char not in SAFE_LOGIN for char in login)):
            raise ApiError("maintainer_identity_unusable")
        if user.get("id") in seen:
            continue
        seen.add(user.get("id"))
        permission = api.request(f"collaborators/{login}/permission")
        if (not isinstance(permission, dict)
                or (permission.get("user") or {}).get("id") != user.get("id")):
            raise ApiError("maintainer_permission_unproven")
        if permission.get("permission") in MAINTAINER_PERMISSIONS:
            ids.add(user["id"])
    return ids


def _carried_review(reviews: list[Any], head: str, policy: mg.Policy) -> str | None:
    """The configured reviewer's latest decisive review, when it approved an older head."""
    latest: dict[str, Any] | None = None
    for item in reviews:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        if not isinstance(user, dict) or not mg.is_configured_reviewer(user, policy):
            continue
        if item.get("state") not in mg.DECISIVE or type(item.get("id")) is not int:
            continue
        if latest is None or item["id"] > latest["id"]:
            latest = item
    if latest is None or latest["state"] != "APPROVED":
        return None
    commit = latest.get("commit_id")
    if not mg.valid_sha(commit) or commit == head:
        return None
    return commit


def _patch_snapshot(comparison: Any) -> list[tuple[Any, Any, Any, Any]] | None:
    """Every file's own patch, or None when any patch is missing (binary, too large)."""
    if not isinstance(comparison, dict):
        return None
    items = comparison.get("files")
    if not isinstance(items, list):
        return None
    snapshot = []
    for item in items:
        if not isinstance(item, dict) or "patch" not in item:
            return None
        snapshot.append((item.get("filename"), item.get("previous_filename"),
                         item.get("status"), item.get("patch")))
    return sorted(snapshot, key=lambda row: (str(row[0]), str(row[1])))


def carried_heads(api: GitHub, pr: dict[str, Any], reviews: list[Any],
                  policy: mg.Policy) -> set[str]:
    """Heads the reviewer approved whose own change is identical to the current one.

    Both sides are the same comparison shape: the current base against the head,
    and the current base against the older head. That is the pull request's own
    change in each case, including renames and conflict-resolution merges.

    A policy that turns ``carry_unchanged_diff`` off gets no carry at all: an
    unchanged patch is not an approval of the commit that carries it, and that
    repository binds every approval to its own head.
    """
    if not policy.carry_unchanged_diff:
        return set()
    old = _carried_review(reviews, pr["head"]["sha"], policy)
    if old is None:
        return set()
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    current = _patch_snapshot(api.compare(base, head))
    previous = _patch_snapshot(api.compare(base, old))
    if current is None or previous is None or not current:
        return set()
    changed = pr.get("changed_files")
    # The compare endpoint returns at most 300 files without paging. A truncated
    # list would compare only the visible files, so a difference past the cap
    # could carry an approval it should not.
    if type(changed) is not int or changed < 1 or len(current) != changed:
        return set()
    if len(current) >= 300 or len(previous) >= 300:
        return set()
    return {old} if current == previous else set()


def collect_reviews(api: GitHub, known_heads: set[str], policy: mg.Policy) -> list[dict[str, Any]]:
    """List first (recording every head), then deep-read each open pull."""
    listing = api.pulls()
    if not isinstance(listing, list):
        raise ApiError("github_pull_listing_unusable")
    for entry in listing:
        head = (entry.get("head") or {}).get("sha") if isinstance(entry, dict) else None
        if not mg.valid_sha(head):
            raise ApiError("github_pull_listing_head_unusable")
        known_heads.add(head)
    snapshot: list[dict[str, Any]] = []
    for entry in listing:
        pr = api.pull(entry.get("number"))
        if not isinstance(pr, dict):
            raise ApiError("github_pull_unreadable")
        if pr.get("state") == "closed":
            continue
        if not mg.pull_scope(pr, policy):
            raise ApiError("github_pull_scope_unusable")
        pr["reviews"] = api.reviews(pr["number"])
        pr["head_committed_at"] = api.commit_time(pr["head"]["sha"])
        pr["maintainers"] = sorted(maintainer_ids(api, pr["reviews"], pr["user"]["login"]))
        pr["carried_heads"] = sorted(carried_heads(api, pr, pr["reviews"], policy))
        snapshot.append(pr)
    return snapshot


def run_review(api: GitHub, policy: mg.Policy, apply: bool, event_head: str | None) -> dict[str, Any]:
    known = {event_head} if mg.valid_sha(event_head) else set()
    try:
        snapshot = collect_reviews(api, known, policy)
        plan = mg.review_plan(snapshot, policy)
        for pr in snapshot:
            if not mg.same_refs(pr, api.pull(pr["number"]), policy):
                raise ApiError("review_snapshot_refs_changed")
    except (ApiError, mg.Unusable):
        if apply:
            for head in sorted(known):
                try:
                    api.publish(head, "pending", "review_snapshot_unavailable")
                except ApiError:
                    pass
        raise
    for head in known - plan.keys():
        plan[head] = ("pending", "no_open_pull_carries_head")
    if apply:
        # Every non-success verdict first. A failed revocation aborts before any
        # success is published, so a broken context cannot leave an old green.
        failed = False
        for head, (state, reason) in sorted(plan.items()):
            if state == "success":
                continue
            try:
                api.publish(head, state, reason)
            except ApiError:
                failed = True
        if failed:
            raise ApiError("review_publication_incomplete")
        for head, (state, reason) in sorted(plan.items()):
            if state == "success":
                api.publish(head, state, reason)
    return {"heads": {head: verdict for head, verdict in plan.items()}}


def run_merge(api: GitHub, policy: mg.Policy, apply: bool) -> list[dict[str, Any]]:
    snapshot = collect_reviews(api, set(), policy)
    plan = mg.review_plan(snapshot, policy)
    counts: dict[str, int] = {}
    for pr in snapshot:
        counts[pr["head"]["sha"]] = counts.get(pr["head"]["sha"], 0) + 1
    decisions: list[dict[str, Any]] = []
    for pr in snapshot:
        head, base, number = pr["head"]["sha"], pr["base"]["sha"], pr["number"]
        decision = {"number": number, "head": head, "eligible": False, "merged": False}
        decisions.append(decision)
        if counts[head] > 1:
            decision["reason"] = "shared_head"
            continue
        if plan[head][0] != "success":
            decision["reason"] = plan[head][1]
            continue
        # The list view carries no mergeable/changed_files: re-read the PR.
        current = api.pull(number)
        if not mg.same_refs(pr, current, policy) or api.branch_head(policy.base_branch) != base:
            decision["reason"] = "refs_changed"
            continue
        reviews = api.reviews(number)
        maintainers = maintainer_ids(api, reviews, current["user"]["login"])
        comparison = api.compare(current["base"]["sha"], head)
        carried = carried_heads(api, current, reviews, policy)
        eligible, reason = mg.merge_decision(
            current, reviews, api.check_runs(head), api.statuses(head), comparison,
            maintainers, policy, carried)
        decision.update(eligible=eligible, reason=reason)
        if eligible and apply:
            api.merge(number, head)
            decision["merged"] = True
        if eligible:
            # One admitted merge per run; the base moved, so the next run decides.
            break
    return decisions


def trusted_context(policy: mg.Policy, mode: str) -> bool:
    """Writes only from the pinned default-branch workflow file."""
    workflow = policy.review_workflow if mode == "review" else policy.merge_workflow
    events = ({"workflow_run", "schedule", "pull_request_target"} if mode == "review"
              else {"workflow_run", "schedule"})
    return (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == policy.repository
        and os.environ.get("GITHUB_EVENT_NAME") in events
        and os.environ.get("GITHUB_REF") == f"refs/heads/{policy.base_branch}"
        and os.environ.get("GITHUB_REF_PROTECTED") == "true"
        and os.environ.get("GITHUB_WORKFLOW_REF") ==
        f"{policy.repository}/.github/workflows/{workflow}@refs/heads/{policy.base_branch}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("review", "merge"))
    parser.add_argument("--policy", default=".github/merge-guard.toml")
    parser.add_argument("--apply", action="store_true", help="trusted default-branch workflow only")
    args = parser.parse_args(argv)
    try:
        data = tomllib.loads(Path(args.policy).read_text(encoding="utf-8"))
        policy = mg.load_policy(data)
    except (OSError, tomllib.TOMLDecodeError, mg.Unusable) as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return 2
    if args.apply and not trusted_context(policy, args.mode):
        print("write_refused_outside_trusted_workflow", file=sys.stderr)
        return 2
    api = GitHub(policy, apply=args.apply)
    try:
        if args.mode == "review":
            result = run_review(api, policy, args.apply, os.environ.get("EVENT_HEAD"))
        else:
            result = run_merge(api, policy, args.apply)
    except (ApiError, mg.Unusable) as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"mode": args.mode, "apply": args.apply, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
