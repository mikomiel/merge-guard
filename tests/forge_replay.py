"""Replay one recorded forge case against an adapter, as the README defines it.

``tests/fixtures/forge/README.md`` owns the case format and the matching rule.
This module is the Python replay of it; another implementation replays the
same files with its own transport stand-in.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any, Self

from merge_guard import engine
from merge_guard import policy as mg
from merge_guard.forgejo import Forgejo
from merge_guard.github import GitHub

CASES = Path(__file__).resolve().parent / "fixtures" / "forge"
FORGES = ("github", "forgejo")
#: The Forgejo base URL every case is replayed under; paths are relative to its
#: API root, so the host never appears in a case.
FORGEJO_URL = "https://forgejo.example"
FORGEJO_TOKEN = "replay-token"


def case_files(forge: str) -> list[Path]:
    return sorted((CASES / forge).glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _key(method: str, target: str, body: Any) -> tuple[Any, ...]:
    path, _, query = target.partition("?")
    return method, path, sorted(urllib.parse.parse_qsl(query, keep_blank_values=True)), body


class Exchanges:
    """Each request takes the first unserved exchange with the same request."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        self.pending = list(exchanges)

    def answer(self, method: str, target: str, body: Any) -> dict[str, Any]:
        wanted = _key(method, target, body)
        for index, exchange in enumerate(self.pending):
            request = exchange["request"]
            if _key(request["method"], request["path"], request.get("body")) == wanted:
                return self.pending.pop(index)["response"]
        raise AssertionError(f"no recorded exchange for {method} {target} {body!r}")


def _body_text(response: dict[str, Any]) -> str:
    return json.dumps(response["body"]) if "body" in response else ""


def github_run(exchanges: Exchanges):
    """A stand-in for ``subprocess.run`` that answers ``gh api`` from the case."""

    def run(command, input=None, **_kwargs):
        if command[:3] == ["gh", "api", "graphql"]:
            fields = dict(item.split("=", 1) for item in command[3:] if "=" in item)
            response = exchanges.answer("POST", "/graphql", {"number": int(fields["number"])})
            ok = 200 <= response["status"] < 300
            return subprocess.CompletedProcess(command, 0 if ok else 1,
                                               stdout=_body_text(response), stderr="")
        assert command[:5] == ["gh", "api", "--hostname", "github.com", "--method"], command
        method, target = command[5], "/" + command[6]
        body = json.loads(input) if input is not None else None
        response = exchanges.answer(method, target, body)
        ok = 200 <= response["status"] < 300
        return subprocess.CompletedProcess(command, 0 if ok else 1,
                                           stdout=_body_text(response), stderr="")

    return run


class _Response:
    def __init__(self, status: int, headers: Message, raw: bytes) -> None:
        self.status, self.headers, self._raw = status, headers, raw

    def read(self, limit: int = -1) -> bytes:
        return self._raw if limit < 0 else self._raw[:limit]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class ForgejoOpener:
    """A stand-in for the adapter's URL opener that answers from the case."""

    root = FORGEJO_URL + "/api/v1"

    def __init__(self, exchanges: Exchanges) -> None:
        self.exchanges = exchanges

    def open(self, request, timeout=None):
        assert request.full_url.startswith(self.root + "/"), request.full_url
        assert request.get_header("Authorization") == f"token {FORGEJO_TOKEN}"
        body = json.loads(request.data) if request.data is not None else None
        response = self.exchanges.answer(request.get_method(),
                                         request.full_url[len(self.root):], body)
        headers = Message()
        for name, value in response.get("headers", {}).items():
            headers[name] = value
        raw = _body_text(response).encode("utf-8")
        if not 200 <= response["status"] < 300:
            raise urllib.error.HTTPError(request.full_url, response["status"], "recorded",
                                         headers, io.BytesIO(raw))
        return _Response(response["status"], headers, raw)


def replay(case: dict[str, Any], monkeypatch) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The case's outcome, and the exchanges no request asked for."""
    policy = mg.load_policy(case["policy"])
    exchanges = Exchanges(case["exchanges"])
    run = case["run"]
    if case["forge"] == "github":
        monkeypatch.setattr(subprocess, "run", github_run(exchanges))
        api: Any = GitHub(policy, apply=run["apply"])
    else:
        api = Forgejo(policy, FORGEJO_URL, FORGEJO_TOKEN, apply=run["apply"],
                      opener=ForgejoOpener(exchanges))
    try:
        if run["mode"] == "merge":
            result: Any = engine.run_merge(api, policy, run["apply"])
        elif run["mode"] == "review":
            result = engine.run_review(api, policy, run["apply"], run.get("event_head"))
        else:
            result = engine.run_review(api, policy, False, None)
    except (engine.ApiError, mg.Unusable) as exc:
        return {"refusal": str(exc)}, exchanges.pending
    # The result as the command line prints it: tuples become JSON arrays.
    return {"result": json.loads(json.dumps(result))}, exchanges.pending
