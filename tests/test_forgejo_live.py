"""The Forgejo adapter against a real Forgejo instance.

``.github/workflows/forgejo.yml`` starts an ephemeral Forgejo container and
runs this module; everywhere else it is skipped. The recorded cases in
``tests/fixtures/forge/`` pin the verdicts with synthetic responses. This
module checks the other half: that the adapter's requests, and its reading of
the responses, hold against the real API.

The module fills the instance with accounts, a repository and pull requests,
so it accepts only a Forgejo on this machine. It needs:

- ``MERGE_GUARD_FORGEJO_LIVE_URL``: the base URL, for example
  ``http://localhost:3000``.
- ``MERGE_GUARD_FORGEJO_LIVE_ADMIN`` and ``MERGE_GUARD_FORGEJO_LIVE_PASSWORD``:
  the login and the password of an administrator account.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from merge_guard import engine, forgejo
from merge_guard import policy as mg
from merge_guard.forge import ApiError

ROOT = Path(__file__).resolve().parents[1]
URL = "MERGE_GUARD_FORGEJO_LIVE_URL"
ADMIN = "MERGE_GUARD_FORGEJO_LIVE_ADMIN"
PASSWORD = "MERGE_GUARD_FORGEJO_LIVE_PASSWORD"
#: What the gate account's token needs: its own identity, the repository
#: (pull requests, reviews, statuses, the merge) and the issue comments.
SCOPES = ["read:user", "write:repository", "write:issue"]
#: Forgejo shows another account's permission only to an administrator, and the
#: gate reads it for each person whose review is decisive. The account
#: "departed" writes one status and is then deleted. The test that deletes it
#: also creates it, so the deletion changes no account that another test uses.
PERMISSIONS = {"merge-gate": "admin", "review-bot": "write", "carol": "write",
               "departed": "write"}
#: The accounts that the module fixture creates for every test.
SHARED_ROLES = ("merge-gate", "review-bot", "carol")
WAIT_SECONDS = 90
#: The verdict for an approved head that no Forgejo Actions run checked.
UNCHECKED = ("pending", "required_check_missing: checks")
#: A workflow whose job no runner takes: Forgejo creates its run and job when
#: the pull request opens, and the job waits.
WORKFLOW = """name: ci
on: [pull_request]
jobs:
  checks:
    runs-on: merge-guard-no-runner
    steps:
      - run: "true"
"""
#: How the context of the status that Forgejo Actions writes for that job ends:
#: "<workflow> / <job> (<event>)".
JOB_COPY = " / checks (pull_request)"


class Client:
    """The test's own setup client. It is not the adapter under test."""

    def __init__(self, base: str, login: str, password: str) -> None:
        self.root = base.rstrip("/") + "/api/v1"
        self.login = login
        self._basic = base64.b64encode(f"{login}:{password}".encode()).decode()

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.root + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Basic {self._basic}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            raise AssertionError(f"{method} {path} as {self.login}: {exc.code} {detail}") from None
        return json.loads(raw) if raw.strip() else None


def _account(admin: Client, base: str, repository: str, run: str,
             role: str) -> tuple[Client, int]:
    """A new account for ``role`` with its permission on the repository: client and id."""
    name, secret = f"{role}-{run}", secrets.token_hex(24)
    created = admin.call("POST", "/admin/users", {
        "username": name, "email": f"{name}@example.com", "password": secret,
        "must_change_password": False})
    admin.call("PUT", f"/repos/{repository}/collaborators/{name}",
               {"permission": PERMISSIONS[role]})
    return Client(base, name, secret), created["id"]


