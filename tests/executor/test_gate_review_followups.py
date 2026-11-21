"""Accepted receipt to tracker delivery, with controlled forge and MCP boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from servers import agent_delegate_mcp as delegate
from servers import gate_snapshot_review as snapshots

# The production entry point prepares the gate import path before loading its helper.
delivery = importlib.import_module("servers.gate_review_followups")
journal = delivery.journal
HEAD, BASE, CLAIM = "a" * 40, "b" * 40, "c" * 64
REPOSITORY = "acme/core-repo"


def finding(classification: str = "OPTIONAL", path: str = "src/example.py") -> dict[str, str]:
    return {"class": classification, "path": path, "line_or_symbol": "example",
            "condition": "Repeated source lookup", "consequence": "An extra read per request",
            "smallest_correction": "Reuse the existing result", "verification": "Count the boundary reads"}


class Forge:
    def __init__(self, source: dict[str, Any]) -> None:
        self.source = source
        self.head = HEAD
        self.base = BASE
        self.state = "closed"
        self.merged = True
        self.branch = "task/example"
        self.labels: list[dict[str, str]] = []
        self.merged_at = "2020-01-01T12:00:00Z"
        self.submitted_at = "2020-01-01T11:00:00Z"
        self.claim = CLAIM
        self.cwd: Path | None = None
        self.merge_commit = "f" * 40
        self.reviews: list[dict[str, Any]] | None = None
        self.approved = True

    def managed_checkout_context(self, alias: str) -> Any:
        return SimpleNamespace(policy=delivery.catalog.policy_for(alias), cwd=self.cwd)

    def _read_pr_state(self, context: Any, pr: int) -> dict[str, Any]:
        return {"number": pr, "head": {"sha": self.head, "ref": self.branch}, "state": self.state,
                "merged": self.merged, "merged_at": self.merged_at, "merge_commit_sha": self.merge_commit,
                "labels": self.labels, "body": "claim",
                "html_url": "https://forge.example.org/acme/core-repo/pulls/7"}

    def _list_pull_reviews(self, context: Any, pr: int) -> list[dict[str, Any]]:
        if self.reviews is not None:
            return self.reviews
        return [{"id": self.source["review_id"], "commit_id": self.source["head_sha"], "dismissed": False,
                 "state": "APPROVED" if self.approved else "REQUEST_CHANGES", "submitted_at": self.submitted_at,
                 "user": {"id": 7, "login": "reviewer-user"},
                 "body": "<!-- delegated-review.v1\n" + f"run: {self.source['run_marker']}\n"
                 + f"base: {self.source['base_sha']}\nworker: reviewer\nharness: alpha\nmodel: model-a-flash\n"
                 + f"claim: {self.source['claim_sha']}\n-->"}]

    _latest_delegated_review = staticmethod(delivery.GitMaintainerGate._latest_delegated_review)
    _delegated_review_provenance = staticmethod(delivery.GitMaintainerGate._delegated_review_provenance)
    _clean_refresh_preserves_review = delivery.GitMaintainerGate._clean_refresh_preserves_review
    _clean_refresh_probe = delivery.GitMaintainerGate._clean_refresh_probe
    _reviewed_paths = delivery.GitMaintainerGate._reviewed_paths
    _reviewed_files_unchanged = delivery.GitMaintainerGate._reviewed_files_unchanged
    _range_diff_digest = delivery.GitMaintainerGate._range_diff_digest

    def _runner(self, argv: list[str], cwd: Path | None) -> Any:
        if cwd is None:
            return SimpleNamespace(returncode=1, stdout="")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)

    def pull_by_index(self, context: Any, pr: int) -> Any:
        return object()

    def _native_claim_allowed(self, pull: Any, native: Any) -> bool:
        return False

    def _claim_identity(self, body: Any, head: str, *, native_claim: bool) -> str:
        return self.claim


class Board:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tasks: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_after_create = False
        self.fail_before_create = False
        self.fail_lookup = False
        self.partial_labels = False
        self.hide_tasks = False

    async def call_tool(self, name: str, arguments: dict[str, Any], **options: Any) -> Any:
        result = self.invoke(name, arguments)
        return SimpleNamespace(is_error=False, data=result)

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        assert arguments["actor"] == ""
        if name == "tracker_list_tasks":
            assert arguments["include_done"] is True
            if self.fail_lookup:
                raise OSError("lookup offline")
            key = arguments["filter"].split("'")[1]
            rows = [] if self.hide_tasks else [task for task in self.tasks.values() if key in task["description"]]
            return {"tasks": [{"id": task["id"]} for task in rows], "total_pages": 1}
        if name == "tracker_create_task":
            assert all(entry["state"] in {"pending", "creating", "delivered", "uncertain", "cancelled"}
                       for entry in journal.read(self.path)["entries"].values())
            assert any(entry["state"] == "creating" for entry in journal.read(self.path)["entries"].values())
            if self.fail_before_create:
                raise OSError("write unknown")
            identifier = len(self.tasks) + 1
            task = {"id": identifier, "title": arguments["title"], "description": arguments["description"],
                    "done": False, "labels": [] if self.partial_labels else
                    [{"title": label} for label in arguments["labels"]], "lane": arguments["lane"],
                    "url": f"https://board.example.invalid/tasks/{identifier}"}
            self.tasks[identifier] = task
            if self.fail_after_create:
                raise OSError("response lost")
            return {"task": task}
        task = self.tasks[arguments["task_id"]]
        if name == "tracker_get_task":
            return {"task": task.copy()}
        if name == "tracker_manage_labels":
            task["labels"] += [{"title": title} for title in arguments["labels"]]
            return {"labels": task["labels"]}
        if name == "tracker_move_task":
            task["lane"] = arguments["lane"]
            return {"task": task}
        if name == "tracker_update_task":
            task["description"] = arguments["description"]
            return {"task": task}
        raise AssertionError(name)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    receipt_root = tmp_path / "receipts"
    journal_root = tmp_path / "outbox"
    journal_root.mkdir(mode=0o700)
    policy = delivery.catalog.effective_review_policy(delivery.catalog.policy_for("core-repo"))
    receipt_path = snapshots._receipt_path(REPOSITORY, 7, HEAD, BASE, policy, receipt_root, CLAIM)
    marker = f"gate-review-{hashlib.sha256(receipt_path.stem.encode()).hexdigest()[:24]}"
    source = {"repository": REPOSITORY, "pull_request": 7, "head_sha": HEAD, "base_sha": BASE,
              "claim_sha": CLAIM, "review_id": 23, "run_marker": marker}
    path = journal.record_request(REPOSITORY, 7, root=journal_root)
    forge = Forge(source)
    board = Board(path)
    original = snapshots.accepted_review_findings
    monkeypatch.setattr(snapshots, "accepted_review_findings", lambda selection: original(
        selection, receipt_root=receipt_root))

    def receipt(findings: list[dict[str, str]], *, verdict: str = "ACCEPT_STATIC_CHANGE",
                review_source: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = review_source or source
        reviewed_head, reviewed_base = selected["head_sha"], selected["base_sha"]
        selected_path = snapshots._receipt_path(REPOSITORY, 7, reviewed_head, reviewed_base, policy,
                                                receipt_root, selected["claim_sha"])
        selected["run_marker"] = f"gate-review-{hashlib.sha256(selected_path.stem.encode()).hexdigest()[:24]}"
        selected_marker = selected["run_marker"]
        parsed = delegate._parse_structured_review(json.dumps({
            "verdict": verdict, "verdict_basis": "Reviewed source behavior", "findings": findings,
            "files_not_reviewed": [],
        }))
        result = delegate._build_review_envelope(
            f"workflow:{REPOSITORY}",
            {"repository": REPOSITORY, "worktree": "/private/snapshot", "head_sha": reviewed_head},
            reviewed_head, reviewed_head, "", "", 0, base_sha=reviewed_base,
            resolved_model="model-a-flash", executor="alpha", parsed=parsed,
        )
        result.update(task_id=selected_marker, worker="reviewer", cleanup_proven=True)
        stored = {"schema": snapshots._SCHEMA, **snapshots._identity(
            REPOSITORY, 7, reviewed_head, reviewed_base, policy, selected_marker, selected["claim_sha"]),
                  "state": "published", "cleanup_proven": True, "verdict": verdict,
                  "review_id": selected["review_id"], "result": result}
        snapshots._atomic_json(selected_path, stored)
        return stored

    return SimpleNamespace(path=path, forge=forge, board=board, receipt=receipt, receipt_path=receipt_path,
                           source=source, root=journal_root)


def execute(setup: Any) -> dict[str, Any]:
    return asyncio.run(delivery.process(setup.path, setup.forge, setup.board))


def test_full_receipt_delivery_classification_repo_labels_and_idempotency(setup: Any) -> None:
    assert journal.read(setup.path)["request"] == {}
    setup.receipt([finding(), finding("PRE_EXISTING", "infra-config/roles/example/tasks/main.yml"),
                   finding("RUNTIME_ONLY_GAP")])
    assert execute(setup) == {"state": "complete"}
    assert len(setup.board.tasks) == 2
    labels = [{label["title"] for label in task["labels"]} for task in setup.board.tasks.values()]
    assert {"question", "review-followup", "review-optional", "core"} in labels
    assert {"question", "review-followup", "review-pre-existing", "infra"} in labels
    for task in setup.board.tasks.values():
        assert task["lane"] == "Followup"
        assert HEAD in task["description"] and BASE in task["description"]
        assert "does not commit anyone" in task["description"]
        assert "smallest_correction" in task["description"]
        assert "acme/core-repo" in task["description"]
    for name, args in setup.board.calls:
        if name == "tracker_create_task":
            assert args["project"] == "Project" and args["lane"] == "Followup"
            assert not ({"priority", "assignees", "due_date"} & args.keys())
    execute(setup)
    assert len(setup.board.tasks) == 2
    setup.forge.state, setup.forge.merged = "closed", True
    execute(setup)
    status = journal.status(REPOSITORY, 7, root=setup.root)
    assert status["state"] == "complete"
    assert len(status["entries"]) == 2 and all(entry["task_url"] for entry in status["entries"])
    assert journal.read(setup.path)["settled"] is True


@pytest.mark.parametrize("mode", ["failed", "rejected", "incomplete", "missing", "overflow", "legacy", "stale"])
def test_ineligible_evidence_cannot_create_tickets(setup: Any, mode: str) -> None:
    stored = setup.receipt([finding()])
    if mode == "failed":
        stored["result"]["transport_error"] = "TIMEOUT"
    elif mode == "rejected":
        stored["state"] = "rejected"
    elif mode == "incomplete":
        stored["cleanup_proven"] = False
    elif mode == "legacy":
        for key in ("structured_findings", "structured_findings_status", "structured_findings_total"):
            stored["result"].pop(key)
    elif mode == "overflow":
        stored["result"].update(structured_findings=None, structured_findings_status="overflow")
    elif mode == "stale":
        stored["review_id"] = 22
    if mode == "missing":
        setup.receipt_path.unlink()
    else:
        snapshots._atomic_json(setup.receipt_path, stored)
    result = execute(setup)
    assert result["state"] in {"unavailable", "overflow"}
    assert setup.board.tasks == {}
    assert journal.status(REPOSITORY, 7, root=setup.root)["state"] == result["state"]


def test_empty_accepted_review_is_explicitly_complete(setup: Any) -> None:
    setup.receipt([])
    assert execute(setup) == {"state": "complete"}
    assert journal.status(REPOSITORY, 7, root=setup.root)["entries"] == []
    assert setup.board.tasks == {}


def test_lost_create_response_reconciles_done_tasks_without_duplicate_or_reopen(setup: Any) -> None:
    setup.receipt([finding()])
    setup.board.fail_after_create = True
    assert execute(setup) == {"state": "pending"}
    assert len(setup.board.tasks) == 1
    task = setup.board.tasks[1]
    task["done"] = True
    setup.board.fail_after_create = False
    assert execute(setup) == {"state": "complete"}
    assert sum(name == "tracker_create_task" for name, _ in setup.board.calls) == 1
    assert task["done"] is True
    assert not [name for name, _ in setup.board.calls if name == "tracker_move_task"]


def test_lost_create_response_repairs_review_lane_without_duplicate(setup: Any) -> None:
    setup.receipt([finding()])
    setup.board.fail_after_create = True
    assert execute(setup)["state"] == "pending"
    task = setup.board.tasks[1]
    task["lane"] = "backlog"  # Creation reached the board, but lane placement did not.
    setup.board.fail_after_create = False
    assert execute(setup)["state"] == "complete"
    assert task["lane"] == "Followup"
    assert sum(name == "tracker_create_task" for name, _ in setup.board.calls) == 1


@pytest.mark.parametrize("lane", ["priority", "in-progress", "blocked"])
def test_retry_preserves_reclassified_review_task_lane(setup: Any, lane: str) -> None:
    setup.receipt([finding()])
    setup.board.fail_after_create = True
    assert execute(setup)["state"] == "pending"
    task = setup.board.tasks[1]
    task["lane"] = lane
    task["labels"] = [{"title": label} for label in ("bug", "review-followup", "review-optional", "core")]
    setup.board.fail_after_create = False
    assert execute(setup)["state"] == "complete"
    assert task["lane"] == lane
    assert not [name for name, _ in setup.board.calls if name == "tracker_move_task"]


def test_unknown_write_with_no_readback_is_not_posted_again(setup: Any) -> None:
    setup.receipt([finding()])
    setup.board.fail_before_create = True
    assert execute(setup)["state"] == "pending"
    setup.board.fail_before_create = False
    assert execute(setup)["state"] == "pending"
    assert sum(name == "tracker_create_task" for name, _ in setup.board.calls) == 1
    entry = journal.status(REPOSITORY, 7, root=setup.root)["entries"][0]
    assert entry["state"] == "uncertain" and "readback required" in entry["error"]


def test_partial_labels_repaired_without_second_task(setup: Any) -> None:
    setup.receipt([finding()])
    setup.board.partial_labels = True
    assert execute(setup)["state"] == "complete"
    assert {label["title"] for label in setup.board.tasks[1]["labels"]} == {
        "question", "review-followup", "review-optional", "core"}
    assert sum(name == "tracker_create_task" for name, _ in setup.board.calls) == 1


def test_cancel_unsent_and_annotate_existing_without_reopening(setup: Any) -> None:
    setup.receipt([finding(), finding("PRE_EXISTING")])
    setup.board.fail_before_create = True
    execute(setup)
    setup.board.fail_before_create = False
    # Resolve one uncertain remote task and leave the other provably unsent.
    record = journal.read(setup.path)
    first, second = list(record["entries"].values())
    first["state"], second["state"] = "pending", "pending"
    journal.save(setup.path, record)
    execute(setup)
    assert len(setup.board.tasks) == 2
    for task in setup.board.tasks.values():
        task["done"] = True
    third = finding(path="unrelated.py")
    record = journal.read(setup.path)
    key = journal.finding_key(REPOSITORY, 7, third)
    record["entries"][key] = {"state": "pending", "source": setup.source, "finding": third}
    record.pop("settled", None)
    journal.save(setup.path, record)
    setup.forge.claim = "d" * 64
    execute(setup)
    entries = journal.read(setup.path)["entries"]
    assert entries[key]["state"] == "cancelled"
    assert len(setup.board.tasks) == 2
    for task in setup.board.tasks.values():
        assert task["done"] is True and "(superseded)" in task["description"]
    execute(setup)
    assert sum(name == "tracker_update_task" for name, _ in setup.board.calls) == 2


def test_lookup_outage_retains_intent_and_retry_delivers(setup: Any) -> None:
    setup.receipt([finding()])
    setup.board.fail_lookup = True
    assert execute(setup)["state"] == "pending"
    entry = next(iter(journal.read(setup.path)["entries"].values()))
    assert entry["state"] == "pending"
    setup.forge.state, setup.forge.merged = "closed", True
    setup.board.fail_lookup = False
    assert execute(setup)["state"] == "complete"
    assert len(setup.board.tasks) == 1


def test_dedupe_ignores_review_identity_and_normalizes_whitespace() -> None:
    original = finding()
    changed = {**original, "condition": "Repeated\n source    lookup"}
    assert journal.finding_key(REPOSITORY, 7, original) == journal.finding_key(REPOSITORY, 7, changed)
    assert journal.finding_key(REPOSITORY, 8, original) != journal.finding_key(REPOSITORY, 7, original)


def test_real_fastmcp_consumer_accepts_tool_result_shape() -> None:
    server = FastMCP("synthetic pinned board")

    @server.tool(name="tracker_list_tasks")
    async def list_tasks(actor: str, include_done: bool, filter: str, page: int, limit: int) -> dict[str, Any]:
        assert actor == "" and include_done and "review-followup:" in filter
        return {"tasks": [], "total_pages": 0}

    async def scenario() -> None:
        async with Client(server) as client:
            assert await delivery.find_task(client, "a" * 64) is None

    asyncio.run(scenario())


def test_ordinary_train_member_uses_its_own_final_review_after_merge(setup: Any) -> None:
    setup.receipt([finding()])
    record = journal.read(setup.path)
    record["request"] = {"head_sha": HEAD, "base_sha": BASE, "train": {"before": BASE, "after": "d" * 40}}
    journal.save(setup.path, record)
    assert execute(setup) == {"state": "complete"}
    assert len(setup.board.tasks) == 1
    assert journal.read(setup.path)["source"] == setup.source


def test_hanging_lookup_rotates_findings_and_preserves_intent(setup: Any) -> None:
    setup.receipt([finding(), finding("PRE_EXISTING")])
    started = []
    original = setup.board.call_tool

    async def hanging(name: str, args: dict[str, Any], **options: Any) -> Any:
        if name == "tracker_list_tasks" and not started:
            started.append(args["filter"].split("'")[1].removeprefix("review-followup:"))
            await asyncio.Event().wait()
        return await original(name, args)

    setup.board.call_tool = hanging
    result = asyncio.run(delivery.run(seconds=1.0, gate=setup.forge, client=setup.board,
                                      root=setup.root, receipt_root=setup.receipt_path.parent))
    assert result["code"] == "REVIEW_FOLLOWUPS_TIMEOUT" and started
    record = journal.read(setup.path)
    keys = sorted(record["entries"])
    assert keys[record["cursor"]] != started[0]
    assert record["last_attempt"] > 0
    setup.board.call_tool = original
    assert execute(setup)["state"] == "complete"
    assert len(setup.board.tasks) == 2


def test_lookup_failure_for_one_finding_does_not_starve_other_findings(setup: Any) -> None:
    setup.receipt([finding(), finding("PRE_EXISTING")])
    original = setup.board.call_tool
    rejected = []

    async def fail_one(name: str, args: dict[str, Any], **options: Any) -> Any:
        if name == "tracker_list_tasks":
            marker = args["filter"]
            if not rejected:
                rejected.append(marker)
            if marker == rejected[0]:
                raise OSError("one unavailable lookup")
        return await original(name, args)

    setup.board.call_tool = fail_one
    assert execute(setup)["state"] == "pending"
    assert len(setup.board.tasks) == 1
    setup.board.call_tool = original
    assert execute(setup)["state"] == "complete" and len(setup.board.tasks) == 2


def test_done_ticket_with_human_assessment_is_never_reclassified(setup: Any) -> None:
    setup.receipt([finding()])
    setup.board.fail_after_create = True
    execute(setup)
    task = setup.board.tasks[1]
    task["done"] = True
    task["labels"] = [{"title": label} for label in ("bug", "review-followup", "review-optional", "core")]
    setup.board.fail_after_create = False
    execute(setup)
    assert {label["title"] for label in task["labels"]} == {"bug", "review-followup", "review-optional", "core"}
    assert task["done"] is True


def test_pr_and_review_links_come_from_forge_web_metadata(setup: Any) -> None:
    setup.receipt([finding()])
    row = journal.read(setup.path)
    key = journal.finding_key(REPOSITORY, 7, finding())
    source = {**setup.source, "review_url": "https://forge.example.org/acme/core-repo/pulls/7#issuecomment-909"}
    body = delivery.ticket_body({**row, "pr_url": "https://forge.example.org/acme/core-repo/pulls/7"},
                                {"finding": finding(), "source": source}, key)
    assert 'href="https://forge.example.org/acme/core-repo/pulls/7#issuecomment-909"' in body
    assert 'href="https://forge.example.org/acme/core-repo/pulls/7"' in body
    assert "git@" not in body


@pytest.mark.parametrize("part", ["entry", "source", "diagnostic"])
def test_malformed_journal_is_unavailable_instead_of_a_false_empty_result(setup: Any, part: str) -> None:
    record = journal.read(setup.path)
    if part == "entry":
        record["entries"]["a" * 64] = {}
    elif part == "source":
        record["source"] = {}
    else:
        record.pop("diagnostic")
    journal.save(setup.path, record)
    assert journal.status(REPOSITORY, 7, root=setup.root)["state"] == "unavailable"


def test_terminal_journals_leave_retry_set_but_remain_readable(setup: Any) -> None:
    setup.receipt([finding()])
    setup.forge.state, setup.forge.merged = "closed", True
    execute(setup)
    assert delivery.pending_paths(setup.root) == []
    assert journal.status(REPOSITORY, 7, root=setup.root)["entries"][0]["task_id"] == 1


def test_real_fastmcp_tool_failure_is_a_pending_delivery_error() -> None:
    server = FastMCP("synthetic failing board")

    @server.tool(name="synthetic_failure")
    async def fail(actor: str) -> dict[str, Any]:
        raise ValueError("synthetic failure")

    async def scenario() -> None:
        async with Client(server) as client:
            with pytest.raises(ValueError, match="REVIEW_FOLLOWUPS_TOOL_ERROR"):
                await delivery.call(client, "synthetic_failure")

    asyncio.run(scenario())


def test_interrupted_create_keeps_top_level_pending_until_readback(setup: Any) -> None:
    setup.receipt([finding()])
    started = []
    original = setup.board.call_tool

    async def hanging(name: str, args: dict[str, Any], **options: Any) -> Any:
        if name == "tracker_create_task":
            assert journal.status(REPOSITORY, 7, root=setup.root)["state"] == "pending"
            started.append(True)
            await asyncio.Event().wait()
        return await original(name, args)

    setup.board.call_tool = hanging
    result = asyncio.run(delivery.run(seconds=1.0, gate=setup.forge, client=setup.board,
                                      root=setup.root, receipt_root=setup.receipt_path.parent))
    assert result["code"] == "REVIEW_FOLLOWUPS_TIMEOUT" and started
    status = journal.status(REPOSITORY, 7, root=setup.root)
    assert status["state"] == "pending" and status["entries"][0]["state"] == "creating"


def closed_legacy_record(setup: Any, entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema": "review-followups.v1", "repository": REPOSITORY, "pull_request": 7,
            "request": {"head_sha": HEAD, "base_sha": BASE, "selection": setup.source},
            "source": setup.source, "entries": entries, "diagnostic": "complete", "pr_state": "closed",
            "pr_url": "https://forge.example.org/acme/core-repo/pulls/7", "settled": True}


def test_legacy_closed_done_ticket_and_findings_reconcile_after_reopen_and_merge(setup: Any) -> None:
    optional, pre_existing = finding(), finding("PRE_EXISTING")
    existing_key = journal.finding_key(REPOSITORY, 7, optional)
    unsent_key = journal.finding_key(REPOSITORY, 7, pre_existing)
    record = closed_legacy_record(setup, {
        existing_key: {"state": "delivered", "source": setup.source, "finding": optional,
                       "task_id": 1, "task_url": "https://board.example.invalid/tasks/1", "context": "closed-unmerged"},
        unsent_key: {"state": "cancelled", "source": setup.source, "finding": pre_existing,
                     "context": "closed-unmerged"},
    })
    original_request = record["request"]
    task = {"id": 1, "title": "Previously assessed finding", "done": True, "lane": "done",
            "labels": [{"title": label} for label in ("bug", "review-followup", "review-optional", "core")],
            "url": "https://board.example.invalid/tasks/1",
            "description": delivery.ticket_body(record, record["entries"][existing_key], existing_key)
            + f"<p>review-followup-context:{existing_key}:closed-unmerged</p>"}
    setup.board.tasks[1] = task
    journal.save(setup.path, record)
    assert json.loads(setup.path.read_text())["settled"] is True
    setup.forge.state, setup.forge.merged = "open", False
    assert scan(setup) == {"ok": True}
    assert json.loads(setup.path.read_text())["settled"] is False
    assert setup.board.calls == []

    source = {**setup.source, "head_sha": "e" * 40, "base_sha": "f" * 40, "review_id": 24}
    new_finding = finding(path="newly-reviewed.py")
    setup.receipt([optional, pre_existing, new_finding], review_source=source)
    setup.forge.source, setup.forge.head, setup.forge.base = source, source["head_sha"], source["base_sha"]
    setup.forge.state, setup.forge.merged = "closed", True
    assert scan(setup) == {"ok": True}
    assert len(setup.board.tasks) == 3
    current = journal.read(setup.path)
    assert current["settled"] is True and current["request"] == original_request
    assert current["entries"][unsent_key]["source"] == source
    assert current["entries"][journal.finding_key(REPOSITORY, 7, new_finding)]["source"] == source
    assert current["entries"][existing_key]["source"] == setup.source
    assert current["entries"][existing_key]["context"] == "accepted"
    new_task = setup.board.tasks[current["entries"][unsent_key]["task_id"]]
    assert source["head_sha"] in new_task["description"] and source["base_sha"] in new_task["description"]
    assert source["run_marker"] in new_task["description"]
    assert task["done"] is True and task["lane"] == "done"
    assert {label["title"] for label in task["labels"]} == {"bug", "review-followup", "review-optional", "core"}
    assert "accepted" in task["description"] and source["head_sha"] in task["description"]
    calls = list(setup.board.calls)
    assert scan(setup) == {"ok": True}
    assert setup.board.calls == calls
    assert sum(name == "tracker_create_task" for name, _ in calls) == 2


def test_legacy_settled_closed_intent_retries_through_reopen_until_manual_merge(setup: Any) -> None:
    optional = finding()
    setup.receipt([optional])
    key = journal.finding_key(REPOSITORY, 7, optional)
    journal.save(setup.path, closed_legacy_record(setup, {
        key: {"state": "cancelled", "source": setup.source, "finding": optional, "context": "closed-unmerged"},
    }))
    assert json.loads(setup.path.read_text())["settled"] is True
    original_read = setup.forge._read_pr_state
    reads = []

    def read(context: Any, pr: int) -> dict[str, Any]:
        assert json.loads(setup.path.read_text())["settled"] is False
        reads.append(pr)
        return original_read(context, pr)

    setup.forge._read_pr_state = read
    setup.forge.merged = False
    assert scan(setup) == {"ok": True}
    assert reads and journal.read(setup.path)["diagnostic"] == "closed-unmerged"
    assert setup.path in delivery.pending_paths(setup.root)
    assert setup.board.calls == []
    setup.forge.state = "open"
    assert scan(setup) == {"ok": True}
    assert journal.read(setup.path)["diagnostic"] == "waiting-for-merge" and setup.board.calls == []
    setup.forge.state, setup.forge.merged = "closed", True
    assert scan(setup) == {"ok": True}
    assert len(setup.board.tasks) == 1 and journal.read(setup.path)["settled"] is True


@pytest.mark.parametrize("diagnostic", ["complete", "excluded"])
def test_legacy_normalization_keeps_merged_and_excluded_journals_settled(setup: Any, diagnostic: str) -> None:
    record = closed_legacy_record(setup, {})
    record.update(pr_state="merged", diagnostic=diagnostic)
    journal.save(setup.path, record)
    assert journal.read(setup.path)["settled"] is True
    assert delivery.pending_paths(setup.root) == []
    setup.forge._read_pr_state = lambda *_args: pytest.fail("terminal journal consulted forge")
    assert scan(setup) == {"ok": True}
    assert json.loads(setup.path.read_text())["settled"] is True
    assert setup.board.calls == []


def prepare_discovery(setup: Any) -> None:
    setup.path.unlink()
    path, checkpoint = journal.discovery_checkpoint(setup.root)
    checkpoint["activated_at"] = "2020-01-01T10:00:00+00:00"
    journal.save(path, checkpoint)


def scan(setup: Any) -> dict[str, Any]:
    return asyncio.run(delivery.run(seconds=5, gate=setup.forge, client=setup.board,
                                    root=setup.root, receipt_root=setup.receipt_path.parent))


def test_receipt_only_manual_merge_is_discovered_with_old_review_and_restart(setup: Any) -> None:
    setup.receipt([finding()])
    os.utime(setup.receipt_path, (1_500_000_000, 1_500_000_000))
    prepare_discovery(setup)
    assert not setup.path.exists()
    assert scan(setup) == {"ok": True}
    assert len(setup.board.tasks) == 1
    _, checkpoint = journal.discovery_checkpoint(setup.root)
    assert checkpoint["activated_at"] == "2020-01-01T10:00:00+00:00"
    assert checkpoint["cursor"] == setup.receipt_path.name
    assert scan(setup) == {"ok": True}
    assert len(setup.board.tasks) == 1
    assert journal.discovery_checkpoint(setup.root)[1]["activated_at"] == checkpoint["activated_at"]
    assert journal.read(setup.path)["settled"] is True


def test_discovery_revisits_open_receipt_when_operator_merges_later(setup: Any) -> None:
    setup.receipt([finding()])
    prepare_discovery(setup)
    setup.forge.state, setup.forge.merged = "open", False
    scan(setup)
    assert not setup.path.exists() and setup.board.calls == []
    setup.forge.state, setup.forge.merged = "closed", True
    scan(setup)
    scan(setup)
    assert len(setup.board.tasks) == 1


@pytest.mark.parametrize("mode", ["historical", "closed-unmerged", "train", "renovate-branch", "renovate-label"])
def test_postmerge_discovery_ignores_ineligible_sources(setup: Any, mode: str) -> None:
    setup.receipt([finding()])
    prepare_discovery(setup)
    if mode == "historical":
        setup.forge.merged_at = "2020-01-01T09:59:59Z"
    elif mode == "closed-unmerged":
        setup.forge.merged = False
    elif mode == "train":
        setup.forge.branch = "train/abcd-7"
    elif mode == "renovate-branch":
        setup.forge.branch = "renovate/dependency"
    else:
        setup.forge.labels = [{"name": "renovate"}]
    scan(setup)
    scan(setup)
    assert setup.board.calls == []
    if setup.path.exists():
        assert journal.read(setup.path)["diagnostic"] == "excluded"
        assert journal.read(setup.path)["settled"] is True


def test_foreign_receipt_filename_does_not_poison_checkpoint(setup: Any) -> None:
    setup.receipt([finding()])
    (setup.receipt_path.parent / "foreign.json").write_text(setup.receipt_path.read_text())
    prepare_discovery(setup)
    scan(setup)
    scan(setup)
    assert len(setup.board.tasks) == 1
    assert journal.discovery_checkpoint(setup.root)[1]["cursor"] == setup.receipt_path.name


def test_comment_rereview_approval_receipt_is_discovered_and_delivered(setup: Any) -> None:
    """the `-cm<set>` receipt matches the filename pattern, and
    `accepted_review_findings` finds it by the review's run marker."""
    stored = setup.receipt([finding()])
    suffixed = setup.receipt_path.with_name(f"{setup.receipt_path.stem}-cm{'e' * 64}.json")
    marker = f"gate-review-{hashlib.sha256(suffixed.stem.encode()).hexdigest()[:24]}"
    setup.source["run_marker"] = marker
    stored["run_marker"] = marker
    stored["result"]["task_id"] = marker
    snapshots._atomic_json(suffixed, stored)
    setup.receipt_path.unlink()
    prepare_discovery(setup)
    scan(setup)
    scan(setup)
    assert len(setup.board.tasks) == 1
    assert journal.discovery_checkpoint(setup.root)[1]["cursor"] == suffixed.name


