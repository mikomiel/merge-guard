"""Queue priority in the automerge executor.

The executor orders each pass high, normal, then low, elects by readiness inside
each group, keeps the Renovate slot inside each group, defers `queue/low` while
the queue is deep, and honours `queue/high` only from the operator's own forge
account.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_gate.contracts import MAX_PAGES, GateError
from git_gate.forge import PAGE_SIZE, GateForgeMixin, LabelEvent, ListedPull
from merge_open_prs_fixtures import (
    FakeGateApplication,
    _core_repo,
    _in_progress,
    _queue_inspect,
    _queue_store,
    _train_repository,
    merge_open_prs,
)

HIGH, NORMAL, LOW = "queue/high", "queue/normal", "queue/low"
HOLD = "gate/hold"
OPERATOR = "acme"
GATE_ACCOUNT = "gate-bot"
# The queue depth the low-priority deferral waits for; the deferral tests build their
# boundary from it, and one test pins the decided number itself.
THRESHOLD = merge_open_prs.QUEUE_BACKPRESSURE_THRESHOLD
WRITES = {"review", "refresh", "merge"}


def _added_by(actor, added=True):
    return {HIGH: {"actor": actor, "added": added}}


class PriorityGate(FakeGateApplication):
    """The fake gate, with the listing's `queue_label_events` as the server
    adapter reports them: one entry per PR carrying `queue/high`, None when
    that PR's timeline could not be read."""

    def __init__(self, labels, *, events=None, heads=None, **kwargs):
        numbers = sorted(labels)
        super().__init__({"core-repo": numbers},
                         head_refs={"core-repo": {number: (heads or {}).get(number, "task/x")
                                                  for number in numbers}},
                         labels={"core-repo": labels}, **kwargs)
        self.events = events

    def candidates(self, repository):
        listed = super().candidates(repository)
        if self.events is not None:
            listed["queue_label_events"] = {str(pr): event for pr, event in self.events.items()}
        return listed


def _pass(tmp_path, gate, store=None, **kwargs):
    return merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store or _queue_store(tmp_path), **kwargs)[0]


def _positions(gate):
    """PR numbers in published queue order: the active PR, then eligible ones."""
    published = gate.statuses[-1][1]
    ranked = {}
    for pr, description in published.items():
        position = description.split("pos=")[1].split()[0]
        if position != "-":
            ranked[int(position)] = pr
    return [ranked[position] for position in sorted(ranked)]


def _writes(gate, pr):
    return [call for call in gate.calls if call[0] in WRITES and call[2] == pr]


def _row(store):
    return store.read()["repositories"]["core-repo"]


def _waiting(gate, *numbers):
    """Each PR waits on CI: the selected one keeps the slot and nothing merges."""
    for number in numbers:
        gate.responses[("inspect", "core-repo", number)] = _in_progress()


# ---------------------------------------------------------------- order


@pytest.mark.parametrize(("streak", "expected"), [
    # No Renovate slot due: inside each group, other PRs before Renovate's.
    (0, [3, 5, 2, 6, 4, 1, 7]),
    # A due slot moves Renovate first inside each group, never across groups.
    (1, [5, 3, 4, 2, 6, 7, 1]),
])
def test_the_queue_serves_high_then_normal_then_low(tmp_path, streak, expected):
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {},
                                              "agent_streak": streak}
        store.save(state)
    gate = PriorityGate(
        {1: [LOW], 2: [], 3: [HIGH], 4: [], 5: [HIGH], 6: [NORMAL], 7: [LOW]},
        heads={4: "renovate/a", 5: "renovate/b", 7: "renovate/c"},
        events={3: _added_by(OPERATOR), 5: _added_by(OPERATOR)})
    _waiting(gate, *range(1, 8))

    _pass(tmp_path, gate, store)

    assert _positions(gate) == expected
    assert _row(store)["active"]["pr"] == expected[0]