@dataclass
class Live:
    base: str
    admin: Client
    accounts: dict[str, tuple[Client, int]]
    repository: str
    token: str
    policy: mg.Policy
    #: The suffix of every name this module creates on the instance.
    run: str

    def account(self, role: str) -> Client:
        """A new account for ``role`` that only the calling test uses."""
        return _account(self.admin, self.base, self.repository, self.run, role)[0]

    def adapter(self, apply: bool = False) -> forgejo.Forgejo:
        return forgejo.Forgejo(self.policy, self.base, self.token, apply=apply)

    def client(self, role: str) -> Client:
        return self.accounts[role][0]

    def user_id(self, role: str) -> int:
        return self.accounts[role][1]

    def open_pull(self, branch: str, path: str, base: str = "main",
                  content: str | None = None) -> tuple[int, str, str]:
        """A pull request that adds one file on a new branch: number, head, base."""
        repo = f"/repos/{self.repository}"
        base_sha = self.admin.call("GET", f"{repo}/branches/{base}")["commit"]["id"]
        text = f"{path}\n" if content is None else content
        created = self.admin.call("POST", f"{repo}/contents/{path}", {
            "content": base64.b64encode(text.encode()).decode(),
            "message": f"docs: add {path}", "branch": base, "new_branch": branch})
        number = self.admin.call("POST", f"{repo}/pulls", {
            "title": f"docs: add {path}", "head": branch, "base": base})["number"]
        self.wait_until_mergeable(number)
        return number, created["commit"]["sha"], base_sha

    def wait_until_mergeable(self, number: int) -> None:
        """Wait until Forgejo reports the pull request mergeable.

        Forgejo checks mergeability in the background; a merge before that
        check ends is refused, and the verdicts read the result.
        """
        deadline = time.monotonic() + WAIT_SECONDS
        while (self.admin.call("GET", f"/repos/{self.repository}/pulls/{number}")
               .get("mergeable") is not True):
            assert time.monotonic() < deadline, "Forgejo did not finish the mergeability check"
            time.sleep(1)

    def review(self, role: str, number: int, head: str, event: str, body: str = "") -> None:
        self.client(role).call("POST", f"/repos/{self.repository}/pulls/{number}/reviews",
                               {"event": event, "body": body, "commit_id": head})


def _wait_for(read: Any, what: str) -> Any:
    """The first value of ``read()`` that is not empty, read until WAIT_SECONDS pass."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        value = read()
        if value:
            return value
        assert time.monotonic() < deadline, f"Forgejo did not show {what}"
        time.sleep(1)


def _wait_until_ready(base: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/api/v1/version", timeout=5):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


@pytest.fixture(scope="module")
def live() -> Live:
    base = os.environ.get(URL)
    if not base:
        pytest.skip(f"{URL} is not set; .github/workflows/forgejo.yml runs this module")
    assert urllib.parse.urlsplit(base).hostname in forgejo.LOOPBACK_HOSTS, (
        "this module fills the instance with test data, so it runs only against "
        "a Forgejo on this machine")
    login, password = os.environ.get(ADMIN), os.environ.get(PASSWORD)
    assert login and password, f"{URL} needs {ADMIN} and {PASSWORD}"
    _wait_until_ready(base)
    admin = Client(base, login, password)
    run = secrets.token_hex(4)  # fresh names, so a second run on one instance works
    repository = f"{login}/widgets-{run}"
    admin.call("POST", "/user/repos", {"name": f"widgets-{run}", "auto_init": True,
                                       "readme": "Default", "default_branch": "main"})
    accounts = {role: _account(admin, base, repository, run, role) for role in SHARED_ROLES}
    gate = accounts["merge-gate"][0]
    token = gate.call("POST", f"/users/{gate.login}/tokens",
                      {"name": f"merge-guard-{run}", "scopes": SCOPES})["sha1"]
    policy = mg.load_policy({
        "repository": {"name": repository, "base_branch": "main", "forge": "forgejo"},
        "review": {"reviewer_id": accounts["review-bot"][1],
                   "reviewer_login": accounts["review-bot"][0].login,
                   "required_checks": ["checks"]},
        "paths": {"ordinary": ["docs/**"]},
    })
    return Live(base, admin, accounts, repository, token, policy, run)


def _verdict(api: forgejo.Forgejo, policy: mg.Policy, head: str) -> tuple[str, str]:
    return tuple(engine.run_review(api, policy, False, None)["heads"][head])


def _policy_file(live: Live, path: Path) -> Path:
    path.write_text(
        "[repository]\n"
        f'name = "{live.repository}"\nbase_branch = "main"\nforge = "forgejo"\n'
        "[review]\n"
        f'reviewer_id = {live.user_id("review-bot")}\n'
        f'reviewer_login = "{live.client("review-bot").login}"\n'
        'required_checks = ["checks"]\n'
        "[paths]\n"
        'ordinary = ["docs/**"]\n', encoding="utf-8")
    return path


def _merge_when_checked(live: Live, writer: forgejo.Forgejo, number: int, head: str) -> None:
    """Merge ``head`` once Forgejo has checked the pull request again.

    A merge that Forgejo refuses still starts a new mergeability check of the
    pull requests into its base branch, in the background
    (services/pull/merge.go). Until that check ends, Forgejo answers a merge
    with 405 (routers/api/v1/repo/pull.go), and the check can start after a
    read that still shows the pull request mergeable. So the merge is retried
    while Forgejo refuses it, until WAIT_SECONDS pass.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        live.wait_until_mergeable(number)
        try:
            writer.merge(number, head)
            return
        except ApiError as exc:
            if str(exc) != "forgejo_api_failed" or time.monotonic() >= deadline:
                raise
        time.sleep(1)


