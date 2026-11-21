"""Private delivery intent for non-blocking findings; never merge scheduling state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .automerge_queue import STATE_PATH, QueueStateError, _private_directory, _regular_private

ROOT = STATE_PATH.parent / "review-followups"
MAX_BYTES = 2 * 1024 * 1024
SCHEMA = "review-followups.v1"
_FIELDS = {"class", "path", "line_or_symbol", "condition", "consequence", "smallest_correction", "verification"}
_STATES = {"pending", "creating", "uncertain", "delivered", "cancelled"}
_KEY = re.compile(r"[0-9a-f]{64}\Z")


def request_path(repository: str, pull_request: int, root: Path = ROOT) -> Path:
    key = hashlib.sha256(f"{repository}\0{pull_request}".encode()).hexdigest()
    return root / f"{key}.json"


def read(path: Path) -> dict[str, Any] | None:
    _private_directory(path.parent)
    try:
        _regular_private(path)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("REVIEW_FOLLOWUPS_OVERFLOW")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or not isinstance(value.get("request"), dict) or not isinstance(value.get("entries"), dict)
            or not isinstance(value.get("diagnostic"), str)
            or ("source" in value and (not isinstance(value["source"], dict)
                                      or type(value["source"].get("review_id")) is not int
                                      or value["source"]["review_id"] <= 0))
            or not isinstance(value.get("repository"), str)
            or type(value.get("pull_request")) is not int
            or any(_KEY.fullmatch(key) is None or not isinstance(entry, dict)
                   or entry.get("state") not in _STATES or not isinstance(entry.get("source"), dict)
                   or not isinstance(entry.get("finding"), dict) or set(entry["finding"]) != _FIELDS
                   or any(not isinstance(field, str) for field in entry["finding"].values())
                   or entry["finding"]["class"] not in {"OPTIONAL", "PRE_EXISTING"}
                   for key, entry in value["entries"].items())):
        raise ValueError("REVIEW_FOLLOWUPS_INVALID")
    # A closed-unmerged PR can later be reopened, so a record that settled it reads as unsettled.
    if value.get("pr_state") == "closed" and value.get("settled") is True:
        value["settled"] = False
    return value


def save(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ValueError("REVIEW_FOLLOWUPS_OVERFLOW")
    _private_directory(path.parent)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".followups-", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() or path.is_symlink():
            _regular_private(path)
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def record_request(repository: str, pull_request: int, *, root: Path = ROOT) -> Path:
    _private_directory(root.parent)
    root.mkdir(mode=0o700, exist_ok=True)
    _private_directory(root)
    path = request_path(repository, pull_request, root)
    # The inert request object stays in the v1 schema so existing records still load.
    value = read(path) or {"schema": SCHEMA, "repository": repository, "pull_request": pull_request,
                           "entries": {}, "request": {}, "diagnostic": "pending"}
    save(path, value)
    return path


def discovery_checkpoint(root: Path = ROOT) -> tuple[Path, dict[str, Any]]:
    _private_directory(root.parent)
    root.mkdir(mode=0o700, exist_ok=True)
    _private_directory(root)
    path = root / "discovery.json"
    try:
        _regular_private(path)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("REVIEW_FOLLOWUPS_DISCOVERY_INVALID")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {"schema": "review-followups-discovery.v1",
                 "activated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                 "cursor": "", "next_phase": "discover"}
        save(path, value)
    if (not isinstance(value, dict) or value.get("schema") != "review-followups-discovery.v1"
            or not isinstance(value.get("activated_at"), str) or not isinstance(value.get("cursor"), str)
            or value.get("next_phase") not in {"discover", "deliver"}):
        raise ValueError("REVIEW_FOLLOWUPS_DISCOVERY_INVALID")
    activated = datetime.fromisoformat(value["activated_at"])
    if activated.tzinfo is None or (value["cursor"] and re.fullmatch(
            # The receipt names the follow-up discovery scan (the comment re-review `-cm` suffix).
            r"[0-9a-f]{64}(?:-[0-9a-f]{64}(?:-cm[0-9a-f]{64})?)?\.json", value["cursor"]) is None):
        raise ValueError("REVIEW_FOLLOWUPS_DISCOVERY_INVALID")
    return path, value


def finding_key(repository: str, pull_request: int, finding: dict[str, str]) -> str:
    normalized = {key: " ".join(value.split()) for key, value in finding.items()}
    payload = json.dumps([repository, pull_request, normalized], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def status(repository: str, pull_request: int, *, root: Path = ROOT) -> dict[str, Any]:
    try:
        value = read(request_path(repository, pull_request, root))
    except (OSError, ValueError, QueueStateError):
        return {"state": "unavailable", "scope": "local", "reason": "delivery journal unavailable"}
    if value is None:
        return {"state": "unavailable", "scope": "local", "reason": "no recorded delivery intent"}
    entries = [{"key": key, "state": entry["state"], "classification": entry["finding"]["class"],
                **{name: entry[name] for name in ("task_id", "task_url", "error", "context") if name in entry}}
               for key, entry in value["entries"].items()]
    return {"state": value["diagnostic"], "scope": "local", "entries": entries,
            **({"review_id": value["source"]["review_id"]} if "source" in value else {}),
            **({"review_findings": value["review_findings"]} if "review_findings" in value else {})}