def test_without_queue_labels_the_order_is_unchanged(tmp_path):
    gate = PriorityGate({1: [], 2: [], 3: [], 4: []}, heads={1: "renovate/a", 3: "renovate/b"})
    _waiting(gate, 1, 2, 3, 4)

    _pass(tmp_path, gate)

    assert _positions(gate) == [2, 4, 1, 3]


@pytest.mark.parametrize(("labels", "expected"), [
    ([HIGH, LOW], [1, 2]),  # low: behind the normal PR
    ([HIGH, NORMAL], [1, 2]),  # normal: ascending among normal PRs
    ([HIGH], [2, 1]),  # control: a lone operator high goes first
])
def test_conflicting_queue_labels_take_the_lower_priority(tmp_path, labels, expected):
    gate = PriorityGate({1: [], 2: labels}, events={2: _added_by(OPERATOR)})
    _waiting(gate, 1, 2)

    _pass(tmp_path, gate)

    assert _positions(gate) == expected


def test_a_normal_and_low_conflict_sorts_behind_normal(tmp_path):
    gate = PriorityGate({1: [NORMAL, LOW], 2: []})
    _waiting(gate, 1, 2)

    _pass(tmp_path, gate)

    assert _positions(gate) == [2, 1]


# ---------------------------------------------------------------- high authority


@pytest.mark.parametrize(("actor", "expected", "untrusted"), [
    (GATE_ACCOUNT, [1, 2], [2]),  # the shared gate identity: an agent's label
    ("someone-else", [1, 2], [2]),
    (OPERATOR, [2, 1], None),
    ("ACME", [2, 1], None),  # Forgejo logins compare case-insensitively
])
def test_queue_high_counts_only_from_the_operators_account(tmp_path, actor, expected, untrusted):
    gate = PriorityGate({1: [], 2: [HIGH]}, events={2: _added_by(actor)})
    _waiting(gate, 1, 2)

    summary = _pass(tmp_path, gate)

    assert _positions(gate) == expected
    assert "priority_unverified" not in summary
    assert summary.get("priority_untrusted") == untrusted


@pytest.mark.parametrize("events", [
    {2: None},  # the gate could not read this PR's timeline
    {},  # the listing reports no event for it
    None,  # the listing carries no timeline evidence at all
    {2: {}},  # the timeline shows no `queue/high` event
    {2: _added_by(OPERATOR, added=False)},  # the newest event is a removal
    {2: {HIGH: {"added": True}}},  # an event without an actor
])
def test_an_unverified_high_label_is_normal_and_recorded(tmp_path, events):
    gate = PriorityGate({1: [], 2: [HIGH], 3: []}, events=events)
    _waiting(gate, 1, 2, 3)

    summary = _pass(tmp_path, gate)

    assert _positions(gate) == [1, 2, 3]
    assert summary["priority_unverified"] == [2]
    assert "priority_untrusted" not in summary


# ---------------------------------------------------------------- readiness

# A PR without a scripted read is approved with green CI: readiness tier 0,
# merged as soon as it is elected (`FakeGateApplication.inspect`).
# Approved, CI still running: tier 1.
APPROVED_PENDING = "approved_pending"
# Not reviewed yet, CI still running: tier 2.
UNREVIEWED = "unreviewed"


def _reads(states):
    """Scripted inspections by PR number: APPROVED_PENDING or UNREVIEWED."""
    reads = {APPROVED_PENDING: _in_progress(), UNREVIEWED: _in_progress(review="not_approved")}
    return {("inspect", "core-repo", pr): reads[state] for pr, state in states.items()}


def _merged(gate):
    return [call[2] for call in gate.calls if call[0] == "merge"]


@pytest.mark.parametrize("state", [APPROVED_PENDING, UNREVIEWED])
def test_a_trusted_high_pr_takes_the_slot_before_an_approved_green_normal_pr(tmp_path, state):
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [], 2: [HIGH]}, events={2: _added_by(OPERATOR)},
                        responses=_reads({2: state}))

    _pass(tmp_path, gate, store)

    assert _row(store)["active"]["pr"] == 2
    assert _merged(gate) == []  # The green normal PR 1 waits behind it.


