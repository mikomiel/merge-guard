"""Run merge-guard for one repository: publish the review status, or merge.

Ported from mikomiel/ci-burst `scripts/github_pr_automation.py` at main
31a0fefe: the same trusted-context guard, snapshot re-check, revocation
ordering, one-merge-per-run and postcondition, with the repository constants
replaced by the consumer's policy file.

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
        return self.request(f"pulls/{number}")

    def reviews(self, number: int) -> list[dict[str, Any]]:
        return self.pages(f"pulls/{number}/reviews")

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        latest: list[dict[str, Any]] = []
        total = None
        for page in range(1, MAX_PAGES + 1):
            data = self.request(
                f"commits/{sha}/check-runs?filter=latest&per_page=100&page={page}")
            if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
                raise ApiError("github_check_runs_unreadable")
            total = data.get("total_count", total)
            latest.extend(data["check_runs"])
            if len(data["check_runs"]) < 100:
                break
        if total is not None and len(latest) != total:
            raise ApiError("github_check_runs_incomplete")
        return latest

    def statuses(self, sha: str) -> list[dict[str, Any]]:
        return self.pages(f"commits/{sha}/statuses")

    def compare(self, base: str, head: str) -> dict[str, Any]:
        return self.request(f"compare/{base}...{head}")

    def branch_head(self, branch: str) -> str:
        return self.request(f"git/ref/heads/{branch}").get("object", {}).get("sha", "")

    def permission(self, login: str) -> str:
        data = self.request(f"collaborators/{login}/permission")
        return data.get("permission", "") if isinstance(data, dict) else ""

    def publish(self, sha: str, state: str, description: str) -> None:
        """Publish unless the newest status already says exactly this."""
        for status in self.statuses(sha):
            if status.get("context") == self.policy.gate_context and status.get("id"):
                if status.get("state") == state and status.get("description") == description[:140]:
                    return
                break
        self.request(f"statuses/{sha}", "POST", {
            "state": state, "context": self.policy.gate_context, "description": description[:140],
        })

    def merge(self, number: int, sha: str) -> None:
        self.request(f"pulls/{number}/merge", "PUT", {"sha": sha, "merge_method": "squash"})
        merged = self.pull(number)
        if merged.get("merged") is not True:
            raise ApiError("merge_postcondition_failed")


def maintainer_ids(api: GitHub, reviews: list[Any], author: str, policy: mg.Policy) -> set[int]:
    """Numeric ids the API confirms have write, maintain or admin permission."""
    ids: set[int] = set()
    for item in reviews:
        user = item.get("user") if isinstance(item, dict) else None
        if not isinstance(user, dict) or type(user.get("id")) is not int:
            continue
        if mg.is_configured_reviewer(user, policy) or user.get("login") == author:
            continue
        if api.permission(str(user.get("login"))) in {"admin", "maintain", "write"}:
            ids.add(user["id"])
    return ids


def collect_reviews(api: GitHub, policy: mg.Policy) -> list[dict[str, Any]]:
    pulls = api.pulls()
    for pr in pulls:
        if not isinstance(pr, dict) or type(pr.get("number")) is not int:
            raise ApiError("github_pull_unreadable")
        pr["reviews"] = api.reviews(pr["number"])
        pr["maintainers"] = sorted(maintainer_ids(api, pr["reviews"], pr["user"]["login"], policy))
    return pulls


def run_review(api: GitHub, policy: mg.Policy, apply: bool, event_head: str | None) -> dict[str, Any]:
    known = {event_head} if mg.valid_sha(event_head) else set()
    try:
        snapshot = collect_reviews(api, policy)
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
    snapshot = collect_reviews(api, policy)
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
        maintainers = maintainer_ids(api, reviews, current["user"]["login"], policy)
        comparison = api.compare(current["base"]["sha"], head)
        eligible, reason = mg.merge_decision(
            current, reviews, api.check_runs(head), api.statuses(head), comparison, maintainers, policy)
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
    events = {"workflow_run", "schedule", "pull_request_target"} if mode == "review" else {
        "workflow_run", "schedule"}
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