def test_a_suffixed_receipt_with_another_run_marker_is_not_selected(setup: Any) -> None:
    stored = setup.receipt([finding()])
    suffixed = setup.receipt_path.with_name(f"{setup.receipt_path.stem}-cm{'e' * 64}.json")
    snapshots._atomic_json(suffixed, {**stored, "run_marker": "gate-review-" + "0" * 24})
    setup.receipt_path.unlink()
    setup.source["run_marker"] = "gate-review-" + "1" * 24
    assert snapshots.accepted_review_findings(setup.source)["state"] == "unavailable"


def test_failed_candidates_cannot_starve_later_receipt_across_bounded_passes(setup: Any) -> None:
    stored = setup.receipt([finding()])
    prepare_discovery(setup)
    original = setup.forge._read_pr_state
    reads = []

    def read(context: Any, pr: int) -> dict[str, Any]:
        reads.append(pr)
        if pr != 7:
            raise OSError("forge lookup unavailable")
        return original(context, pr)

    setup.forge._read_pr_state = read
    for number in range(1, 18):
        payload = {**stored, "pull_request": 100 + number}
        path = setup.receipt_path.parent / f"{number:064x}.json"
        snapshots._atomic_json(path, payload)
    scan(setup)
    assert len(reads) == 16 and 7 not in reads
    first_cursor = journal.discovery_checkpoint(setup.root)[1]["cursor"]
    assert first_cursor == f"{16:064x}.json"
    scan(setup)
    scan(setup)
    assert len(setup.board.tasks) == 1
    assert journal.read(setup.path)["settled"] is True