@pytest.mark.parametrize("events", [
    {2: _added_by(GATE_ACCOUNT)},  # an agent's label through the shared gate account
    {2: _added_by("someone-else")},
    {2: None},  # the gate could not read this PR's timeline
    None,  # the listing carries no timeline evidence at all
])
def test_an_untrusted_or_unread_high_label_leaves_readiness_in_charge(tmp_path, events):
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [], 2: [HIGH]}, events=events,
                        responses=_reads({2: APPROVED_PENDING}))

    _pass(tmp_path, gate, store)

    # As without any label: the green PR 1 is elected and merged first.
    assert _merged(gate) == [1]
    assert _row(store)["active"]["pr"] == 2


def test_a_held_trusted_high_pr_stays_held(tmp_path):
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [], 2: [HOLD, HIGH]}, events={2: _added_by(OPERATOR)})

    _pass(tmp_path, gate, store)

    assert _row(store)["parked"]["2"]["reason"] == "author_hold"
    assert _writes(gate, 2) == []
    assert _merged(gate) == [1]


def test_a_parked_trusted_high_pr_stays_parked_until_its_evidence_changes(tmp_path):
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [], 2: [HIGH]}, events={2: _added_by(OPERATOR)})
    # The fake gate reads PR 2 exactly as it was when the park was made.
    parked = merge_open_prs._park_record(gate.inspect("core-repo", 2), "conflict",
                                         lambda: 1000.0)
    gate.calls.clear()
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"2": parked}}
        store.save(state)

    _pass(tmp_path, gate, store)

    assert _row(store)["parked"]["2"]["reason"] == "conflict"
    assert _writes(gate, 2) == []
    assert _merged(gate) == [1]


def test_a_high_pr_parked_in_its_own_pass_leaves_a_due_renovate_slot_due(tmp_path):
    """the park gives back only the 1 its election added. The slot was
    already due, so the normal group serves its Renovate PR before its agent PR."""
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {}, "agent_streak": 1}
        store.save(state)
    red = _queue_inspect(ci="red", conclusion="failure", blocking_reason="CI_RUN_FAILED")
    gate = PriorityGate({1: [HIGH], 2: [], 3: []}, events={1: _added_by(OPERATOR)},
                        heads={3: "renovate/a"}, responses={("inspect", "core-repo", 1): red})
    _waiting(gate, 2, 3)

    summary = _pass(tmp_path, gate, store)

    row = _row(store)
    assert row["parked"]["1"]["reason"] == "ci_failed"
    assert [elected["pr"] for elected in summary["elected"]] == [1, 3]
    assert (row["active"]["pr"], row["agent_streak"]) == (3, 0)


def test_an_approved_green_low_pr_waits_behind_an_unreviewed_normal_pr(tmp_path):
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [LOW], 2: []}, responses=_reads({2: UNREVIEWED}))

    _pass(tmp_path, gate, store)

    assert _row(store)["active"]["pr"] == 2
    assert _merged(gate) == []


def test_readiness_elects_inside_the_high_group_and_high_before_normal(tmp_path):
    """Two operator high PRs: the approved green one is elected first although
    its number is higher; the other high PR then takes the slot before the
    approved green normal PR 1."""
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [], 2: [HIGH], 3: [HIGH]},
                        events={2: _added_by(OPERATOR), 3: _added_by(OPERATOR)},
                        responses=_reads({2: UNREVIEWED}))

    _pass(tmp_path, gate, store)

    assert _merged(gate) == [3]
    assert _row(store)["active"]["pr"] == 2


def test_selection_reads_no_lower_group_once_a_high_pr_is_found(tmp_path):
    store = _queue_store(tmp_path)
    gate = PriorityGate({1: [], 2: [], 3: [HIGH]}, events={3: _added_by(OPERATOR)},
                        responses=_reads({3: UNREVIEWED}))

    _pass(tmp_path, gate, store)

    assert _row(store)["active"]["pr"] == 3
    assert [call[2] for call in gate.calls if call[0] == "inspect"] == [3]


