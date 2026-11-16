"""The Forgejo adapter: the forge port over the Forgejo REST API (``/api/v1``).

Standard library only (``urllib.request``). Every response is untrusted input:
its size is bounded, a body that is not JSON is refused, a redirect is refused
so the token never follows one, and a list is read only when it holds as many
items as its count names and no item twice.

Forgejo has no check runs and no actor type, so the adapter maps what Forgejo
does return onto the port's records (``forge.py``):

- CI evidence is the jobs of the Forgejo Actions runs for exactly the head,
  whose state Forgejo Actions sets. A commit status is never CI evidence:
  Forgejo shows no ``creator`` both for the Actions service and for an account
  that was deleted.
- The statuses that Forgejo Actions copies from its jobs are left out of the
  statuses; every other status stays, so a red one still blocks.
- The gate's own status counts as ``policy.GATE_PUBLISHER`` only when the
  account this adapter authenticates as wrote it.
- A review's actor type is the policy's ``reviewer_type`` for the configured
  reviewer (id and login both match) and ``User`` for every other account.
- A review request (``REQUEST_REVIEW``) is not a review and is dropped; a
  dismissed review becomes ``DISMISSED``.
- ``mergeable_state`` is ``clean`` or ``dirty`` from ``mergeable``: Forgejo
  reports conflicts only, and its own branch protection is enforced again by
  the expected-head merge.
- A comparison takes its commits from ``compare``, proves that the head
  contains the base by comparing the other way, and takes the changed files
  from the one open pull request whose head is that commit, only when each
  file names that commit. Forgejo returns no per-file patch, so an
  unchanged-diff approval never carries.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import policy as mg
from .forge import ApiError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10
TIMEOUT_SECONDS = 30
STATUS_STATES = frozenset({"pending", "success", "error", "failure", "warning", "skipped"})
#: The states of a Forgejo Actions job (models/actions/status.go). A job in one
#: of these four states has finished, with this check-run conclusion.
JOB_CONCLUSIONS = {"success": "success", "skipped": "skipped", "failure": "failure",
                   "cancelled": "cancelled"}
#: A job in one of the other states has not finished yet.
JOB_STATES = frozenset(JOB_CONCLUSIONS) | {"unknown", "waiting", "running", "blocked"}
#: The conclusion of a check whose jobs have all finished: the first of these
#: that one of its jobs has. A failure or a cancellation outweighs a pass, and
#: a pass outweighs a skip.
WORST_FIRST = ("failure", "cancelled", "success", "skipped")
#: The events for which Forgejo Actions writes a commit status for each job.
STATUS_EVENTS = frozenset({"push", "pull_request", "release"})
#: Forgejo review states in the port's vocabulary. An unknown state is passed
#: through, and the engine refuses it as ``unknown_review_state``.
REVIEW_STATES = {"APPROVED": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED",
                 "COMMENT": "COMMENTED", "PENDING": "PENDING"}
#: A plain-HTTP URL sends the token in clear text, so it is accepted only for a
#: forge on this machine, and its requests never go through a proxy.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: following one would resend the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _at(data: Any, *keys: str) -> Any:
    """The value at a path of object keys, or None when any step is missing."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _count(value: Any) -> int | None:
    """The count a header names: at most 18 ASCII digits, not U+00B2 or U+0663.

    The length bound keeps ``int`` below Python's limit on digits in a string.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        return None
    return int(value)


def _status_event(run: dict[str, Any]) -> str | None:
    """The event that Forgejo Actions names in the status it writes for a job of ``run``.

    Forgejo writes the context ``<workflow> / <job> (<event>)`` for a push, a
    pull request or a release (services/actions/commit_status.go), and no
    status for another event.
    """
    event = run.get("event")
    if event == "pull_request" and run.get("trigger_event") == "pull_request_target":
        return "pull_request_target"
    return event if isinstance(event, str) and event in STATUS_EVENTS else None


def _ref_of(url: Any) -> str | None:
    """The commit a ``contents_url`` names: its whole query is ``ref=<commit>``."""
    if not isinstance(url, str):
        return None
    try:
        query = urllib.parse.urlsplit(url).query
    except ValueError:
        return None
    name, separator, value = query.partition("=")
    return value if name == "ref" and separator and mg.valid_sha(value) else None


def _refuse_repeats(items: list[Any], identity: str) -> None:
    """Refuse a list in which two items name the same ``identity``."""
    seen: set[int | str] = set()
    for item in items:
        value = item.get(identity) if isinstance(item, dict) else None
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            if value in seen:
                raise ApiError("forgejo_page_item_repeated")
            seen.add(value)


#: An authority is a host name or a bracketed IPv6 address, then an optional
#: port. Current Python releases refuse "[::1]suffix:3000" in urlsplit; this check
#: does not depend on the release that runs it.
_AUTHORITY = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[^\[\]:/@%]+)(?::[0-9]+)?")


def api_root(base_url: Any) -> str:
    """``<base_url>/api/v1``, or a refusal for a URL the token must not reach.

    The checks read the string that urllib requests. ``urlsplit`` drops a tab, a
    line break and leading control characters and spaces, and it reads an empty
    user name, query or fragment as none; urllib keeps all of them. urllib also
    decodes a percent-encoded host before it connects, so the host and the port
    must not hold ``%``. A space, ``?``, ``#`` and every control character
    (Unicode category Cc: U+0000 to U+001F and U+007F to U+009F) are refused.
    """
    if not isinstance(base_url, str) or not base_url:
        raise mg.Unusable("forgejo_url_missing")
    if any(char in " ?#" or unicodedata.category(char) == "Cc" for char in base_url):
        raise mg.Unusable("forgejo_url_invalid")
    try:
        # A malformed authority, such as an IPv6 address that does not close or
        # a port that is not a number from 0 to 65535, raises here.
        parts = urllib.parse.urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise mg.Unusable("forgejo_url_invalid") from None
    secure = parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS)
    if (not secure or not host or port == 0 or "@" in parts.netloc or "%" in parts.netloc
            or not _AUTHORITY.fullmatch(parts.netloc)):
        raise mg.Unusable("forgejo_url_invalid")
    return base_url.rstrip("/") + "/api/v1"


class Forgejo:
    name = "forgejo"

    def __init__(self, policy: mg.Policy, base_url: str, token: str, apply: bool = False,
                 opener: Any = None) -> None:
        if policy.forge is None:
            raise mg.Unusable("policy_forge_missing")
        if policy.forge != "forgejo":
            raise mg.Unusable("policy_forge_conflict")
        if any(part in {".", ".."} for part in policy.repository.split("/")):
            raise mg.Unusable("policy_repository_invalid")
        root = api_root(base_url)
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise mg.Unusable("forgejo_token_missing")
        self.policy = policy
        self.apply = apply
        self.root = root
        self._token = token
        handlers: list[Any] = [_NoRedirect]
        if urllib.parse.urlsplit(root).scheme != "https":
            # The token travels in clear text, so it never goes to a proxy that
            # the environment names: no_proxy does not match "[::1]", for example.
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = opener or urllib.request.build_opener(*handlers)
        self._page_size: int | None = None
        self._identity: int | None = None

    @classmethod
    def from_environment(cls, policy: mg.Policy, apply: bool = False) -> Forgejo:
        """The adapter for ``FORGEJO_URL`` with the token in ``FORGEJO_TOKEN``."""
        return cls(policy, os.environ.get("FORGEJO_URL", ""),
                   os.environ.get("FORGEJO_TOKEN", ""), apply)

    # Transport ---------------------------------------------------------------

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.root}/{path}"
        return f"{url}?{urllib.parse.urlencode(query)}" if query else url

    def _repo(self, path: str, query: dict[str, Any] | None = None) -> str:
        return self._url(f"repos/{self.policy.repository}/{path}", query)

    def _send(self, method: str, url: str, body: Any = None) -> tuple[dict[str, str], Any]:
        """One exchange: the response headers (lower-case names) and its JSON body."""
        if method != "GET" and not self.apply:
            raise ApiError("write_disabled")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"token {self._token}")
        request.add_header("User-Agent", "merge-guard")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                status = response.status
                headers = {name.lower(): value for name, value in response.headers.items()}
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ApiError("forgejo_api_failed") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise ApiError("forgejo_transport_unavailable") from exc
        if not 200 <= status < 300 or len(raw) > MAX_RESPONSE_BYTES:
            raise ApiError("forgejo_api_failed")
        if not raw.strip():
            return headers, None
        try:
            return headers, json.loads(raw)
        except (ValueError, RecursionError) as exc:
            # ValueError covers a decode error and an integer longer than Python's
            # digit limit; RecursionError a body nested past the recursion limit.
            raise ApiError("forgejo_response_unreadable") from exc

    def _get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        return self._send("GET", self._repo(path, query))[1]

    def _limit(self) -> int:
        """The page size: the server caps every page at its max_response_items."""
        if self._page_size is None:
            size = _at(self._send("GET", self._url("settings/api"))[1], "max_response_items")
            if type(size) is not int or not 1 <= size <= 1000:
                raise ApiError("forgejo_settings_unusable")
            self._page_size = size
        return self._page_size

    def _pages(self, path: str, query: dict[str, Any] | None = None,
               identity: str = "id", envelope: str | None = None,
               incomplete: str = "forgejo_pagination_incomplete") -> list[Any]:
        """Every item of a list: as many items as X-Total-Count names, none twice.

        A count does not prove a list complete on its own: a page can repeat an
        item of another page and skip one when the order is not unique. So an
        item whose ``identity`` field appears twice is a refusal. A list that
        holds another number of items than its count is refused with
        ``incomplete``.

        The list of Actions runs is an object: its items are under
        ``envelope`` and its count is ``total_count``, not X-Total-Count.
        """
        limit = self._limit()
        items: list[Any] = []
        total: int | None = None
        page = 1
        while True:
            if page > MAX_PAGES:
                raise ApiError("forgejo_pagination_exhausted")
            headers, body = self._send("GET", self._repo(path, {**(query or {}),
                                                                "limit": limit, "page": page}))
            if envelope is None:
                chunk, count = body, _count(headers.get("x-total-count"))
            else:
                chunk, count = _at(body, envelope), _at(body, "total_count")
                if type(count) is not int or count < 0:
                    count = None
            if not isinstance(chunk, list):
                raise ApiError("forgejo_page_unreadable")
            if count is None:
                raise ApiError("forgejo_page_total_unusable")
            if total is None:
                total = count
            elif count != total:
                raise ApiError("forgejo_page_total_changed")
            items.extend(chunk)
            if page * limit >= total:
                break
            page += 1
        if len(items) != total:
            raise ApiError(incomplete)
        _refuse_repeats(items, identity)
        return items

    def _own_id(self) -> int:
        """The account this adapter's token writes as."""
        if self._identity is None:
            identity = _at(self._send("GET", self._url("user"))[1], "id")
            if type(identity) is not int:
                raise ApiError("forgejo_identity_unusable")
            self._identity = identity
        return self._identity

    # Pull requests and reviews -------------------------------------------------

    def pulls(self) -> list[dict[str, Any]]:
        listing = self._pages("pulls", {"state": "open", "base": self.policy.base_branch})
        return [{"number": entry.get("number"), "head": {"sha": _at(entry, "head", "sha")}}
                if isinstance(entry, dict) else entry for entry in listing]

    def pull(self, number: int) -> dict[str, Any]:
        if type(number) is not int or number < 1:
            raise ApiError("pull_number_unusable")
        data = self._get(f"pulls/{number}")
        if not isinstance(data, dict):
            return data
        mergeable = data.get("mergeable")
        return {
            "number": data.get("number"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "mergeable": mergeable,
            "mergeable_state": ("clean" if mergeable else "dirty")
            if type(mergeable) is bool else None,
            "changed_files": data.get("changed_files"),
            "merged": data.get("merged"),
            "merge_commit_sha": data.get("merge_commit_sha"),
            "base": {"ref": _at(data, "base", "ref"), "sha": _at(data, "base", "sha"),
                     "repo": {"full_name": _at(data, "base", "repo", "full_name")}},
            "head": {"sha": _at(data, "head", "sha")},
            "user": {"login": _at(data, "user", "login")},
        }

    def _actor(self, user: Any) -> Any:
        if not isinstance(user, dict):
            return user
        identity = (user.get("id"), user.get("login"))
        configured = identity == (self.policy.reviewer_id, self.policy.reviewer_login)
        return {"id": identity[0], "login": identity[1],
                "type": self.policy.reviewer_type if configured else "User"}

    def reviews(self, number: int) -> list[dict[str, Any]]:
        """Every review, or a refusal when the list does not hold its count.

        Forgejo counts the pending reviews of other accounts but returns them
        only to a site administrator, and it answers a read of such a review as
        it answers a read of a review that does not exist. No response proves
        that a review the list leaves out is pending rather than a change
        request, so a list that does not hold its count is refused.
        """
        if type(number) is not int or number < 1:
            raise ApiError("pull_number_unusable")
        result: list[Any] = []
        for item in self._pages(f"pulls/{number}/reviews",
                                incomplete="forgejo_review_list_incomplete"):
            if not isinstance(item, dict):
                result.append(item)
                continue
            state = item.get("state")
            if state == "REQUEST_REVIEW":
                continue
            # A state that is not a string becomes None, which the engine refuses
            # as unknown_review_state; a list or an object cannot be a set member.
            mapped = REVIEW_STATES.get(state, state) if isinstance(state, str) else None
            if mapped in {"APPROVED", "CHANGES_REQUESTED"}:
                dismissed = item.get("dismissed")
                if type(dismissed) is not bool:
                    raise ApiError("forgejo_review_unusable")
                if dismissed:
                    mapped = "DISMISSED"
            result.append({"id": item.get("id"), "state": mapped,
                           "commit_id": item.get("commit_id"),
                           "submitted_at": item.get("submitted_at"),
                           "user": self._actor(item.get("user"))})
        return result

    def request(self, path: str) -> Any:
        """A repository-relative read; the engine reads collaborator permission."""
        return self._get(path)

    # Commits, statuses and comparisons ----------------------------------------------

    def commit_time(self, sha: str) -> str:
        if not mg.valid_sha(sha):
            raise ApiError("invalid_head")
        data = self._get(f"git/commits/{sha}",
                         {"stat": "false", "verification": "false", "files": "false"})
        if not isinstance(data, dict) or data.get("sha") != sha:
            raise ApiError("commit_time_unusable")
        date = _at(data, "commit", "committer", "date")
        if not isinstance(date, str) or not date:
            raise ApiError("commit_time_unusable")
        return date

    def _statuses(self, sha: str) -> list[dict[str, Any]]:
        """Every status of the commit; ``statuses/{sha}`` takes only a commit id.

        The default order (creation time, in seconds) is not unique, so a status
        could move between pages. ``leastindex`` orders by the index that
        Forgejo keeps unique for each commit, newest first; the ``id`` of a
        status is that index.
        """
        if not mg.valid_sha(sha):
            raise ApiError("invalid_head")
        items = self._pages(f"statuses/{sha}", {"sort": "leastindex"})
        for item in items:
            state = item.get("status") if isinstance(item, dict) else None
            if not isinstance(state, str) or state not in STATUS_STATES or "creator" not in item:
                raise ApiError("forgejo_status_unusable")
        return items

    def _jobs(self, sha: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Every job of every Forgejo Actions run for exactly this commit, with its run.

        Forgejo Actions sets the state of a job, so an account cannot write that
        result through the API. Each run must name this commit itself, and each
        job must name its run.
        """
        if not mg.valid_sha(sha):
            raise ApiError("invalid_head")
        result: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for run in self._pages("actions/runs", {"head_sha": sha}, envelope="workflow_runs"):
            if (not isinstance(run, dict) or type(run.get("id")) is not int
                    or run.get("commit_sha") != sha):
                raise ApiError("forgejo_run_unusable")
            jobs = self._get(f"actions/runs/{run['id']}/jobs")
            if not isinstance(jobs, list):
                raise ApiError("forgejo_job_unusable")
            _refuse_repeats(jobs, "id")
            for job in jobs:
                if (not isinstance(job, dict) or type(job.get("id")) is not int
                        or job.get("run_id") != run["id"]
                        or not isinstance(job.get("name"), str) or not job["name"]
                        or not isinstance(job.get("status"), str)
                        or job["status"] not in JOB_STATES):
                    raise ApiError("forgejo_job_unusable")
                result.append((run, job))
        return result

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        """One check run for each job name: the jobs of the Actions runs for this commit.

        A re-run updates a job in place, so the job with the highest id is not
        always the newest result. When jobs of several runs share a name, they
        are one check: it has not finished while one of them has not, and
        otherwise its conclusion is the first in ``WORST_FIRST`` that one of
        them has. A failed job therefore keeps the check red, and a job that
        was skipped in one run does not fail a check that another run passed.
        """
        named: dict[str, list[dict[str, Any]]] = {}
        for _run, job in self._jobs(sha):
            named.setdefault(job["name"], []).append(job)
        result = []
        for name, jobs in named.items():
            if any(job["status"] not in JOB_CONCLUSIONS for job in jobs):
                status, conclusion = "in_progress", None
            else:
                found = {JOB_CONCLUSIONS[job["status"]] for job in jobs}
                status = "completed"
                conclusion = next(value for value in WORST_FIRST if value in found)
            result.append({"id": max(job["id"] for job in jobs), "name": name, "head_sha": sha,
                           "status": status, "conclusion": conclusion,
                           "app": {"id": mg.ACTIONS_APP_ID}})
        return result

    def _creator(self, creator: Any) -> Any:
        if not isinstance(creator, dict):
            return None
        if creator.get("id") == self._own_id():
            return {"login": mg.GATE_PUBLISHER[0], "type": mg.GATE_PUBLISHER[1]}
        return {"login": creator.get("login"), "type": "User"}

    def statuses(self, sha: str) -> list[dict[str, Any]]:
        """Every commit status, except the copies that Forgejo Actions writes of its jobs.

        Forgejo Actions writes a status for each job of a push, a pull request
        or a release, with no ``creator``, and ``check_runs`` reads the job
        itself. Forgejo also shows no ``creator`` for a status whose account was
        deleted, so a status with no creator is left out only when its context
        ends as the copy of a job of this commit does: ``/ <job> (<event>)``.
        Every other status stays, so a red one still blocks the merge.
        """
        items = self._statuses(sha)
        copies: tuple[str, ...] = ()
        if any(item["creator"] is None for item in items):
            copies = tuple(f" / {job['name']} ({event})" for run, job in self._jobs(sha)
                           if (event := _status_event(run)) is not None)
        return [{"id": item.get("id"), "context": item.get("context"), "state": item["status"],
                 "description": item.get("description"), "creator": self._creator(item["creator"])}
                for item in items
                if not (item["creator"] is None and isinstance(item.get("context"), str)
                        and item["context"] != self.policy.gate_context
                        and item["context"].endswith(copies))]

    def _commits(self, base: str, head: str) -> list[dict[str, Any]]:
        """Commits in ``head`` that ``base`` lacks, newest first as Forgejo lists them."""
        data = self._get(f"compare/{base}...{head}", {"files": "false", "verification": "false"})
        commits = _at(data, "commits")
        total = _at(data, "total_commits")
        if (not isinstance(commits, list) or type(total) is not int or total != len(commits)
                or not all(isinstance(commit, dict) for commit in commits)):
            raise ApiError("forgejo_compare_unusable")
        return commits

    def _pull_files(self, head: str) -> tuple[list[Any] | None, Any]:
        """The changed files and base of the one open pull request at ``head``.

        Forgejo computes this list at ``refs/pull/{n}/head``, which it moves to
        the head branch in the background and can leave behind. Each file names
        the commit the list was computed at (``contents_url`` ends with
        ``?ref=<commit>``), so a list for another commit is refused.
        """
        listing = self._pages("pulls", {"state": "open", "base": self.policy.base_branch})
        matches = [entry for entry in listing if _at(entry, "head", "sha") == head]
        if len(matches) != 1 or type(matches[0].get("number")) is not int:
            return None, None
        files: list[Any] = []
        for item in self._pages(f"pulls/{matches[0]['number']}/files", identity="filename"):
            if not isinstance(item, dict):
                files.append(item)
                continue
            if _ref_of(item.get("contents_url")) != head:
                raise ApiError("forgejo_files_head_mismatch")
            row = {"filename": item.get("filename"), "status": item.get("status")}
            if item.get("previous_filename") is not None:
                row["previous_filename"] = item["previous_filename"]
            files.append(row)
        return files, _at(matches[0], "base", "sha")

    def compare(self, base: str, head: str) -> dict[str, Any]:
        if not mg.valid_sha(base) or not mg.valid_sha(head):
            raise ApiError("invalid_head")
        ahead = self._commits(base, head)
        behind = self._commits(head, base)
        status = ("identical" if not ahead and not behind else "behind" if not ahead
                  else "diverged" if behind else "ahead")
        comparison: dict[str, Any] = {
            "status": status,
            "commits": [{"sha": commit.get("sha"),
                         "commit": {"committer": {"date": _at(commit, "commit", "committer",
                                                              "date")}}}
                        for commit in reversed(ahead)],
        }
        if not behind:
            # The base has no commit the head lacks, so the base is the merge base.
            comparison["merge_base_commit"] = {"sha": base}
        files, listed_base = self._pull_files(head)
        if files is not None:
            comparison["files"] = files
            comparison["base_commit"] = {"sha": listed_base}
        return comparison

    def branch_head(self, branch: str) -> str:
        sha = _at(self._get(f"branches/{urllib.parse.quote(branch, safe='/')}"), "commit", "id")
        return sha if isinstance(sha, str) else ""

    # Writes --------------------------------------------------------------------

    def publish(self, sha: str, state: str, description: str) -> None:
        """Write the gate status unless the newest one is this adapter's, saying this."""
        latest: dict[str, dict[str, Any]] = {}
        for status in self._statuses(sha):
            if (type(status.get("id")) is not int or not isinstance(status.get("context"), str)
                    or not status["context"]):
                raise ApiError("forgejo_status_unusable")
            key = status["context"]
            if key not in latest or status["id"] > latest[key]["id"]:
                latest[key] = status
        current = latest.get(self.policy.gate_context)
        if (current is not None and current["status"] == state
                and current.get("description") == description[:140]
                and isinstance(current["creator"], dict)
                and current["creator"].get("id") == self._own_id()):
            return
        created = self._send("POST", self._repo(f"statuses/{sha}"), {
            "state": state, "context": self.policy.gate_context,
            "description": description[:140]})[1]
        if (not isinstance(created, dict) or created.get("context") != self.policy.gate_context
                or created.get("status") != state):
            raise ApiError("forgejo_status_not_confirmed")

    def merge(self, number: int, sha: str) -> None:
        """Merge exactly ``sha``: Forgejo refuses a moved head (``head_commit_id``)."""
        if not self.apply or type(number) is not int or not mg.valid_sha(sha):
            raise ApiError("merge_write_refused")
        self._send("POST", self._repo(f"pulls/{number}/merge"),
                   {"Do": "squash", "head_commit_id": sha})
        after = self.pull(number)
        if (not isinstance(after, dict) or after.get("merged") is not True
                or after.get("state") != "closed" or _at(after, "head", "sha") != sha
                or not mg.valid_sha(after.get("merge_commit_sha"))):
            raise ApiError("merge_postcondition_unproven")

    def comment(self, number: int, body: str) -> None:
        if (not self.apply or type(number) is not int or number < 1
                or not isinstance(body, str) or not body):
            raise ApiError("comment_write_refused")
        created = self._send("POST", self._repo(f"issues/{number}/comments"), {"body": body})[1]
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ApiError("comment_not_confirmed")
