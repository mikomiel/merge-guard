"""The engine and the adapters around the reviewed-head rule.

With ``[review] require_reviewed_head`` on, the engine reads the body and the edit
state of each counted approval from one answer of the forge, attaches them to the
review, and only then asks for a verdict. A switch that is off never reads them.
``docs/design/reviewed-head.md`` owns the rule; ``tests/test_reviewed_head.py``
covers the verdict itself.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest
from test_runner import HEAD, OTHER, REVIEWER_ID, Fake, policy, pull

from merge_guard import engine as runner
from merge_guard import forgejo
from merge_guard import github as github_adapter
from merge_guard import policy as mg
from merge_guard.forge import Forge

OLD = "c" * 40
LINE = f"Reviewed-Head: {HEAD}"
EDITED_AT = "2026-11-18T10:00:00Z"
SUBMITTED_AT = "2026-11-11T07:30:30Z"
MAINTAINER_ID = 99


def strict():
    return mg.load_policy({
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {"reviewer_id": REVIEWER_ID, "reviewer_login": "reviewer[bot]",
                   "required_checks": ["checks"], "gate_context": "review",
                   "carry_unchanged_diff": False, "require_reviewed_head": True},
        "paths": {"ordinary": ["README.md"]},
    })


def record(body=LINE, edited=None):
    return {"body": body, "last_edited_at": edited}


def rest(rid, state="APPROVED", commit=HEAD, maintainer=False):
    """A REST review of the configured reviewer, or of a verified maintainer."""
    user = ({"id": MAINTAINER_ID, "login": "maint", "type": "User"} if maintainer
            else {"id": REVIEWER_ID, "login": "reviewer[bot]", "type": "Bot"})
    return {"id": rid, "state": state, "commit_id": commit, "submitted_at": SUBMITTED_AT,
            "user": user}


class Edits(Fake):
    """A forge that answers the edit-state read: one answer per ask, the last repeats."""

    def __init__(self, answers, **kwargs):
        super().__init__(**kwargs)
        self.answers = answers
        self.asked: list[int] = []

    def review_edit_state(self, number):
        self.asked.append(number)
        answer = self.answers[min(len(self.asked), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Silent(Fake):
    """A forge that must never be asked for the edit state."""

    def review_edit_state(self, number):
        raise AssertionError("the edit state was read")


def publish(fake, rule=None):
    runner.run_review(fake, rule or strict(), apply=True, event_head=None)
    return fake.published


# Review mode -----------------------------------------------------------------------


def test_review_publishes_success_for_an_attested_approval():
    fake = Edits([{1: record()}])
    assert publish(fake) == [(HEAD, "success", "approved_at_head")]
    assert fake.asked == [1]


@pytest.mark.parametrize(
    ("body", "edited", "code"),
    [
        ("", None, "reviewed_head_missing"),
        (" " + LINE, None, "reviewed_head_malformed"),
        (f"Reviewed-Head: {OTHER}", None, "reviewed_head_mismatch"),
        (LINE, EDITED_AT, "reviewed_head_edited"),
    ],
)
def test_review_publishes_the_reason_an_approval_was_set_aside(body, edited, code):
    fake = Edits([{1: record(body, edited)}])
    assert publish(fake) == [(HEAD, "pending", code)]


@pytest.mark.parametrize(
    ("reviews", "state", "reason"),
    [
        ([], "pending", "current_head_approval_required"),
        ([rest(1, "COMMENTED")], "pending", "current_head_approval_required"),
        ([rest(1, commit=OLD)], "pending", "current_head_approval_required"),
        ([rest(1, "CHANGES_REQUESTED")], "failure", "changes_requested"),
    ],
)
def test_a_pull_request_with_no_counted_approval_asks_the_forge_nothing(reviews, state, reason):
    fake = Silent(reviews={"default": reviews})
    assert publish(fake) == [(HEAD, state, reason)]


@pytest.mark.parametrize("answer", [{}, {2: record()}, {"1": record()}])
def test_a_counted_approval_missing_from_the_answer_is_an_operation_failure(answer):
    fake = Edits([answer])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "pending", "review_snapshot_unavailable")]


@pytest.mark.parametrize(
    "bad",
    [None, "text", [], {}, {"body": LINE}, {"last_edited_at": None},
     {"body": None, "last_edited_at": None}, {"body": 5, "last_edited_at": None},
     {"body": LINE, "last_edited_at": 5}, {"body": LINE, "last_edited_at": False}],
)
def test_an_unusable_record_is_an_operation_failure_never_an_unedited_review(bad):
    fake = Edits([{1: bad}])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "pending", "review_snapshot_unavailable")]


@pytest.mark.parametrize("answer", [None, [], [record()], "text", 7])
def test_an_answer_that_is_not_a_table_is_an_operation_failure(answer):
    fake = Edits([answer])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "pending", "review_snapshot_unavailable")]


def test_an_error_of_the_adapter_stops_the_run_and_revokes_the_head():
    fake = Edits([runner.ApiError("review_edit_state_unusable")])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "pending", "review_snapshot_unavailable")]


def test_a_dry_run_reads_the_edit_state_and_publishes_nothing():
    fake = Edits([{1: record()}])
    result = runner.run_review(fake, strict(), apply=False, event_head=None)
    assert result == {"heads": {HEAD: ("success", "approved_at_head")}}
    assert fake.published == [] and fake.asked == [1]


# Merge mode ------------------------------------------------------------------------


def test_merge_reads_the_edit_state_again_just_before_it_merges():
    fake = Edits([{1: record()}])
    decisions = runner.run_merge(fake, strict(), apply=True)
    assert decisions[0]["merged"] is True
    assert fake.merged == [(1, HEAD)]
    assert fake.asked == [1, 1]


def test_an_edit_after_the_first_read_stops_the_merge():
    fake = Edits([{1: record()}, {1: record(edited=EDITED_AT)}])
    decisions = runner.run_merge(fake, strict(), apply=True)
    assert decisions[0]["eligible"] is False and decisions[0]["merged"] is False
    assert decisions[0]["reason"] == "reviewed_head_edited"
    assert fake.merged == [] and fake.asked == [1, 1]


def test_a_new_line_after_the_first_read_is_judged_on_what_the_second_read_says():
    fake = Edits([{1: record()}, {1: record(f"Reviewed-Head: {OTHER}")}])
    decisions = runner.run_merge(fake, strict(), apply=True)
    assert decisions[0]["reason"] == "reviewed_head_mismatch"
    assert fake.merged == []


def test_an_unreadable_second_read_stops_the_merge_run():
    fake = Edits([{1: record()}, runner.ApiError("review_edit_state_unusable")])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_merge(fake, strict(), apply=True)
    assert fake.merged == []


def test_a_second_read_that_lacks_a_counted_approval_stops_the_merge_run():
    fake = Edits([{1: record()}, {}])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_merge(fake, strict(), apply=True)
    assert fake.merged == []


def test_a_merge_dry_run_decides_the_same_way():
    fake = Edits([{1: record()}])
    decisions = runner.run_merge(fake, strict(), apply=False)
    assert decisions[0]["eligible"] is True and decisions[0]["merged"] is False
    assert fake.merged == []


# Maintainers -----------------------------------------------------------------------


MIXED = {"default": [rest(1), rest(2, maintainer=True)]}


def test_a_maintainer_approval_that_attests_is_enough_to_publish_success():
    fake = Edits([{1: record(""), 2: record()}], reviews=MIXED)
    assert publish(fake) == [(HEAD, "success", "approved_at_head")]
    assert fake.asked == [1]


def test_automerge_is_refused_when_only_the_maintainer_approval_attests():
    fake = Edits([{1: record(""), 2: record()}], reviews=MIXED)
    decisions = runner.run_merge(fake, strict(), apply=True)
    assert decisions[0]["reason"] == "configured_reviewer_required_for_automerge"
    assert decisions[0]["merged"] is False and fake.merged == []


def test_a_maintainer_approval_without_the_line_does_not_count():
    fake = Edits([{1: record(""), 2: record("Looks good.")}], reviews=MIXED)
    assert publish(fake) == [(HEAD, "pending", "reviewed_head_missing")]


def test_every_counted_approval_needs_its_own_record():
    fake = Edits([{1: record()}], reviews=MIXED)
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)


def test_an_approval_by_the_author_or_an_unverified_user_needs_no_record():
    outsider = {**rest(3, maintainer=True), "user": {"id": 7, "login": "other", "type": "User"}}
    author = {**rest(4, maintainer=True), "user": {"id": 8, "login": "author", "type": "User"}}
    fake = Edits([{1: record()}], reviews={"default": [rest(1), outsider, author]},
                 permission={"user": {"id": 7}, "permission": "read"})
    assert publish(fake) == [(HEAD, "success", "approved_at_head")]


# What the engine trusts -----------------------------------------------------------------


def test_the_rest_body_and_a_rest_edit_state_are_never_the_line():
    forged = {**rest(1), "body": LINE, "edit_state": record()}
    fake = Edits([{1: record("")}], reviews={"default": [forged]})
    assert publish(fake) == [(HEAD, "pending", "reviewed_head_missing")]


def test_a_forged_edit_state_does_not_stand_in_for_a_missing_record():
    forged = {**rest(1), "edit_state": record()}
    fake = Edits([{}], reviews={"default": [forged]})
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)


def test_the_engine_attaches_the_record_to_the_counted_approval_only():
    reviews = [
        rest(1),                                     # superseded by 5: not counted
        rest(5),                                     # counted
        {**rest(6, commit=OLD, maintainer=True), "edit_state": record()},  # an earlier head
        rest(7, "COMMENTED"),                        # not decisive
    ]
    answer = {5: record("Looks good.", EDITED_AT), 1: record(), 6: record(), 7: record()}
    fake = Edits([answer])
    attached = runner.with_edit_state(fake, pull(), reviews, {MAINTAINER_ID}, strict())
    assert fake.asked == [1]
    assert [item["id"] for item in attached] == [1, 5, 6, 7]
    assert {item["id"] for item in attached if "edit_state" in item} == {5}
    assert attached[1]["edit_state"] == record("Looks good.", EDITED_AT)
    # A key of that name that the review already carried is dropped, not trusted.
    assert "edit_state" not in attached[2]


def test_the_engine_hands_out_copies_and_leaves_its_inputs_alone():
    reviews = [rest(1), {**rest(2, maintainer=True), "edit_state": record()}]
    answer = {1: record()}
    attached = runner.with_edit_state(Edits([answer]), pull(), reviews, set(), strict())
    assert attached is not reviews
    assert all(new is not old for new, old in zip(attached, reviews, strict=True))
    assert "edit_state" not in reviews[0] and "edit_state" in reviews[1]
    attached[0]["edit_state"]["body"] = "changed"
    assert answer[1]["body"] == LINE


def test_a_snapshot_carries_the_record_on_each_counted_approval():
    snapshot = runner.collect_reviews(Edits([{1: record()}]), set(), strict())
    assert snapshot[0]["reviews"][0]["edit_state"] == record()
    assert mg.review_plan(snapshot, strict()) == {HEAD: ("success", "approved_at_head")}


# The switch off --------------------------------------------------------------------------


def test_with_the_switch_off_the_forge_is_not_asked_and_the_reviews_are_the_same_list():
    reviews = [rest(1)]
    assert runner.with_edit_state(Silent(), pull(), reviews, set(), policy()) is reviews


def test_with_the_switch_off_nothing_changes_for_review_and_merge():
    forged = {**rest(1), "body": "Looks good.", "edit_state": record("", EDITED_AT)}
    fake = Silent(reviews={"default": [forged]})
    assert publish(fake, policy()) == [(HEAD, "success", "approved_at_head")]
    decisions = runner.run_merge(Silent(reviews={"default": [forged]}), policy(), apply=True)
    assert decisions[0]["merged"] is True


def test_a_forge_without_the_method_still_serves_every_switch_off_run():
    assert not hasattr(Fake, "review_edit_state")
    assert publish(Fake(), policy()) == [(HEAD, "success", "approved_at_head")]


# No carry under the switch ------------------------------------------------------------------


class NoCompare(Fake):
    def compare(self, base, head):
        raise AssertionError("the comparison was read")


def test_the_carry_is_not_computed_under_the_switch():
    # The loader refuses this pair, so build it directly: a caller of the library can.
    both = replace(policy(), require_reviewed_head=True)
    assert both.carry_unchanged_diff is True
    stale = [rest(1, commit=OLD)]
    assert runner.carried_heads(NoCompare(), pull(), stale, both) == set()
    # Control: with the switch off the same inputs do reach the comparison.
    with pytest.raises(AssertionError, match="the comparison was read"):
        runner.carried_heads(NoCompare(), pull(), stale, policy())


# The adapters --------------------------------------------------------------------------------


def test_the_port_names_the_read_and_both_adapters_answer_it():
    assert callable(Forge.review_edit_state)
    assert callable(github_adapter.GitHub.review_edit_state)
    assert callable(forgejo.Forgejo.review_edit_state)


def test_the_forgejo_adapter_refuses_rather_than_report_an_unedited_review():
    named = mg.load_policy({
        "repository": {"name": "o/r", "base_branch": "main", "forge": "forgejo"},
        "review": {"reviewer_id": 1, "reviewer_login": "r[bot]", "required_checks": ["c"]}})
    api = forgejo.Forgejo(named, "https://forgejo.example", "token")
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        api.review_edit_state(1)


def node(identity, body=LINE, edited=None):
    return {"fullDatabaseId": str(identity), "body": body, "lastEditedAt": edited}


def reviews_page(nodes, more=False, cursor=None):
    return {"data": {"repository": {"pullRequest": {"reviews": {
        "pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}}}}}


def answering(payload, captured=None):
    def fake_run(command, **kwargs):
        if captured is not None:
            captured.append(command)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(payload), stderr="")
    return fake_run


def by_cursor(pages, captured=None):
    """Answer successive pages by the ``after=`` cursor in the command."""

    def fake_run(command, **kwargs):
        if captured is not None:
            captured.append(command)
        cursor = next((part.split("=", 1)[1] for part in command if part.startswith("after=")), "")
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=json.dumps(pages[cursor]), stderr="")
    return fake_run


def read(monkeypatch, run, number=7):
    monkeypatch.setattr(github_adapter.subprocess, "run", run)
    return github_adapter.GitHub(strict(), apply=False).review_edit_state(number)


def test_the_adapter_reads_each_body_with_its_edit_time_from_one_node(monkeypatch):
    captured: list[list[str]] = []
    payload = reviews_page([node(11), node(12, "Looks good.", EDITED_AT)])
    states = read(monkeypatch, answering(payload, captured))
    assert states == {11: record(), 12: record("Looks good.", EDITED_AT)}
    assert all(type(key) is int for key in states)
    assert len(captured) == 1
    command = " ".join(captured[0])
    for needle in ("graphql", "reviews(first:100", "fullDatabaseId body lastEditedAt",
                   "owner=o", "name=r", "number=7"):
        assert needle in command
    # An edit is read from lastEditedAt and nothing else, and the read writes nothing.
    assert "userContentEdits" not in command and "mutation" not in command


def test_the_adapter_reads_an_integer_id_the_same_way(monkeypatch):
    payload = reviews_page([{"fullDatabaseId": 11, "body": LINE, "lastEditedAt": None}])
    assert read(monkeypatch, answering(payload)) == {11: record()}


def test_the_adapter_follows_the_cursor_to_the_last_page(monkeypatch):
    captured: list[list[str]] = []
    pages = {"": reviews_page([node(1)], True, "c1"),
             "c1": reviews_page([node(2, "x", EDITED_AT)], False)}
    states = read(monkeypatch, by_cursor(pages, captured))
    assert states == {1: record(), 2: record("x", EDITED_AT)}
    assert len(captured) == 2
    assert not any(part.startswith("after=") for part in captured[0])
    assert "after=c1" in captured[1]


def test_the_adapter_refuses_a_list_that_does_not_end_within_the_bound(monkeypatch):
    captured: list[list[str]] = []
    more = reviews_page([], True, "c1")
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, by_cursor({"": more, "c1": more}, captured))
    assert len(captured) == github_adapter.MAX_PAGES


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        "text",
        {"data": None},
        {"data": {"repository": None}},
        {"data": {"repository": {"pullRequest": None}}},
        {"data": {"repository": {"pullRequest": {"reviews": None}}}},
        {"data": {"repository": {"pullRequest": {"reviews": {"nodes": None, "pageInfo": {
            "hasNextPage": False, "endCursor": None}}}}}},
        {"data": {"repository": {"pullRequest": {"reviews": {"nodes": [node(1)]}}}}},
        {**reviews_page([node(1)]), "errors": [{"message": "partial"}]},
        reviews_page([node(1)], True, None),
        reviews_page([node(1)], True, ""),
        reviews_page([node(1)], True, 5),
        reviews_page([node(1)], "false"),
        reviews_page([node(1)], None),
    ],
)
def test_the_adapter_refuses_an_unusable_answer(monkeypatch, payload):
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, answering(payload))


GOOD = {"fullDatabaseId": "1", "body": LINE, "lastEditedAt": None}


def without(key):
    return {name: value for name, value in GOOD.items() if name != key}


@pytest.mark.parametrize(
    "bad",
    [
        None, [], "1", 1, {},
        without("fullDatabaseId"), without("body"), without("lastEditedAt"),
        {**GOOD, "fullDatabaseId": "0"}, {**GOOD, "fullDatabaseId": "01"},
        {**GOOD, "fullDatabaseId": "-1"}, {**GOOD, "fullDatabaseId": "1.5"},
        {**GOOD, "fullDatabaseId": " 1"}, {**GOOD, "fullDatabaseId": "1 "},
        {**GOOD, "fullDatabaseId": ""}, {**GOOD, "fullDatabaseId": "١٢٣"},
        {**GOOD, "fullDatabaseId": "1234567890123456789"},
        {**GOOD, "fullDatabaseId": ["1"]}, {**GOOD, "fullDatabaseId": None},
        {**GOOD, "fullDatabaseId": 0}, {**GOOD, "fullDatabaseId": -1},
        {**GOOD, "fullDatabaseId": True}, {**GOOD, "fullDatabaseId": 1.0},
        {**GOOD, "body": None}, {**GOOD, "body": 5}, {**GOOD, "body": ["x"]},
        {**GOOD, "lastEditedAt": ""}, {**GOOD, "lastEditedAt": 5},
        {**GOOD, "lastEditedAt": True}, {**GOOD, "lastEditedAt": []},
    ],
)
def test_the_adapter_refuses_a_review_it_cannot_read_completely(monkeypatch, bad):
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, answering(reviews_page([node(2), bad])))


def test_the_adapter_refuses_a_review_that_is_named_twice(monkeypatch):
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, answering(reviews_page([node(1), node(1, "x")])))
    pages = {"": reviews_page([node(1)], True, "c1"), "c1": reviews_page([node(1)])}
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, by_cursor(pages))


def stdout_of(text, returncode=0):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=text, stderr="")
    return fake_run


@pytest.mark.parametrize(
    "run",
    [
        stdout_of("", returncode=1),
        stdout_of(json.dumps(reviews_page([node(1)])), returncode=1),
        stdout_of("not json"),
        stdout_of("[" * 200000 + "]" * 200000),
        stdout_of('{"data": ' + "9" * 5000 + "}"),
        stdout_of("{}"),
    ],
)
def test_the_adapter_refuses_a_failed_or_unreadable_read(monkeypatch, run):
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, run)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(cmd="gh", timeout=30),
                                     FileNotFoundError("gh")])
def test_the_adapter_refuses_a_read_that_did_not_finish(monkeypatch, failure):
    def fake_run(command, **kwargs):
        raise failure

    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, fake_run)


def test_the_adapter_refuses_an_answer_over_the_size_bound(monkeypatch):
    monkeypatch.setattr(github_adapter, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        read(monkeypatch, answering(reviews_page([node(1, "x" * 200)])))


@pytest.mark.parametrize("number", [0, -1, True, "7", None, 7.0])
def test_the_adapter_refuses_a_pull_number_that_is_not_one_before_any_read(monkeypatch, number):
    def fake_run(command, **kwargs):
        raise AssertionError("gh ran")

    with pytest.raises(runner.ApiError, match="pull_number_unusable"):
        read(monkeypatch, fake_run, number)


class Wired(Fake):
    """A forge whose edit-state read is the GitHub adapter's, over a stubbed ``gh``."""

    def __init__(self, monkeypatch, nodes, **kwargs):
        super().__init__(**kwargs)
        monkeypatch.setattr(github_adapter.subprocess, "run", answering(reviews_page(nodes)))
        self.adapter = github_adapter.GitHub(strict(), apply=False)

    def review_edit_state(self, number):
        return self.adapter.review_edit_state(number)


def test_the_answer_of_the_adapter_joins_to_the_rest_review_by_id(monkeypatch):
    # The engine joins on the REST id, an int; GraphQL names it as a decimal string.
    fake = Wired(monkeypatch, [node(1), node(2, "x", EDITED_AT)])
    assert publish(fake) == [(HEAD, "success", "approved_at_head")]
    fake = Wired(monkeypatch, [node(1, edited=EDITED_AT)])
    assert publish(fake) == [(HEAD, "pending", "reviewed_head_edited")]
    fake = Wired(monkeypatch, [node(1, "")])
    assert publish(fake) == [(HEAD, "pending", "reviewed_head_missing")]


def test_a_review_the_adapter_does_not_name_is_a_failed_run_not_an_unedited_review(monkeypatch):
    fake = Wired(monkeypatch, [node(2)])
    with pytest.raises(runner.ApiError, match="review_edit_state_unusable"):
        runner.run_review(fake, strict(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "pending", "review_snapshot_unavailable")]