def test_review_ahead_reaches_a_high_pr_before_a_normal_one(tmp_path):
    """The active high PR waits on CI and lends its review slot. The last
    scan stopped at PR 3, so a rotation over the whole queue would resume at
    the normal PR 2; the rotation inside each group reaches PR 3 first."""
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {}, "review_ahead": 3}
        store.save(state)
    gate = PriorityGate({1: [HIGH], 2: [], 3: [HIGH]},
                        events={1: _added_by(OPERATOR), 3: _added_by(OPERATOR)},
                        responses={("inspect", "core-repo", 1): _in_progress(),
                                   ("inspect", "core-repo", 2): _queue_inspect(
                                       review="not_approved"),
                                   ("inspect", "core-repo", 3): _queue_inspect(
                                       review="not_approved")})

    _pass(tmp_path, gate, store, max_reviews=1)

    assert _row(store)["active"]["pr"] == 1
    assert [call[2] for call in gate.calls if call[0] == "review"] == [3]


@pytest.mark.parametrize(("cursor", "expected"), [
    (0, [3, 5, 1, 2, 4]),  # no cursor: each group from its head
    (3, [5, 3, 4, 1, 2]),  # the rotation runs inside each group
    (1, [3, 5, 2, 4, 1]),
])
def test_the_bounded_scans_rotate_inside_each_priority_group(cursor, expected):
    priorities = {1: 1, 2: 1, 3: 0, 4: 1, 5: 0}

    assert merge_open_prs._rotating_order([1, 2, 3, 4, 5], cursor, priorities) == expected


# ---------------------------------------------------------------- low deferral


@pytest.mark.parametrize(("held", "renovate", "reviewed"), [
    # Two over the threshold in open non-Renovate PRs: low takes no review ahead.
    (range(3, THRESHOLD + 3), (), []),
    # At the threshold: low is ordered last but still served.
    (range(3, THRESHOLD + 1), (), [1]),
    # Three more open, but they are Renovate's: depth counts only the others.
    (range(3, THRESHOLD + 1), (THRESHOLD + 1, THRESHOLD + 2, THRESHOLD + 3), [1]),
])
def test_a_deep_queue_defers_low_prs(tmp_path, held, renovate, reviewed):
    labels = {1: [LOW], 2: [], **{number: [HOLD] for number in (*held, *renovate)}}
    gate = PriorityGate(labels, heads={number: f"renovate/{number}" for number in renovate},
                        responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 2): _in_progress()})

    _pass(tmp_path, gate, max_reviews=1)

    assert [call[2] for call in gate.calls if call[0] == "review"] == reviewed
    assert merge_open_prs.QUEUE_BACKPRESSURE_THRESHOLD == 15


def _rider(pr):
    """A PR whose own run passed on an older base and whose head the
    delegated reviewer approved: the train's member shape."""
    return {"ok": True, "head_sha": str(pr) * 40, "base_sha": "b" * 40, "pr_state": "open",
            "ci_state": "pending", "blocking_reason": "CI_BASE_STALE",
            "ci_reason": "CI_BASE_STALE", "next_action": "refresh_task",
            "ci_run_conclusion": "success", "ci_run_id": 10 + pr, "ci_run_attempt": 1,
            "review_state": "not_approved", "latest_delegated_review_id": 10 + pr,
            "latest_delegated_review_state": "APPROVED"}


class _TrainBuildGate(PriorityGate):
    """Records the members the pass asks to build, then refuses the build,
    so the pass falls back to its serial refresh."""

    def train_build(self, repository, members, expected_base):
        self.calls.append(("train_build", tuple(pr for pr, _head in members)))
        return {"ok": False, "code": "TRAIN_BUILD_REFUSED"}


