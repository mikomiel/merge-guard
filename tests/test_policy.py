"""The acceptance cases from issue #2 plus the behavioural guards behind them."""

import tomllib
from pathlib import Path

import pytest

from scripts import merge_guard_policy as mg

HEAD = "a" * 40
BASE = "b" * 40
OLD = "c" * 40
REVIEWER_ID = 42


def make_policy(**overrides):
    data = {
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {
            "reviewer_id": REVIEWER_ID,
            "reviewer_login": "reviewer[bot]",
            "reviewer_type": "Bot",
            "trusted_associations": ["OWNER"],
            "required_checks": ["checks"],
            "gate_context": "review",
        },
        "paths": {"ordinary": ["README.md", "docs/**"]},
    }
    for key, value in overrides.items():
        data[key].update(value)
    return mg.load_policy(data)


def review(state, commit=HEAD, login="reviewer[bot]", kind="Bot", rid=1, user_id=REVIEWER_ID):
    return {"id": rid, "state": state, "commit_id": commit,
            "user": {"id": user_id, "login": login, "type": kind}}


def run(name="checks", sha=HEAD, status="completed", conclusion="success", rid=1):
    return {"id": rid, "name": name, "head_sha": sha, "status": status,
            "conclusion": conclusion, "app": {"id": mg.ACTIONS_APP_ID}}


def gate_status(state="success", login="github-actions[bot]", kind="Bot", rid=1, context="review"):
    return {"id": rid, "context": context, "state": state,
            "creator": {"login": login, "type": kind}}


def comparison(files, sha=BASE):
    return {"status": "ahead", "base_commit": {"sha": sha}, "merge_base_commit": {"sha": sha},
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
           maintainers=()):
    policy = policy or make_policy()
    pr = pr or pull()
    return mg.merge_decision(
        pr, reviews if reviews is not None else [review("APPROVED")],
        runs if runs is not None else [run()],
        statuses if statuses is not None else [gate_status()],
        comparison(files or ["README.md"]), set(maintainers), policy)


def test_draft_pull_request_is_not_eligible():
    assert decide(pr=pull(draft=True)) == (False, "pull_not_ready")


def test_rename_is_judged_on_both_sides():
    rename = {"filename": "docs/guide.md", "previous_filename": ".github/workflows/x.yml"}
    assert decide(pr=pull(changed_files=1), files=[rename]) == (False, "sensitive_paths")


def test_status_from_untrusted_creator_blocks():
    allowed, reason = decide(statuses=[gate_status(login="someone", kind="User")])
    assert (allowed, reason) == (False, "untrusted_review_status")


def test_stale_head_check_blocks():
    assert decide(runs=[run(sha=OLD)]) == (False, "check_head_mismatch")


def test_approval_on_an_older_head_does_not_count():
    assert decide(reviews=[review("APPROVED", commit=OLD)]) == (False, "current_head_approval_required")


def test_lock_file_only_change_is_sensitive_by_default():
    assert decide(pr=pull(changed_files=1), files=["requirements-dev.txt"]) == (False, "sensitive_paths")


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
    assert mg.review_verdict([maintainer], HEAD, "author", {99}, policy)[0] == "success"
    assert decide(policy=policy, reviews=[maintainer], maintainers=[])[0] is False
    assert decide(policy=policy, reviews=[maintainer], maintainers=[99]) == (
        False, "configured_reviewer_required_for_automerge")


def test_policy_requires_a_reviewer_id():
    with pytest.raises(mg.Unusable):
        mg.load_policy({"repository": {"name": "o/r", "base_branch": "main"},
                        "review": {"reviewer_login": "x[bot]", "required_checks": ["c"]}})


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
