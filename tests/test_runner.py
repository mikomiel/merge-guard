"""Runner behaviour: the ci-burst guards the port must keep."""

import pytest

from scripts import merge_guard as runner
from scripts import merge_guard_policy as mg

HEAD = "a" * 40
BASE = "b" * 40
OTHER = "d" * 40
MERGE = "e" * 40
REVIEWER_ID = 42


def policy():
    return mg.load_policy({
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {"reviewer_id": REVIEWER_ID, "reviewer_login": "reviewer[bot]",
                   "required_checks": ["checks"], "gate_context": "review"},
        "paths": {"ordinary": ["README.md"]},
    })


def review(state="APPROVED", commit=HEAD):
    return {"id": 1, "state": state, "commit_id": commit,
            "user": {"id": REVIEWER_ID, "login": "reviewer[bot]", "type": "Bot"}}


def pull(number=1, head=HEAD, mergeable=True, draft=False, state="open"):
    return {"number": number, "state": state, "draft": draft, "mergeable": mergeable,
            "mergeable_state": "clean" if mergeable else "dirty", "changed_files": 1,
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
            "head": {"sha": head}, "user": {"login": "author"}}


def check():
    return {"id": 1, "name": "checks", "head_sha": HEAD, "status": "completed",
            "conclusion": "success", "app": {"id": mg.ACTIONS_APP_ID}}


def status(state="success", description="approved_at_head", rid=1, login="github-actions[bot]"):
    return {"id": rid, "context": "review", "state": state, "description": description,
            "creator": {"login": login, "type": "Bot"}}


class Fake:
    def __init__(self, prs=None, reviews=None, fail_state=None, permission=None):
        self.prs = prs or [pull()]
        self.reviews_by = reviews or {}
        self.fail_state = fail_state
        self.permission = permission or {"user": {"id": 99}, "permission": "write"}
        self.published = []
        self.merged = []

    def pulls(self):
        return [{"number": pr["number"], "head": {"sha": pr["head"]["sha"]}} for pr in self.prs]

    def pull(self, number):
        return next(pr for pr in self.prs if pr["number"] == number)

    def reviews(self, number):
        return self.reviews_by.get(number, [self.reviews_by.get("default", [review()])][0])

    def check_runs(self, sha):
        return [check()]

    def statuses(self, sha):
        return [status()]

    def compare(self, base, head):
        return {"status": "ahead", "base_commit": {"sha": base},
                "merge_base_commit": {"sha": base}, "files": [{"filename": "README.md"}]}

    def branch_head(self, branch):
        return BASE

    def request(self, path, method="GET", body=None):
        if path.startswith("collaborators/"):
            return self.permission
        raise AssertionError(path)

    def publish(self, sha, state, description):
        if self.fail_state == state:
            raise runner.ApiError("github_api_failed")
        self.published.append((sha, state, description))

    def merge(self, number, sha):
        self.merged.append((number, sha))


def test_merge_re_reads_each_pull_before_deciding():
    # The list entry carries only number and head; mergeability comes from pull().
    fake = Fake()
    decisions = runner.run_merge(fake, policy(), apply=True)
    assert decisions[0]["merged"] is True
    assert fake.merged == [(1, HEAD)]


def test_merge_refuses_when_the_re_read_entry_is_dirty():
    fake = Fake(prs=[pull(mergeable=False)])
    decisions = runner.run_merge(fake, policy(), apply=False)
    assert decisions[0]["eligible"] is False
    assert decisions[0]["reason"] == "mergeability_not_clean"


def test_a_closed_pull_during_collection_is_skipped():
    fake = Fake(prs=[pull(state="closed")])
    assert runner.run_merge(fake, policy(), apply=False) == []


def test_success_is_published_only_after_revocations():
    fake = Fake()
    runner.run_review(fake, policy(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "success", "approved_at_head")]


def test_revocation_failure_publishes_no_success():
    prs = [pull(1, HEAD), pull(2, OTHER)]
    fake = Fake(prs=prs, reviews={2: [review("CHANGES_REQUESTED", commit=OTHER)],
                                  "default": [review()]},
                fail_state="failure")
    with pytest.raises(runner.ApiError):
        runner.run_review(fake, policy(), apply=True, event_head=None)
    assert fake.published == []


def test_a_failed_snapshot_revokes_every_known_head():
    class Shifting(Fake):
        def __init__(self):
            super().__init__(prs=[pull(1, HEAD)])
            self.calls = 0

        def pull(self, number):
            self.calls += 1
            return pull(1, HEAD) if self.calls == 1 else pull(1, OTHER)  # refs moved

    fake = Shifting()
    with pytest.raises(runner.ApiError):
        runner.run_review(fake, policy(), apply=True, event_head=OTHER)
    assert sorted(fake.published) == sorted([
        (HEAD, "pending", "review_snapshot_unavailable"),
        (OTHER, "pending", "review_snapshot_unavailable"),
    ])