@pytest.mark.parametrize(("last_held", "members"), [
    (THRESHOLD + 3, (1, 3)),  # three over the threshold: the low rider 2 takes no seat
    (THRESHOLD, (1, 3, 2)),  # at the threshold: low still rides, behind the normal one
])
def test_a_deep_queue_gives_a_low_pr_no_train_seat(tmp_path, last_held, members):
    labels = {1: [], 2: [LOW], 3: [], **{number: [HOLD] for number in range(4, last_held + 1)}}
    gate = _TrainBuildGate(labels, responses={
        ("inspect", "core-repo", pr): _rider(pr) for pr in (1, 2, 3)})

    merge_open_prs.run_automerge_pass([_train_repository(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path), now=lambda: 1000.0)

    assert [call[1] for call in gate.calls if call[0] == "train_build"] == [members]


@pytest.fixture
def review_polls(monkeypatch):
    """Shrink the in-review slice; `reached` opens once three slices ended
    with a review still running, so the pass had repeated lookahead turns."""
    monkeypatch.setattr(merge_open_prs, "REVIEW_TRAIN_POLL_SECONDS", 0.01)
    real_wait = merge_open_prs.wait
    polls = SimpleNamespace(count=0, reached=threading.Event())

    def counting_wait(futures, timeout=None, **kwargs):
        finished = real_wait(futures, timeout=timeout, **kwargs)
        if not finished.done:
            polls.count += 1
            if polls.count >= 3:
                polls.reached.set()
        return finished

    monkeypatch.setattr(merge_open_prs, "wait", counting_wait)
    return polls


class _LookaheadGate(PriorityGate):
    """The first review runs until three poll slices passed; any review that
    starts meanwhile is a lookahead."""

    def __init__(self, labels, polls, **kwargs):
        super().__init__(labels, **kwargs)
        self.polls = polls
        self.lock = threading.Lock()
        self.primary = None
        self.primary_done = threading.Event()
        self.lookaheads = []

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        with self.lock:
            first = self.primary is None
            if first:
                self.primary = pr
            elif not self.primary_done.is_set():
                self.lookaheads.append(pr)
        try:
            if first:
                self.calls.append(("polled", self.polls.reached.wait(timeout=10)))
            return super().review(repository, pr, head, base, executor_run, retry_of)
        finally:
            if first:
                self.primary_done.set()


@pytest.mark.parametrize(("last_held", "lookaheads"), [
    (THRESHOLD + 2, []),  # two over the threshold: the low PR gets no lookahead review
    (THRESHOLD, [2]),  # at the threshold: the same PR is reviewed beside the active one
])
def test_a_deep_queue_gives_a_low_pr_no_lookahead_review(tmp_path, review_polls,
                                                          last_held, lookaheads):
    published = {"ok": True, "code": "REVIEW_PUBLISHED", "dispatched": True,
                 "verdict": "ACCEPT_STATIC_CHANGE"}
    labels = {1: [], 2: [LOW], **{number: [HOLD] for number in range(3, last_held + 1)}}
    gate = _LookaheadGate(labels, review_polls, responses={
        ("inspect", "core-repo", 1): [_queue_inspect(review="missing"), _queue_inspect()],
        ("inspect", "core-repo", 2): _queue_inspect(head="2", review="not_approved"),
        ("review", "core-repo", 1): published, ("review", "core-repo", 2): published})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path), review_workers=2,
                                       now=lambda: 1000.0)

    assert gate.primary == 1
    assert ("polled", True) in gate.calls
    assert gate.lookaheads == lookaheads


def test_a_deep_queue_still_serves_low_when_nothing_else_is_eligible(tmp_path):
    labels = {1: [LOW], **{number: [HOLD] for number in range(2, 13)}}
    gate = PriorityGate(labels)

    _pass(tmp_path, gate)

    assert [call[0] for call in _writes(gate, 1)] == ["merge"]


# ---------------------------------------------------------------- forge timeline


def _timeline_forge(*pages):
    """A forge serving *pages* of one PR timeline, then `null`: Forgejo's
    body for a page past the end."""
    calls = []

    def list_issue_timeline(runner, cwd, repository, index, page, size):
        calls.append(page)
        served = pages[page - 1] if page <= len(pages) else None
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(served))

    forge = SimpleNamespace(_backend_for=lambda policy: SimpleNamespace(
        list_issue_timeline=list_issue_timeline), _runner=None)
    forge._pull_label_events = (
        lambda context, index, **kwargs: GateForgeMixin._pull_label_events(
            forge, context, index, **kwargs))
    context = SimpleNamespace(policy=SimpleNamespace(forge_repository="acme/core-repo"),
                              cwd=Path("/tmp/read-only"))
    return forge, context, calls