def test_discovery_and_existing_delivery_alternate_when_board_hangs(setup: Any) -> None:
    setup.receipt([finding()])
    checkpoint_path, checkpoint = journal.discovery_checkpoint(setup.root)
    checkpoint["next_phase"] = "deliver"
    journal.save(checkpoint_path, checkpoint)
    started = []

    async def hanging(name: str, args: dict[str, Any], **options: Any) -> Any:
        started.append(name)
        await asyncio.Event().wait()

    setup.board.call_tool = hanging
    result = asyncio.run(delivery.run(seconds=1.0, gate=setup.forge, client=setup.board,
                                      root=setup.root, receipt_root=setup.receipt_path.parent))
    assert result["code"] == "REVIEW_FOLLOWUPS_TIMEOUT" and started
    checkpoint = journal.discovery_checkpoint(setup.root)[1]
    assert checkpoint["next_phase"] == "discover" and checkpoint["cursor"] == ""
    asyncio.run(delivery.run(seconds=1.0, gate=setup.forge, client=setup.board,
                             root=setup.root, receipt_root=setup.receipt_path.parent))
    assert journal.discovery_checkpoint(setup.root)[1]["cursor"] == setup.receipt_path.name


@pytest.mark.parametrize("mode", ["changes-requested", "postmerge", "missing-time", "wrong-claim", "outdated-head"])
def test_only_last_applicable_premerge_approval_can_deliver(setup: Any, mode: str) -> None:
    setup.receipt([finding()])
    if mode == "changes-requested":
        first = setup.forge._list_pull_reviews(None, 7)[0]
        setup.forge.reviews = [first, {**first, "id": 24, "state": "REQUEST_CHANGES",
                                       "submitted_at": "2020-01-01T11:59:59Z"}]
    elif mode == "postmerge":
        setup.forge.submitted_at = "2020-01-01T12:00:01Z"
    elif mode == "missing-time":
        setup.forge.submitted_at = ""
    elif mode == "wrong-claim":
        setup.forge.claim = "d" * 64
    else:
        setup.forge.head = "d" * 40
    execute(setup)
    assert setup.board.calls == []