def test_pulls_are_filtered_to_the_base_branch():
    api = runner.GitHub(policy())
    seen = []

    def request(path, method="GET", body=None):
        seen.append(path)
        return []

    api.request = request
    api.pulls()
    assert seen == ["pulls?state=open&base=main&per_page=100&page=1"]


def test_check_runs_pagination_requires_a_stable_total():
    api = runner.GitHub(policy())
    pages = {1: {"total_count": 101, "check_runs": [check()] * 100},
             2: {"total_count": 101, "check_runs": [check()]}}
    api.request = lambda path, method="GET", body=None: pages[int(path.rsplit("page=", 1)[1])]
    assert len(api.check_runs(HEAD)) == 101


def test_check_runs_pagination_mismatch_is_an_error():
    api = runner.GitHub(policy())
    api.request = lambda path, method="GET", body=None: {"total_count": 5, "check_runs": [check()]}
    with pytest.raises(runner.ApiError):
        api.check_runs(HEAD)


def test_check_runs_requires_an_integer_total():
    api = runner.GitHub(policy())
    api.request = lambda path, method="GET", body=None: {"total_count": "1", "check_runs": []}
    with pytest.raises(runner.ApiError):
        api.check_runs(HEAD)


def test_publish_dedups_only_an_unchanged_bot_status():
    api = runner.GitHub(policy(), apply=True)
    posts = []
    api.statuses = lambda sha: [status(), status(rid=2, state="pending", description="other")]
    api.request = lambda path, method="GET", body=None: posts.append((method, path))
    api.publish(HEAD, "success", "approved_at_head")
    # The highest id is "pending", so publishing success is required.
    assert [path for method, path in posts if method == "POST"]


def test_publish_skips_an_identical_official_status():
    api = runner.GitHub(policy(), apply=True)
    posts = []
    api.statuses = lambda sha: [status()]
    api.request = lambda path, method="GET", body=None: posts.append((method, path))
    api.publish(HEAD, "success", "approved_at_head")
    assert [path for method, path in posts if method == "POST"] == []


def test_merge_postcondition_checks_the_put_and_the_re_read():
    api = runner.GitHub(policy(), apply=True)
    api.request = lambda path, method="GET", body=None: {"merged": True, "sha": MERGE}
    api.pull = lambda number: {"merged": True, "state": "closed",
                               "head": {"sha": HEAD}, "merge_commit_sha": MERGE}
    api.merge(1, HEAD)

    api.pull = lambda number: {"merged": True, "state": "closed",
                               "head": {"sha": HEAD}, "merge_commit_sha": "f" * 40}
    with pytest.raises(runner.ApiError):
        api.merge(1, HEAD)


def test_maintainer_ids_binds_the_permission_to_the_reviewer_id():
    api = Fake(permission={"user": {"id": 7}, "permission": "write"})
    human = {"id": 1, "state": "APPROVED", "commit_id": HEAD,
             "user": {"id": 99, "login": "owner", "type": "User"}}
    with pytest.raises(runner.ApiError):
        runner.maintainer_ids(api, [human], "author")
    assert runner.maintainer_ids(api, [review()], "author") == set()


def test_publish_republishes_when_the_official_status_was_not_from_the_bot():
    api = runner.GitHub(policy(), apply=True)
    posts = []
    api.statuses = lambda sha: [status(login="someone-else")]
    api.request = lambda path, method="GET", body=None: posts.append((method, path))
    api.publish(HEAD, "success", "approved_at_head")
    assert [path for method, path in posts if method == "POST"]


OLD_HEAD = "c" * 40


def strict_policy():
    return mg.load_policy({
        "repository": {"name": "o/r", "base_branch": "main"},
        "review": {"reviewer_id": REVIEWER_ID, "reviewer_login": "reviewer[bot]",
                   "required_checks": ["checks"], "gate_context": "review",
                   "carry_unchanged_diff": False},
        "paths": {"ordinary": ["README.md"]},
    })


class Carrying(Fake):
    """The same patch at both heads, so carry-over is possible at all."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reviews_by = {"default": [review(commit="c" * 40)]}

    def compare(self, base, head):
        return {"status": "ahead", "base_commit": {"sha": base},
                "merge_base_commit": {"sha": base},
                "files": [{"filename": "README.md", "status": "modified",
                           "patch": "@@ -1 +1 @@\n-a\n+b"}]}


def test_a_carried_approval_merges_under_the_default_policy():
    fake = Carrying()
    decisions = runner.run_merge(fake, policy(), apply=True)
    assert decisions[0]["merged"] is True
    assert fake.merged == [(1, HEAD)]


def test_a_carried_approval_never_merges_under_a_strict_policy():
    fake = Carrying()
    decisions = runner.run_merge(fake, strict_policy(), apply=True)
    assert decisions[0]["eligible"] is False
    assert decisions[0]["reason"] == "current_head_approval_required"
    assert fake.merged == []


def test_a_strict_policy_publishes_pending_for_a_carried_approval():
    fake = Carrying()
    runner.run_review(fake, strict_policy(), apply=True, event_head=None)
    assert fake.published == [(HEAD, "pending", "current_head_approval_required")]