def _label(name, actor, body):
    return {"type": "label", "label": {"name": name}, "user": {"login": actor}, "body": body}


def _newest(*events):
    forge, context, _calls = _timeline_forge(list(events))
    return GateForgeMixin.newest_label_events(forge, context, 7, (HIGH, NORMAL, LOW))


def test_the_newest_label_event_names_its_actor():
    assert _newest(
        {"type": "comment", "body": "hello"},
        _label(HIGH, GATE_ACCOUNT, "1"),
        _label("renovate", "gate-bot", "1"),
        _label(LOW, OPERATOR, "1"),
    ) == {HIGH: LabelEvent(GATE_ACCOUNT, True), LOW: LabelEvent(OPERATOR, True)}


@pytest.mark.parametrize(("events", "expected"), [
    # Removed, then re-added: the re-add is the newest event.
    ([_label(HIGH, GATE_ACCOUNT, "1"), _label(HIGH, OPERATOR, ""), _label(HIGH, OPERATOR, "1")],
     LabelEvent(OPERATOR, True)),
    # The operator's add was removed and an agent re-added it.
    ([_label(HIGH, OPERATOR, "1"), _label(HIGH, OPERATOR, ""), _label(HIGH, GATE_ACCOUNT, "1")],
     LabelEvent(GATE_ACCOUNT, True)),
    ([_label(HIGH, OPERATOR, "1"), _label(HIGH, GATE_ACCOUNT, "")],
     LabelEvent(GATE_ACCOUNT, False)),
])
def test_only_the_newest_event_of_a_label_counts(events, expected):
    assert _newest(*events) == {HIGH: expected}


@pytest.mark.parametrize("event", [
    {"type": "label", "label": {"name": HIGH}, "body": "1"},  # no actor
    {"type": "label", "label": {"name": HIGH}, "user": {"login": ""}, "body": "1"},
    _label(HIGH, OPERATOR, "2"),  # neither an add nor a removal
    _label(HIGH, OPERATOR, []),  # an unhashable body fails the read, never raises
    _label(HIGH, OPERATOR, {}),
    _label(HIGH, OPERATOR, None),
    {"type": "label", "label": {}, "user": {"login": OPERATOR}, "body": "1"},  # no name
    "not an event",
])
def test_an_unproven_newest_event_fails_the_read(event):
    with pytest.raises(GateError) as exc:
        _newest(_label(HIGH, OPERATOR, "1"), event)
    assert exc.value.code == "PR_LABEL_HISTORY_UNAVAILABLE"


def test_an_older_malformed_event_of_a_requested_label_does_not_hide_the_newest():
    """Only the newest event of a requested label must name its actor."""
    assert _newest({"type": "label", "label": {"name": HIGH}, "body": "1"},
                   _label(HIGH, OPERATOR, "1")) == {HIGH: LabelEvent(OPERATOR, True)}


def test_a_malformed_event_of_a_label_nobody_asked_for_is_ignored():
    forge, context, _calls = _timeline_forge([
        {"type": "label", "label": {"name": "bug"}, "body": "1"},
        _label(HIGH, OPERATOR, "1")])

    assert GateForgeMixin.newest_label_events(forge, context, 7, (HIGH,)) == {
        HIGH: LabelEvent(OPERATOR, True)}


def test_the_newest_event_read_goes_past_a_short_page():
    """Forgejo filters timeline rows after paging: a short page is not the last."""
    forge, context, calls = _timeline_forge(
        [_label(HIGH, GATE_ACCOUNT, "1"), {"type": "comment", "body": "x"}],
        [_label(HIGH, OPERATOR, ""), _label(HIGH, OPERATOR, "1")])

    assert GateForgeMixin.newest_label_events(forge, context, 7, (HIGH,)) == {
        HIGH: LabelEvent(OPERATOR, True)}
    assert calls == [1, 2, 3]