def test_the_adapter_reads_writes_and_merges_on_a_live_forgejo(live, tmp_path):
    number, head, base = live.open_pull("guide", "docs/guide.md")
    live.review("review-bot", number, head, "APPROVED")
    api = live.adapter()

    # The reads, in the shapes the port defines.
    assert {"number": number, "head": {"sha": head}} in api.pulls()
    pull = api.pull(number)
    assert (pull["state"], pull["draft"], pull["merged"]) == ("open", False, False)
    assert (pull["mergeable"], pull["mergeable_state"]) == (True, "clean")
    assert (pull["head"]["sha"], pull["base"]["sha"], pull["base"]["ref"]) == (head, base, "main")
    assert pull["base"]["repo"]["full_name"] == live.repository
    assert pull["user"]["login"] == live.admin.login
    assert pull["changed_files"] == 1
    [review] = api.reviews(number)
    assert (review["state"], review["commit_id"]) == ("APPROVED", head)
    assert review["user"] == {"id": live.user_id("review-bot"),
                              "login": live.client("review-bot").login, "type": "Bot"}
    assert datetime.fromisoformat(review["submitted_at"]).tzinfo is not None
    assert datetime.fromisoformat(api.commit_time(head)).tzinfo is not None
    comparison = api.compare(base, head)
    assert comparison["status"] == "ahead"
    assert [commit["sha"] for commit in comparison["commits"]] == [head]
    assert comparison["merge_base_commit"] == comparison["base_commit"] == {"sha": base}
    assert comparison["files"] == [{"filename": "docs/guide.md", "status": "added"}]
    assert api.branch_head("main") == base
    permission = api.request(f"collaborators/{live.client('carol').login}/permission")
    assert (permission["permission"], permission["user"]["id"]) == ("write", live.user_id("carol"))
    # The reason the gate account is a repository administrator.
    with pytest.raises(AssertionError, match=": 403 "):
        live.client("carol").call(
            "GET", f"/repos/{live.repository}/collaborators/{live.client('review-bot').login}"
            "/permission")

    # No Forgejo Actions run checked the head, so the approval waits for it.
    assert _verdict(api, live.policy, head) == UNCHECKED
    # A status that an account writes through the API is not a check run, even
    # under the name of a required check.
    live.client("carol").call("POST", f"/repos/{live.repository}/statuses/{head}", {
        "state": "success", "context": "checks", "description": "written through the API"})
    assert api.check_runs(head) == []
    assert _verdict(api, live.policy, head) == UNCHECKED
    [decision] = [item for item in engine.run_merge(api, live.policy, False)
                  if item["number"] == number]
    assert decision == {"number": number, "head": head, "eligible": False, "merged": False,
                        "reason": "required_check_run_missing"}

    # The command line reaches the same verdict from the environment.
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "merge_guard.py"), "status",
         "--policy", str(_policy_file(live, tmp_path / "merge-guard.toml"))],
        env={**os.environ, "FORGEJO_URL": live.base, "FORGEJO_TOKEN": live.token},
        capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"]["heads"][head] == list(UNCHECKED)

    # The gate status: written once, by the token's account, and then not again.
    writer = live.adapter(apply=True)
    engine.run_review(writer, live.policy, True, None)
    engine.run_review(writer, live.policy, True, None)
    gate = [status for status in api.statuses(head) if status["context"] == "review"]
    assert [(status["state"], status["description"], status["creator"]) for status in gate] == [
        (*UNCHECKED, {"login": mg.GATE_PUBLISHER[0], "type": mg.GATE_PUBLISHER[1]})]
    checks = [status["creator"] for status in api.statuses(head) if status["context"] == "checks"]
    assert checks == [{"login": live.client("carol").login, "type": "User"}]

    writer.comment(number, "merge-guard live test: a parked pull request")
    comments = live.admin.call("GET", f"/repos/{live.repository}/issues/{number}/comments")
    assert "merge-guard live test: a parked pull request" in [item["body"] for item in comments]

    # The expected-head merge: a commit that is not the head is refused, and
    # the pull request stays open; the head itself merges.
    with pytest.raises(ApiError, match="forgejo_api_failed"):
        writer.merge(number, base)
    assert api.pull(number)["state"] == "open"
    _merge_when_checked(live, writer, number, head)
    merged = api.pull(number)
    assert (merged["merged"], merged["state"], merged["head"]["sha"]) == (True, "closed", head)
    assert api.branch_head("main") != base


