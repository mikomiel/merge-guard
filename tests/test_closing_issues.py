"""Closing the issues a merge closes, using the forge's own list.

GitHub closes a pull request's linked issues only when the identity that performs
the merge holds ``issues: write``; without it the auto-close is skipped silently,
which is why bot merges left tracking issues open while human merges closed them
one second later (merge-guard#58, #37 against #44, #60). The engine therefore
repeats the close from ``closingIssuesReferences`` -- GitHub's own parse of the
body -- and never models Markdown itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from test_runner import HEAD, Fake, policy, pull

from merge_guard import engine as runner
from merge_guard import github as github_adapter

ROOT = Path(__file__).resolve().parents[1]


def _merge(closing=None, issues=None, close_fails=False):
    fake = Fake(prs=[pull()], issues=issues or {}, close_fails=close_fails,
                closing=closing or [])
    return fake, runner.run_merge(fake, policy(), apply=True)


# Through the engine ---------------------------------------------------------------


def test_a_merge_closes_the_open_issue_the_forge_lists():
    fake, decisions = _merge(closing=[7], issues={7: {"state": "open"}})
    assert decisions[0]["merged"] is True
    assert decisions[0]["issues_closed"] == [7]
    assert fake.closed == [7]


@pytest.mark.parametrize(
    ("closing", "issues"),
    [
        ([8], {8: {"state": "open", "pull_request": {"url": "u"}}}),
        ([9], {9: {"state": "closed"}}),
        ([1], {1: {"state": "open"}}),
        ([10], {}),
    ],
)
def test_the_engine_closes_nothing_it_cannot_justify(closing, issues):
    fake, decisions = _merge(closing=closing, issues=issues)
    assert decisions[0]["merged"] is True
    assert "issues_closed" not in decisions[0]
    assert fake.closed == []


def test_a_refused_close_is_reported_and_the_merge_is_not_retried():
    fake, decisions = _merge(closing=[11], issues={11: {"state": "open"}}, close_fails=True)
    assert decisions[0]["merged"] is True
    assert "issues_closed" not in decisions[0]
    assert decisions[0]["issues_unclosed"] == [11]
    assert fake.merged == [(1, HEAD)]


def test_a_forge_that_lists_nothing_closes_nothing():
    fake, decisions = _merge()
    assert decisions[0]["merged"] is True
    assert "issues_closed" not in decisions[0] and "issues_unclosed" not in decisions[0]
    assert fake.closed == []


def test_a_failed_cleanup_read_keeps_the_merge_and_closes_nothing():
    class Exploding(Fake):
        def closing_issues(self, number):
            raise runner.ApiError("closing_issues_unreadable")

    fake = Exploding(prs=[pull()], issues={12: {"state": "open"}})
    decisions = runner.run_merge(fake, policy(), apply=True)
    assert decisions[0]["merged"] is True
    # Unread is not "names nothing": the two outcomes stay distinguishable.
    assert decisions[0]["issues_unread"] is True
    assert "issues_closed" not in decisions[0] and "issues_unclosed" not in decisions[0]
    assert fake.closed == []


def test_the_cleanup_stops_after_its_request_budget():
    numbers = list(range(100, 100 + runner.MAX_CLOSES + 3))
    fake = Fake(prs=[pull()], issues={n: {"state": "open"} for n in numbers}, closing=numbers)
    decisions = runner.run_merge(fake, policy(), apply=True)
    assert decisions[0]["issues_closed"] == numbers[: runner.MAX_CLOSES]
    assert decisions[0]["issues_unclosed"] == numbers[runner.MAX_CLOSES :]
    assert fake.closed == numbers[: runner.MAX_CLOSES]


def test_the_cleanup_stops_at_its_deadline(monkeypatch):
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks, 100.0))
    numbers = [100, 101, 102]
    fake = Fake(prs=[pull()], issues={n: {"state": "open"} for n in numbers}, closing=numbers)
    decisions = runner.run_merge(fake, policy(), apply=True)
    assert decisions[0]["merged"] is True
    assert decisions[0]["issues_closed"] == [100]
    assert decisions[0]["issues_unclosed"] == [101, 102]
    assert fake.closed == [100]


def test_a_dry_run_closes_nothing():
    fake = Fake(prs=[pull()], issues={12: {"state": "open"}}, closing=[12])
    decisions = runner.run_merge(fake, policy(), apply=False)
    assert decisions[0]["merged"] is False
    assert "issues_closed" not in decisions[0]
    assert fake.closed == []


# The permission the forge needs ----------------------------------------------------


def test_the_merge_workflow_grants_issues_write():
    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "merge-guard-merge.yml").read_text(encoding="utf-8"))
    assert document["permissions"]["issues"] == "write"


def test_the_consumer_example_grants_the_same_permissions():
    """A consumer copies the example, so a missing scope there disables closing."""
    example = yaml.safe_load((ROOT / "examples" / "merge.yml").read_text(encoding="utf-8"))
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "merge-guard-merge.yml").read_text(encoding="utf-8"))
    assert example["permissions"] == workflow["permissions"]
    assert example["permissions"]["issues"] == "write"


# The adapter's read and write paths -------------------------------------------------


def _graphql(payload, captured=None):
    def fake_run(command, **kwargs):
        if captured is not None:
            captured.append(command)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(payload), stderr="")
    return fake_run


def test_the_github_adapter_reads_the_closing_references(monkeypatch):
    captured: list[list[str]] = []
    payload = {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {"number": 7, "repository": {"nameWithOwner": "o/r"}},
            {"number": 8, "repository": {"nameWithOwner": "other/tracker"}},
            {"number": 9, "repository": {"nameWithOwner": "o/r"}},
        ]}}}}}
    monkeypatch.setattr(github_adapter.subprocess, "run", _graphql(payload, captured))
    # A foreign reference keeps its own repository: only local numbers come back.
    assert github_adapter.GitHub(policy(), apply=False).closing_issues(7) == [7, 9]
    command = " ".join(captured[0])
    assert "graphql" in command
    assert "closingIssuesReferences" in command
    assert "name=r" in command and "owner=o" in command


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {"repository": None}},
        {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [{}]}}}}},
        {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [{"number": 0}]}}}}},
        {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [{"number": 7}]}}}}},
        {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [
            {"number": 7, "repository": {"nameWithOwner": ""}}]}}}}},
    ],
)
def test_the_github_adapter_refuses_an_unusable_list(monkeypatch, payload):
    monkeypatch.setattr(github_adapter.subprocess, "run", _graphql(payload))
    with pytest.raises(runner.ApiError):
        github_adapter.GitHub(policy(), apply=False).closing_issues(7)


def _pages(pages, captured=None):
    """Answer successive GraphQL pages by the ``after=`` cursor in the command."""

    def fake_run(command, **kwargs):
        if captured is not None:
            captured.append(command)
        cursor = next((item.split("=", 1)[1] for item in command if item.startswith("after=")), None)
        payload = pages.get(cursor or "")
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(payload), stderr="")
    return fake_run


def _page(nodes, has_next, cursor=None):
    return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes}}}},}


def test_the_github_adapter_refuses_a_second_page(monkeypatch):
    """Following pages is cleanup the job cannot afford; a longer list is refused."""
    captured: list[list[str]] = []
    pages = {
        "": _page([{"number": 8, "repository": {"nameWithOwner": "other/tracker"}}], True, "c1"),
        "c1": _page([{"number": 7, "repository": {"nameWithOwner": "o/r"}}], False),
    }
    monkeypatch.setattr(github_adapter.subprocess, "run", _pages(pages, captured))
    with pytest.raises(runner.ApiError, match="closing_issues_incomplete"):
        github_adapter.GitHub(policy(), apply=False).closing_issues(7)
    assert len(captured) == 1


def test_the_github_adapter_refuses_an_endless_list(monkeypatch):
    captured: list[list[str]] = []
    page = _page([], True, "c1")
    monkeypatch.setattr(github_adapter.subprocess, "run", _pages({"": page, "c1": page}, captured))
    with pytest.raises(runner.ApiError, match="closing_issues_incomplete"):
        github_adapter.GitHub(policy(), apply=False).closing_issues(7)
    assert len(captured) == github_adapter.MAX_CLOSING_PAGES


@pytest.mark.parametrize(
    "stdout",
    ["not json", "[" * 200000 + "]" * 200000, "{\"data\": {}}"],
)
def test_the_github_adapter_refuses_a_bad_answer(monkeypatch, stdout):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(github_adapter.subprocess, "run", fake_run)
    with pytest.raises(runner.ApiError):
        github_adapter.GitHub(policy(), apply=False).closing_issues(7)


def test_the_github_adapter_refuses_a_timeout(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr(github_adapter.subprocess, "run", fake_run)
    with pytest.raises(runner.ApiError):
        github_adapter.GitHub(policy(), apply=False).closing_issues(7)


def test_the_github_adapter_refuses_a_failed_read(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(github_adapter.subprocess, "run", fake_run)
    with pytest.raises(runner.ApiError):
        github_adapter.GitHub(policy(), apply=False).closing_issues(7)


def test_the_github_adapter_refuses_to_close_without_apply():
    with pytest.raises(runner.ApiError):
        github_adapter.GitHub(policy(), apply=False).close_issue(7)


def test_the_github_adapter_sends_the_close_and_requires_the_closed_state(monkeypatch):
    captured: list[list[str]] = []
    monkeypatch.setattr(
        github_adapter.subprocess, "run",
        _graphql({"number": 7, "state": "closed"}, captured))
    github_adapter.GitHub(policy(), apply=True).close_issue(7)
    command = captured[0]
    assert "PATCH" in command
    assert any(part.endswith("/issues/7") for part in command)
    assert command[-2:] == ["--input", "-"]


def test_the_github_adapter_refuses_an_unproven_close(monkeypatch):
    monkeypatch.setattr(
        github_adapter.subprocess, "run",
        _graphql({"number": 7, "state": "open"}))
    with pytest.raises(runner.ApiError):
        github_adapter.GitHub(policy(), apply=True).close_issue(7)