@pytest.mark.parametrize("end", [None, []])
def test_the_newest_event_read_ends_at_a_null_or_empty_page(end):
    """Forgejo serves a page past the end as `null`, not `[]`."""
    forge, context, calls = _timeline_forge([_label(HIGH, OPERATOR, "1")], end, [
        _label(HIGH, GATE_ACCOUNT, "1")])

    assert GateForgeMixin.newest_label_events(forge, context, 7, (HIGH,)) == {
        HIGH: LabelEvent(OPERATOR, True)}
    assert calls == [1, 2]


@pytest.mark.parametrize("body", ["{}", '"x"', "7", "[[]]"])
def test_a_page_that_is_neither_a_list_nor_null_fails_the_read(body):
    forge, context, _calls = _timeline_forge()
    forge._backend_for = lambda policy: SimpleNamespace(
        list_issue_timeline=lambda *args: subprocess.CompletedProcess([], 0, stdout=body))

    with pytest.raises(GateError) as exc:
        GateForgeMixin.newest_label_events(forge, context, 7, (HIGH,))
    assert exc.value.code == "PR_LABEL_HISTORY_UNAVAILABLE"


def test_a_timeline_without_an_empty_page_within_the_bound_fails_the_read():
    forge, context, calls = _timeline_forge(*[[_label(HIGH, OPERATOR, "1")]] * MAX_PAGES)

    with pytest.raises(GateError) as exc:
        GateForgeMixin.newest_label_events(forge, context, 7, (HIGH,))
    assert exc.value.code == "PR_LABEL_HISTORY_UNAVAILABLE"
    assert calls == list(range(1, MAX_PAGES + 1))


def test_the_label_history_still_reads_every_page():
    first = [_label(f"label-{index}", OPERATOR, "1") for index in range(PAGE_SIZE)]
    forge, context, calls = _timeline_forge(first, [_label(HIGH, OPERATOR, "")])

    assert GateForgeMixin._pull_label_history(forge, context, 7) == sorted(
        {f"label-{index}" for index in range(PAGE_SIZE)} | {HIGH})
    assert calls == [1, 2]


@pytest.mark.parametrize("pages", [
    pytest.param((), id="first-page"),
    pytest.param(([_label(f"label-{index}", OPERATOR, "1") for index in range(PAGE_SIZE)],),
                 id="after-a-full-page"),
])
def test_the_label_history_still_reads_null_as_an_unreadable_page(pages):
    """The Renovate consent read is unchanged: Forgejo's `null` (a page past
    the end, or an empty or fully dropped one) fails it as before, so a
    partial history never reads as one without the hold label."""
    forge, context, _calls = _timeline_forge(*pages)

    with pytest.raises(GateError) as exc:
        GateForgeMixin._pull_label_history(forge, context, 7)
    assert exc.value.code == "PR_LABEL_HISTORY_UNAVAILABLE"


# ---------------------------------------------------------------- server adapter listing


class _ListingGate:
    """The gate's two reads the listing makes. A PR's newest `queue/high`
    event is the operator's add unless `newest` says otherwise."""

    def __init__(self, pulls, unreadable=(), newest=None):
        self.pulls = pulls
        self.unreadable = set(unreadable)
        self.newest = newest or {}
        self.reads = []

    def candidate_pulls(self, context):
        return self.pulls

    def newest_label_events(self, context, index, names):
        self.reads.append((index, tuple(names)))
        if index in self.unreadable:
            raise GateError("PR_LABEL_HISTORY_UNAVAILABLE")
        return {HIGH: self.newest.get(index, LabelEvent(OPERATOR, True))}


def _adapter_candidates(monkeypatch, gate):
    from gate_server_estate import EstateGateOperations

    operations = EstateGateOperations(executor_authority=lambda: (False, "not selected"))
    context = SimpleNamespace(policy=SimpleNamespace(), cwd=Path("/tmp/read-only"))
    monkeypatch.setattr(operations, "_gate", lambda repository: (gate, context))
    return operations.candidates("core-repo")