def test_a_maintainer_change_request_blocks_on_a_live_forgejo(live):
    number, head, _ = live.open_pull("other", "docs/other.md")
    live.review("review-bot", number, head, "APPROVED")
    live.review("carol", number, head, "REQUEST_CHANGES", "Split this change, please.")
    assert _verdict(live.adapter(), live.policy, head) == ("failure", "changes_requested")


def test_the_pull_listing_holds_only_the_base_branch(live):
    live.admin.call("POST", f"/repos/{live.repository}/branches",
                    {"new_branch_name": "release", "old_ref_name": "main"})
    number, head, _ = live.open_pull("backport", "docs/backport.md", base="release")
    listed = live.adapter().pulls()
    assert number not in [entry["number"] for entry in listed]
    assert head not in [entry["head"]["sha"] for entry in listed]


def _decision(api: forgejo.Forgejo, policy: mg.Policy, number: int) -> dict[str, Any]:
    [decision] = [item for item in engine.run_merge(api, policy, False)
                  if item["number"] == number]
    return decision


def test_a_status_whose_account_was_deleted_is_not_ci_evidence_on_a_live_forgejo(live):
    number, head, _ = live.open_pull("departed", "docs/departed.md")
    live.review("review-bot", number, head, "APPROVED")
    departed = live.account("departed")
    departed.call("POST", f"/repos/{live.repository}/statuses/{head}", {
        "state": "success", "context": "checks", "description": "written before a deletion"})
    live.admin.call("DELETE", f"/admin/users/{departed.login}")
    # Forgejo keeps the status and shows no creator for it, as it does for the
    # statuses that Forgejo Actions writes.
    written = live.admin.call("GET", f"/repos/{live.repository}/statuses/{head}")
    assert [(item["context"], item["creator"]) for item in written] == [("checks", None)]
    api = live.adapter()
    assert api.check_runs(head) == []
    assert _verdict(api, live.policy, head) == UNCHECKED
    assert _decision(api, live.policy, number)["reason"] == "required_check_run_missing"


def test_an_actions_job_is_the_ci_evidence_on_a_live_forgejo(live):
    number, head, _ = live.open_pull("workflow", ".forgejo/workflows/ci.yml", content=WORKFLOW)
    live.review("review-bot", number, head, "APPROVED")
    repo = f"/repos/{live.repository}"
    _wait_for(lambda: live.admin.call("GET", f"{repo}/actions/runs?head_sha={head}")
              ["workflow_runs"], "the Actions run of the pull request")
    copies = _wait_for(lambda: [item for item in live.admin.call("GET", f"{repo}/statuses/{head}")
                                if item["context"].endswith(JOB_COPY)], "the status of the job")
    assert {item["creator"] is None for item in copies} == {True}
    api = live.adapter()
    [check] = api.check_runs(head)
    assert (check["name"], check["head_sha"], check["app"]) == (
        "checks", head, {"id": mg.ACTIONS_APP_ID})
    # No runner takes the job, so it has not passed.
    assert (check["status"], check["conclusion"]) != ("completed", "success")
    # The status that Forgejo Actions wrote is the copy of the job, not a status.
    assert not [item for item in api.statuses(head) if item["context"].endswith(JOB_COPY)]
    # A run exists, so the review verdict does not name a missing check; the
    # merge waits for the job.
    assert _verdict(api, live.policy, head) == ("success", "approved_at_head")
    assert _decision(api, live.policy, number)["reason"] == "required_check_not_success"
