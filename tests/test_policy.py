"""Acceptance cases from issue #2 plus the guards the second review asked for."""

import tomllib
from pathlib import Path

import pytest

from scripts import merge_guard_policy as mg

HEAD = "a" * 40
BASE = "b" * 40
OLD = "c" * 40
REVIEWER_ID = 42
# The head commits at 07:29:06Z and the reviewer approves it a minute later.
HEAD_COMMITTED_AT = "2026-11-11T07:29:06Z"
REVIEW_SUBMITTED_AT = "2026-11-11T07:30:30Z"


def make_policy(**overrides):
    data = {
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {
            "reviewer_id": REVIEWER_ID,
            "reviewer_login": "reviewer[bot]",
            "reviewer_type": "Bot",
            "required_checks": ["checks"],
            "gate_context": "review",
        },
        "paths": {"ordinary": ["README.md", "docs/**"]},
    }
    for key, value in overrides.items():
        data[key].update(value)
    return mg.load_policy(data)


def review(state, commit=HEAD, login="reviewer[bot]", kind="Bot", rid=1, user_id=REVIEWER_ID,
           submitted_at=REVIEW_SUBMITTED_AT):
    return {"id": rid, "state": state, "commit_id": commit, "submitted_at": submitted_at,
            "user": {"id": user_id, "login": login, "type": kind}}


def run(name="checks", sha=HEAD, conclusion="success", rid=1):
    return {"id": rid, "name": name, "head_sha": sha, "status": "completed",
            "conclusion": conclusion, "app": {"id": mg.ACTIONS_APP_ID}}


def gate_status(state="success", login="github-actions[bot]", kind="Bot", rid=1):
    return {"id": rid, "context": "review", "state": state,
            "creator": {"login": login, "type": kind}}


def comparison(files, sha=BASE, head_committed_at=HEAD_COMMITTED_AT):
    return {"status": "ahead", "base_commit": {"sha": sha}, "merge_base_commit": {"sha": sha},
            "commits": [{"sha": HEAD, "commit": {"committer": {"date": head_committed_at}}}],
            "files": [{"filename": f} if isinstance(f, str) else f for f in files]}


def pull(**over):
    data = {
        "number": 1, "state": "open", "draft": False, "mergeable": True,
        "mergeable_state": "clean", "changed_files": 1,
        "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
        "head": {"sha": HEAD},
        "user": {"login": "author"},
    }
    data.update(over)
    return data


def decide(policy=None, pr=None, reviews=None, runs=None, statuses=None, files=None,
           maintainers=(), head_committed_at=HEAD_COMMITTED_AT, comparison_data=None):
    policy = policy or make_policy()
    return mg.merge_decision(
        pr or pull(),
        reviews if reviews is not None else [review("APPROVED")],
        runs if runs is not None else [run()],
        statuses if statuses is not None else [gate_status()],
        comparison_data if comparison_data is not None else comparison(
            files or ["README.md"], head_committed_at=head_committed_at),
        set(maintainers), policy)


def test_draft_pull_request_is_not_eligible():
    assert decide(pr=pull(draft=True)) == (False, "pull_not_ready")


def test_rename_is_judged_on_both_sides():
    rename = {"filename": "docs/guide.md", "previous_filename": ".github/workflows/x.yml"}
    assert decide(pr=pull(changed_files=1), files=[rename]) == (False, "sensitive_paths")


def test_status_from_untrusted_creator_blocks():
    assert decide(statuses=[gate_status(login="someone", kind="User")]) == (
        False, "untrusted_review_status")


def test_stale_head_check_blocks():
    assert decide(runs=[run(sha=OLD)]) == (False, "check_head_mismatch")


def test_approval_on_an_older_head_does_not_count():
    assert decide(reviews=[review("APPROVED", commit=OLD)]) == (
        False, "current_head_approval_required")


def test_lock_file_only_change_is_sensitive_by_default():
    assert decide(pr=pull(changed_files=1), files=["requirements-dev.txt"]) == (
        False, "sensitive_paths")


def test_lock_file_only_change_needs_the_exemption():
    policy = make_policy(paths={"dependency_update_exemption": True})
    assert decide(policy=policy, pr=pull(changed_files=1), files=["uv.lock"]) == (
        True, "eligible_dependency_update")


def test_nested_manifest_is_not_a_dependency_update():
    policy = make_policy(paths={"dependency_update_exemption": True})
    assert decide(policy=policy, pr=pull(changed_files=1), files=["tools/uv.lock"]) == (
        False, "sensitive_paths")


def test_approval_after_ci_is_eligible():
    assert decide() == (True, "eligible")


def test_author_review_is_ignored():
    assert decide(reviews=[review("APPROVED", login="author", kind="User", user_id=7)]) == (
        False, "current_head_approval_required")


def test_changes_requested_by_the_reviewer_blocks():
    assert decide(reviews=[review("CHANGES_REQUESTED")]) == (False, "changes_requested")


def test_maintainer_review_satisfies_the_verdict_but_not_automerge():
    maintainer = review("APPROVED", login="owner", kind="User", user_id=99)
    policy = make_policy()
    assert mg.review_verdict([maintainer], HEAD, "author", {99}, policy,
                             HEAD_COMMITTED_AT)[0] == "success"
    assert decide(policy=policy, reviews=[maintainer], maintainers=[99]) == (
        False, "configured_reviewer_required_for_automerge")


def test_an_approval_submitted_after_the_head_commits_is_still_approved_at_head():
    # The forge's own example: the review at 07:30:30Z names a head committed a
    # minute earlier, which is the only order in which it proves anything.
    assert mg.review_verdict([review("APPROVED")], HEAD, "author", set(), make_policy(),
                             HEAD_COMMITTED_AT) == ("success", "approved_at_head")


