"""Approval carry-over: only when the pull request's own diff is unchanged."""

import pytest

from scripts import merge_guard as runner
from scripts import merge_guard_policy as mg

HEAD = "a" * 40
BASE = "b" * 40
OLD = "c" * 40
REVIEWER_ID = 42
COMMITTED_AT = "2026-11-11T07:29:06Z"


def policy():
    return mg.load_policy({
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {"reviewer_id": REVIEWER_ID, "reviewer_login": "reviewer[bot]",
                   "required_checks": ["checks"], "gate_context": "review"},
        "paths": {"ordinary": ["README.md"]},
    })


def review(state, commit, rid=1, submitted_at="2026-11-11T07:30:30Z"):
    return {"id": rid, "state": state, "commit_id": commit, "submitted_at": submitted_at,
            "user": {"id": REVIEWER_ID, "login": "reviewer[bot]", "type": "Bot"}}


def pr(head=HEAD, changed_files=1):
    return {"number": 1, "state": "open", "draft": False, "changed_files": changed_files,
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
            "head": {"sha": head}, "user": {"login": "author"}}


def comparison(*files):
    return {"files": list(files)}


def file(name="README.md", patch="@@ -1 +1 @@\n-a\n+b", status="modified"):
    return {"filename": name, "status": status, "patch": patch}


class Compare:
    def __init__(self, current, previous):
        self.current, self.previous = current, previous

    def compare(self, base, head):
        return self.current if head == HEAD else self.previous


def test_an_unchanged_diff_carries():
    api = Compare(comparison(file()), comparison(file()))
    carried = runner.carried_heads(api, pr(), [review("APPROVED", OLD)], policy())
    assert carried == {OLD}
    state, reason = mg.review_verdict(
        [review("APPROVED", OLD)], HEAD, "author", set(), policy(), COMMITTED_AT, carried)
    assert state == "success"
    assert reason == "approved at cccccccccccc (carried, unchanged diff)"


def test_a_changed_hunk_does_not_carry():
    api = Compare(comparison(file(patch="@@ -1 +1 @@\n-a\n+c")), comparison(file()))
    assert runner.carried_heads(api, pr(), [review("APPROVED", OLD)], policy()) == set()


def test_an_extra_file_does_not_carry():
    api = Compare(comparison(file(), file("docs/x.md")), comparison(file()))
    assert runner.carried_heads(api, pr(changed_files=2), [review("APPROVED", OLD)], policy()) == set()


def test_a_conflict_resolution_that_changes_the_patch_does_not_carry():
    api = Compare(
        comparison(file(patch="@@ -1 +1 @@\n-a\n+resolved")),
        comparison(file(patch="@@ -1 +1 @@\n-a\n+theirs")),
    )
    assert runner.carried_heads(api, pr(), [review("APPROVED", OLD)], policy()) == set()


def test_a_missing_patch_does_not_carry():
    api = Compare(comparison(file()), comparison({"filename": "README.md", "status": "modified"}))
    assert runner.carried_heads(api, pr(), [review("APPROVED", OLD)], policy()) == set()


def test_a_later_change_request_wins():
    reviews = [review("APPROVED", OLD, rid=1), review("CHANGES_REQUESTED", HEAD, rid=2)]
    api = Compare(comparison(file()), comparison(file()))
    assert runner.carried_heads(api, pr(), reviews, policy()) == set()
    assert mg.review_verdict(reviews, HEAD, "author", set(), policy(), COMMITTED_AT)[0] == "failure"


def test_the_plan_carries_the_description_to_the_status():
    pr_data = dict(pr(), reviews=[review("APPROVED", OLD)], maintainers=[], carried_heads=[OLD],
                   head_committed_at=COMMITTED_AT)
    plan = mg.review_plan([pr_data], policy())
    assert plan[HEAD] == ("success", "approved at cccccccccccc (carried, unchanged diff)")


def test_a_truncated_file_list_does_not_carry():
    many = [file(f"docs/{index}.md") for index in range(300)]
    api = Compare(comparison(*many), comparison(*many))
    pr_data = dict(pr(), changed_files=301)
    assert runner.carried_heads(api, pr_data, [review("APPROVED", OLD)], policy()) == set()


def strict_policy():
    return mg.load_policy({
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {"reviewer_id": REVIEWER_ID, "reviewer_login": "reviewer[bot]",
                   "required_checks": ["checks"], "gate_context": "review",
                   "carry_unchanged_diff": False},
        "paths": {"ordinary": ["README.md"]},
    })


def test_the_policy_defaults_to_carrying():
    assert policy().carry_unchanged_diff is True


def test_a_policy_that_binds_the_approval_to_its_head_carries_nothing():
    api = Compare(comparison(file()), comparison(file()))
    assert runner.carried_heads(api, pr(), [review("APPROVED", OLD)], strict_policy()) == set()
    state, reason = mg.review_verdict(
        [review("APPROVED", OLD)], HEAD, "author", set(), strict_policy(), COMMITTED_AT, set())
    assert (state, reason) == ("pending", "current_head_approval_required")


def test_the_same_policy_accepts_an_approval_of_this_head():
    state, reason = mg.review_verdict(
        [review("APPROVED", HEAD)], HEAD, "author", set(), strict_policy(), COMMITTED_AT, set())
    assert (state, reason) == ("success", "approved_at_head")


def test_a_non_boolean_carry_setting_is_refused():
    with pytest.raises(mg.Unusable, match="policy_carry_unchanged_diff_invalid"):
        mg.load_policy({
            "repository": {"name": "o/r", "base_branch": "main"},
            "review": {"reviewer_id": REVIEWER_ID, "reviewer_login": "reviewer[bot]",
                       "required_checks": ["checks"], "carry_unchanged_diff": "false"},
        })
