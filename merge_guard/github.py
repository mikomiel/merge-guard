"""The GitHub adapter: the forge port over the GitHub REST API, through ``gh``.

The class moved here from ``engine.py`` unchanged, apart from its ``name``, the
check that the policy does not name another forge, and ``comment``. Its
requests, its refusal codes and its postconditions are the ones the engine had.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from . import policy as mg
from .forge import ApiError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10
BOT_CREATOR = ("github-actions[bot]", "Bot")


class GitHub:
    name = "github"

    def __init__(self, policy: mg.Policy, apply: bool = False) -> None:
        # A policy written before the forge key existed names no forge; one
        # that names another forge must never be read through this adapter.
        if policy.forge not in (None, "github"):
            raise mg.Unusable("policy_forge_conflict")
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
        except (ValueError, RecursionError) as exc:
            # ValueError covers a decode error and an integer longer than Python's
            # digit limit; RecursionError a body nested past the recursion limit.
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

    def comment(self, number: int, body: str) -> None:
        if (not self.apply or type(number) is not int or number < 1
                or not isinstance(body, str) or not body):
            raise ApiError("comment_write_refused")
        created = self.request(f"issues/{number}/comments", "POST", {"body": body})
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ApiError("comment_not_confirmed")