def test_an_approval_submitted_34_minutes_before_the_head_commit_is_refused():
    # review 5855285914 was submitted 34 minutes before the commit the forge
    # later reassigned it to; `commit_id` matched the head but proves nothing.
    late_head = "2026-11-11T08:04:39Z"
    assert mg.review_verdict([review("APPROVED")], HEAD, "author", set(), make_policy(),
                             late_head) == ("pending", "current_head_approval_required")
    assert decide(head_committed_at=late_head) == (False, "current_head_approval_required")


def test_an_approval_submitted_60_seconds_before_the_head_commit_is_accepted():
    assert mg.review_verdict(
        [review("APPROVED", submitted_at="2026-11-11T07:28:06Z")], HEAD, "author", set(),
        make_policy(), HEAD_COMMITTED_AT) == ("success", "approved_at_head")


def test_an_approval_without_a_usable_submitted_at_is_refused():
    missing = review("APPROVED")
    del missing["submitted_at"]
    for item in (missing, review("APPROVED", submitted_at="not-a-timestamp")):
        with pytest.raises(mg.Unusable, match="invalid_review_submitted_at"):
            mg.review_verdict([item], HEAD, "author", set(), make_policy(), HEAD_COMMITTED_AT)


def test_an_unparseable_head_commit_time_is_refused():
    with pytest.raises(mg.Unusable, match="invalid_head_commit_time"):
        mg.review_verdict([review("APPROVED")], HEAD, "author", set(), make_policy(),
                          "the day before yesterday")


def test_a_comparison_whose_last_commit_is_not_the_head_is_refused():
    shifted = comparison(["README.md"])
    shifted["commits"] = [{"sha": OLD, "commit": {"committer": {"date": HEAD_COMMITTED_AT}}}]
    assert decide(comparison_data=shifted) == (False, "comparison_head_mismatch")


def test_a_comparison_without_commits_is_refused():
    stripped = comparison(["README.md"])
    del stripped["commits"]
    assert decide(comparison_data=stripped) == (False, "invalid_comparison_commits")


def test_the_tolerance_boundary_is_exactly_300_seconds():
    # 300 s of clock skew is allowed and one second more is not, so the rule
    # cannot drift into admitting a head that moved after the review.
    assert mg.review_verdict(
        [review("APPROVED", submitted_at="2026-11-11T07:24:06Z")], HEAD, "author", set(),
        make_policy(), HEAD_COMMITTED_AT) == ("success", "approved_at_head")
    assert mg.review_verdict(
        [review("APPROVED", submitted_at="2026-11-11T07:24:05Z")], HEAD, "author", set(),
        make_policy(), HEAD_COMMITTED_AT) == ("pending", "current_head_approval_required")


def test_equivalent_instants_in_different_offsets_compare_as_one_instant():
    # 08:29:06+01:00 is 07:29:06Z, so the approval is at the head, not before it.
    assert mg.review_verdict(
        [review("APPROVED", submitted_at="2026-11-11T08:29:06+01:00")], HEAD, "author", set(),
        make_policy(), HEAD_COMMITTED_AT) == ("success", "approved_at_head")


def test_a_timestamp_without_an_offset_is_refused():
    # An offset-free stamp cannot be ordered against the other side, so it is
    # unusable rather than assumed to be UTC.
    with pytest.raises(mg.Unusable, match="invalid_review_submitted_at"):
        mg.review_verdict([review("APPROVED", submitted_at="2026-11-11T07:30:30")], HEAD,
                          "author", set(), make_policy(), HEAD_COMMITTED_AT)


def test_an_empty_comparison_commit_list_is_refused():
    empty = comparison(["README.md"])
    empty["commits"] = []
    assert decide(comparison_data=empty) == (False, "invalid_comparison_commits")


def test_a_backdated_head_is_a_documented_limit():
    # A committer date is chosen by whoever creates the commit, so a head with a
    # forged date still reads as approved. Issue #20 owns closing that gap; this
    # test pins the limit so it cannot be mistaken for a proof of intent.
    assert mg.review_verdict(
        [review("APPROVED", submitted_at="2026-11-11T07:30:30Z")], HEAD, "author", set(),
        make_policy(), "2026-11-11T07:29:06Z") == ("success", "approved_at_head")


def test_policy_requires_a_reviewer_id():
    with pytest.raises(mg.Unusable):
        mg.load_policy({"repository": {"name": "o/r", "base_branch": "main"},
                        "review": {"reviewer_login": "x[bot]", "required_checks": ["c"]}})


def test_policy_refuses_a_string_ordinary_list():
    with pytest.raises(mg.Unusable):
        make_policy(paths={"ordinary": "docs/**"})


def test_policy_refuses_a_string_boolean():
    with pytest.raises(mg.Unusable):
        make_policy(paths={"dependency_update_exemption": "false"})


def test_ordinary_is_an_allowlist_and_star_cannot_cross_a_slash():
    policy = make_policy(paths={"ordinary": ["docs/*"]})
    assert mg.ordinary("docs/guide.md", policy)
    assert not mg.ordinary("docs/nested/guide.md", policy)
    assert not mg.ordinary("scripts/engine.py", policy)


def test_the_example_policy_loads():
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "examples/merge-guard.toml").read_text())
    data["review"]["reviewer_id"] = 1
    policy = mg.load_policy(data)
    assert policy.repository == "OWNER/REPO"
    assert policy.dependency_update_exemption is False
