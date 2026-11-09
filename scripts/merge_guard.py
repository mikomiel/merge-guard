"""Run merge-guard for one repository: publish the review status, or merge.

Adapted from ci-burst `scripts/github_pr_automation.py`: the same
trusted-context guard, snapshot re-check and one-merge-per-run shape, with the
repository, reviewer and rules taken from the consumer's policy file.

Local execution is a dry run. Writes need `--apply` *and* a trusted workflow
context bound to the default branch, so a fork branch cannot publish or merge.
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

import merge_guard_policy as mg

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TRUSTED_EVENTS = {"workflow_run", "workflow_dispatch", "schedule", "pull_request_target"}


class ApiError(RuntimeError):
    """A GitHub operation is unavailable or its postcondition is unproven."""


class GitHub:
    def __init__(self, repository: str, context: str, apply: bool = False) -> None:
        self.repository = repository
        self.context = context
        self.apply = apply

    def request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        if method != "GET" and not self.apply:
            raise ApiError("write_disabled")
        command = ["gh", "api", "--hostname", "github.com", "--method", method,
                   f"repos/{self.repository}/{path}"]
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
            # Never echo the provider response: it can carry credentials or private data.
            raise ApiError("github_api_failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ApiError("github_response_unreadable") from exc

    def pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in range(1, 11):
            chunk = self.request(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if not isinstance(chunk, list):
                raise ApiError("github_page_unreadable")
            items.extend(chunk)
            if len(chunk) < 100:
                return items
        raise ApiError("github_pagination_exhausted")

    def pulls(self) -> list[dict[str, Any]]:
        return self.pages("pulls?state=open")

    def pull(self, number: int) -> dict[str, Any]:
        return self.request(f"pulls/{number}")

    def reviews(self, number: int) -> list[dict[str, Any]]:
        return self.pages(f"pulls/{number}/reviews")

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        return self.request(f"commits/{sha}/check-runs?per_page=100").get("check_runs", [])

    def statuses(self, sha: str) -> list[dict[str, Any]]:
        return self.pages(f"commits/{sha}/statuses")

    def compare(self, base: str, head: str) -> dict[str, Any]:
        return self.request(f"compare/{base}...{head}")

    def branch_head(self, branch: str) -> str:
        return self.request(f"git/ref/heads/{branch}").get("object", {}).get("sha", "")

    def publish(self, sha: str, state: str, description: str) -> None:
        self.request(f"statuses/{sha}", "POST", {
            "state": state, "context": self.context, "description": description[:140],
        })

    def merge(self, number: int, sha: str) -> None:
        self.request(f"pulls/{number}/merge", "PUT", {"sha": sha, "merge_method": "squash"})


def collect(api: GitHub) -> list[dict[str, Any]]:
    pulls = api.pulls()
    for pr in pulls:
        if isinstance(pr, dict) and type(pr.get("number")) is int:
            pr["reviews"] = api.reviews(pr["number"])
    return pulls


def run_review(api: GitHub, policy: mg.Policy, apply: bool, event_head: str | None) -> dict[str, Any]:
    known = {event_head} if mg.valid_sha(event_head) else set()
    try:
        snapshot = collect(api)
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
        # Revocations first, and each failure isolated: a broken context must not
        # leave an unrelated withdrawn approval green.
        failed = False
        for state in ("failure", "pending", "success"):
            for head, (verdict, reason) in sorted(plan.items()):
                if verdict != state:
                    continue
                try:
                    api.publish(head, state, reason)
                except ApiError:
                    failed = True
        if failed:
            raise ApiError("review_publication_incomplete")
    return {"heads": {head: verdict for head, verdict in plan.items()}}


def run_merge(api: GitHub, policy: mg.Policy, apply: bool) -> list[dict[str, Any]]:
    snapshot = collect(api)
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
        comparison = api.compare(base, head)
        eligible, reason = mg.merge_decision(
            pr, pr["reviews"], api.check_runs(head), api.statuses(head), comparison, policy)
        if eligible:
            current = api.pull(number)
            if not mg.same_refs(pr, current, policy) or api.branch_head(policy.base_branch) != base:
                eligible, reason = False, "refs_changed"
            else:
                eligible, reason = mg.merge_decision(
                    current, api.reviews(number), api.check_runs(head),
                    api.statuses(head), comparison, policy)
        decision.update(eligible=eligible, reason=reason)
        if eligible:
            if apply:
                api.merge(number, head)
                decision["merged"] = True
            # One admitted merge per run: the base moved, so the next run decides.
            break
    return decisions


def trusted_context(policy: mg.Policy, mode: str) -> bool:
    """Writes only from a default-branch workflow, never from a pull-request run."""
    if mode == "review":
        events = TRUSTED_EVENTS
    else:
        events = {"workflow_run", "workflow_dispatch", "schedule"}
    return (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == policy.repository
        and os.environ.get("GITHUB_EVENT_NAME") in events
        and os.environ.get("GITHUB_REF") == f"refs/heads/{policy.base_branch}"
        and os.environ.get("GITHUB_REF_PROTECTED") == "true"
    )


def load_policy(path: Path) -> mg.Policy:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise mg.Unusable(f"policy_unreadable: {exc}") from exc
    return mg.load_policy(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("review", "merge"))
    parser.add_argument("--policy", default=".github/merge-guard.toml")
    parser.add_argument("--apply", action="store_true", help="trusted default-branch workflow only")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(Path(args.policy))
    except mg.Unusable as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return 2
    if args.apply and not trusted_context(policy, args.mode):
        print("write_refused_outside_trusted_workflow", file=sys.stderr)
        return 2
    api = GitHub(policy.repository, policy.gate_context, apply=args.apply)
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