def test_later_postmerge_approval_cannot_replace_valid_premerge_receipt(setup: Any) -> None:
    setup.receipt([finding()])
    original = setup.forge._list_pull_reviews(None, 7)[0]
    setup.forge.reviews = [original, {**original, "id": 24, "submitted_at": "2020-01-01T12:00:01Z",
                                      "commit_id": "d" * 40}]
    assert execute(setup) == {"state": "complete"}
    assert journal.read(setup.path)["source"]["review_id"] == 23
    assert len(setup.board.tasks) == 1


@pytest.mark.parametrize("edited", [False, True])
def test_merged_clean_refresh_uses_immutable_parent_and_real_diff_proof(setup: Any, tmp_path: Path,
                                                                      edited: bool) -> None:
    repo = tmp_path / "graph"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                              text=True, timeout=60).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Synthetic Review Test")
    git("config", "user.email", "synthetic@example.invalid")
    (repo / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    reviewed_base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "feature")
    (repo / "lane.txt").write_text("reviewed\n")
    git("add", ".")
    git("commit", "-qm", "reviewed change")
    reviewed_head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (repo / "main.txt").write_text("advance\n")
    git("add", ".")
    git("commit", "-qm", "advance main")
    refreshed_base = git("rev-parse", "HEAD")
    git("checkout", "-q", "feature")
    git("merge", "--no-ff", "-qm", "refresh", "main")
    if edited:
        (repo / "lane.txt").write_text("edited after review\n")
        git("add", ".")
        git("commit", "--amend", "--no-edit", "-q")
    final_head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("merge", "--no-ff", "-qm", "merge feature", "feature")
    merged_commit = git("rev-parse", "HEAD")
    (repo / "later.txt").write_text("later main\n")
    git("add", ".")
    git("commit", "-qm", "main advances after merge")
    source = {**setup.source, "head_sha": reviewed_head, "base_sha": reviewed_base}
    setup.receipt([finding()], review_source=source)
    setup.forge.source, setup.forge.head = source, final_head
    setup.forge.cwd, setup.forge.merge_commit = repo, merged_commit
    assert delivery._refresh_base(setup.forge, setup.forge.managed_checkout_context("core-repo"),
                                  setup.forge._read_pr_state(None, 7)) == refreshed_base
    result = execute(setup)
    if edited:
        assert result == {"state": "unavailable"}
        assert setup.board.calls == []
        assert journal.read(setup.path).get("settled") is not True
    else:
        assert result == {"state": "complete"}
        assert len(setup.board.tasks) == 1
        body = setup.board.tasks[1]["description"]
        assert reviewed_head in body and reviewed_base in body and final_head not in body
        assert journal.read(setup.path)["source"]["head_sha"] == reviewed_head


