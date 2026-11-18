"""The reviewed-head rule: docs/design/reviewed-head.md.

With ``[review] require_reviewed_head`` on, an approval of the head counts only
when its unedited body ends with ``Reviewed-Head: <head>``. These cases drive the
policy functions and the command line directly. ``tests/test_reviewed_head_engine.py``
covers the forge read that supplies the edit state.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_policy import (
    HEAD,
    HEAD_COMMITTED_AT,
    OLD,
    REVIEWER_ID,
    decide,
    make_policy,
    pull,
    review,
)

from merge_guard import cli
from merge_guard import policy as mg

OTHER_HEAD = "d" * 40
LINE = f"Reviewed-Head: {HEAD}"
MAINTAINER_ID = 99
EDITED_AT = "2026-11-11T08:00:00Z"
# 300 seconds before the head commit is the last moment that still counts.
LAST_COUNTED = "2026-11-11T07:24:06Z"
TOO_EARLY = "2026-11-11T07:24:05Z"


def strict(**fields):
    """A policy with the switch on. The loader requires the carry to be off."""
    return make_policy(review={"carry_unchanged_diff": False, "require_reviewed_head": True,
                               **fields})


def with_state(item, body=LINE, edited=None):
    """A review that carries the edit state which the engine attaches."""
    return {**item, "edit_state": {"body": body, "last_edited_at": edited}}


def reviewer(body=LINE, edited=None, rid=1, commit=HEAD, state="APPROVED", **fields):
    return with_state(review(state, commit=commit, rid=rid, **fields), body, edited)


def maintainer(body=LINE, edited=None, rid=2, commit=HEAD, state="APPROVED", **fields):
    return with_state(review(state, commit=commit, login="maint", kind="User", rid=rid,
                             user_id=MAINTAINER_ID, **fields), body, edited)


def verdict(reviews, policy=None, maintainers=(), carried=frozenset(),
            head_committed_at=HEAD_COMMITTED_AT):
    return mg.review_verdict(reviews, HEAD, "author", set(maintainers), policy or strict(),
                             head_committed_at, carried)


# The grammar -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # The line attests: last, alone, exact case, one space, 40 lowercase hex.
        (LINE, None),
        ("Looks good.\n\n" + LINE, None),
        ("Looks good.\n" + LINE + "\n", None),
        (LINE + "\n\n\n", None),
        (LINE + "\r\n", None),
        ("Looks good.\r\n\r\n" + LINE + "\r\n", None),
        (LINE + "  \t ", None),
        (LINE + " \r\n", None),
        (LINE + "  \n \n\t\n", None),
        (LINE + "\n   \n", None),
        # No candidate: a line has to start with the key, in any case.
        ("", "reviewed_head_missing"),
        ("  \n\t\n", "reviewed_head_missing"),
        ("Looks good.", "reviewed_head_missing"),
        ("Reviewed by me", "reviewed_head_missing"),
        ("Reviewed Head: " + HEAD, "reviewed_head_missing"),
        ("Reviewed_Head: " + HEAD, "reviewed_head_missing"),
        ("Reviewed‑Head: " + HEAD, "reviewed_head_missing"),
        ("LGTM " + LINE, "reviewed_head_missing"),
        ("> " + LINE, "reviewed_head_missing"),
        ("- " + LINE, "reviewed_head_missing"),
        # The body is split at LF only: CR and the Unicode separators join lines.
        ("Looks good.\r" + LINE, "reviewed_head_missing"),
        ("Looks good. " + LINE, "reviewed_head_missing"),
        ("Looks good.\x0b" + LINE, "reviewed_head_missing"),
        (LINE + "\rThanks", "reviewed_head_malformed"),
        # A candidate that does not match from its first character.
        (" " + LINE, "reviewed_head_malformed"),
        ("\t" + LINE, "reviewed_head_malformed"),
        ("reviewed-head: " + HEAD, "reviewed_head_malformed"),
        ("REVIEWED-HEAD: " + HEAD, "reviewed_head_malformed"),
        ("Reviewed-head: " + HEAD, "reviewed_head_malformed"),
        ("Reviewed-Head: " + HEAD.upper(), "reviewed_head_malformed"),
        ("Reviewed-Head: " + HEAD[:12], "reviewed_head_malformed"),
        ("Reviewed-Head: " + HEAD + "a", "reviewed_head_malformed"),
        ("Reviewed-Head: " + "g" * 40, "reviewed_head_malformed"),
        ("Reviewed-Head:" + HEAD, "reviewed_head_malformed"),
        ("Reviewed-Head:  " + HEAD, "reviewed_head_malformed"),
        ("Reviewed-Head:\t" + HEAD, "reviewed_head_malformed"),
        ("Reviewed-Head : " + HEAD, "reviewed_head_malformed"),
        ("Reviewed-Head", "reviewed_head_malformed"),
        ("reviewed-heads are great", "reviewed_head_malformed"),
        (LINE + " thanks", "reviewed_head_malformed"),
        (LINE + ", really", "reviewed_head_malformed"),
        # Only one trailing CR is removed.
        (LINE + "\r\r\n", "reviewed_head_malformed"),
        # Two candidates never count, whatever they say.
        (LINE + "\n" + LINE, "reviewed_head_malformed"),
        (f"Reviewed-Head: {OTHER_HEAD}\n{LINE}", "reviewed_head_malformed"),
        (f"{LINE}\nReviewed-Head: {OTHER_HEAD}", "reviewed_head_malformed"),
        # A candidate that is not the last line.
        (LINE + "\nThanks.", "reviewed_head_malformed"),
        (LINE + "\n\nThanks.\n", "reviewed_head_malformed"),
        ("```\n" + LINE + "\n```", "reviewed_head_malformed"),
        # A valid line for another commit.
        (f"Reviewed-Head: {OTHER_HEAD}", "reviewed_head_mismatch"),
        (f"Looks good.\n\nReviewed-Head: {OTHER_HEAD}\n", "reviewed_head_mismatch"),
    ],
)
def test_the_line_grammar(body, expected):
    assert mg.reviewed_head_code(body, HEAD) == expected


def test_the_line_names_the_head_it_is_asked_about():
    assert mg.reviewed_head_code(f"Reviewed-Head: {OTHER_HEAD}", OTHER_HEAD) is None
    assert mg.reviewed_head_code(LINE, OTHER_HEAD) == "reviewed_head_mismatch"


# The verdict with the switch on ----------------------------------------------------


def test_an_approval_that_attests_the_head_counts():
    assert verdict([reviewer()]) == ("success", "approved_at_head")


@pytest.mark.parametrize(
    ("body", "edited", "code"),
    [
        ("Looks good.", None, "reviewed_head_missing"),
        (" " + LINE, None, "reviewed_head_malformed"),
        (LINE + "\n" + LINE, None, "reviewed_head_malformed"),
        (f"Reviewed-Head: {OTHER_HEAD}", None, "reviewed_head_mismatch"),
        # An edit refuses whatever the body says, and the body is not parsed.
        (LINE, EDITED_AT, "reviewed_head_edited"),
        ("Looks good.", EDITED_AT, "reviewed_head_edited"),
        (" " + LINE, EDITED_AT, "reviewed_head_edited"),
        # Only null means "never edited": any other time, however odd, is an edit.
        (LINE, "", "reviewed_head_edited"),
        (LINE, "not a time", "reviewed_head_edited"),
    ],
)
def test_each_reason_is_a_pending_verdict_never_a_refusal(body, edited, code):
    assert verdict([reviewer(body, edited)]) == ("pending", code)


def test_an_approval_that_cannot_be_judged_without_its_edit_state_is_refused():
    base = review("APPROVED")
    for state in (None, "text", {}, {"body": LINE}, {"last_edited_at": None},
                  {"body": 5, "last_edited_at": None},
                  {"body": LINE, "last_edited_at": 5},
                  {"body": LINE, "last_edited_at": False}):
        with pytest.raises(mg.Unusable, match="review_edit_state_unusable"):
            verdict([{**base, "edit_state": state}])
    with pytest.raises(mg.Unusable, match="review_edit_state_unusable"):
        verdict([base])


def test_the_rest_body_is_not_the_line():
    # Only edit_state counts: a review that carries a REST body gets no credit for it.
    assert verdict([{**reviewer("Looks good."), "body": LINE}]) == (
        "pending", "reviewed_head_missing")


# The #19 timestamp check comes before the line --------------------------------------


@pytest.mark.parametrize(
    ("body", "edited"),
    [("Looks good.", None), (LINE, None), (LINE, EDITED_AT), (" " + LINE, None)],
)
def test_an_approval_the_timestamp_check_ignores_produces_no_reviewed_head_code(body, edited):
    early = reviewer(body, edited, submitted_at=TOO_EARLY)
    assert verdict([early]) == ("pending", "current_head_approval_required")


def test_the_timestamp_check_runs_before_the_edit_state_is_read():
    ignored = review("APPROVED", submitted_at=TOO_EARLY)
    assert "edit_state" not in ignored
    assert verdict([ignored]) == ("pending", "current_head_approval_required")


def test_the_last_counted_moment_still_reaches_the_line():
    assert verdict([reviewer("Looks good.", submitted_at=LAST_COUNTED)]) == (
        "pending", "reviewed_head_missing")
    assert verdict([reviewer(submitted_at=LAST_COUNTED)]) == ("success", "approved_at_head")


def test_an_ignored_approval_leaves_a_counted_one_to_decide():
    early = reviewer("Looks good.", submitted_at=TOO_EARLY, rid=1)
    assert verdict([early, maintainer()], maintainers={MAINTAINER_ID}) == (
        "success", "approved_at_head")
    assert verdict([early, maintainer("")], maintainers={MAINTAINER_ID}) == (
        "pending", "reviewed_head_missing")


# An approval of an earlier head is never examined ------------------------------------


@pytest.mark.parametrize("body", ["", f"Reviewed-Head: {OLD}", LINE, " " + LINE])
def test_an_approval_of_an_earlier_head_keeps_the_stale_reason(body):
    assert verdict([reviewer(body, commit=OLD)]) == (
        "pending", "current_head_approval_required")
    assert verdict([reviewer(body, EDITED_AT, commit=OLD)]) == (
        "pending", "current_head_approval_required")


def test_an_approval_of_an_earlier_head_needs_no_edit_state():
    assert verdict([review("APPROVED", commit=OLD)]) == (
        "pending", "current_head_approval_required")


def test_a_stale_approval_does_not_hide_the_reason_of_the_current_one():
    stale = reviewer("", commit=OLD, rid=1)
    current = maintainer("Looks good.", rid=2)
    assert verdict([stale, current], maintainers={MAINTAINER_ID}) == (
        "pending", "reviewed_head_missing")


# The rank ---------------------------------------------------------------------------


def test_a_change_request_beats_an_attesting_approval():
    blocking = maintainer(state="CHANGES_REQUESTED", rid=3)
    assert verdict([reviewer(), blocking], maintainers={MAINTAINER_ID}) == (
        "failure", "changes_requested")
    later = review("CHANGES_REQUESTED", rid=2)
    assert verdict([reviewer(rid=1), later]) == ("failure", "changes_requested")
    stale = review("CHANGES_REQUESTED", commit=OLD, rid=3, login="maint", kind="User",
                   user_id=MAINTAINER_ID)
    assert verdict([reviewer(), stale], maintainers={MAINTAINER_ID}) == (
        "failure", "changes_requested")


@pytest.mark.parametrize("ids", [(1, 2), (2, 1)])
def test_an_attesting_approval_beats_one_that_was_set_aside(ids):
    bad = reviewer("Looks good.", rid=ids[0])
    good = maintainer(rid=ids[1])
    for reviews in ([bad, good], [good, bad]):
        assert verdict(reviews, maintainers={MAINTAINER_ID}) == ("success", "approved_at_head")


@pytest.mark.parametrize(
    ("first", "second", "code"),
    [
        (("", None), (f"Reviewed-Head: {OTHER_HEAD}", None), "reviewed_head_mismatch"),
        ((LINE, EDITED_AT), ("", None), "reviewed_head_missing"),
        ((" " + LINE, None), ("Looks good.", EDITED_AT), "reviewed_head_edited"),
    ],
)
def test_the_newest_approval_that_was_set_aside_names_the_reason(first, second, code):
    older = reviewer(*first, rid=1)
    newer = maintainer(*second, rid=2)
    for reviews in ([older, newer], [newer, older]):
        assert verdict(reviews, maintainers={MAINTAINER_ID}) == ("pending", code)


def test_the_newest_by_review_id_wins_not_the_order_of_the_list():
    # The configured reviewer's review has the higher id, so its reason is reported.
    older = maintainer("Looks good.", rid=2)
    newer = reviewer(f"Reviewed-Head: {OTHER_HEAD}", rid=3)
    for reviews in ([older, newer], [newer, older]):
        assert verdict(reviews, maintainers={MAINTAINER_ID}) == (
            "pending", "reviewed_head_mismatch")


def test_a_reason_beats_the_stale_default():
    assert verdict([reviewer("Looks good.")]) == ("pending", "reviewed_head_missing")
    assert verdict([]) == ("pending", "current_head_approval_required")


def test_only_the_latest_decisive_review_of_an_identity_counts():
    good, bad = reviewer(rid=1), reviewer("Looks good.", rid=2)
    # An older approval with a matching line does not count after a newer one.
    assert verdict([good, bad]) == ("pending", "reviewed_head_missing")
    assert verdict([bad, good]) == ("pending", "reviewed_head_missing")
    assert verdict([bad, reviewer(rid=3)]) == ("success", "approved_at_head")
    # A comment is not decisive, a dismissal is.
    assert verdict([good, review("COMMENTED", rid=2)]) == ("success", "approved_at_head")
    assert verdict([good, review("DISMISSED", rid=2)]) == (
        "pending", "current_head_approval_required")


def test_a_draft_still_reads_as_a_draft():
    prs = [{**pull(draft=True), "reviews": [reviewer("Looks good.")],
            "head_committed_at": HEAD_COMMITTED_AT}]
    assert mg.review_plan(prs, strict()) == {HEAD: ("pending", "pull_request_is_draft")}
    prs[0]["draft"] = False
    assert mg.review_plan(prs, strict()) == {HEAD: ("pending", "reviewed_head_missing")}


def test_a_bad_line_on_one_pull_request_does_not_stop_the_others():
    other = {**pull(number=2, head={"sha": OTHER_HEAD}), "head_committed_at": HEAD_COMMITTED_AT,
             "reviews": [with_state(review("APPROVED", commit=OTHER_HEAD),
                                    f"Reviewed-Head: {OTHER_HEAD}")]}
    bad = {**pull(), "head_committed_at": HEAD_COMMITTED_AT, "reviews": [reviewer("")]}
    assert mg.review_plan([bad, other], strict()) == {
        HEAD: ("pending", "reviewed_head_missing"),
        OTHER_HEAD: ("success", "approved_at_head"),
    }


# Maintainers need the line too, and automerge needs the reviewer's -------------------


def test_a_verified_maintainer_approval_needs_the_line():
    assert verdict([maintainer("")], maintainers={MAINTAINER_ID}) == (
        "pending", "reviewed_head_missing")
    assert verdict([maintainer(edited=EDITED_AT)], maintainers={MAINTAINER_ID}) == (
        "pending", "reviewed_head_edited")
    assert verdict([maintainer()], maintainers={MAINTAINER_ID}) == ("success", "approved_at_head")


def test_a_user_the_api_did_not_verify_never_counts():
    assert verdict([maintainer()]) == ("pending", "current_head_approval_required")


def test_automerge_is_refused_when_only_a_maintainer_approval_attests():
    for configured in (reviewer(""), reviewer(edited=EDITED_AT), reviewer(commit=OLD)):
        assert decide(policy=strict(), reviews=[configured, maintainer()],
                      maintainers={MAINTAINER_ID}) == (
            False, "configured_reviewer_required_for_automerge")


def test_automerge_needs_the_reviewer_and_the_maintainer_to_attest():
    assert decide(policy=strict(), reviews=[reviewer(), maintainer("")],
                  maintainers={MAINTAINER_ID}) == (True, "eligible")
    assert decide(policy=strict(), reviews=[reviewer()]) == (True, "eligible")
    assert decide(policy=strict(), reviews=[reviewer("")]) == (False, "reviewed_head_missing")


def test_a_merge_decision_without_the_edit_state_reports_it_and_never_merges():
    assert decide(policy=strict(), reviews=[review("APPROVED")]) == (
        False, "review_edit_state_unusable")


# Switch off: the rule does not exist -------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{}, {"edit_state": None}, {"edit_state": "text"}, {"edit_state": {"body": 5}},
     {"edit_state": {"body": "Looks good.", "last_edited_at": EDITED_AT}},
     {"body": "Looks good."}],
)
def test_with_the_switch_off_the_line_and_the_edit_state_are_never_read(extra):
    approval = {**review("APPROVED"), **extra}
    assert mg.review_verdict([approval], HEAD, "author", set(), make_policy(),
                             HEAD_COMMITTED_AT) == ("success", "approved_at_head")


def test_with_the_switch_off_an_early_approval_is_still_ignored():
    early = review("APPROVED", submitted_at=TOO_EARLY)
    assert mg.review_verdict([early], HEAD, "author", set(), make_policy(),
                             HEAD_COMMITTED_AT) == ("pending", "current_head_approval_required")


# No carry under the switch ------------------------------------------------------------


def test_the_switch_never_carries_even_for_a_policy_built_without_the_loader():
    built = replace(strict(), carry_unchanged_diff=True)
    assert built.require_reviewed_head is True
    assert verdict([reviewer(commit=OLD)], policy=built, carried={OLD}) == (
        "pending", "current_head_approval_required")


# Who counts: one definition ------------------------------------------------------------


def test_approvals_at_head_lists_what_the_verdict_weighs():
    reviews = [
        review("APPROVED", rid=1),                                  # superseded below
        review("APPROVED", rid=5),                                  # the reviewer, counted
        review("APPROVED", commit=OLD, rid=6, login="maint", kind="User",
               user_id=MAINTAINER_ID),                              # an earlier head
        review("APPROVED", rid=7, login="other", kind="User", user_id=7),  # not verified
        review("APPROVED", rid=8, login="author", kind="User", user_id=8),
        review("CHANGES_REQUESTED", rid=9, login="late", kind="User", user_id=11),
        review("APPROVED", rid=3, login="stale", kind="User", user_id=12),
        review("DISMISSED", rid=4, login="stale", kind="User", user_id=12),
    ]
    counted = mg.approvals_at_head(reviews, HEAD, "author", {MAINTAINER_ID, 8, 11, 12}, strict())
    assert [item["id"] for item in counted] == [5]
    assert REVIEWER_ID == counted[0]["user"]["id"]


def test_approvals_at_head_validates_its_input_like_the_verdict():
    for bad_head in ("", "abc", None):
        with pytest.raises(mg.Unusable, match="invalid_review_snapshot"):
            mg.approvals_at_head([], bad_head, "author", set(), strict())
    with pytest.raises(mg.Unusable, match="invalid_review_snapshot"):
        mg.approvals_at_head([], HEAD, "", set(), strict())
    with pytest.raises(mg.Unusable, match="invalid_maintainer_set"):
        mg.approvals_at_head([], HEAD, "author", [], strict())
    with pytest.raises(mg.Unusable, match="invalid_review$"):
        mg.approvals_at_head(["x"], HEAD, "author", set(), strict())


# Policy -----------------------------------------------------------------------------


def policy_data(**review_fields):
    return {"repository": {"name": "o/r", "base_branch": "main"},
            "review": {"reviewer_id": 1, "reviewer_login": "r[bot]", "required_checks": ["c"],
                       **review_fields}}


def test_the_switch_is_off_by_default():
    assert mg.load_policy(policy_data()).require_reviewed_head is False
    assert make_policy().require_reviewed_head is False


@pytest.mark.parametrize("value", ["true", "false", "", 1, 0, None, [], {}, [True], 1.0])
def test_a_switch_that_is_not_a_boolean_is_refused(value):
    with pytest.raises(mg.Unusable, match="policy_require_reviewed_head_invalid"):
        mg.load_policy(policy_data(require_reviewed_head=value, carry_unchanged_diff=False))


def test_an_invalid_switch_is_reported_before_the_carry_conflict():
    with pytest.raises(mg.Unusable, match="policy_require_reviewed_head_invalid"):
        mg.load_policy(policy_data(require_reviewed_head="yes"))


@pytest.mark.parametrize("carry", [{}, {"carry_unchanged_diff": True}])
def test_the_switch_refuses_a_policy_that_can_carry(carry):
    with pytest.raises(mg.Unusable, match="policy_reviewed_head_requires_carry_off"):
        mg.load_policy(policy_data(require_reviewed_head=True, **carry))


def test_the_switch_loads_with_the_carry_off():
    loaded = mg.load_policy(policy_data(require_reviewed_head=True, carry_unchanged_diff=False))
    assert loaded.require_reviewed_head is True
    assert loaded.carry_unchanged_diff is False


@pytest.mark.parametrize("carry", [{}, {"carry_unchanged_diff": True},
                                   {"carry_unchanged_diff": False}])
def test_the_switch_off_leaves_every_carry_setting_alone(carry):
    loaded = mg.load_policy(policy_data(require_reviewed_head=False, **carry))
    assert loaded.require_reviewed_head is False
    assert loaded.carry_unchanged_diff is carry.get("carry_unchanged_diff", True)


def test_the_field_belongs_to_the_review_section_only():
    assert mg.unknown_fields({"review": {"require_reviewed_head": True}}) == []
    assert mg.unknown_fields({"paths": {"require_reviewed_head": True}}) == [
        "paths.require_reviewed_head"]
    with pytest.raises(mg.Unusable, match="policy_unknown_field: paths.require_reviewed_head"):
        mg.load_policy({**policy_data(), "paths": {"require_reviewed_head": True}})


def test_the_switch_has_no_deprecation_of_its_own():
    assert "review.require_reviewed_head" not in mg.DEPRECATED_FIELDS


# The forge --------------------------------------------------------------------------


def forge_policy(forge=None, switch=True):
    repository = {"name": "o/r", "base_branch": "main"}
    if forge is not None:
        repository["forge"] = forge
    return mg.load_policy({"repository": repository, "review": {
        "reviewer_id": 1, "reviewer_login": "r[bot]", "required_checks": ["c"],
        "carry_unchanged_diff": False, "require_reviewed_head": switch}})


def test_the_switch_is_refused_on_forgejo_once_the_forge_is_known():
    with pytest.raises(mg.Unusable, match="policy_reviewed_head_forge_unsupported"):
        mg.require_forge(forge_policy("forgejo"))
    with pytest.raises(mg.Unusable, match="policy_reviewed_head_forge_unsupported"):
        mg.require_forge(forge_policy("forgejo"), "forgejo")
    # The forge can be unnamed when the policy loads, so the loader cannot refuse it.
    unnamed = forge_policy()
    assert unnamed.forge is None
    with pytest.raises(mg.Unusable, match="policy_reviewed_head_forge_unsupported"):
        mg.require_forge(unnamed, "forgejo")


def test_the_switch_is_accepted_on_github():
    named = mg.require_forge(forge_policy("github"))
    assert (named.forge, named.require_reviewed_head) == ("github", True)
    assert mg.require_forge(forge_policy(), "github").require_reviewed_head is True


def test_forgejo_without_the_switch_is_unchanged():
    assert mg.require_forge(forge_policy("forgejo", switch=False)).forge == "forgejo"
    assert mg.require_forge(forge_policy(switch=False), "forgejo").forge == "forgejo"


def test_the_forge_refusals_keep_their_order():
    with pytest.raises(mg.Unusable, match="policy_forge_missing"):
        mg.require_forge(forge_policy())
    with pytest.raises(mg.Unusable, match="policy_forge_conflict"):
        mg.require_forge(forge_policy("forgejo"), "github")
    with pytest.raises(mg.Unusable, match="policy_forge_invalid"):
        mg.require_forge(forge_policy("forgejo"), "gitlab")


POLICY_FILE = """
[repository]
name = "owner/repo"
base_branch = "main"
{forge}
[review]
reviewer_id = 1
reviewer_login = "reviewer[bot]"
required_checks = ["checks"]
{review}
"""


def write(tmp_path, forge="", review=""):
    path = tmp_path / "merge-guard.toml"
    path.write_text(POLICY_FILE.format(forge=forge, review=review), encoding="utf-8")
    return str(path)


SWITCH_ON = "carry_unchanged_diff = false\nrequire_reviewed_head = true"


@pytest.mark.parametrize(
    ("forge", "review", "flag", "code"),
    [
        ('forge = "forgejo"', SWITCH_ON, [], "policy_reviewed_head_forge_unsupported"),
        ("", SWITCH_ON, ["--forge", "forgejo"], "policy_reviewed_head_forge_unsupported"),
        ('forge = "github"', "require_reviewed_head = true", [],
         "policy_reviewed_head_requires_carry_off"),
        ('forge = "github"', "require_reviewed_head = true\ncarry_unchanged_diff = true", [],
         "policy_reviewed_head_requires_carry_off"),
        ('forge = "github"', 'require_reviewed_head = "true"', [],
         "policy_require_reviewed_head_invalid"),
    ],
)
def test_the_command_line_refuses_an_unusable_switch_before_any_forge_is_built(
        tmp_path, monkeypatch, capsys, forge, review, flag, code):
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    monkeypatch.setattr(cli, "GitHub", None)
    monkeypatch.setattr(cli, "Forgejo", None)
    exit_code = cli.main(["status", *flag, "--policy", write(tmp_path, forge, review)])
    assert exit_code == cli.EXIT_UNUSABLE
    assert capsys.readouterr().err == f"merge_guard_refused: {code}\n"


def test_the_command_line_accepts_the_switch_on_github(tmp_path, monkeypatch):
    seen = []

    class Built:
        def __init__(self, policy, apply=False):
            seen.append(policy)

    monkeypatch.setattr(cli, "GitHub", Built)
    monkeypatch.setattr(cli, "run_review", lambda api, policy, apply, head: {"heads": {}})
    code = cli.main(["status", "--forge", "github", "--policy", write(tmp_path, "", SWITCH_ON)])
    assert code == cli.EXIT_OK
    assert seen[0].require_reviewed_head is True
