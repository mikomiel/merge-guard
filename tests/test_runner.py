"""Runner behaviour: the ci-burst guards the port must keep."""

import pytest

from scripts import merge_guard as runner
from scripts import merge_guard_policy as mg

HEAD = "a" * 40
BASE = "b" * 40
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


def pull(mergeable=True, changed=1, draft=False):
    return {"number": 1, "state": "open", "draft": draft, "mergeable": mergeable,
            "mergeable_state": "clean" if mergeable else "dirty", "changed_files": changed,
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
            "head": {"sha": HEAD}, "user": {"login": "author"}}


def check():
    return {"id": 1, "name": "checks", "head_sha": HEAD, "status": "completed",
            "conclusion": "success", "app": {"id": mg.ACTIONS_APP_ID}}


def status(state="success", description="approved_at_head"):
    return {"id": 1, "context": "review", "state": state, "description": description,
            "creator": {"login": "github-actions[bot]", "type": "Bot"}}


class Fake:
    def __init__(self, list_pull=None, full_pull=None, fail_state=None, merged=True):
        self.list_pull = list_pull or pull()
        self.full_pull = full_pull or pull()
        self.fail_state = fail_state
        self.merged = merged
        self.published = []
        self.merge_calls = []
        self.pull_calls = []

    def pulls(self):
        return [dict(self.list_pull, reviews=[review()], maintainers=[])]

    def pull(self, number):
        self.pull_calls.append(number)
        return dict(self.full_pull, reviews=[review()], maintainers=[])

    def reviews(self, number):
        return [review()]

    def check_runs(self, sha):
        return [check()]

    def statuses(self, sha):
        return [status()]

    def compare(self, base, head):
        return {"status": "ahead", "base_commit": {"sha": base},
                "merge_base_commit": {"sha": base}, "files": [{"filename": "README.md"}]}

    def branch_head(self, branch):
        return BASE

    def permission(self, login):
        return "write"

    def publish(self, sha, state, description):
        if self.fail_state == state:
            raise runner.ApiError("github_api_failed")
        self.published.append((state, description))

    def merge(self, number, sha):
        self.merge_calls.append((number, sha))
        if not self.merged:
            raise runner.ApiError("merge_postcondition_failed")


def test_merge_re_reads_each_pull_before_deciding():
    # The list entry has no mergeable/changed_files; only the re-read does.
    fake = Fake()
    decisions = runner.run_merge(fake, policy(), apply=True)
    assert fake.pull_calls == [1]
    assert decisions[0]["merged"] is True
    assert fake.merge_calls == [(1, HEAD)]


def test_merge_refuses_when_the_re_read_entry_is_dirty():
    fake = Fake(full_pull=pull(mergeable=False))
    decisions = runner.run_merge(fake, policy(), apply=False)
    assert decisions[0]["eligible"] is False
    assert decisions[0]["reason"] == "mergeability_not_clean"


def test_merge_postcondition_is_checked():
    fake = Fake(merged=False)
    with pytest.raises(runner.ApiError):
        fake.merge(1, HEAD)


def test_revocation_failure_publishes_no_success():
    class Blocking(Fake):
        def reviews(self, number):
            return [review("CHANGES_REQUESTED")]

    fake = Blocking(fail_state="failure")
    with pytest.raises(runner.ApiError):
        runner.run_review(fake, policy(), apply=True, event_head=None)
    assert fake.published == []


def test_success_is_published_only_after_revocations():
    fake = Fake()
    runner.run_review(fake, policy(), apply=True, event_head=None)
    assert fake.published == [("success", "approved_at_head")]


def test_a_failed_snapshot_revokes_the_known_head():
    fake = Fake()
    fake.pull = lambda number: dict(pull(), head={"sha": "d" * 40})
    with pytest.raises(runner.ApiError):
        runner.run_review(fake, policy(), apply=True, event_head=HEAD)
    assert fake.published == [("pending", "review_snapshot_unavailable")]


def test_pulls_are_filtered_to_the_base_branch():
    api = runner.GitHub(policy())
    seen = []

    def request(path, method="GET", body=None):
        seen.append(path)
        return []

    api.request = request
    api.pulls()
    assert seen == ["pulls?state=open&base=main&per_page=100&page=1"]


def test_check_runs_pagination_requires_the_full_total():
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


def test_publish_dedups_an_identical_latest_status():
    api = runner.GitHub(policy(), apply=True)
    posts = []
    api.statuses = lambda sha: [status()]
    api.request = lambda path, method="GET", body=None: posts.append((method, path))
    api.publish(HEAD, "success", "approved_at_head")
    assert [call for call in posts if call[0] != "GET"] == []
