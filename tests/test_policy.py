"""The six acceptance cases from issue #2, plus the policy's own refusals."""

import pytest

from scripts import merge_guard_policy as mg

HEAD = "a" * 40
BASE = "b" * 40
OLD = "c" * 40


def make_policy(**overrides):
    data = {
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {
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


def review(state, commit=HEAD, login="reviewer[bot]", kind="Bot", rid=1, association="NONE"):
    return {"id": rid, "state": state, "commit_id": commit,
            "user": {"id": 10, "login": login, "type": kind, "association": association}}


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


def decide(policy=None, pr=None, reviews=None, runs=None, statuses=None, files=None):
    policy = policy or make_policy()
    pr = pr or pull()
    return mg.merge_decision(
        pr, reviews if reviews is not None else [review("APPROVED")],
        runs if runs is not None else [run()],
        statuses if statuses is not None else [gate_status()],
        comparison(files or ["README.md"]), policy)


def test_draft_pull_request_is_not_eligible():
    assert decide(pr=pull(draft=True)) == (False, "pull_not_ready")


def test_rename_is_judged_on_both_sides():
    rename = {"filename": "docs/guide.md", "previous_filename": ".github/workflows/x.yml"}
    pr = pull(changed_files=1)
    allowed, reason = decide(pr=pr, files=[rename])
    assert (allowed, reason) == (False, "sensitive_paths")


def test_status_from_untrusted_creator_blocks():
    allowed, reason = decide(statuses=[gate_status(login="someone", kind="User")])
    assert (allowed, reason) == (False, "untrusted_review_status")


def test_stale_head_check_blocks():
    allowed, reason = decide(runs=[run(sha=OLD)])
    assert (allowed, reason) == (False, "check_head_mismatch")


def test_approval_on_an_older_head_does_not_count():
    allowed, reason = decide(reviews=[review("APPROVED", commit=OLD)])
    assert (allowed, reason) == (False, "current_head_approval_required")


def test_lock_file_only_change_is_sensitive_by_default():
    allowed, reason = decide(pr=pull(changed_files=1), files=["requirements-dev.txt"])
    assert (allowed, reason) == (False, "sensitive_paths")


def test_lock_file_only_change_needs_the_exemption():
    policy = make_policy(paths={"dependency_update_exemption": True})
    allowed, reason = decide(policy=policy, pr=pull(changed_files=1), files=["uv.lock"])
    assert (allowed, reason) == (True, "eligible_dependency_update")


def test_approval_after_ci_is_eligible():
    assert decide() == (True, "eligible")


def test_author_review_is_ignored():
    allowed, reason = decide(reviews=[review("APPROVED", login="author", kind="User")])
    assert (allowed, reason) == (False, "current_head_approval_required")


def test_changes_requested_by_the_reviewer_blocks():
    allowed, reason = decide(reviews=[review("CHANGES_REQUESTED")])
    assert (allowed, reason) == (False, "changes_requested")


def test_policy_missing_field_is_refused():
    with pytest.raises(mg.Unusable):
        mg.load_policy({"repository": {"name": "o/r", "base_branch": "main"}})


def test_ordinary_is_an_allowlist():
    policy = make_policy()
    assert mg.ordinary("README.md", policy)
    assert mg.ordinary("docs/anything/deep.md", policy)
    assert not mg.ordinary("scripts/engine.py", policy)


def test_the_example_policy_loads():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    policy = mg.load_policy(tomllib.loads((root / "examples/merge-guard.toml").read_text()))
    assert policy.repository == "mikomiel/ci-burst"
    assert policy.gate_context == "review"
    assert policy.dependency_update_exemption is False