@pytest.mark.parametrize("mode", ["open", "closed-unmerged", "excluded", "unavailable"])
def test_ineligible_legacy_intent_does_not_even_connect_to_board(setup: Any, monkeypatch: pytest.MonkeyPatch,
                                                               mode: str) -> None:
    setup.receipt([finding()])
    monkeypatch.setattr(delivery, "pinned_transport", lambda: pytest.fail("ineligible source connected to board"))
    if mode == "open":
        setup.forge.state, setup.forge.merged = "open", False
    elif mode == "closed-unmerged":
        setup.forge.merged = False
    elif mode == "excluded":
        setup.forge.branch = "train/abcd-7"
    else:
        setup.forge.submitted_at = ""
    state = asyncio.run(delivery.process(setup.path, setup.forge))["state"]
    assert state == ("waiting-for-merge" if mode == "open" else mode)
    assert setup.board.calls == []


def test_missing_native_review_is_retryable_until_original_approval_is_visible(setup: Any) -> None:
    setup.receipt([finding()])
    setup.forge.reviews = []
    assert execute(setup) == {"state": "unavailable"}
    assert setup.board.calls == []
    assert journal.read(setup.path).get("settled") is not True
    setup.forge.reviews = None
    assert execute(setup) == {"state": "complete"}
    assert len(setup.board.tasks) == 1
    execute(setup)
    assert len(setup.board.tasks) == 1


def test_latest_premerge_approved_round_replaces_older_findings(setup: Any) -> None:
    setup.receipt([finding(path="older.py")])
    earlier = setup.forge._list_pull_reviews(None, 7)[0]
    later_source = {**setup.source, "review_id": 24}
    setup.receipt([finding("PRE_EXISTING", "latest.py")], review_source=later_source)
    setup.forge.reviews = [earlier, {**earlier, "id": 24, "submitted_at": "2020-01-01T11:59:59Z"}]
    assert execute(setup) == {"state": "complete"}
    assert len(setup.board.tasks) == 1
    body = setup.board.tasks[1]["description"]
    assert "latest.py" in body and "older.py" not in body
    assert journal.read(setup.path)["source"]["review_id"] == 24