def test_the_listing_reads_the_timeline_of_high_prs_only(monkeypatch):
    gate = _ListingGate({7: ListedPull("task/a", frozenset({HIGH})),
                         8: ListedPull("task/b", frozenset({LOW})),
                         9: ListedPull("task/c", None),
                         10: ListedPull("task/d", frozenset({HIGH, "bug"})),
                         11: ListedPull("task/e", frozenset({HIGH})),
                         12: ListedPull("task/f", frozenset({HIGH}))},
                        unreadable={10},
                        newest={11: LabelEvent(GATE_ACCOUNT, True),
                                12: LabelEvent(OPERATOR, False)})

    listed = _adapter_candidates(monkeypatch, gate)

    assert listed["ok"] is True
    # The projection is the trust boundary: the actor and the add or removal
    # reach the executor exactly as the forge reader reported them.
    assert listed["queue_label_events"] == {
        "7": {HIGH: {"actor": OPERATOR, "added": True}}, "10": None,
        "11": {HIGH: {"actor": GATE_ACCOUNT, "added": True}},
        "12": {HIGH: {"actor": OPERATOR, "added": False}}}
    # Only `queue/high` is asked for: the executor reads no other label's event.
    assert gate.reads == [(7, (HIGH,)), (10, (HIGH,)), (11, (HIGH,)), (12, (HIGH,))]


def test_the_executor_judges_what_the_listing_ships(monkeypatch):
    """The listing's output, not a hand-built one, goes through `_queue_priorities`:
    the ranks follow the actor and the add or removal the forge reported."""
    from gate_server_estate import QUEUE_HIGH_TIMELINE_READS

    assert QUEUE_HIGH_TIMELINE_READS >= 4  # PRs 1-4 below each need a read
    past_the_bound = QUEUE_HIGH_TIMELINE_READS + 1
    numbers = list(range(1, past_the_bound + 1))
    gate = _ListingGate({number: ListedPull("task/x", frozenset({HIGH})) for number in numbers},
                        unreadable={4},
                        newest={2: LabelEvent(GATE_ACCOUNT, True), 3: LabelEvent(OPERATOR, False)})

    listed = _adapter_candidates(monkeypatch, gate)
    ranks, unverified, untrusted = merge_open_prs._queue_priorities(listed, numbers)

    trusted = {1, *range(5, QUEUE_HIGH_TIMELINE_READS + 1)}
    assert ranks == {number: merge_open_prs.PRIORITY_HIGH if number in trusted
                     else merge_open_prs.PRIORITY_NORMAL for number in numbers}
    assert untrusted == [2]  # another account's add
    assert unverified == [3, 4, past_the_bound]  # a removal, an unreadable timeline, no read


def test_the_listing_reads_a_bounded_number_of_high_timelines(monkeypatch):
    from gate_server_estate import QUEUE_HIGH_TIMELINE_READS

    count = QUEUE_HIGH_TIMELINE_READS + 2
    # Listed newest first: the bound still reads the lowest numbers.
    gate = _ListingGate({number: ListedPull("task/a", frozenset({HIGH}))
                         for number in range(count, 0, -1)}, unreadable={1})

    listed = _adapter_candidates(monkeypatch, gate)

    read = list(range(1, QUEUE_HIGH_TIMELINE_READS + 1))
    assert [index for index, _names in gate.reads] == read
    assert listed["queue_label_events"] == {
        str(number): None if number == 1 or number not in read
        else {HIGH: {"actor": OPERATOR, "added": True}}
        for number in range(1, count + 1)}


def test_a_listing_without_high_prs_reads_no_timeline(monkeypatch):
    gate = _ListingGate({7: ListedPull("task/a", frozenset({LOW}))})

    listed = _adapter_candidates(monkeypatch, gate)

    assert "queue_label_events" not in listed
    assert gate.reads == []
