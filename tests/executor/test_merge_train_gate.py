"""The batched merge train's gate routes against real repositories.

The train operation decides everything a train may do from the commit
graph -- the candidate chain, the tree equalities around each member merge,
and the whitespace-exact digest that carries a member's approval to its
train position. A runner answering `merge-tree` and `rev-parse` could only
restate those expectations, so git is real here: a bare origin, a managed
checkout, and member branches pushed from a scratch clone.

The forge is a stub at the backend JSON boundary, as in the real-git
behaviour tests, with one real effect: `merge_pr` performs
a genuine two-parent merge onto the origin's `main`, so the gate's tree
postcondition has something to verify. Successful branch deletion also consumes
Forgejo's automatic PR-close event eagerly for deterministic recovery tests.
`tamper` makes that merge land a
different tree, the case the postcondition exists for. CI is stubbed at
`_automerge_ci_status`: it answers green only for the exact `(base, head)`
pair a run is configured to have judged, which is the binding
`ci.evaluate_evidence` enforces. Like the real method, it re-binds to the
run's own base when the recorded-base review projection's `accept_run_base`
predicate accepts it, and then reports that base as `accepted_run_base`, and
it reports every answer of that predicate as `run_base_proof`; that
predicate is production code running on real git.
The CI evidence tests prove the re-binding itself against the Actions
evidence path.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from gate_server_estate import EstateGateOperations
from git_gate import catalog, ci, contracts, forge, operations
from git_gate.contracts import GateError, RepositoryPolicy
from git_gate.forge import _FORGE_BACKENDS
from git_gate.operations import deployment as deployment_operations
from git_gate.operations import landing
from git_gate.operations import train as train_operations
from git_gate.repository import _lock_busy_probe, run_command
from gitfixtures import clone_origin, commit_file, git, make_origin, rev_parse
from merge_open_prs_fixtures import QUEUE_STORE_TYPE, FakeGateApplication, merge_open_prs

ALIAS = "train"
REPOSITORY = f"test/{ALIAS}"


def _done(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def _by_head(rows: list[dict], total: int | None = None) -> subprocess.CompletedProcess[str]:
    """Forgejo's answer to one exact-head lookup: rows on stdout, status and count on stderr."""
    count = len(rows) if total is None else total
    return _done(json.dumps(rows), f"HTTP/2.0 200 OK\nX-Total-Count: {count}\n")


def _approval(head: str, base: str, claim_sha: str, review_id: int = 1) -> dict[str, object]:
    """A delegated approval of *head* as the reviewer saw it against *base*."""
    return {
        "id": review_id,
        "user": {"id": 7, "login": "reviewer-user"},
        "body": ("<!-- delegated-review.v1\nrun: test-review\n"
                 f"base: {base}\nworker: reviewer\nharness: example\nmodel: model-a\n"
                 f"claim: {claim_sha}\n-->"),
        "state": "APPROVED",
        "commit_id": head,
        "stale": True,
        "dismissed": False,
        "submitted_at": f"2020-01-01T00:00:0{review_id}Z",
    }


class TrainForge:
    """Forge stub: per-PR state, reviews and labels, and a real merge onto main."""

    def __init__(self, origin: Path, checkout: Path) -> None:
        self.origin = origin
        self.checkout = checkout
        self.pulls: dict[int, dict[str, object]] = {}
        self.reviews: dict[int, list[dict[str, object]]] = {}
        self.labels = {"gate/merge-train", "harness/alpha"}
        self.comments: list[tuple[int, str]] = []
        self.merges: list[tuple[int, str]] = []
        self.merge_messages: dict[int, str | None] = {}
        self.descriptions: dict[int, str] = {}
        self.head_lookups: list[str] = []
        self.tamper = False
        self.refusal: str | None = None
        self._next = 900

    def main(self) -> str:
        return rev_parse(self.origin, "refs/heads/main")

    def _head(self, pull: dict[str, object]) -> str:
        found = git(["rev-parse", "--verify", f"refs/heads/{pull['head']}"], self.origin,
                    check=False)
        return found.stdout.strip() if found.returncode == 0 else str(pull["head_sha"])

    def add(self, index: int, head_ref: str, *, title: str = "feat: member") -> None:
        self.pulls[index] = {"state": "open", "title": title, "head": head_ref,
                             "head_sha": "", "merged": False, "mergeable": True, "labels": []}
        self.pulls[index]["head_sha"] = self._head(self.pulls[index])

    # -- read side ------------------------------------------------------
    def branch_head_sha(self, runner, cwd, forge_repo, branch):
        """As Forgejo's branch API: the pushed head, or a 404 for none."""
        found = git(["rev-parse", "--verify", f"refs/heads/{branch}"], self.origin, check=False)
        if found.returncode != 0:
            return subprocess.CompletedProcess([], 1, '{"message": "Not Found"}',
                                               "Not Found (HTTP 404)")
        return _done(json.dumps({"name": branch, "commit": {"id": found.stdout.strip()}}))

    def read_pr_state(self, runner, cwd, forge_repo, index):
        """As Forgejo: `base.sha` is main's tip, `merge_base` the head's fork point."""
        if index not in self.pulls:
            return _done('{"message":"Not Found"}', "HTTP/2.0 404 Not Found\n")
        pull = self.pulls[index]
        fork = git(["merge-base", "refs/heads/main", self._head(pull)], self.origin, check=False)
        return _done(json.dumps({
            "number": index, "id": pull.get("id", index),
            "created_at": pull.get("created_at", "2020-01-01T00:00:00Z"),
            "state": pull["state"], "title": pull["title"],
            "merged": pull["merged"], "mergeable": pull["mergeable"],
            "base": {"ref": "main", "sha": self.main(), "repo": {"full_name": forge_repo}},
            "head": {"ref": pull.get("native_head_ref", pull["head"]),
                     "label": pull.get("native_label", pull["head"]), "sha": self._head(pull),
                     # A fork PR names its own repository; its head branch is not origin's.
                     "repo": {"full_name": pull.get("head_repo", forge_repo)}},
            # A test may name another value, None included, for a forge
            # record without a valid fork point.
            "merge_base": pull.get("merge_base", fork.stdout.strip()),
            "body": pull.get("body", ""),
            "labels": pull.get("labels", []),
            "updated_at": pull.get("updated_at", "2020-01-01T00:00:00Z"),
        }))

    def list_pulls_by_head(self, runner, cwd, forge_repo, head):
        """As Forgejo does: `state=all&head=<name>` matches that
        exact head name only, in either state, and still finds a PR whose branch was
        deleted, whose API `head.ref` is then `refs/pull/<n>/head` and `head.label` the name.
        `-i` puts the status line and `X-Total-Count` on stderr."""
        self.head_lookups.append(head)
        rows = [json.loads(self.read_pr_state(runner, cwd, forge_repo, index).stdout)
                for index, pull in sorted(self.pulls.items(), reverse=True)
                if pull.get("native_label", pull["head"]) == head]
        return _by_head(rows)

    def list_pulls(self, runner, cwd, forge_repo, page, page_size):
        rows = [{"index": index, "state": "open", "base": "main", "base-commit": self.main(),
                 "head": pull["head"], "title": pull["title"]}
                for index, pull in self.pulls.items() if pull["state"] == "open"]
        return _done(json.dumps(rows if page == 1 else []))

    def list_reviews(self, runner, cwd, forge_repo, index, page, page_size):
        return _done(json.dumps(self.reviews.get(index, []) if page == 1 else []))

    def list_labels(self, runner, cwd, forge_repo):
        return _done(json.dumps([{"name": name} for name in sorted(self.labels)]))

    def list_check_runs(self, runner, cwd, forge_repo, sha, limit):
        """No commit statuses: the server status read reads no `gate/queue` text."""
        return _done("[]")

    def create_label(self, runner, cwd, forge_repo, name, color, description):
        self.labels.add(name)
        return _done()

    def refresh_auth(self, runner, cwd):
        return _done()

    # -- write side -----------------------------------------------------
    def create_pr(self, runner, cwd, forge_repo, title, head, base, label=None):
        self._next += 1
        self.add(self._next, head, title=title)
        git(["update-ref", f"refs/pull/{self._next}/head", self._head(self.pulls[self._next])], self.origin)
        self.pulls[self._next]["labels"] = [label] if label else []
        return _done("{}")

    def write_description(self, runner, cwd, forge_repo, index, description):
        self.descriptions[index] = description
        return _done()

    def add_pr_comment(self, runner, cwd, forge_repo, index, body):
        self.comments.append((index, body))
        return _done()

    def close_pr(self, runner, cwd, forge_repo, index):
        self.pulls[index]["state"] = "closed"
        return _done("{}", "HTTP/1.1 201 Created")

    def merge_pr(self, runner, cwd, forge_repo, index, expected_head_sha, *,
                 merge_message=None):
        """Merge *expected_head_sha* onto main as Forgejo's `Do=merge` would.

        *merge_message* is `MergeMessageField`: Forgejo appends it as the body
        under its own title line.
        """
        pull = self.pulls[index]
        if self._head(pull) != expected_head_sha:
            return _done(code=1)
        if self.refusal is not None:  # `tea api` exits 0 on an HTTP error status
            return _done('{"message":"refused"}', self.refusal)
        main = self.main()
        git(["fetch", "-q", str(self.origin), f"refs/heads/{pull['head']}"], self.checkout)
        tree = git(["merge-tree", "--write-tree", main, expected_head_sha],
                   self.checkout).stdout.split()[0]
        if self.tamper:
            tree = git(["rev-parse", f"{main}^{{tree}}"], self.checkout).stdout.strip()
        message = f"Merge pull request #{index}"
        if merge_message is not None:
            message += f"\n\n{merge_message}"
        self.merge_messages[index] = merge_message
        commit = git(["commit-tree", tree, "-p", main, "-p", expected_head_sha,
                      "-m", message], self.checkout).stdout.strip()
        git(["push", "-q", str(self.origin), f"{commit}:refs/heads/main"], self.checkout)
        pull.update(state="closed", merged=True, head_sha=expected_head_sha)
        self.merges.append((index, expected_head_sha))
        return _done("{}")


class TrainWorld:
    """Origin, managed checkout, a scratch clone for authors, forge and CI."""

    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch, *,
                 policy: str = "train-v1", merge_candidate: bool = False) -> None:
        fixture = make_origin(base, clone_name="managed")
        self.origin = fixture.origin
        self.checkout = fixture.clone
        self.author = clone_origin(fixture, base / "author")
        commit_file(self.author, "shared.txt", "".join(f"s{n}\n" for n in range(1, 13)),
                    "shared")
        git(["push", "-q", "origin", "main"], self.author)
        self.reviewed_base = rev_parse(self.author)
        lock_parent = base / "run" / "lock"
        lock_dir = lock_parent / "merge-guard"
        lock_dir.mkdir(parents=True)
        os.chmod(lock_dir, 0o700)
        monkeypatch.setattr(contracts, "GATE_LOCK_DIR", lock_dir)
        monkeypatch.setattr(contracts, "GATE_LOCK_PARENT", lock_parent)
        self.policy = RepositoryPolicy(
            name=REPOSITORY, remote_url=str(self.origin), managed_checkout=str(self.checkout),
            task_root=str(base / "worktrees"), forge="forgejo", forge_repository=REPOSITORY,
            automatic_review_workflow="automerge-review-v1",
            review_disclosure_policy="external-reviewer-v1",
            review_reuse_policy="clean-refresh-v1", merge_batch_policy=policy,
            # CI narrows every head that is not the merge candidate.
            merge_candidate_policy="label-v1" if merge_candidate else "",
        )
        monkeypatch.setattr(catalog, "CLOSED_REPOSITORIES", {REPOSITORY: self.policy})
        monkeypatch.setattr(catalog, "CLOSED_ALIASES", frozenset({ALIAS}))
        self.forge = TrainForge(self.origin, self.checkout)
        monkeypatch.setitem(_FORGE_BACKENDS, "forgejo", self.forge)
        self.gate = operations.GitMaintainerGate(runner=self._run_with_forge_events, cwd=self.checkout)
        # The (base, head) pair each PR's newest trusted run judged.
        self.runs: dict[int, tuple[str, str]] = {}
        self.ci_calls: list[tuple[int, str, str]] = []
        self.failed_runs: set[int] = set()
        # PRs whose newest run is the narrowed `checks/c2` run of a C1-class head.
        self.narrowed: set[int] = set()
        # PRs whose newest run is the `checks/c2` run of a C2-class head: green
        # on the class's own profile, insufficient where a merge candidate's
        # `checks/c1` is required, so the two reads of a marked head differ.
        self.c2_class: set[int] = set()
        # PRs whose CI read is the real evaluator's answer for these keyword
        # arguments to `ci.evaluate_evidence` (a failed listing, an ambiguous run).
        self.evaluated: dict[int, dict[str, object]] = {}
        monkeypatch.setattr(self.gate, "_automerge_ci_status", self._ci)

    def _run_with_forge_events(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        result = run_command(argv, cwd)
        if (result.returncode == 0 and len(argv) > 1 and argv[1] == "push"
                and argv[-1].startswith(":refs/heads/")):
            # Forgejo can close branch PRs asynchronously after a push.
            # Consume that event eagerly so owner loss is deterministic here.
            branch = argv[-1].removeprefix(":refs/heads/")
            for index, pull in self.forge.pulls.items():
                if pull["head"] == branch:
                    pull["state"] = "closed"
                    pull["native_head_ref"] = f"refs/pull/{index}/head"
        return result

    def _ci(self, context, pull, head_sha, *, accept_run_base=None, merge_candidate=False,
            task_pages=None):
        self.ci_calls.append((pull.index, pull.base_commit, head_sha))
        if pull.index in self.evaluated:
            evaluated = ci.evaluate_evidence(**{
                "repository": REPOSITORY, "pull_request": pull.index,
                "base_sha": pull.base_commit, "head_sha": head_sha,
                "required_profile": ci.STRONGEST_PROFILE, "run": None, "jobs": None,
                "launcher_file_exists": True, "api_failure": False,
                "malformed_response": False, "observed_at": "2020-01-01T00:00:00+00:00",
                **self.evaluated[pull.index]})
            return {"verdict": ci.derive_verdict(evaluated)[0], "ci_evidence": evaluated}
        judged = self.runs.get(pull.index)
        if judged is None:
            return {"verdict": "pending", "ci_evidence": {"result": "UNAVAILABLE",
                                                          "reason_code": "CI_RUN_MISSING"}}
        run_base, run_head = judged
        evidence = {"head_sha": head_sha, "base_sha": pull.base_commit,
                    "pull_request": pull.index, "workflow_run_id": 50 + pull.index,
                    "workflow_run_attempt": 1, "workflow_run_conclusion": "success",
                    "accepted_profile": "checks/c1", "required_profile": "checks/c1",
                    "observed_profiles": ["checks/c1"]}
        if run_head != head_sha:
            return {"verdict": "pending",
                    "ci_evidence": {**evidence, "result": "UNAVAILABLE",
                                    "reason_code": "CI_HEAD_STALE"}}
        # As `_automerge_ci_status`: whatever the re-read finds, the answer names
        # the base it re-bound to, and only then; and every answer the predicate
        # gave, True, False or None (it could not decide), as `run_base_proof`.
        rebound: dict[str, str] = {}
        if run_base != pull.base_commit:
            proven = None if accept_run_base is None else accept_run_base(run_base)
            if proven is not True:
                return {"verdict": "pending",
                        "ci_evidence": {**evidence, "result": "UNAVAILABLE",
                                        "reason_code": "CI_BASE_STALE"},
                        **({} if accept_run_base is None else {
                            "run_base_proof": "refused" if proven is False else "unknown"})}
            evidence["base_sha"] = run_base
            rebound = {"accepted_run_base": run_base, "run_base_proof": "proven"}
        if pull.index in self.narrowed or (merge_candidate and pull.index in self.c2_class):
            # A C1-class head's `c2` run on an accepted base: the gate reads it as
            # unavailable, never green. It judges the profile before the
            # identity job's conclusion, so a failed run reads the same way.
            failed = pull.index in self.failed_runs
            return {"verdict": "unavailable", "ci_evidence": {
                **evidence, "result": "UNAVAILABLE", "reason_code": "CI_PROFILE_INSUFFICIENT",
                "required_profile": ci.STRONGEST_PROFILE, "observed_profiles": ["checks/c2"],
                "accepted_profile": "checks/c2",
                "workflow_run_conclusion": "failure" if failed else "success"}, **rebound}
        if pull.index in self.failed_runs:
            return {"verdict": "red", "ci_evidence": {
                **evidence, "result": "FAIL", "reason_code": "CI_RUN_FAILED",
                "workflow_run_conclusion": "failure"}, **rebound}
        if pull.index in self.c2_class:
            return {"verdict": "green", "ci_evidence": {
                **evidence, "result": "PASS", "required_profile": "checks/c2",
                "observed_profiles": ["checks/c2"], "accepted_profile": "checks/c2"},
                **rebound}
        if self.policy.merge_candidate_policy and pull.head.startswith("train/"):
            # A `train/` head is the merge candidate: checks.yml runs it in full.
            evidence["accepted_profile"] = ci.STRONGEST_PROFILE
        return {"verdict": "green", "ci_evidence": {**evidence, "result": "PASS"}, **rebound}

    # -- authors ----------------------------------------------------------
    def member(self, index: int, path: str, content: str, *, from_ref: str | None = None,
               native_renovate: bool = False) -> str:
        """Push one approved member branch whose own CI passed on its fork base."""
        branch = f"{'renovate' if native_renovate else 'task'}/member-{index}"
        git(["checkout", "-q", "-B", branch, from_ref or self.reviewed_base], self.author)
        head = commit_file(self.author, path, content, f"feat: member {index}")
        git(["push", "-q", "origin", f"{branch}:refs/heads/{branch}"], self.author)
        git(["checkout", "-q", "main"], self.author)
        self.forge.add(index, branch)
        fields = {field: "test claim" for field in contracts.CAPSULE_V2_FIELDS}
        description = (
            f"Renovate dependency update for member {index}"
            if native_renovate else self.gate._build_pr_description_v2(
                fields, head, self.reviewed_base, ["PASS"]
            )
        )
        self.forge.pulls[index]["body"] = description
        claim_sha = self.gate._claim_identity(description, head,
                                              native_claim=native_renovate)
        assert claim_sha is not None
        self.forge.reviews[index] = [_approval(head, self.reviewed_base, claim_sha)]
        self.runs[index] = (self.reviewed_base, head)
        return head

    def advance_main(self, path: str = "main.txt", content: str = "main moved\n") -> str:
        git(["checkout", "-q", "main"], self.author)
        git(["pull", "-q", "--ff-only", "origin", "main"], self.author)
        sha = commit_file(self.author, path, content, "chore: main moves")
        git(["push", "-q", "origin", "main"], self.author)
        return sha

    def replace_claim(self, index: int, head: str, intent: str = "changed claim") -> None:
        """Give one member a different valid review claim and a fresh approval.

        The head and the code are unchanged; only the reviewer's input
        identity changes, which is exactly the case a v2 generation freezes.
        """
        fields = {field: "test claim" for field in contracts.CAPSULE_V2_FIELDS}
        fields["Intent"] = intent
        description = self.gate._build_pr_description_v2(
            fields, head, self.reviewed_base, ["PASS"])
        self.forge.pulls[index]["body"] = description
        claim = self.gate._claim_identity(description, head)
        assert claim is not None
        self.forge.reviews[index] = [_approval(head, self.reviewed_base, claim, review_id=7)]

    def replace_provenance(self, index: int, head: str, run: str = "changed-review") -> None:
        """Keep the claim and head, but present a different review provenance."""
        claim = self.gate._claim_identity(self.forge.pulls[index]["body"], head)
        assert claim is not None
        self.forge.reviews[index] = [{
            "id": 8,
            "user": {"id": 7, "login": "reviewer-user"},
            "body": ("<!-- delegated-review.v1\n"
                     f"run: {run}\nbase: {self.reviewed_base}\nworker: reviewer\n"
                     "harness: example\nmodel: model-a\n"
                     f"claim: {claim}\n-->"),
            "state": "APPROVED",
            "commit_id": head,
            "stale": True,
            "dismissed": False,
            "submitted_at": "2020-01-01T00:00:08Z",
        }]

    def move_member_head(self, index: int, head: str, content: str) -> str:
        """Advance a member branch; the native PR head follows the branch ref."""
        branch = str(self.forge.pulls[index]["head"])
        git(["checkout", "-q", "-B", branch, head], self.author)
        moved = commit_file(self.author, f"member-{index}-extra.txt", content,
                            f"feat: member {index} moves")
        git(["push", "-q", "origin", f"{branch}:refs/heads/{branch}"], self.author)
        git(["checkout", "-q", "main"], self.author)
        self.forge.pulls[index]["head_sha"] = moved
        return moved

    # -- gate -------------------------------------------------------------
    def execute(self, operation: str, **request: object) -> dict[str, object]:
        return self.gate.execute(operation, ALIAS, train_request=request)

    def build(self, members: list[tuple[int, str]]) -> dict[str, object]:
        return self.execute(contracts.TRAIN_BUILD, expected_base=self.forge.main(),
                            members=[{"pr": pr, "head": head} for pr, head in members])

    def merge(self, train: dict[str, object], pr: int, head: str) -> dict[str, object]:
        return self.execute(contracts.TRAIN_MEMBER_MERGE, train_pr=train["train_pr"],
                            member_pr=pr, member_head=head)

    def green(self, train: dict[str, object]) -> None:
        for prefix in train["prefixes"]:
            self.runs[int(prefix["pr"])] = (str(train["base_sha"]), str(prefix["candidate"]))

    def tree(self, ref: str) -> str:
        git(["fetch", "-q", "origin", "main:refs/remotes/origin/main"], self.checkout)
        return rev_parse(self.checkout, f"{ref}^{{tree}}")

    def push_main_with_message(self, message: str) -> str:
        """Put a synthetic commit on origin/main with a chosen message body."""
        self.tree("origin/main")
        main = rev_parse(self.checkout, "refs/remotes/origin/main")
        tree = git(["rev-parse", f"{main}^{{tree}}"], self.checkout).stdout.strip()
        commit = git(["commit-tree", tree, "-p", main, "-m", message],
                     self.checkout).stdout.strip()
        git(["push", "-q", "origin", f"{commit}:refs/heads/main"], self.checkout)
        return commit

    def origin_has(self, branch: str) -> bool:
        return git(["rev-parse", "--verify", f"refs/heads/{branch}"], self.origin,
                   check=False).returncode == 0


@pytest.fixture
def world(tmp_path, monkeypatch):
    return TrainWorld(tmp_path, monkeypatch)


@pytest.fixture
def world_v2(tmp_path, monkeypatch):
    return TrainWorld(tmp_path, monkeypatch, policy="train-v2", merge_candidate=True)


def _three_members(world: TrainWorld) -> list[tuple[int, str]]:
    members = [(pr, world.member(pr, f"m{pr}.txt", f"member {pr}\n")) for pr in (11, 12, 13)]
    world.advance_main()  # Each member's own run judged an older base.
    return members


def test_native_renovate_leader_can_join_a_train(world):
    first = world.member(11, "renovate.txt", "dependency update\n",
                         native_renovate=True)
    second = world.member(12, "member.txt", "regular update\n")
    world.advance_main()

    train = world.build([(11, first), (12, second)])

    assert [member["pr"] for member in train["members"]] == [11, 12]


def test_three_clean_members_merge_one_by_one_under_the_train_basis(world):
    world.forge.labels.remove("gate/merge-train")  # First use provisions the definition.
    members = _three_members(world)
    base = world.forge.main()

    train = world.build(members)

    assert train["code"] == "TRAIN_BUILT"
    assert train["base_sha"] == base
    assert [member["pr"] for member in train["members"]] == [11, 12, 13]
    assert train["dropped"] == []
    branch = f"train/{base[:12]}-11"
    assert train["train_branch"] == branch
    assert rev_parse(world.origin, f"refs/heads/{branch}") == train["candidate_sha"]
    opened = world.forge.pulls[int(train["train_pr"])]
    assert opened["title"] == "WIP: merge-train #11, #12, #13"
    assert opened["labels"] == ["gate/merge-train"]
    # The chain on the pushed branch is the members' heads merged in order.
    candidates = [member["candidate"] for member in train["members"]]
    for (pr, head), candidate, before in zip(members, candidates, [base, *candidates]):
        assert git(["log", "-1", "--format=%P%n%s", candidate], world.checkout).stdout.split("\n")[:2] \
            == [f"{before} {head}", f"Merge PR #{pr} into train"]

    world.green(train)
    world.ci_calls.clear()
    for (pr, head), candidate in zip(members, candidates):
        merged = world.merge(train, pr, head)
        assert merged["code"] == "TRAIN_MEMBER_MERGED"
        assert merged["ci_basis"] == "train"
        assert merged["approval_basis"] == "exact"
        assert merged["candidate_sha"] == candidate
        assert world.tree("origin/main") == world.tree(candidate)
    assert world.forge.merges == members
    # Each native merge reads its matching prefix run on the original base,
    # including after main moved under members 2 and 3.
    assert set(world.ci_calls) == {
        (prefix["pr"], base, prefix["candidate"]) for prefix in train["prefixes"]
    }

    closed = world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"],
                           reason="merged #11, #12, #13")
    assert closed["code"] == "TRAIN_CLOSED"
    assert closed["branch_deleted"] is True
    assert world.forge.pulls[int(train["train_pr"])]["state"] == "closed"
    assert world.forge.pulls[int(train["train_pr"])]["merged"] is False
    assert not world.origin_has(branch)


def test_train_member_merge_records_the_member_lane_not_the_technical_train(
    world: TrainWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_root = tmp_path / 'logs'
    monkeypatch.setenv('OPERATION_LOG_ROOT', str(log_root))
    monkeypatch.setenv('OPERATION_LOG_PARENT_ID', 'f' * 32)
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    pr, head = members[0]
    assert world.merge(train, pr, head)['code'] == 'TRAIN_MEMBER_MERGED'
    events = [json.loads(line) for path in log_root.glob('gate/*.jsonl') for line in path.read_text().splitlines()]
    merged = [event for event in events if event['event_name'] == 'PR_MERGED']
    assert len(merged) == 1
    attributes = merged[0]['attributes']
    assert attributes['operation'] == 'train-member-merge'
    assert attributes['pull_request'] == pr
    assert attributes['revision'] == head
    assert merged[0]['parent_run_id'] == 'f' * 32
    assert attributes['lane_key'] == 'f594a7736e489f1abe5373f73d7b8b89aca774e1e6f6faba445fa7b77319abdf'
    assert world.forge.pulls[pr]['head'] not in json.dumps(events)
    assert train['train_branch'] not in json.dumps(events)


def test_a_conflicting_member_is_dropped_and_the_rest_ride(world):
    first = world.member(11, "shared.txt", "".join(f"s{n}\n" for n in range(1, 12)) + "ONE\n")
    second = world.member(12, "shared.txt", "".join(f"s{n}\n" for n in range(1, 12)) + "TWO\n")
    third = world.member(13, "m13.txt", "member 13\n")
    world.advance_main()

    train = world.build([(11, first), (12, second), (13, third)])

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "PR_REFRESH_CONFLICT"}]


def test_too_few_members_after_a_conflict_leaves_nothing_on_the_forge(world):
    first = world.member(11, "shared.txt", "".join(f"s{n}\n" for n in range(1, 12)) + "ONE\n")
    second = world.member(12, "shared.txt", "".join(f"s{n}\n" for n in range(1, 12)) + "TWO\n")
    world.advance_main()
    before = set(world.forge.pulls)

    with pytest.raises(GateError) as refused:
        world.build([(11, first), (12, second)])

    assert refused.value.code == "TRAIN_TOO_SHORT"
    assert refused.value.context["dropped"] == [{"pr": 12, "reason": "PR_REFRESH_CONFLICT"}]
    assert set(world.forge.pulls) == before
    listed = git(["for-each-ref", "--format=%(refname)", "refs/heads/train"], world.origin)
    assert listed.stdout == ""


def test_a_member_whose_reviewed_diff_differs_from_its_position_is_refused(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    assert world.merge(train, *members[0])["code"] == "TRAIN_MEMBER_MERGED"
    main_after_first = world.forge.main()
    # The only approval of #12 now names a base that is not its fork point:
    # the diff that reviewer read is not the diff #12 adds to the train.
    claim_sha = world.gate._claim_identity(world.forge.pulls[12]["body"], members[1][1])
    assert claim_sha is not None
    world.forge.reviews[12] = [_approval(members[1][1], main_after_first, claim_sha, review_id=2)]

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[1])

    assert refused.value.code == "PR_REVIEW_NOT_APPROVED"
    assert world.forge.main() == main_after_first
    assert world.forge.merges == [members[0]]


def test_a_member_whose_digest_differs_at_build_leaves_the_train(world):
    members = _three_members(world)
    claim_sha = world.gate._claim_identity(world.forge.pulls[12]["body"], members[1][1])
    assert claim_sha is not None
    world.forge.reviews[12] = [_approval(members[1][1], world.forge.main(), claim_sha, review_id=2)]

    train = world.build(members)

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "PR_REVIEW_NOT_APPROVED"}]


def test_main_moving_under_the_train_refuses_before_any_forge_merge(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    world.advance_main("elsewhere.txt", "someone else landed\n")

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code == "TRAIN_BASE_MOVED"
    assert world.forge.merges == []


def _main_on_chain(world: TrainWorld, train: dict[str, object]) -> object:
    context = world.gate.managed_checkout_context(ALIAS)
    return world.gate.inspect_train_by_index(context, int(train["train_pr"]))["main_on_chain"]


def test_main_on_chain_is_false_once_main_carries_an_unrelated_tree(world):
    train = world.build(_three_members(world))
    assert _main_on_chain(world, train) is True

    world.advance_main("elsewhere.txt", "someone else landed\n")

    assert _main_on_chain(world, train) is False


def test_main_on_chain_stays_true_after_an_in_order_hand_merge(world):
    """An operator merging member 1 by hand lands candidate 1's tree: the
    train can still merge member 2, so the executor must not release it."""
    members = _three_members(world)
    train = world.build(members)

    world.forge.merge_pr(None, world.checkout, REPOSITORY, *members[0])

    assert world.tree("origin/main") == world.tree(str(train["members"][0]["candidate"]))
    assert _main_on_chain(world, train) is True


def test_main_on_chain_is_false_after_an_out_of_order_hand_merge(world):
    members = _three_members(world)
    train = world.build(members)

    world.forge.merge_pr(None, world.checkout, REPOSITORY, *members[1])

    assert _main_on_chain(world, train) is False


def test_a_merge_that_lands_another_tree_is_an_integrity_failure(
    world: TrainWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_root = tmp_path / 'logs'
    monkeypatch.setenv('OPERATION_LOG_ROOT', str(log_root))
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    world.forge.tamper = True

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code == "TRAIN_TREE_MISMATCH"
    assert refused.value.context["candidate_sha"] == train["members"][0]["candidate"]
    events = [json.loads(line) for path in log_root.glob('gate/*.jsonl') for line in path.read_text().splitlines()]
    assert not any(event['event_name'] == 'PR_MERGED' for event in events)


def test_a_member_merge_the_forge_refuses_is_pr_merge_failed(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    main_before = world.forge.main()
    world.forge.refusal = "HTTP/1.1 405 Method Not Allowed\n"

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code == "PR_MERGE_FAILED"
    assert refused.value.context["http_status"] == 405
    assert "mergeable" not in refused.value.context
    assert world.forge.main() == main_before


def test_member_merge_needs_the_train_run_on_the_train_base(world):
    members = _three_members(world)
    train = world.build(members)
    # Even a run of the right prefix must bind the original train base.
    prefix = train["prefixes"][0]
    world.runs[int(prefix["pr"])] = (world.reviewed_base, str(prefix["candidate"]))

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code == "PR_CI_NOT_GREEN"
    assert world.forge.merges == []


def test_neither_automerge_nor_the_manual_merge_path_accepts_train_evidence(world):
    """The train's green run lands a member only through `train_member_merge`."""
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    context = world.gate.managed_checkout_context(ALIAS)
    pr, head = members[0]

    # AUTOMERGE_PR reads the member's own run, which judged an older base.
    with pytest.raises(GateError) as automerge:
        world.gate.execute(contracts.AUTOMERGE_PR, ALIAS, str(pr),
                           expected_head_sha=head, expected_base_sha=world.forge.main())
    assert automerge.value.code == "PR_CI_NOT_GREEN"

    # The manual path's shared merge step, handed the train's own green
    # evidence for this member, still refuses: it binds the member's head.
    train_ci = world.gate._train_ci_status(
        context, world.gate.pull_by_index(context, int(train["train_pr"])),
        str(train["candidate_sha"]), str(train["base_sha"]))
    assert train_ci["verdict"] == "green"
    with pytest.raises(GateError) as manual:
        world.gate._merge_approved_pull(
            context, world.gate.pull_by_index(context, pr), head, world.forge.main(),
            train_ci, operation="merge-pr")
    assert manual.value.code == "PR_CI_NOT_GREEN"
    assert world.forge.merges == []


def test_a_draft_is_admitted_only_for_a_train_branch(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    context = world.gate.managed_checkout_context(ALIAS)

    with pytest.raises(GateError) as draft:
        world.gate.inspect_pr_by_index(context, int(train["train_pr"]))
    assert draft.value.code == "PR_DRAFT"

    world.forge.add(20, "task/draft-work", title="WIP: not a train")
    with pytest.raises(GateError) as not_train:
        world.gate.inspect_train_by_index(context, 20)
    assert not_train.value.code == "TRAIN_INVALID"

    read = world.gate.inspect_train_by_index(context, int(train["train_pr"]))
    assert read["pr_state"] == "open"
    assert read["ci_state"] == "green"
    assert read["base_sha"] == train["base_sha"]
    assert read["members"] == train["members"]


def _force_push(world: TrainWorld, branch: str, start: str, path: str) -> str:
    """Rewrite *branch* on the origin to a commit that does not descend from it."""
    git(["fetch", "-q", "origin"], world.author)
    git(["checkout", "-q", "-B", "rewrite", start], world.author)
    head = commit_file(world.author, path, "rewritten\n", "feat: rewritten")
    git(["push", "-q", "--force", "origin", f"rewrite:refs/heads/{branch}"], world.author)
    git(["checkout", "-q", "main"], world.author)
    return head


def test_a_force_pushed_member_head_is_fetched_and_still_bound_to_the_forge(world, monkeypatch):
    first = world.member(11, "m11.txt", "member 11\n")
    context = world.gate.managed_checkout_context(ALIAS)
    assert world.gate.inspect_pr_by_index(context, 11)["head_sha"] == first

    # Renovate rebases: the tracking ref's update is not a fast-forward.
    rewritten = _force_push(world, "task/member-11", world.reviewed_base, "m11.txt")
    world.runs[11] = (world.reviewed_base, rewritten)
    assert world.gate.inspect_pr_by_index(context, 11)["head_sha"] == rewritten

    # The fetched ref is a cache: a forge head that differs still refuses.
    monkeypatch.setattr(world.forge, "_head", lambda _pull: first)
    with pytest.raises(GateError) as refused:
        world.gate.inspect_pr_by_index(context, 11)
    assert refused.value.code == "REMOTE_HEAD_MISMATCH"


def test_a_force_pushed_train_head_is_fetched_then_judged_by_its_chain(world):
    train = world.build(_three_members(world))
    context = world.gate.managed_checkout_context(ALIAS)
    assert world.gate.inspect_train_by_index(context, int(train["train_pr"]))["pr_state"] == "open"

    _force_push(world, str(train["train_branch"]), str(train["base_sha"]), "t.txt")

    with pytest.raises(GateError) as refused:
        world.gate.inspect_train_by_index(context, int(train["train_pr"]))
    assert refused.value.code == "TRAIN_INVALID"


def test_review_inspection_accepts_green_ci_on_the_recorded_base(world):
    """Review may read a PR on its fork point when its own run judged that pair.

    Main moved after the push, so on main's tip the run is `CI_BASE_STALE`.
    Only the review caller passes `review_base`; every other inspection,
    and the merge that follows, still reads main's tip."""
    head = world.member(11, "m11.txt", "member 11\n")
    recorded = world.reviewed_base
    world.advance_main()
    context = world.gate.managed_checkout_context(ALIAS)

    on_main = world.gate.inspect_pr_by_index(context, 11)
    assert on_main["ci_state"] == "pending"
    assert on_main["blocking_reason"] == "CI_BASE_STALE"
    assert on_main["recorded_base_sha"] == recorded

    reviewed = world.gate.inspect_pr_by_index(context, 11, review_base=recorded)
    assert (reviewed["head_sha"], reviewed["base_sha"]) == (head, recorded)
    assert reviewed["ci_state"] == "green"
    assert reviewed["_pull"].base_commit == world.forge.main()

    # The run judged another head, or a base the recorded base does not
    # reach: the existing refusal. A run base between the recorded base and
    # main's tip is the next test's subject.
    world.runs[11] = (recorded, "f" * 40)
    assert world.gate.inspect_pr_by_index(
        context, 11, review_base=recorded)["ci_state"] != "green"
    world.runs[11] = (rev_parse(world.checkout, f"{recorded}~1"), head)
    assert world.gate.inspect_pr_by_index(
        context, 11, review_base=recorded)["ci_state"] != "green"

    # Any other base projects main's tip, which the review caller refuses.
    world.runs[11] = (recorded, head)
    other = world.gate.inspect_pr_by_index(context, 11, review_base=head)
    assert other["base_sha"] == world.forge.main()
    assert other["ci_state"] == "pending"


def test_review_inspection_fetches_only_under_the_checkout_fetch_lock(world, monkeypatch):
    """a concurrent review of the same repository cannot race these fetches."""
    import fcntl

    world.member(11, "m11.txt", "member 11\n")
    context = world.gate.managed_checkout_context(ALIAS)
    lock = Path(context.cwd) / ".git" / "gate-fetch.lock"
    runner = world.gate._runner
    held = []

    def probing(argv, cwd, **kwargs):
        if list(argv[1:2]) == ["fetch"]:
            fd = os.open(lock, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return runner(argv, cwd, **kwargs)

    monkeypatch.setattr(world.gate, "_runner", probing)

    assert world.gate.inspect_pr_by_index(context, 11)["head_sha"]
    # The head fetch and the main fetch.
    assert held == [True, True]


def test_review_inspection_accepts_a_run_on_main_as_it_was_at_the_push(world):
    """The production shape: the head forks from an older main, its run
    judged main's tip at the push, and main moved on again before review.

    The run tested the head's tree; its base, which descends from the fork
    point and which main still contains, only selected the checks. The
    review range stays fork point..head."""
    head = world.member(11, "m11.txt", "member 11\n")
    recorded = world.reviewed_base
    at_push = world.advance_main("main1.txt", "main at the push\n")
    world.runs[11] = (at_push, head)
    world.advance_main("main2.txt", "main after the push\n")
    context = world.gate.managed_checkout_context(ALIAS)

    on_main = world.gate.inspect_pr_by_index(context, 11)
    assert (on_main["ci_state"], on_main["blocking_reason"]) == ("pending", "CI_BASE_STALE")
    assert on_main["recorded_base_sha"] == recorded

    reviewed = world.gate.inspect_pr_by_index(context, 11, review_base=recorded)
    assert (reviewed["head_sha"], reviewed["base_sha"]) == (head, recorded)
    assert reviewed["ci_state"] == "green"
    assert reviewed["_ci_status"]["ci_evidence"]["base_sha"] == at_push

    # Main's own tip is the upper bound, and is inside it.
    world.runs[11] = (world.forge.main(), head)
    assert world.gate.inspect_pr_by_index(
        context, 11, review_base=recorded)["ci_state"] == "green"


@pytest.mark.parametrize("run_base", ["before_fork", "beside_main", "unknown"])
def test_review_inspection_refuses_a_run_base_outside_fork_point_to_main(world, run_base):
    """The run's base must descend from the recorded base and be in main.

    `beside_main` descends from the recorded base and is present locally
    but main does not contain it; `unknown` is an object the checkout lacks,
    which fails closed."""
    head = world.member(11, "m11.txt", "member 11\n")
    beside = world.member(12, "m12.txt", "member 12\n")
    recorded = world.reviewed_base
    world.advance_main()
    context = world.gate.managed_checkout_context(ALIAS)
    world.gate.inspect_pr_by_index(context, 12)  # Fetches `beside` into the checkout.
    world.runs[11] = ({"before_fork": rev_parse(world.checkout, f"{recorded}~1"),
                       "beside_main": beside, "unknown": "e" * 40}[run_base], head)

    reviewed = world.gate.inspect_pr_by_index(context, 11, review_base=recorded)

    assert reviewed["ci_state"] == "pending"
    assert reviewed["blocking_reason"] == "CI_BASE_STALE"


def test_a_run_on_an_older_main_is_no_merge_ci_evidence(world):
    """Merge CI stays the exact (head, main's tip) pair, whatever review accepts."""
    head = world.member(11, "m11.txt", "member 11\n")
    at_push = world.advance_main("main1.txt", "main at the push\n")
    world.runs[11] = (at_push, head)
    tip = world.advance_main("main2.txt", "main after the push\n")
    # Isolate the CI half: an approval of the current (head, main) pair.
    claim_sha = world.gate._claim_identity(world.forge.pulls[11]["body"], head,
                                           native_claim=False)
    world.forge.reviews[11].append(
        {**_approval(head, tip, str(claim_sha), review_id=2), "stale": False})
    context = world.gate.managed_checkout_context(ALIAS)
    assert world.gate.inspect_pr_by_index(
        context, 11, review_base=world.reviewed_base)["ci_state"] == "green"
    on_main = world.gate.inspect_pr_by_index(context, 11)
    assert on_main["review_state"] == "approved"
    assert (on_main["ci_state"], on_main["blocking_reason"]) == ("pending", "CI_BASE_STALE")

    with pytest.raises(GateError) as refused:
        world.gate.execute(contracts.AUTOMERGE_PR, ALIAS, "11",
                           expected_head_sha=head, expected_base_sha=tip)

    assert refused.value.code == "PR_CI_NOT_GREEN"
    assert world.forge.merges == []


def test_an_approval_on_the_recorded_base_is_no_merge_authority(world):
    """The approval binds (head, recorded base); the merge reads main's tip."""
    head = world.member(11, "m11.txt", "member 11\n")
    world.advance_main()
    # Isolate the review half: CI is green on main's tip too.
    world.runs[11] = (world.forge.main(), head)
    context = world.gate.managed_checkout_context(ALIAS)
    inspected = world.gate.inspect_pr_by_index(context, 11)
    assert inspected["ci_state"] == "green"
    assert inspected["blocking_reason"] == "PR_BASE_STALE"
    assert inspected["recorded_base_sha"] != inspected["base_sha"]
    assert world.gate._train_member_refusal(context, 11, head) is None

    with pytest.raises(GateError) as refused:
        world.gate.execute(contracts.AUTOMERGE_PR, ALIAS, "11",
                           expected_head_sha=head, expected_base_sha=world.forge.main())
    # The unapproved current-base range cannot borrow approval on the fork
    # point. This review refusal keeps precedence over the stale-head reason.
    assert refused.value.code == "PR_REVIEW_NOT_APPROVED"
    assert world.forge.merges == []


def test_a_member_on_a_narrowed_run_the_review_accepts_rides_the_train(tmp_path, monkeypatch):
    """a C1-class head's narrowed `c2` run admits it; the prefix run merges it.

    Where CI narrows every non-candidate head, a C1-class head's recorded-base
    run is accepted for review even after main moves, while the
    current-main verdict stays stale. A train uses that projected answer too.
    The member's own run
    is no merge evidence: a member merges only on its prefix run, which is a
    `train/` head and so a full `checks/c1` run.
    """
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    members = _three_members(world)
    for pr, head in members:  # Each narrowed run judged the head's fork point.
        world.runs[pr] = (world.reviewed_base, head)
        world.narrowed.add(pr)
    context = world.gate.managed_checkout_context(ALIAS)
    inspected = world.gate.inspect_pr_by_index(context, 11)
    assert (inspected["ci_state"], inspected["blocking_reason"]) == (
        "pending", "CI_BASE_STALE")
    assert inspected["ci_review_accepted"] is True
    reviewed = world.gate.inspect_pr_by_index(context, 11, review_base=world.reviewed_base)
    assert reviewed["ci_state"] == "unavailable"
    assert reviewed["ci_review_accepted"] is True
    assert world.gate._train_member_refusal(context, 11, members[0][1]) is None

    train = world.build(members)

    assert [member["pr"] for member in train["members"]] == [11, 12, 13]
    assert train["dropped"] == []
    with pytest.raises(GateError) as early:  # No prefix run yet: nothing merges.
        world.merge(train, *members[0])
    assert early.value.code == "PR_CI_NOT_GREEN"
    assert world.forge.merges == []
    world.green(train)
    for pr, head in members:
        assert world.merge(train, pr, head)["code"] == "TRAIN_MEMBER_MERGED"
    assert world.forge.merges == members


def _record_run_base_predicate(world: TrainWorld, monkeypatch) -> list[bool]:
    """Record, per CI read, whether the caller passed an ``accept_run_base`` predicate.

    Observation only: the stub answers exactly as it would without this.
    """
    stub = world.gate._automerge_ci_status
    passed: list[bool] = []

    def status(context, pull, head_sha, *, accept_run_base=None, **options):
        passed.append(accept_run_base is not None)
        return stub(context, pull, head_sha, accept_run_base=accept_run_base, **options)

    monkeypatch.setattr(world.gate, "_automerge_ci_status", status)
    return passed


def _narrowed_on_a_later_base(world: TrainWorld, run_base: str | None = None):
    """One C1-class member whose narrowed run judged *run_base*, then main moved on.

    Default *run_base* is a main commit after the member's fork point and
    before main's tip, which the head does not contain.
    """
    head = world.member(11, "m11.txt", "member 11\n")
    later = world.advance_main("first.txt", "main moved once\n")
    world.advance_main("second.txt", "main moved twice\n")
    world.runs[11] = (run_base or later, head)
    world.narrowed.add(11)
    return head


def test_a_narrowed_run_on_a_proven_later_base_is_accepted_for_review(tmp_path, monkeypatch):
    """review ahead reviews a stale C1-class head on the run's own base.

    The run judged a main commit between the head's fork point and main's tip,
    so the ancestry predicate accepts it and `ci_review_accepted` is true. The
    current-main verdict stays stale: no stale-base run may merge.
    """
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    head = _narrowed_on_a_later_base(world)
    passed = _record_run_base_predicate(world, monkeypatch)
    context = world.gate.managed_checkout_context(ALIAS)

    inspected = world.gate.inspect_pr_by_index(context, 11)

    assert (inspected["ci_state"], inspected["blocking_reason"]) == ("pending", "CI_BASE_STALE")
    assert inspected["ci_review_accepted"] is True
    assert inspected["recorded_base_sha"] == world.reviewed_base
    assert passed == [False, True]
    assert world.gate._train_member_refusal(context, 11, head) is None


@pytest.mark.parametrize("run_base", [
    pytest.param("9" * 40, id="a_run_base_with_no_object"),
    pytest.param("reviewed_parent", id="a_run_base_before_the_fork_point"),
    pytest.param("side_branch", id="a_run_base_outside_main"),
])
def test_a_narrowed_run_on_an_unproven_base_is_not_accepted_for_review(
        tmp_path, monkeypatch, run_base):
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    if run_base == "reviewed_parent":
        run_base = git(["rev-parse", f"{world.reviewed_base}~1"], world.author).stdout.strip()
    elif run_base == "side_branch":
        git(["checkout", "-q", "-B", "side", world.reviewed_base], world.author)
        run_base = commit_file(world.author, "side.txt", "side\n", "side")
        git(["push", "-q", "origin", "side:refs/heads/side"], world.author)
        git(["checkout", "-q", "main"], world.author)
    _narrowed_on_a_later_base(world, run_base)
    context = world.gate.managed_checkout_context(ALIAS)

    inspected = world.gate.inspect_pr_by_index(context, 11)

    assert inspected["ci_review_accepted"] is False


def test_every_projection_proves_a_run_base(tmp_path, monkeypatch):
    """the server status path projects through the same method and proves too.

    A per-task status memo is a memo only; it must not switch the proof
    off, or the status read would refuse what the executor read accepts. The
    proof reads main's commits only, so a missing head object does not stop
    it.
    """
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    _narrowed_on_a_later_base(world)
    passed = _record_run_base_predicate(world, monkeypatch)
    context = world.gate.managed_checkout_context(ALIAS)
    world.gate.inspect_pr_by_index(context, 11)  # fetches main's tip and the head
    pull = world.gate.pull_by_index(context, 11)
    native = world.gate._read_pr_state(context, 11)
    head = str(native["head"]["sha"])
    tip = world.forge.main()

    def project(**flags):
        passed.clear()
        projected = world.gate._project_pr_evidence(
            context, pull, native, head, tip, **{"objects_available": True, **flags})
        return projected["ci_review_accepted"], passed[-1]

    assert project() == (True, True)
    assert project(task_pages={}) == (True, True)
    assert project(objects_available=False) == (True, True)


def test_the_executors_read_only_inspect_proves_a_run_base(tmp_path, monkeypatch):
    """the automerge executor reads `inspect_pr_readonly_by_index`.

    Its single-PR read proves a narrowed run's later base, so review ahead
    sees `ci_review_accepted`; the server status read gives the same
    answer. The read-only path does not fetch:
    main's commits come from the checkout.
    """
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    _narrowed_on_a_later_base(world)
    context = world.gate.managed_checkout_context(ALIAS)
    git(["fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"], world.checkout)

    single = world.gate.inspect_pr_readonly_by_index(context, 11)
    status = world.gate.inspect_pr_readonly_by_index(context, 11, task_pages={})

    assert (single["ci_state"], single["blocking_reason"]) == ("pending", "CI_BASE_STALE")
    assert single["ci_review_accepted"] is True
    assert status["ci_review_accepted"] is True
    assert _answer(single) == _answer(status) == ("accepted", "NARROWED_RUN", True)


def test_an_unfetched_read_only_checkout_reports_unknown_not_rejected(tmp_path, monkeypatch):
    """Without main's tip in the checkout the ancestry proof cannot run.

    Before, that read refused like a run base outside the range. Now it is
    unknown with its reason, never accepted, while a fetched read of the
    same PR accepts the run.
    """
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    _narrowed_on_a_later_base(world)
    context = world.gate.managed_checkout_context(ALIAS)
    operations = _operations(world)

    single = world.gate.inspect_pr_readonly_by_index(context, 11)
    status = operations.status(ALIAS, 11)["pull_requests"][0]

    assert single["ci_review_accepted"] is False
    expected = ("unknown", "MAIN_TIP_MISSING", False)
    assert _answer(single) == _answer(operations.inspect(ALIAS, 11)) == _answer(status) == expected
    # A fetched path decides: the train admission fetches main and the head.
    assert world.gate._train_member_refusal(context, 11, str(single["head_sha"]),
                                            mode=train_operations.TRAIN_V2_POLICY) is None
    # And the read-only reads then decide the same way.
    assert _answer(operations.inspect(ALIAS, 11)) == ("accepted", "NARROWED_RUN", True)


def test_a_narrowed_run_admits_no_member_where_ci_is_not_narrowed(world):
    """No `merge_candidate_policy`: a profile shortfall is no design, so refuse."""
    head = world.member(11, "m11.txt", "member 11\n")
    world.advance_main()
    world.runs[11] = (world.forge.main(), head)
    world.narrowed.add(11)
    context = world.gate.managed_checkout_context(ALIAS)

    inspected = world.gate.inspect_pr_by_index(context, 11)

    assert inspected["ci_review_accepted"] is False
    assert world.gate._train_member_refusal(context, 11, head) == "CI_PROFILE_INSUFFICIENT"


def test_a_failed_narrowed_run_still_refuses_a_member(tmp_path, monkeypatch):
    """The narrowed-run admission is for a successful run only.

    The gate judges the profile before the identity job's conclusion, so a
    failed `c2` run reads CI_PROFILE_INSUFFICIENT too; its conclusion is what
    keeps it out (`ci_evidence_answer`)."""
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=True)
    head = world.member(11, "m11.txt", "member 11\n")
    world.advance_main()
    world.runs[11] = (world.forge.main(), head)
    world.narrowed.add(11)
    world.failed_runs.add(11)
    context = world.gate.managed_checkout_context(ALIAS)

    inspected = world.gate.inspect_pr_by_index(context, 11)

    assert (inspected["ci_state"], inspected["blocking_reason"]) == (
        "unavailable", "CI_PROFILE_INSUFFICIENT")
    assert inspected["ci_run_conclusion"] == "failure"
    assert inspected["ci_review_accepted"] is False
    assert world.gate._train_member_refusal(context, 11, head) == "CI_PROFILE_INSUFFICIENT"


# --- one CI-evidence answer on every inspection path ----------------
#
# The same PR, head, recorded base, run base and main tip give the same
# (answer, reason) on the executor read, the status read, the review dispatch
# read over either base, the `train-v2` member refusal and the refresh read.
# Each case is arranged on PR 11 of a fresh world.


def _answer(row: dict[str, object]) -> tuple[object, object, object]:
    """The CI-evidence answer one read publishes, with the review's bool."""
    return row["ci_evidence_answer"], row["ci_evidence_reason"], row["ci_review_accepted"]


def _operations(world: TrainWorld) -> EstateGateOperations:
    """The executor's and the status's operations over this world's gate.

    Not the executor host: no queue state, receipts or delivery journal.
    """
    return EstateGateOperations(gate_factory=lambda **_kwargs: world.gate,
                                hostname=lambda: "test-host",
                                executor_authority=lambda: (False, "not the executor"))


def _fetch(world: TrainWorld, index: int = 11) -> None:
    """Bring main's tip and the PR's head into the checkout, as a fetched path has them."""
    head_ref = str(world.forge.pulls[index]["head"])
    git(["fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main",
         f"+refs/heads/{head_ref}:refs/remotes/origin/{head_ref}"], world.checkout)


def _behind_main(world: TrainWorld) -> str:
    """PR 11 forks at the reviewed base, its run judged that base, and main moves on."""
    head = world.member(11, "m11.txt", "member 11\n")
    world.advance_main()
    return head


def _on_tip(world: TrainWorld) -> str:
    """PR 11's run judged main's current tip, which the head does not contain."""
    head = _behind_main(world)
    world.runs[11] = (world.forge.main(), head)
    return head


def _marked(world: TrainWorld) -> None:
    world.forge.pulls[11]["labels"] = [{"name": contracts.CANDIDATE_LABEL}]


def _proven_later_base(world, _monkeypatch):
    _narrowed_on_a_later_base(world)


def _exact_tip_narrowed(world, _monkeypatch):
    _on_tip(world)
    world.narrowed.add(11)


def _recorded_base_narrowed(world, _monkeypatch):
    _behind_main(world)
    world.narrowed.add(11)


def _green_on_recorded(world, _monkeypatch):
    _behind_main(world)


def _marked_proven_later_base(world, _monkeypatch):
    _narrowed_on_a_later_base(world)
    world.narrowed.discard(11)
    world.c2_class.add(11)
    _marked(world)


def _marked_green_on_recorded(world, _monkeypatch):
    _behind_main(world)
    world.c2_class.add(11)
    _marked(world)


def _marked_exact_tip(world, _monkeypatch):
    _on_tip(world)
    world.c2_class.add(11)
    _marked(world)


def _outside_range(world, _monkeypatch):
    git(["checkout", "-q", "-B", "side", world.reviewed_base], world.author)
    side = commit_file(world.author, "side.txt", "side\n", "side")
    git(["push", "-q", "origin", "side:refs/heads/side"], world.author)
    git(["checkout", "-q", "main"], world.author)
    _narrowed_on_a_later_base(world, side)


def _objectless_run_base(world, _monkeypatch):
    _narrowed_on_a_later_base(world, "9" * 40)


def _failed_narrowed(world, _monkeypatch):
    _exact_tip_narrowed(world, _monkeypatch)
    world.failed_runs.add(11)


def _listing_unavailable(world, _monkeypatch):
    _behind_main(world)
    world.evaluated[11] = {"api_failure": True}


def _ambiguous_run_record(world, _monkeypatch):
    head = _behind_main(world)
    record = {"number": 11, "head": {"sha": head}, "base": {"sha": world.forge.main()}}
    # A trusted run that names two pull requests: it judged no single one.
    world.evaluated[11] = {"run": {
        "id": 61, "status": "completed", "conclusion": "success",
        "event": "pull_request_target", "path": ci.FORGEJO_LAUNCHER_PATH,
        "head_sha": head, "pull_requests": [record, {**record, "number": 12}]}}


def _narrowed_no_recorded_base(world, _monkeypatch):
    _exact_tip_narrowed(world, _monkeypatch)
    world.forge.pulls[11]["merge_base"] = None


def _range_unclassified(world, monkeypatch):
    _exact_tip_narrowed(world, monkeypatch)
    # Every changed-path read is over the bound: a range the gate cannot
    # classify, on a checkout that holds all of its objects.
    monkeypatch.setattr(forge, "LANE_CHANGED_PATHS_MAX", 0)


def _criss_cross_head(world: TrainWorld) -> str:
    """PR 11's head and main's tip each merge the same two commits.

    `left` and `right` fork at the reviewed base. The member starts from one
    merge of them and main's tip is another, so `merge-base --all tip head`
    names both and the (tip, head) range cannot be classified. The forge's
    recorded base is one of the two, with a single merge base with the head,
    so the head's own range can. The member changes a C1-class path
    (a catalog file), so every read of it asks for `checks/c1`.
    """
    author = world.author
    git(["checkout", "-q", "-B", "left", world.reviewed_base], author)
    left = commit_file(author, "left.txt", "left\n", "left")
    git(["checkout", "-q", "-B", "right", world.reviewed_base], author)
    right = commit_file(author, "right.txt", "right\n", "right")
    git(["merge", "-q", "--no-ff", "-m", "right joins left", left], author)
    start = rev_parse(author)
    git(["checkout", "-q", "left"], author)
    git(["merge", "-q", "--no-ff", "-m", "left joins right", right], author)
    tip = rev_parse(author)
    git(["push", "-q", "origin", f"{tip}:refs/heads/main"], author)
    git(["checkout", "-q", "main"], author)
    head = world.member(11, "repos.yaml", "member 11\n", from_ref=start)
    # The arrangement must hold, or the case proves nothing about either range.
    assert world.forge.main() == tip != start
    assert sorted(git(["merge-base", "--all", tip, head], author).stdout.split()) == sorted(
        [left, right])
    return head


def _criss_cross_range(world, _monkeypatch):
    """A narrowed run on main's exact tip, where only the recorded range classifies.

    The CI read is this world's stub, which ignores the required profile, so
    the case does not cover it. The evaluator classifies that profile from
    the path's own base too (`forge._range_required_profile`): a head whose
    recorded range classifies below C1 is asked `checks/c2` over the recorded
    base and `checks/c1` over main's tip, so the paths can differ for it. This
    head changes a C1-class path, so every path asks for `checks/c1`.
    """
    head = _criss_cross_head(world)
    world.runs[11] = (world.forge.main(), head)
    world.narrowed.add(11)


def _narrowed_policy_off_no_recorded_base(world, monkeypatch):
    """Without a fork point every path classifies the range from main's tip."""
    _exact_tip_narrowed(world, monkeypatch)
    world.forge.pulls[11]["merge_base"] = None


#: case -> (merge-candidate policy on, arrangement, the answer every path gives)
CI_ANSWER_CASES: dict[str, tuple[bool, Callable[..., None], tuple[str, str]]] = {
    "proven_later_base": (True, _proven_later_base, ("accepted", "NARROWED_RUN")),
    "exact_tip_narrowed": (True, _exact_tip_narrowed, ("accepted", "NARROWED_RUN")),
    "recorded_base_narrowed": (True, _recorded_base_narrowed, ("accepted", "NARROWED_RUN")),
    "green_on_recorded": (True, _green_on_recorded, ("accepted", "GREEN")),
    "green_on_recorded_no_policy": (False, _green_on_recorded, ("accepted", "GREEN")),
    "marked_proven_later_base": (True, _marked_proven_later_base, ("accepted", "NARROWED_RUN")),
    "marked_green_on_recorded": (True, _marked_green_on_recorded, ("accepted", "NARROWED_RUN")),
    "marked_exact_tip": (True, _marked_exact_tip, ("accepted", "NARROWED_RUN")),
    "outside_range": (True, _outside_range, ("rejected", "RUN_BASE_OUTSIDE_RANGE")),
    "objectless_run_base": (True, _objectless_run_base, ("rejected", "RUN_BASE_OUTSIDE_RANGE")),
    "failed_narrowed": (True, _failed_narrowed, ("rejected", "CI_PROFILE_INSUFFICIENT")),
    "narrowed_policy_off": (False, _exact_tip_narrowed, ("rejected", "NARROWED_POLICY_OFF")),
    "listing_unavailable": (True, _listing_unavailable, ("unknown", "CI_EVIDENCE_UNAVAILABLE")),
    "ambiguous_run_record": (True, _ambiguous_run_record, ("unknown", "CI_EVIDENCE_UNAVAILABLE")),
    "narrowed_no_recorded_base": (
        True, _narrowed_no_recorded_base, ("unknown", "RECORDED_BASE_MISSING")),
    "range_unclassified": (False, _range_unclassified, ("unknown", "RANGE_UNCLASSIFIED")),
    "criss_cross_range": (False, _criss_cross_range, ("rejected", "NARROWED_POLICY_OFF")),
    "narrowed_policy_off_no_recorded_base": (
        False, _narrowed_policy_off_no_recorded_base, ("rejected", "NARROWED_POLICY_OFF")),
}
#: `train-v1` keeps its looser rule: a stale base on a successful run rides,
#: whatever base the run judged. It is exempt from the one-answer rule.
V1_ADMITS_A_REJECTED_ANSWER = {"outside_range", "objectless_run_base"}


def _arrange(case: str, tmp_path: Path, monkeypatch) -> tuple[TrainWorld, tuple[str, str, bool]]:
    policy, arrange, (state, reason) = CI_ANSWER_CASES[case]
    world = TrainWorld(tmp_path, monkeypatch, merge_candidate=policy)
    arrange(world, monkeypatch)
    return world, (state, reason, state == "accepted")


@pytest.mark.parametrize("case", list(CI_ANSWER_CASES))
def test_the_executor_read_gives_the_shared_ci_answer(tmp_path, monkeypatch, case):
    """The automerge executor's single-PR read-only inspect."""
    world, expected = _arrange(case, tmp_path, monkeypatch)
    _fetch(world)

    row = _operations(world).inspect(ALIAS, 11)

    assert row["ok"] is True
    assert _answer(row) == expected
    assert merge_open_prs._narrowed_run_accepted(row) is (
        expected[0] == "accepted" and row["ci_state"] != "green")


@pytest.mark.parametrize("case", list(CI_ANSWER_CASES))
def test_the_status_read_gives_the_shared_ci_answer(tmp_path, monkeypatch, case):
    """The server status row: its per-task status memo is no second rule."""
    world, expected = _arrange(case, tmp_path, monkeypatch)
    _fetch(world)

    rows = _operations(world).status(ALIAS, 11)["pull_requests"]

    assert [row["pull_request"] for row in rows] == [11]
    assert _answer(rows[0]) == expected


@pytest.mark.parametrize("case", list(CI_ANSWER_CASES))
def test_the_review_dispatch_read_gives_the_shared_ci_answer(tmp_path, monkeypatch, case):
    """The snapshot review reads over the recorded base and over main's tip."""
    world, expected = _arrange(case, tmp_path, monkeypatch)
    context = world.gate.managed_checkout_context(ALIAS)
    recorded = landing._recorded_base(world.gate._read_pr_state(context, 11))

    for review_base in (recorded, world.forge.main()):
        record = world.gate.inspect_pr_by_index(context, 11, review_base=review_base)
        answer = landing.ci_evidence_answer(world.policy, record)
        assert tuple(answer) == expected[:2], review_base
        assert _answer(record) == expected, review_base


@pytest.mark.parametrize("case", list(CI_ANSWER_CASES))
def test_the_train_refusal_gives_the_shared_ci_answer(tmp_path, monkeypatch, case):
    """`train-v2` admits what the answer accepts and names an unknown one.

    `train-v1` is exempt from the rule: no repository selects it. Through
    `ci_review_accepted` it now also admits a narrowed run on main's exact
    tip (`exact_tip_narrowed`).
    """
    world, (state, reason, _accepted) = _arrange(case, tmp_path, monkeypatch)
    context = world.gate.managed_checkout_context(ALIAS)
    head = str(world.forge.pulls[11]["head_sha"])

    refusal = world.gate._train_member_refusal(
        context, 11, head, mode=train_operations.TRAIN_V2_POLICY)
    v1 = world.gate._train_member_refusal(context, 11, head, mode=train_operations.TRAIN_POLICY)

    if state == "accepted":
        assert refusal is None
    elif state == "unknown":
        assert refusal == reason
    else:
        assert isinstance(refusal, str) and refusal not in ci.CI_ANSWER_REASONS["unknown"]
    assert (v1 is None) is (state == "accepted" or case in V1_ADMITS_A_REJECTED_ANSWER)


@pytest.mark.parametrize("case", list(CI_ANSWER_CASES))
def test_the_refresh_read_gives_the_shared_ci_answer(tmp_path, monkeypatch, case):
    """The refresh read's eligibility profile is the class's own; its answer is everyone's."""
    world, expected = _arrange(case, tmp_path, monkeypatch)
    context = world.gate.managed_checkout_context(ALIAS)

    refresh = world.gate.inspect_pr_by_index(context, 11, for_refresh=True)
    executor = _operations(world).inspect(ALIAS, 11)

    assert _answer(refresh) == _answer(executor) == expected


@pytest.mark.parametrize("case", ["exact_tip_narrowed", "narrowed_no_recorded_base"])
def test_a_refresh_refusal_names_the_ci_answer(tmp_path, monkeypatch, case):
    """`PR_REFRESH_NOT_ELIGIBLE` carries the read's answer; eligibility is unchanged."""
    world, (state, reason, _accepted) = _arrange(case, tmp_path, monkeypatch)
    context = world.gate.managed_checkout_context(ALIAS)

    with pytest.raises(GateError) as refused:
        world.gate.refresh_pr_by_index(
            context, 11, str(world.forge.pulls[11]["head_sha"]), world.forge.main())

    assert refused.value.code == "PR_REFRESH_NOT_ELIGIBLE"
    assert refused.value.context["blocking_reason"] == "CI_PROFILE_INSUFFICIENT"
    assert (refused.value.context["ci_evidence_answer"],
            refused.value.context["ci_evidence_reason"]) == (state, reason)


def test_close_refuses_a_pull_request_that_is_not_a_train(world):
    world.member(11, "m11.txt", "member 11\n")

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=11, reason="CI_RUN_FAILED")

    assert refused.value.code == "TRAIN_INVALID"
    assert world.forge.pulls[11]["state"] == "open"


def test_every_train_route_refuses_without_the_repository_policy(tmp_path, monkeypatch):
    # A lock holder is reported by operation name only from this closed set.
    assert contracts.TRAIN_OPERATIONS <= contracts.LOCK_OWNER_OPERATIONS
    world = TrainWorld(tmp_path, monkeypatch, policy="")
    members = _three_members(world)

    for operation, request in (
        (contracts.TRAIN_BUILD, {"expected_base": world.forge.main(),
                                 "members": [{"pr": pr, "head": head} for pr, head in members]}),
        (contracts.TRAIN_MEMBER_MERGE, {"train_pr": 1, "member_pr": 11,
                                        "member_head": members[0][1]}),
        (contracts.TRAIN_CLOSE, {"train_pr": 1, "reason": "CI_RUN_FAILED"}),
    ):
        with pytest.raises(GateError) as refused:
            world.gate.execute(operation, ALIAS, train_request=request)
        assert refused.value.code == "TRAIN_POLICY_DISABLED"
    assert world.forge.merges == []
    assert not any(pull["title"].startswith("WIP: merge-train")
                   for pull in world.forge.pulls.values())


def test_every_member_merge_carries_its_own_tested_prefix_trailers(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)

    for member, prefix in zip(members, train["prefixes"]):
        result = world.merge(train, *member)
        expected = (f"Train-Candidate: {prefix['candidate']}\n"
                    f"Train-Base: {train['base_sha']}")
        assert world.forge.merge_messages[member[0]] == expected
        assert result["merge_trailer_landed"] is True
        body = git(["log", "-1", "--format=%B", "origin/main"], world.checkout).stdout
        assert body.strip().endswith(expected)
        assert world.tree("origin/main") == world.tree(str(prefix["candidate"]))


def test_final_candidate_green_never_authorizes_an_untested_first_prefix(world):
    members = _three_members(world)
    train = world.build(members)
    # Cn can repair a failure introduced by C1. A run of Cn says nothing
    # about the intermediate tree that the first native merge will land.
    world.runs[int(train["train_pr"])] = (str(train["base_sha"]), str(train["candidate_sha"]))

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code in {"PR_CI_NOT_GREEN", "TRAIN_INVALID"}
    assert world.forge.merges == []
    assert world.forge.main() == train["base_sha"]


def test_a_green_first_prefix_merges_before_the_final_candidate_has_any_run(world):
    members = _three_members(world)
    train = world.build(members)
    prefix = train["prefixes"][0]
    world.runs[int(prefix["pr"])] = (str(train["base_sha"]), str(prefix["candidate"]))

    merged = world.merge(train, *members[0])

    assert merged["prefix_pr"] == prefix["pr"]
    assert world.forge.merges == [members[0]]
    assert world.tree("origin/main") == world.tree(str(prefix["candidate"]))
    context = world.gate.managed_checkout_context(ALIAS)
    observed = world.gate.inspect_train_by_index(context, int(train["train_pr"]))
    assert [row["ci_state"] for row in observed["prefixes"]] == ["green", "pending", "pending"]
    assert observed["base_sha"] == train["base_sha"]


def test_a_red_first_prefix_blocks_a_green_later_candidate_on_real_git(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    world.failed_runs.add(int(train["prefixes"][0]["pr"]))

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code == "PR_CI_NOT_GREEN"
    assert refused.value.context["ci_verdict"] == "red"
    assert world.forge.merges == []
    assert world.forge.main() == train["base_sha"]


@pytest.mark.parametrize("mutation", ["head", "family", "closed"])
def test_a_tampered_prefix_never_borrows_the_final_candidate_proof(world, mutation):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    prefix = train["prefixes"][0]
    if mutation == "head":
        _force_push(world, str(prefix["branch"]), str(train["base_sha"]), "tamper.txt")
    elif mutation == "family":
        world.forge.pulls[int(prefix["pr"])]["head"] = f"train/{str(train['base_sha'])[:12]}-99-prefix-1"
    else:
        world.forge.pulls[int(prefix["pr"])]["state"] = "closed"

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])

    assert refused.value.code == "TRAIN_INVALID"
    assert world.forge.merges == []


def test_a_final_only_legacy_train_is_refused_and_can_be_cleaned(world):
    members = _three_members(world)
    train = world.build(members)
    for prefix in train["prefixes"][:-1]:
        world.forge.pulls.pop(int(prefix["pr"]))
        git(["push", "-q", "origin", f":refs/heads/{prefix['branch']}"], world.checkout)
    world.runs[int(train["train_pr"])] = (str(train["base_sha"]), str(train["candidate_sha"]))

    with pytest.raises(GateError) as refused:
        world.merge(train, *members[0])
    assert refused.value.code == "TRAIN_INVALID"
    assert world.forge.merges == []

    closed = world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="TRAIN_INVALID")
    assert closed["branch_deleted"] is True
    assert not world.origin_has(str(train["train_branch"]))


@pytest.mark.parametrize("failed_position", [2, 3], ids=["second-prefix", "owner"])
def test_orphan_cleanup_keeps_its_open_owner_until_all_refs_are_removed(world, monkeypatch, failed_position):
    train = world.build(_three_members(world))
    owner = int(train["train_pr"])
    branches = [str(prefix["branch"]) for prefix in train["prefixes"]]
    failed_branch = branches[failed_position - 1]
    runner = world.gate._runner
    attempted = []

    def refuse_delete(argv, cwd):
        if argv[1] == "push" and argv[-1].startswith(":refs/heads/"):
            branch = argv[-1].removeprefix(":refs/heads/")
            attempted.append(branch)
            if branch == failed_branch:
                return _done(code=1)
        return runner(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", refuse_delete)
    # Only native open PR discovery survives a build-before-queue-save crash.
    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="TRAIN_ORPHANED")

    assert refused.value.code == "TRAIN_CLOSE_FAILED"
    assert refused.value.context["failed_branches"] == [failed_branch]
    assert world.forge.pulls[owner]["state"] == "open"
    assert world.origin_has(str(train["train_branch"]))
    assert attempted == branches[:failed_position]
    assert world.forge.comments == []
    for prefix in train["prefixes"][:failed_position - 1]:
        assert not world.origin_has(str(prefix["branch"]))
        assert world.forge.pulls[int(prefix["pr"])]["state"] == "closed"
    assert owner in {pull.index for pull in world.gate.list_open_pulls(world.policy, world.checkout)}
    assert all(world.forge.pulls[pr]["state"] == "open" for pr in (11, 12, 13))

    monkeypatch.setattr(world.gate, "_runner", runner)
    cleaned = world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="TRAIN_ORPHANED")

    assert cleaned["branch_deleted"] is True
    assert all(not world.origin_has(branch) for branch in branches)
    assert all(world.forge.pulls[int(prefix["pr"])]["state"] == "closed" for prefix in train["prefixes"])
    assert not any(pull.head in branches for pull in world.gate.list_open_pulls(world.policy, world.checkout))


def test_cleanup_retries_a_closed_owner_when_a_prefix_branch_delete_failed(world, monkeypatch):
    train = world.build(_three_members(world))
    # A known historical closed owner still anchors cleanup of leftover refs.
    for prefix in train["prefixes"]:
        world.forge.pulls[int(prefix["pr"])]["state"] = "closed"
    git(["push", "-q", "origin", f":refs/heads/{train['train_branch']}"], world.checkout)
    world.forge.pulls[int(train["train_pr"])]["native_head_ref"] = f"refs/pull/{train['train_pr']}/head"
    retained = str(train["prefixes"][0]["branch"])
    runner = world.gate._runner

    def refuse_delete(argv, cwd):
        if argv[1] == "push" and argv[-1] == f":refs/heads/{retained}":
            return _done(code=1)
        return runner(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", refuse_delete)
    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="CI_RUN_FAILED")
    assert refused.value.code == "TRAIN_CLOSE_FAILED"
    assert refused.value.context["failed_branches"] == [retained]
    assert all(world.forge.pulls[int(prefix["pr"])]["state"] == "closed" for prefix in train["prefixes"])
    assert world.origin_has(retained)
    assert not world.origin_has(str(train["train_branch"]))
    monkeypatch.setattr(world.gate, "_runner", runner)

    retried = world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="CI_RUN_FAILED")

    assert retried["branch_deleted"] is True
    assert not world.origin_has(retained)
    repeated = world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="CI_RUN_FAILED")
    assert repeated["branch_deleted"] is True


def test_closed_native_deleted_head_cleanup_and_read_are_idempotent(world: TrainWorld) -> None:
    train = world.build(_three_members(world))
    owner = int(train["train_pr"])
    world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="CI_RUN_FAILED")
    native = json.loads(world.forge.read_pr_state(None, world.checkout, REPOSITORY, owner).stdout)
    assert native["head"]["ref"] == f"refs/pull/{owner}/head"
    assert native["head"]["label"] == train["train_branch"]
    assert native["head"]["sha"] == train["candidate_sha"]
    assert native["state"] == "closed" and native["merged"] is False
    comments = list(world.forge.comments)

    repeated = world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="CI_RUN_FAILED")
    observed = world.gate.inspect_train_by_index(world.gate.managed_checkout_context(ALIAS), owner)

    assert repeated["code"] == "TRAIN_CLOSED"
    assert repeated["train_branch"] == train["train_branch"]
    assert repeated["branch_deleted"] is True
    assert repeated["deleted_branches"] == [] and repeated["closed_prs"] == []
    assert observed["pr_state"] == "closed" and observed["train_branch"] == train["train_branch"]
    assert world.forge.comments == comments and world.forge.merges == []
    assert all(not world.origin_has(str(prefix["branch"])) for prefix in train["prefixes"])


@pytest.mark.parametrize("mutation", [
    "wrong_ref_number", "wrong_label_family", "label_is_not_train", "label_is_prefix",
    "open", "merged", "foreign_head_repository", "foreign_base_repository", "head_sha", "title",
])
def test_closed_hidden_head_normalization_refuses_mismatched_native_identity(
    world: TrainWorld, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    train = world.build(_three_members(world))
    owner = int(train["train_pr"])
    world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="CI_RUN_FAILED")
    read = world.forge.read_pr_state

    def changed_native(runner: object, cwd: Path, forge_repo: str, index: int) -> subprocess.CompletedProcess[str]:
        result = read(runner, cwd, forge_repo, index)
        if index != owner:
            return result
        native = json.loads(result.stdout)
        if mutation == "wrong_ref_number":
            native["head"]["ref"] = f"refs/pull/{owner + 1}/head"
        elif mutation == "wrong_label_family":
            native["head"]["label"] = f"train/{str(train['base_sha'])[:12]}-99"
        elif mutation == "label_is_not_train":
            native["head"]["label"] = "task/foreign"
        elif mutation == "label_is_prefix":
            native["head"]["label"] = f"{train['train_branch']}-prefix-1"
        elif mutation == "open":
            native["state"] = "open"
        elif mutation == "merged":
            native["merged"] = True
        elif mutation == "foreign_head_repository":
            native["head"]["repo"]["full_name"] = "foreign/train"
        elif mutation == "foreign_base_repository":
            native["base"]["repo"]["full_name"] = "foreign/train"
        elif mutation == "head_sha":
            native["head"]["sha"] = train["base_sha"]
        elif mutation == "title":
            native["title"] = "WIP: ordinary work"
        return _done(json.dumps(native))

    monkeypatch.setattr(world.forge, "read_pr_state", changed_native)
    writes = []
    runner = world.gate._runner

    def track_writes(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv[1] == "push":
            writes.append(argv)
        return runner(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", track_writes)
    comments = list(world.forge.comments)

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="CI_RUN_FAILED")

    assert refused.value.code in {"TRAIN_INVALID", "REMOTE_HEAD_MISMATCH"}
    assert writes == [] and world.forge.comments == comments and world.forge.merges == []


def test_an_open_train_never_selects_cleanup_from_a_different_native_label(
    world: TrainWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    train = world.build(_three_members(world))
    read = world.forge.read_pr_state

    def changed_label(runner: object, cwd: Path, forge_repo: str, index: int) -> subprocess.CompletedProcess[str]:
        native = json.loads(read(runner, cwd, forge_repo, index).stdout)
        native["head"]["label"] = "train/aaaaaaaaaaaa-99"
        return _done(json.dumps(native))

    monkeypatch.setattr(world.forge, "read_pr_state", changed_label)
    closed = world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="CI_RUN_FAILED")

    assert closed["train_branch"] == train["train_branch"]
    assert set(closed["deleted_branches"]) == {prefix["branch"] for prefix in train["prefixes"]}


def test_closed_hidden_owner_cannot_clean_a_foreign_remaining_prefix(world: TrainWorld) -> None:
    train = world.build(_three_members(world))
    owner = int(train["train_pr"])
    branch = str(train["prefixes"][0]["branch"])
    world.gate._runner([contracts.GIT_PATH, "push", "origin", f":refs/heads/{train['train_branch']}"],
                       world.checkout)
    _force_push(world, branch, str(train["base_sha"]), "foreign-prefix.txt")

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="CI_RUN_FAILED")

    assert refused.value.code == "TRAIN_INVALID"
    assert world.origin_has(branch)
    assert world.forge.comments == [] and world.forge.merges == []


def test_scheduler_retries_failed_cleanup_with_native_closed_proof_then_advances_serially(
    world: TrainWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    world.failed_runs.add(int(train["prefixes"][0]["pr"]))
    directory = tmp_path / "queue"
    directory.mkdir(mode=0o700)
    store = QUEUE_STORE_TYPE(directory / "automerge.json", repositories=(REPOSITORY,))
    with store.locked() as state:
        state["repositories"][REPOSITORY] = {"active": None, "parked": {}}
        state["repositories"][REPOSITORY]["active"] = {
            "pr": members[0][0], "head": members[0][1], "base": train["base_sha"],
            "run": [None, None], "wait_since": 1000.0, "started": False,
            "train": {"pr": train["train_pr"], "branch": train["train_branch"],
                      "base": train["base_sha"], "started": 1000.0,
                      "members": train["members"], "prefixes": train["prefixes"]},
        }
        store.save(state)
    application = FakeGateApplication({REPOSITORY: [pr for pr, _head in members]}, responses={
        ("inspect", REPOSITORY, pr): {
            "ok": True, "pr_state": "open", "head_sha": head, "base_sha": train["base_sha"],
            "ci_state": "pending", "review_state": "not_approved",
            "blocking_reason": "PR_BASE_STALE", "latest_delegated_review_state": "APPROVED",
            "ci_run_conclusion": "success",
        } for pr, head in members
    })

    def inspect(_repository: str, index: int) -> dict[str, object]:
        return {"ok": True, **world.gate.inspect_train_by_index(world.gate.managed_checkout_context(ALIAS), index)}

    def close(repository: str, index: int, reason: str) -> dict[str, object]:
        application.calls.append(("train_close", repository, index, reason))
        try:
            return world.execute(contracts.TRAIN_CLOSE, train_pr=index, reason=reason)
        except GateError as exc:
            return {"ok": False, "code": exc.code}

    monkeypatch.setattr(application, "train_inspect", inspect, raising=False)
    monkeypatch.setattr(application, "train_close", close, raising=False)
    repository = merge_open_prs.Repository(REPOSITORY, world.checkout, "forgejo", REPOSITORY,
                                           merge_batch_policy="train-v1")
    runner = world.gate._runner
    inventories = []

    def fail_after_deleting_refs(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv[1] == "ls-remote":
            inventories.append(argv)
            if len(inventories) == 2:
                return _done(code=1)
        return runner(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", fail_after_deleting_refs)
    first = merge_open_prs.run_automerge_pass([repository], application, queue_store=store,
                                            now=lambda: 1000.0)[0]
    active = store.read()["repositories"][REPOSITORY]["active"]
    assert first["results"] == [] and first["train"]["close_failed"] == "TRAIN_CLOSE_FAILED"
    assert active["train"]["cleanup"] == {"reason": "CI_RUN_FAILED", "outcome": "closed"}
    assert world.forge.pulls[int(train["train_pr"])]["state"] == "closed"
    assert world.forge.pulls[int(train["train_pr"])]["native_head_ref"] == f"refs/pull/{train['train_pr']}/head"
    assert not any(call[0] == "refresh" for call in application.calls)
    monkeypatch.setattr(world.gate, "_runner", runner)

    second = merge_open_prs.run_automerge_pass([repository], application, queue_store=store,
                                             now=lambda: 1000.0)[0]

    assert second["train"]["outcome"] == "closed" and "close_failed" not in second["train"]
    assert second["results"] == [{"pr": members[0][0], "outcome": "refreshed", "head_sha": "c" * 40}]
    active = store.read()["repositories"][REPOSITORY]["active"]
    assert active["pr"] == members[0][0] and "train" not in active
    assert all(not world.origin_has(str(prefix["branch"])) for prefix in train["prefixes"])
    assert world.forge.merges == []


def test_cleanup_waits_for_a_verified_empty_ref_inventory_before_closing_prs(world, monkeypatch):
    train = world.build(_three_members(world))
    branches = [str(prefix["branch"]) for prefix in train["prefixes"]]
    listings = []

    def refuse_final_inventory(argv, cwd):
        if argv[1] == "ls-remote":
            listings.append(argv)
            if len(listings) == 2:
                return _done(code=1)
        # Keep native automatic closure queued to expose explicit-close order.
        return run_command(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", refuse_final_inventory)
    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="TRAIN_ORPHANED")

    assert refused.value.code == "TRAIN_CLOSE_FAILED"
    assert refused.value.context["step"] == "list_refs"
    assert len(listings) == 2
    assert all(not world.origin_has(branch) for branch in branches)
    assert all(world.forge.pulls[int(prefix["pr"])]["state"] == "open" for prefix in train["prefixes"])
    assert world.forge.comments == []
    monkeypatch.setattr(world.gate, "_runner", run_command)

    cleaned = world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="TRAIN_ORPHANED")

    assert cleaned["branch_deleted"] is True
    assert cleaned["deleted_branches"] == []
    assert all(world.forge.pulls[int(prefix["pr"])]["state"] == "closed" for prefix in train["prefixes"])


def test_cleanup_retries_an_open_pr_after_ref_deletion_and_explicit_close_failure(world, monkeypatch):
    train = world.build(_three_members(world))
    owner = int(train["train_pr"])
    branches = [str(prefix["branch"]) for prefix in train["prefixes"]]
    close = world.forge.close_pr

    def refuse_owner_close(runner, cwd, forge_repo, index):
        return _done(code=1) if index == owner else close(runner, cwd, forge_repo, index)

    # Native push events are queued; model their automatic closure as delayed.
    monkeypatch.setattr(world.gate, "_runner", run_command)
    monkeypatch.setattr(world.forge, "close_pr", refuse_owner_close)
    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="TRAIN_ORPHANED")

    assert refused.value.code == "TRAIN_CLOSE_FAILED"
    assert refused.value.context["failed_prs"] == [owner]
    assert refused.value.context["failed_branches"] == []
    assert refused.value.context["deleted_branches"] == branches
    assert all(not world.origin_has(branch) for branch in branches)
    assert world.forge.pulls[owner]["state"] == "open"
    assert owner in {pull.index for pull in world.gate.list_open_pulls(world.policy, world.checkout)}
    monkeypatch.setattr(world.forge, "close_pr", close)

    cleaned = world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason="TRAIN_ORPHANED")

    assert cleaned["branch_deleted"] is True
    assert cleaned["deleted_branches"] == []
    assert world.forge.pulls[owner]["state"] == "closed"


def test_a_partial_build_with_a_branch_but_no_pr_is_reconciled_before_rebuild(world, monkeypatch):
    members = _three_members(world)
    create = world.forge.create_pr
    calls = []

    def refuse_second_pr(*args, **kwargs):
        calls.append(args[4])
        return _done(code=1) if len(calls) == 2 else create(*args, **kwargs)

    monkeypatch.setattr(world.forge, "create_pr", refuse_second_pr)
    # Simulate losing cleanup as well: the new first-prefix ref has no PR.
    runner = world.gate._runner

    def refuse_delete(argv, cwd):
        if argv[1] == "push" and argv[-1].startswith(":refs/heads/train/"):
            return _done(code=1)
        return runner(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", refuse_delete)
    with pytest.raises(GateError) as refused:
        world.build(members)
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["cleanup_failed"] == "TRAIN_CLOSE_FAILED"
    assert isinstance(refused.value.context["train_pr"], int)
    assert refused.value.context["members"][0]["pr"] == 11
    family = f"train/{world.forge.main()[:12]}-11"
    assert world.origin_has(family + "-prefix-1")
    assert not any(pull["head"] == family + "-prefix-1" for pull in world.forge.pulls.values())
    monkeypatch.setattr(world.forge, "create_pr", create)
    monkeypatch.setattr(world.gate, "_runner", runner)

    rebuilt = world.build(members)

    assert len(rebuilt["prefixes"]) == 3
    assert all(world.forge.pulls[int(prefix["pr"])]["state"] == "open" for prefix in rebuilt["prefixes"])
    assert all(world.origin_has(str(prefix["branch"])) for prefix in rebuilt["prefixes"])


def test_cleanup_never_deletes_an_unproven_foreign_family_ref(world):
    train = world.build(_three_members(world))
    branch = str(train["prefixes"][0]["branch"])
    foreign = _force_push(world, branch, str(train["base_sha"]), "foreign.txt")

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=train["train_pr"], reason="TRAIN_INVALID")

    assert refused.value.code == "TRAIN_INVALID"
    assert rev_parse(world.origin, f"refs/heads/{branch}") == foreign
    assert world.forge.pulls[int(train["train_pr"])]["state"] == "open"


@pytest.mark.parametrize("profile", ["c1", "c2"])
def test_prefix_ci_keeps_the_canonical_profile_requirement(world, monkeypatch, profile):
    members = _three_members(world)
    train = world.build(members)
    prefix = train["prefixes"][0]
    seen = []

    def evidence_run(context, *, pull_override, head_override, required_profile_override):
        seen.append((pull_override.head, pull_override.base_commit, head_override, required_profile_override))
        run = {"id": 7001, "run_attempt": 1, "name": "checks", "workflow_id": 3001,
               "head_sha": head_override, "head_branch": pull_override.head,
               "event": "pull_request_target", "path": ci.FORGEJO_LAUNCHER_PATH,
               "status": "completed", "conclusion": "success",
               "updated_at": "2020-01-01T10:00:00Z",
               "pull_requests": [{"number": pull_override.index,
                                  "head": {"sha": head_override},
                                  "base": {"sha": pull_override.base_commit}}]}
        jobs = {"total_count": 1, "jobs": [{"id": 8001, "run_id": 7001, "name": profile,
                                           "status": "completed", "conclusion": "success"}]}
        evidence = ci.evaluate_evidence(
            repository=REPOSITORY, pull_request=pull_override.index,
            base_sha=pull_override.base_commit, head_sha=head_override,
            required_profile=required_profile_override, run=run, jobs=jobs,
            launcher_file_exists=True, api_failure=False, malformed_response=False,
            observed_at="2020-01-01T10:00:00Z")
        return context.policy, evidence, run, jobs

    monkeypatch.setattr(world.gate, "_automerge_ci_status",
                        operations.GitMaintainerGate._automerge_ci_status.__get__(world.gate))
    monkeypatch.setattr(world.gate, "_range_required_profile", lambda *args: "checks/c1")
    monkeypatch.setattr(world.gate, "_policy_evidence_run", evidence_run)
    if profile == "c1":
        assert world.merge(train, *members[0])["code"] == "TRAIN_MEMBER_MERGED"
    else:
        with pytest.raises(GateError) as refused:
            world.merge(train, *members[0])
        assert refused.value.code == "PR_CI_NOT_GREEN"
        assert world.forge.merges == []
    assert seen == [(prefix["branch"], train["base_sha"], prefix["candidate"], "checks/c1")]


def test_a_one_member_prefix_never_becomes_a_canonical_train_owner(world):
    members = _three_members(world)
    train = world.build(members)
    world.green(train)
    prefix = train["prefixes"][0]
    context = world.gate.managed_checkout_context(ALIAS)

    with pytest.raises(GateError) as inspected:
        world.gate.inspect_train_by_index(context, int(prefix["pr"]))
    assert inspected.value.code == "TRAIN_INVALID"
    with pytest.raises(GateError) as merged:
        world.execute(contracts.TRAIN_MEMBER_MERGE, train_pr=prefix["pr"],
                      member_pr=members[0][0], member_head=members[0][1])
    assert merged.value.code == "TRAIN_INVALID"
    assert world.forge.merges == []


# ------------------------------------------------------------------ train-v2

@pytest.mark.parametrize("change,expected", [
    ({}, True), ({"workflow_run_attempt": 1}, True),
    ({"result": "UNAVAILABLE"}, False), ({"reason_code": "CI_PROFILE_INSUFFICIENT"}, False),
    ({"required_profile": "checks/c2"}, False), ({"observed_profiles": ["checks/c2"]}, False),
    ({"observed_profiles": ["checks/c1", "checks/c2"]}, False),
    ({"workflow_run_conclusion": "cancelled"}, False),
    ({"workflow_run_conclusion": "skipped"}, False),
    ({"workflow_run_id": True}, False), ({"workflow_run_id": None}, False),
    ({"workflow_run_attempt": 0}, False), ({"workflow_run_attempt": []}, False),
    ({"retry_after_seconds": 30}, False),
])
def test_terminal_full_failure_requires_the_ci_owners_strict_c1_record(change, expected):
    evidence = {
        "result": "FAIL", "reason_code": "CI_RUN_FAILED", "required_profile": "checks/c1",
        "observed_profiles": ["checks/c1"], "workflow_run_conclusion": "failure",
        "workflow_run_id": 950, "workflow_run_attempt": None, "retry_after_seconds": None,
    }

    assert ci.terminal_full_failure(evidence | change) is expected


@pytest.mark.parametrize("narrowed", [False, True])
def test_v2_inspect_exposes_only_strict_full_terminal_red(world_v2, narrowed):
    world = world_v2
    built = world.build(_v2_members(world))
    world.green(built)
    owner = int(built["train_pr"])
    world.failed_runs.add(owner)
    if narrowed:
        world.narrowed.add(owner)

    inspected = world.gate.inspect_train_by_index(world.gate.managed_checkout_context(ALIAS), owner)

    assert inspected["mode"] == "full"
    assert inspected["generation"] == built["generation"]
    assert inspected["ci_terminal_red"] is (not narrowed)


@pytest.mark.parametrize("damage", [
    None, "green", "event", "task-head", "run-head", "missing-binding", "workflow", "file",
    "pending", "cancelled", "profile", "two-profiles", "foreign-leaf", "partial-list",
    "missing-graph", "duplicate-id", "malformed-name",
])
def test_base_push_failed_jobs_requires_a_bound_complete_full_c1_run(damage):
    base = "b" * 40
    run = {"id": 900, "event": "push", "head_sha": base, "push_head_sha": base,
           "path": ci.FORGEJO_LAUNCHER_PATH, "status": "completed", "conclusion": "failure"}
    rows = [
        {"id": 701, "run_id": 900, "name": "lint", "status": "completed", "conclusion": "failure"},
        {"id": 702, "run_id": 900, "name": "c1", "status": "completed", "conclusion": "failure"},
    ]
    jobs = {"jobs": rows, "total_count": 2, "native_graph_complete": True}
    launcher = True
    if damage == "green":
        run["conclusion"] = "success"
        for row in rows:
            row["conclusion"] = "success"
    elif damage == "event":
        run["event"] = "pull_request_target"
    elif damage == "task-head":
        run["head_sha"] = "c" * 40
    elif damage == "run-head":
        run["push_head_sha"] = "c" * 40
    elif damage == "missing-binding":
        run.pop("push_head_sha")
    elif damage == "workflow":
        run["path"] = ".forgejo/workflows/review.yml"
    elif damage == "file":
        launcher = False
    elif damage == "pending":
        run.update(status="in_progress", conclusion=None)
    elif damage == "cancelled":
        run["conclusion"] = "cancelled"
    elif damage == "profile":
        rows[-1]["name"] = "c2"
    elif damage == "two-profiles":
        rows.append({"id": 703, "run_id": 900, "name": "c2", "status": "completed", "conclusion": "failure"})
        jobs["total_count"] = 3
    elif damage == "foreign-leaf":
        rows[0]["run_id"] = 901
    elif damage == "partial-list":
        jobs["total_count"] = 3
    elif damage == "missing-graph":
        jobs["native_graph_complete"] = False
    elif damage == "duplicate-id":
        rows[-1]["id"] = 701
    elif damage == "malformed-name":
        rows[0]["name"] = {}

    observed = ci.base_push_failed_jobs(
        base_sha=base, run=run, jobs=jobs, launcher_file_exists=launcher)

    expected = frozenset({"lint", "c1"}) if damage is None else frozenset() if damage == "green" else None
    assert observed == expected


def test_v2_owner_publication_listing_failure_is_uncertain(world_v2, monkeypatch):
    """A create that returned but cannot be listed is an uncertain publication."""
    world = world_v2
    original = world.gate.list_open_pulls
    created = {"done": False}
    real_create = world.forge.create_pr

    def create(*args, **kwargs):
        result = real_create(*args, **kwargs)
        created["done"] = True
        return result

    def flaky(policy, cwd):
        if created["done"]:
            raise OSError("listing failed after create")
        return original(policy, cwd)

    monkeypatch.setattr(world.forge, "create_pr", create)
    monkeypatch.setattr(world.gate, "list_open_pulls", flaky)

    with pytest.raises(GateError) as refused:
        world.build(_v2_members(world))
    assert refused.value.context.get("step") == "open_pr"


@pytest.mark.parametrize("base_state,expected", [
    ("matching", True), ("subset", True), ("different", False), ("green", False),
    ("aggregate-only", None), ("pending", None), ("unreadable", None),
    ("newer-pending", None),
])
def test_v2_inspect_compares_the_owner_with_the_trusted_base_push(
        world_v2, monkeypatch, base_state, expected):
    world = world_v2
    world.reviewed_base = world.advance_main(ci.FORGEJO_LAUNCHER_PATH, "name: checks\n")
    built = world.build(_v2_members(world))
    world.green(built)
    owner = int(built["train_pr"])
    owner_run = 50 + owner
    world.failed_runs.add(owner)
    base = str(built["base_sha"])
    base_run = {"id": 1000, "event": "push", "path": ci.FORGEJO_LAUNCHER_PATH,
                "head_sha": base, "push_head_sha": base,
                "status": "completed", "conclusion": "failure"}
    if base_state == "green":
        base_run["conclusion"] = "success"
    elif base_state == "pending":
        base_run.update(status="in_progress", conclusion=None)

    def jobs(run_id, leaf, conclusion):
        rows = [
            {"id": run_id * 10, "run_id": run_id, "name": leaf,
             "status": "completed", "conclusion": conclusion},
            {"id": run_id * 10 + 1, "run_id": run_id, "name": "c1",
             "status": "completed", "conclusion": conclusion},
        ]
        if base_state == "subset" and run_id == owner_run:
            # The batch reproduces the base's lint failure and adds its own.
            rows.append({"id": run_id * 10 + 2, "run_id": run_id, "name": "leaf-02",
                         "status": "completed", "conclusion": "failure"})
        if base_state == "aggregate-only":
            # Both inventories failed only the aggregate identity job.
            return {"native_graph_complete": True, "total_count": 1, "jobs": rows[1:]}
        return {"native_graph_complete": True, "total_count": len(rows), "jobs": rows}

    def read_runs(runner, cwd, repository, workflow, head, page_size):
        assert repository == REPOSITORY and workflow == ci.FORGEJO_LAUNCHER_PATH
        assert head == base
        if base_state == "unreadable":
            return _done("not JSON")
        runs = [base_run]
        if base_state == "newer-pending":
            runs.append({**base_run, "id": 1001, "status": "in_progress", "conclusion": None})
        return _done(json.dumps({"workflow_runs": runs, "total_count": len(runs)}))

    def read_jobs(runner, cwd, repository, run_id, page_size):
        assert repository == REPOSITORY and run_id in {1000, owner_run}
        leaf = "other" if run_id == 1000 and base_state == "different" else "lint"
        conclusion = "success" if run_id == 1000 and base_state == "green" else "failure"
        return _done(json.dumps(jobs(run_id, leaf, conclusion)))

    monkeypatch.setattr(world.forge, "actions_runs_for_workflow", read_runs, raising=False)
    monkeypatch.setattr(world.forge, "actions_run_jobs", read_jobs, raising=False)

    inspected = world.gate.inspect_train_by_index(world.gate.managed_checkout_context(ALIAS), owner)

    assert inspected["ci_terminal_red"] is True
    assert inspected["base_failure_match"] is expected
    assert world.forge.merges == [] and world.origin_has(str(built["train_branch"]))


@pytest.mark.parametrize("owner_jobs,expected", [
    ("leaves", ["leaf-02", "leaf-24", "leaf-test", "lint"]),
    ("identity-only", ["c1"]), ("all-success", None), ("unreadable", None),
    ("no-graph", None), ("partial-list", None), ("raises", None),
])
def test_v2_inspect_names_the_failed_jobs_of_a_red_owner_for_attribution(
        world_v2, monkeypatch, owner_jobs, expected):
    """attribution only, and the sole owner-jobs read on a green base.

    A green base push returns before the base comparison reads the owner's
    jobs, so the attribution read is the only one. Whatever it finds, the
    strict-red verdict and the base comparison are what a green base gives.
    """
    world = world_v2
    world.reviewed_base = world.advance_main(ci.FORGEJO_LAUNCHER_PATH, "name: checks\n")
    built = world.build(_v2_members(world))
    world.green(built)
    owner = int(built["train_pr"])
    owner_run = 50 + owner
    world.failed_runs.add(owner)
    base = str(built["base_sha"])
    base_run = {"id": 1000, "event": "push", "path": ci.FORGEJO_LAUNCHER_PATH,
                "head_sha": base, "push_head_sha": base,
                "status": "completed", "conclusion": "success"}
    reads: list[int] = []

    def inventory(run_id, outcomes):
        rows = [{"id": run_id * 10 + position, "run_id": run_id, "name": name,
                 "status": "completed", "conclusion": conclusion}
                for position, (name, conclusion) in enumerate(outcomes)]
        return {"native_graph_complete": True, "total_count": len(rows), "jobs": rows}

    def read_runs(runner, cwd, repository, workflow, head, page_size):
        assert repository == REPOSITORY and workflow == ci.FORGEJO_LAUNCHER_PATH
        assert head == base
        return _done(json.dumps({"workflow_runs": [base_run], "total_count": 1}))

    def read_jobs(runner, cwd, repository, run_id, page_size):
        assert repository == REPOSITORY and run_id in {1000, owner_run}
        reads.append(run_id)
        if run_id == 1000:
            return _done(json.dumps(inventory(1000, [("lint", "success"), ("c1", "success")])))
        if owner_jobs == "raises":
            raise GateError("COMMAND_TIMEOUT")
        if owner_jobs == "unreadable":
            return _done("not JSON")
        # Only the aggregate identity job failed; an inconsistent inventory
        # of a red run lists no failed job; or four unsorted leaves failed
        # beside the aggregate (a passing leaf is never named).
        rows = inventory(owner_run, [("lint", "success"), ("c1", "failure")]
                         if owner_jobs == "identity-only" else
                         [("lint", "success"), ("c1", "success")]
                         if owner_jobs == "all-success" else
                         [("leaf-02", "failure"), ("c1", "failure"), ("lint", "failure"),
                          ("leaf-24", "failure"), ("leaf-test", "failure"),
                          ("docs", "success")])
        if owner_jobs == "no-graph":
            rows["native_graph_complete"] = False
        elif owner_jobs == "partial-list":
            rows["total_count"] += 1
        return _done(json.dumps(rows))

    monkeypatch.setattr(world.forge, "actions_runs_for_workflow", read_runs, raising=False)
    monkeypatch.setattr(world.forge, "actions_run_jobs", read_jobs, raising=False)

    inspected = world.gate.inspect_train_by_index(world.gate.managed_checkout_context(ALIAS), owner)

    assert inspected["owner_failed_jobs"] == expected
    assert inspected["ci_terminal_red"] is True
    assert inspected["base_failure_match"] is False
    assert reads.count(owner_run) == 1
    assert world.forge.merges == [] and world.origin_has(str(built["train_branch"]))


@pytest.mark.parametrize("owner_state", ["green", "narrowed-red"])
def test_v2_inspect_reads_no_jobs_unless_the_owner_is_strict_red(world_v2, monkeypatch, owner_state):
    """the attribution read costs nothing off the strict-red path."""
    world = world_v2
    built = world.build(_v2_members(world))
    world.green(built)
    owner = int(built["train_pr"])
    if owner_state == "narrowed-red":
        world.failed_runs.add(owner)
        world.narrowed.add(owner)
    reads: list[int] = []

    def read_jobs(runner, cwd, repository, run_id, page_size):
        reads.append(run_id)
        return _done("not reached")

    monkeypatch.setattr(world.forge, "actions_run_jobs", read_jobs, raising=False)

    inspected = world.gate.inspect_train_by_index(world.gate.managed_checkout_context(ALIAS), owner)

    assert inspected["ci_terminal_red"] is False
    assert inspected["owner_failed_jobs"] is None
    assert reads == []


@pytest.mark.parametrize("proof", ["green", "pending", "narrowed", "base-red", "base-unknown", "main-moved", "red"])
def test_v2_red_close_rechecks_proof_before_mutating(world_v2, monkeypatch, proof):
    world = world_v2
    built = world.build(_v2_members(world))
    world.green(built)
    owner = int(built["train_pr"])
    if proof != "green":
        world.failed_runs.add(owner)
    if proof == "pending":
        world.runs.pop(owner)
    elif proof == "narrowed":
        world.narrowed.add(owner)
    elif proof == "main-moved":
        world.advance_main("later-main.txt", "later\n")
    baseline = True if proof == "base-red" else None if proof == "base-unknown" else False
    monkeypatch.setattr(world.gate, "_train_base_failure_match", lambda *args: baseline)
    context = world.gate.managed_checkout_context(ALIAS)
    request = {"train_pr": owner, "reason": "TRAIN_OWNER_RED"}

    if proof == "red":
        closed = world.gate.train_close(context, request)
        assert closed["branch_deleted"] is True
        # Lost-response recovery is idempotent, but still reads native proof.
        assert world.gate.train_close(context, request)["branch_deleted"] is True
    else:
        with pytest.raises(GateError) as refused:
            world.gate.train_close(context, request)
        assert refused.value.context["cleanup_started"] is False
        assert refused.value.context["park"] is True
        assert world.origin_has(str(built["train_branch"]))
        assert world.forge.pulls[owner]["state"] == "open"
        assert world.forge.comments == []


@pytest.mark.parametrize("red", [False, True])
def test_v2_closed_owner_inspection_retains_native_ci_proof(world_v2, monkeypatch, red):
    world = world_v2
    members, built = _retired_v2(world, monkeypatch)
    if not red:
        world.runs.pop(int(built["train_pr"]))
    context = world.gate.managed_checkout_context(ALIAS)

    inspected = world.gate.inspect_train_by_index(context, int(built["train_pr"]))

    assert inspected["pr_state"] == "closed"
    assert inspected["mode"] == "full"
    assert inspected["generation"] == built["generation"]
    assert inspected["head_sha"] == built["candidate_sha"]
    assert [(m["pr"], m["head"]) for m in inspected["members"]] == members
    assert inspected["ci_terminal_red"] is red
    assert inspected["main_is_final"] is False


def test_v2_closed_completed_owner_is_proven_from_git(world_v2):
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    world.green(built)
    for member in members:
        world.merge(built, *member)
    context = world.gate.managed_checkout_context(ALIAS)
    world.gate._train_cleanup_family(context, str(built["train_branch"]), "fixture completion")

    inspected = world.gate.inspect_train_by_index(context, int(built["train_pr"]))

    assert inspected["pr_state"] == "closed"
    assert inspected["main_is_final"] is True
    assert inspected["ci_state"] == "green"


@pytest.mark.parametrize("basis", ["exact", "clean_refresh"])
@pytest.mark.parametrize("drift", [False, True])
def test_v2_train_position_binds_reviewed_file_identities(world_v2, basis, drift):
    world = world_v2
    reviewed_base = world.reviewed_base
    reviewed_head = world.member(11, "shared.txt", "MEMBER\n" + "".join(f"s{n}\n" for n in range(2, 13)))
    member_head = reviewed_head
    if basis == "clean_refresh":
        original_reviews = world.forge.reviews[11]
        refresh_base = world.advance_main("outside-review.txt", "unrelated base change\n")
        branch = str(world.forge.pulls[11]["head"])
        git(["checkout", "-q", "-B", branch, reviewed_head], world.author)
        git(["merge", "--no-ff", "--no-edit", refresh_base], world.author)
        member_head = rev_parse(world.author)
        git(["push", "-q", "origin", f"{branch}:refs/heads/{branch}"], world.author)
        git(["checkout", "-q", "main"], world.author)
        world.replace_claim(11, member_head, intent="test claim")
        current_claim = world.gate._claim_identity(world.forge.pulls[11]["body"], member_head)
        reviewed_claim = world.gate._delegated_review_provenance(original_reviews[0], base_sha=None)
        assert reviewed_claim is not None and current_claim == reviewed_claim["claim_sha"]
        world.forge.reviews[11] = original_reviews
        world.runs[11] = (refresh_base, member_head)
    if drift:
        before = world.advance_main("shared.txt", "".join(f"s{n}\n" for n in range(1, 12)) + "BASE-LAST\n")
    else:
        before = world.advance_main("outside-position.txt", "another unrelated base change\n")
    context = world.gate.managed_checkout_context(ALIAS)
    assert world.gate._train_member_refusal(context, 11, member_head, mode="train-v2") is None
    after, conflicts = world.gate._auto_merge(context.cwd, before, member_head)
    assert not conflicts and after is not None
    reviewed_digest = world.gate._range_diff_digest(context, reviewed_base, reviewed_head)
    assert reviewed_digest is not None
    assert reviewed_digest == world.gate._range_diff_digest(context, before, after)
    # These are the actual auto-merge position endpoints, not equal digests
    # standing in for the complete reviewed file identities.
    reviewed_preimage = rev_parse(world.checkout, f"{reviewed_base}:shared.txt")
    position_preimage = rev_parse(world.checkout, f"{before}:shared.txt")
    reviewed_postimage = rev_parse(world.checkout, f"{reviewed_head}:shared.txt")
    position_postimage = rev_parse(world.checkout, f"{after}:shared.txt")
    assert (reviewed_preimage != position_preimage) is drift
    assert (reviewed_postimage != position_postimage) is drift

    approval = world.gate._train_approval(context, 11, member_head, before, after)

    if drift:
        assert approval is None
    else:
        assert approval is not None and approval[0] == basis


@pytest.mark.parametrize("endpoint", ["preimage", "postimage"])
def test_v2_train_position_probe_read_failure_refuses_approval(world_v2, monkeypatch, endpoint):
    world = world_v2
    reviewed_base = world.reviewed_base
    head = world.member(11, "shared.txt", "MEMBER\n" + "".join(f"s{n}\n" for n in range(2, 13)))
    before = world.advance_main("outside-position.txt", "unrelated base change\n")
    context = world.gate.managed_checkout_context(ALIAS)
    assert world.gate._train_member_refusal(context, 11, head, mode="train-v2") is None
    after, conflicts = world.gate._auto_merge(context.cwd, before, head)
    assert not conflicts and after is not None
    pair = (reviewed_base, before) if endpoint == "preimage" else (head, after)
    original = world.gate._runner
    failed_reads = []

    def read_failure(argv, cwd):
        if "--name-status" in argv and tuple(argv[-2:]) == pair:
            failed_reads.append(tuple(argv))
            return _done(code=1)
        return original(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", read_failure)

    approval = world.gate._train_approval(context, 11, head, before, after)

    assert approval is None
    assert failed_reads, "both endpoint probes must run; unreadable is not equal"


def _retired_v2(world, monkeypatch):
    members = _v2_members(world)
    built = world.build(members)
    world.green(built)
    world.failed_runs.add(int(built["train_pr"]))
    context = world.gate.managed_checkout_context(ALIAS)
    world.gate._train_cleanup_family(context, str(built["train_branch"]), "fixture retirement")
    # Baseline comparison has its own native-boundary tests. Here only its
    # positive nonmatching verdict is supplied, never classification data.
    monkeypatch.setattr(world.gate, "_train_base_failure_match", lambda *args: False)
    return members, built


@pytest.mark.parametrize("reordered", [False, True])
def test_v2_closed_red_batch_cannot_reform_after_restart(world_v2, monkeypatch, reordered):
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    native_pulls = set(world.forge.pulls)
    world.gate = operations.GitMaintainerGate(runner=world._run_with_forge_events, cwd=world.checkout)
    monkeypatch.setattr(world.gate, "_automerge_ci_status", world._ci)
    monkeypatch.setattr(world.gate, "_train_base_failure_match", lambda *args: False)

    with pytest.raises(GateError) as refused:
        world.build(list(reversed(members)) if reordered else members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["serial_only"] is True
    assert refused.value.context["train_pr"] == old["train_pr"]
    assert set(world.forge.pulls) == native_pulls
    assert not world.origin_has(str(old["train_branch"]))


def _owner_run(world, built, **run):
    """The closed owner's selected run, judged by the real evaluator."""
    owner, candidate = int(built["train_pr"]), str(built["candidate_sha"])
    record = {"number": owner, "head": {"sha": candidate}, "base": {"sha": str(built["base_sha"])}}
    world.evaluated[owner] = {"run": {
        "id": 70, "event": "pull_request_target", "path": ci.FORGEJO_LAUNCHER_PATH,
        "head_sha": candidate, "pull_requests": [record], **run}}


@pytest.mark.parametrize("lost", ["cancelled", "skipped", "missing"])
def test_v2_closed_batch_with_a_lost_owner_run_returns_to_serial(world_v2, monkeypatch, lost):
    """a run that can never give a verdict does not park the set."""
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    if lost == "missing":
        # The listing holds no run for the closed owner's exact head.
        world.evaluated[int(old["train_pr"])] = {"run": None}
    else:
        _owner_run(world, old, status="completed", conclusion=lost)

    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["serial_only"] is True
    assert refused.value.context.get("park") is not True
    assert refused.value.context["train_pr"] == old["train_pr"]
    assert len(world.forge.descriptions) == 1


def test_v2_closed_batch_with_a_nonstrict_failed_owner_run_returns_to_serial(world_v2, monkeypatch):
    """A run read CI_RUN_FAILED without a strict full-C1 failure.
    The base comparison cannot decide that; it is lost, as the executor retires it."""
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    strict = world._ci

    def not_strict(context, pull, head_sha, **kwargs):
        answer = strict(context, pull, head_sha, **kwargs)
        if pull.index == int(old["train_pr"]):
            answer["ci_evidence"]["workflow_run_conclusion"] = "cancelled"
        return answer

    monkeypatch.setattr(world.gate, "_automerge_ci_status", not_strict)
    # A strict red on a red base parks; the non-strict reading never asks.
    compared = []
    monkeypatch.setattr(world.gate, "_train_base_failure_match",
                        lambda *args: compared.append(args) or True)

    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["serial_only"] is True
    assert refused.value.context.get("park") is not True
    assert refused.value.context["train_pr"] == old["train_pr"]
    assert compared == []


@pytest.mark.parametrize(
    "proof", ["queued", "in_progress", "narrowed", "base-red", "base-unknown"])
def test_v2_closed_batch_with_unqualified_proof_parks(world_v2, monkeypatch, proof):
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    owner = int(old["train_pr"])
    if proof in ("queued", "in_progress"):
        # A live run may still give a verdict: wait for it.
        _owner_run(world, old, status=proof, conclusion=None)
    elif proof == "narrowed":
        world.narrowed.add(owner)
    else:
        baseline = True if proof == "base-red" else None
        monkeypatch.setattr(world.gate, "_train_base_failure_match", lambda *args: baseline)

    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.context["park"] is True
    assert refused.value.context.get("serial_only") is not True
    assert len(world.forge.descriptions) == 1


def test_v2_closed_red_batch_does_not_forbid_a_different_head_set(world_v2, monkeypatch):
    members, old = _retired_v2(world_v2, monkeypatch)

    built = world_v2.build([members[0], members[2]])

    assert built["train_pr"] != old["train_pr"]
    assert len(built["members"]) == 2
    assert world_v2.origin_has(str(built["train_branch"]))


# Built from the executor's own set, so a code it adds without the gate's reason
# pattern following fails here.
_LOST_OWNER_CLOSES = [
    f"TRAIN_OWNER_CI_LOST: {code}" for code in sorted(merge_open_prs._TRAIN_OWNER_LOST)]


@pytest.mark.parametrize("reason", ["TRAIN_OWNER_RED", *_LOST_OWNER_CLOSES])
@pytest.mark.parametrize("hidden_head_ref", [False, True])
def test_v2_red_close_retries_an_open_owner_after_the_ref_is_deleted(
        world_v2, monkeypatch, hidden_head_ref, reason):
    """A qualified red close deletes the ref before the PR close may fail. So does the
    close of an owner that lost its CI run, which re-proves the family and
    never the loss: the owner stays green here."""
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    owner = int(built["train_pr"])
    branch = str(built["train_branch"])
    world.green(built)
    if reason == "TRAIN_OWNER_RED":
        world.failed_runs.add(owner)
        monkeypatch.setattr(world.gate, "_train_base_failure_match", lambda *args: False)
    git(["push", "-q", "origin", f"{built['candidate_sha']}:refs/pull/{owner}/head"], world.checkout)
    git(["push", "-q", "origin", "--delete", branch], world.checkout)
    world.forge.pulls[owner]["head_sha"] = str(built["candidate_sha"])
    if hidden_head_ref:
        world.forge.pulls[owner]["native_head_ref"] = f"refs/pull/{owner}/head"
        world.forge.pulls[owner]["native_label"] = branch
        world.forge.pulls[owner]["head"] = f"refs/pull/{owner}/head"
    close = world.forge.close_pr

    def refuse_owner_close(runner, cwd, forge_repo, index):
        return _done(code=1) if index == owner else close(runner, cwd, forge_repo, index)

    monkeypatch.setattr(world.forge, "close_pr", refuse_owner_close)
    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason=reason)

    assert refused.value.code == "TRAIN_CLOSE_FAILED"
    assert refused.value.context["failed_prs"] == [owner]
    assert world.forge.pulls[owner]["state"] == "open"
    monkeypatch.setattr(world.forge, "close_pr", close)

    cleaned = world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason=reason)

    assert cleaned["branch_deleted"] is True
    assert world.forge.pulls[owner]["state"] == "closed"


def _open_owner_with_deleted_ref(world, members):
    """An open v2 owner whose ref is gone: Forgejo reports its head as `refs/pull/<n>/head`."""
    built = world.build(members)
    owner = int(built["train_pr"])
    branch = str(built["train_branch"])
    git(["push", "-q", "origin", f"{built['candidate_sha']}:refs/pull/{owner}/head"], world.checkout)
    git(["push", "-q", "origin", "--delete", branch], world.checkout)
    pull = world.forge.pulls[owner]
    pull.update(head_sha=str(built["candidate_sha"]), head=f"refs/pull/{owner}/head",
                native_head_ref=f"refs/pull/{owner}/head", native_label=branch)
    return owner, branch, pull


@pytest.mark.parametrize("tamper,code", [
    ("label", "TRAIN_INVALID"), ("title", "TRAIN_INVALID"), ("head", "REMOTE_HEAD_MISMATCH")])
@pytest.mark.parametrize("reason", _LOST_OWNER_CLOSES)
def test_v2_lost_owner_close_proves_an_open_hidden_head_owner_before_it_writes(
        world_v2, reason, tamper, code):
    """The lost-run close reaches no red proof, so recovering an open owner whose ref is
    gone must prove its native identity and frozen chain first. A refusal leaves the owner
    open and posts nothing."""
    world = world_v2
    members = _v2_members(world)
    owner, branch, pull = _open_owner_with_deleted_ref(world, members)
    if tamper == "label":
        # The name of a train that starts at another PR: this chain does not.
        first = members[0][0]
        pull["native_label"] = branch.replace(f"-{first}-g", f"-{first + 1}-g", 1)
        assert pull["native_label"] != branch
    elif tamper == "title":
        pull["title"] = f"{train_operations.TRAIN_TITLE_PREFIX} #1, #2, #3"
    else:
        pull["head_sha"] = world.forge.main()

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason=reason)

    assert refused.value.code == code
    assert pull["state"] == "open"
    assert world.forge.comments == []


@pytest.mark.parametrize("reason", [
    "TRAIN_OWNER_CI_LOST: CI_API_UNAVAILABLE", "TRAIN_OWNER_CI_LOST: CI_RUN_IN_PROGRESS",
    "TRAIN_OWNER_CI_LOST", "TRAIN_ORPHANED"])
def test_v2_open_hidden_head_owner_is_recovered_only_for_red_and_a_lost_run(world_v2, reason):
    """Only strict red and the three lost-run codes may normalise that representation;
    a reason that merely looks like them keeps the strict closed-owner rule."""
    world = world_v2
    owner, _branch, pull = _open_owner_with_deleted_ref(world, _v2_members(world))

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=owner, reason=reason)

    assert refused.value.code == "TRAIN_INVALID"
    assert pull["state"] == "open"
    assert world.forge.comments == []


def test_v2_closed_owner_inspection_fetches_the_hidden_head_once(world_v2, monkeypatch):
    """One deleted closed owner costs one hidden-head fetch and one chain proof."""
    world = world_v2
    _members, old = _retired_v2(world, monkeypatch)
    owner = int(old["train_pr"])
    fetches: list[tuple[str, ...]] = []
    inner = world.gate._runner

    def counting(argv, cwd, *args, **kwargs):
        if any(str(part).startswith("refs/pull/") for part in argv):
            fetches.append(tuple(str(part) for part in argv))
        return inner(argv, cwd, *args, **kwargs)

    monkeypatch.setattr(world.gate, "_runner", counting)
    world.gate.inspect_train_by_index(
        world.gate.managed_checkout_context(ALIAS), owner)

    assert len(fetches) == 1, fetches


def test_v2_closed_owner_head_mismatch_parks(world_v2, monkeypatch):
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    original = world.forge.read_pr_state

    def damaged(runner, cwd, repository, index):
        data = json.loads(original(runner, cwd, repository, index).stdout)
        if index == old["train_pr"]:
            data["head"]["sha"] = "f" * 40
        return _done(json.dumps(data))

    monkeypatch.setattr(world.forge, "read_pr_state", damaged)
    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.context["park"] is True
    assert len(world.forge.descriptions) == 1


def _v2_members(world: TrainWorld) -> list[tuple[int, str]]:
    members = [(pr, world.member(pr, f"m{pr}.txt", f"member {pr}\n")) for pr in (11, 12, 13)]
    world.advance_main()  # Each member's own run judged an older base.
    return members


def test_v2_publishes_only_the_full_owner_under_a_generation(world_v2):
    members = _v2_members(world_v2)
    base = world_v2.forge.main()

    train = world_v2.build(members)

    assert train["code"] == "TRAIN_BUILT"
    assert train["mode"] == "full"
    assert len(train["generation"]) == 32
    branch = f"train/{base[:12]}-11-g{train['generation']}"
    assert train["train_branch"] == branch
    assert branch == _owner_name(base, 11, [head for _pr, head in members])
    assert world_v2.origin_has(branch)
    assert not world_v2.origin_has(f"{branch}-prefix-1")
    assert not world_v2.origin_has(f"{branch}-prefix-2")
    assert [record["pr"] for record in train["prefixes"]] == [train["train_pr"]] * 3
    assert [record["candidate"] for record in train["prefixes"]] == [
        member["candidate"] for member in train["members"]]
    assert any("tests the whole batch once" in body
               for body in world_v2.forge.descriptions.values())


@pytest.mark.parametrize("judged", ["recorded", "intermediate"])
def test_v2_admits_a_member_on_its_narrowed_run(world_v2, judged):
    members = _v2_members(world_v2)
    judged_base = world_v2.reviewed_base
    if judged == "intermediate":
        judged_base = world_v2.forge.main()
        world_v2.runs[12] = (judged_base, members[1][1])
        world_v2.advance_main("later-main.txt")
    world_v2.narrowed.add(12)

    train = world_v2.build(members)

    assert [member["pr"] for member in train["members"]] == [11, 12, 13]
    assert train["dropped"] == []
    context = world_v2.gate.managed_checkout_context(ALIAS)
    inspected = world_v2.gate.inspect_pr_by_index(
        context, 12, review_base=world_v2.reviewed_base)
    assert inspected["ci_state"] == "unavailable"
    evidence = inspected["_ci_status"]["ci_evidence"]
    assert evidence["result"] == "UNAVAILABLE"
    assert evidence["reason_code"] == "CI_PROFILE_INSUFFICIENT"
    assert evidence["accepted_profile"] == "checks/c2"
    assert evidence["workflow_run_conclusion"] == "success"
    assert evidence["base_sha"] == judged_base != train["base_sha"]


@pytest.mark.parametrize("judged", ["recorded", "intermediate"])
def test_v2_admission_refuses_a_failed_narrowed_run_on_an_accepted_older_base(world_v2, judged):
    members = _v2_members(world_v2)
    if judged == "intermediate":
        world_v2.runs[12] = (world_v2.forge.main(), members[1][1])
        world_v2.advance_main("later-main.txt")
    world_v2.narrowed.add(12)
    world_v2.failed_runs.add(12)

    train = world_v2.build(members)

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "CI_PROFILE_INSUFFICIENT"}]


def test_v2_admission_refuses_a_narrowed_run_judged_outside_the_base_range(world_v2):
    members = _v2_members(world_v2)
    # A commit descending from the recorded base but not contained in
    # canonical main: a legal judged base for CI, outside the admission range.
    sibling = world_v2.member(99, "sibling.txt", "sibling\n")
    git(["fetch", "-q", "origin", "refs/heads/task/member-99"], world_v2.checkout)
    world_v2.runs[members[1][0]] = (sibling, members[1][1])

    train = world_v2.build(members)

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "CI_BASE_STALE"}]


def test_v2_drops_a_member_whose_paths_overlap_an_earlier_member(world_v2):
    first = world_v2.member(11, "shared.txt",
                            "ONE\n" + "".join(f"s{n}\n" for n in range(2, 13)))
    second = world_v2.member(12, "shared.txt",
                             "".join(f"s{n}\n" for n in range(1, 12)) + "TWELVE\n")
    third = world_v2.member(13, "m13.txt", "member 13\n")
    world_v2.advance_main()

    train = world_v2.build([(11, first), (12, second), (13, third)])

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "PR_TRAIN_OVERLAP"}]


def test_v2_drops_a_member_that_changes_the_trusted_workflow(world_v2):
    first = world_v2.member(11, "m11.txt", "member 11\n")
    second = world_v2.member(12, ci.FORGEJO_LAUNCHER_PATH, "name: forged\non: push\n")
    third = world_v2.member(13, "m13.txt", "member 13\n")
    world_v2.advance_main()

    train = world_v2.build([(11, first), (12, second), (13, third)])

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "PR_TRAIN_WORKFLOW"}]


def test_v2_merges_every_member_on_the_one_owner_run(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.ci_calls.clear()

    first = world_v2.merge(train, *members[0])
    assert first["mode"] == "full"
    assert first["interim"] is True
    assert first["prefix_pr"] == train["train_pr"]
    message = world_v2.forge.merge_messages[members[0][0]]
    assert f"Train-Final: {train['candidate_sha']}" in message
    assert f"Train-Owner: {train['train_pr']}" in message
    assert world_v2.ci_calls == [
        (int(train["train_pr"]), train["base_sha"], train["candidate_sha"])]

    second = world_v2.merge(train, *members[1])
    assert second["interim"] is True
    third = world_v2.merge(train, *members[2])
    assert third["interim"] is False
    assert world_v2.tree("origin/main") == world_v2.tree(train["candidate_sha"])


def test_v2_rebuild_adopts_the_same_generation(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)

    second = world_v2.build(members)

    assert second["train_pr"] == first["train_pr"]
    assert second["train_branch"] == first["train_branch"]
    assert len(world_v2.forge.descriptions) == 1
    # An adopted owner is never retired.
    assert world_v2.forge.pulls[int(first["train_pr"])]["state"] == "open"
    assert world_v2.origin_has(str(first["train_branch"]))


def test_v2_recovers_a_crash_before_the_owner_pr_existed(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    world_v2.forge.pulls.pop(int(first["train_pr"]))

    second = world_v2.build(members)

    assert second["train_branch"] == first["train_branch"]
    assert second["candidate_sha"] == first["candidate_sha"]
    assert second["train_pr"] != first["train_pr"]
    assert world_v2.origin_has(first["train_branch"])
    # The own name's ref was adopted, not retired and not pushed again.
    assert world_v2.forge.pulls[int(second["train_pr"])]["state"] == "open"


def test_v2_rebuild_with_a_changed_batch_retires_the_family(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)

    second = world_v2.build([members[0], members[2]])

    assert second["train_branch"] != first["train_branch"]
    assert world_v2.forge.pulls[int(first["train_pr"])]["state"] == "closed"
    assert not world_v2.origin_has(first["train_branch"])
    assert [member["pr"] for member in second["members"]] == [11, 13]


def test_v2_merge_does_not_accept_a_narrowed_owner_run(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.narrowed.add(int(train["train_pr"]))

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "PR_CI_NOT_GREEN"
    assert world_v2.forge.merges == []


def test_v2_cleanup_never_deletes_another_generation(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    other = f"{first['train_branch'].split('-g')[0]}-g{'b' * 32}"
    git(["push", "-q", "origin", f"{first['candidate_sha']}:refs/heads/{other}"],
        world_v2.checkout)
    context = world_v2.gate.managed_checkout_context(ALIAS)

    world_v2.gate._train_cleanup_family(context, first["train_branch"], "test cleanup")

    assert not world_v2.origin_has(first["train_branch"])
    assert world_v2.origin_has(other)


# ---------------------------------------------------------------------------
# exact-name owner discovery and the deterministic generation `G`.

_G_BASE = "0123456789abcdef0123456789abcdef01234567"
_G_HEADS = ["c" * 40, "a" * 40, "b" * 40]
_OK = "HTTP/2.0 200 OK\n"


def _owner_name(base: str, first_pr: int, heads: list[str]) -> str:
    """The owner name the train-v2 format specifies, from `hashlib` alone and none of the gate's code."""
    payload = (b"train-v2\0" + base.encode("ascii") + b"\0"
               + "\n".join(sorted(heads)).encode("ascii"))
    return f"train/{base[:12]}-{first_pr}-g{hashlib.sha256(payload).hexdigest()[:32]}"


def _variant_names(world: TrainWorld, members: list[tuple[int, str]]) -> list[str]:
    """One owner name per member in request order; the first is the batch's own."""
    heads = [head for _pr, head in members]
    return [_owner_name(world.forge.main(), pr, heads) for pr, _head in members]


def _fork_row(number: int, name: str) -> dict[str, object]:
    """A PR from another repository whose head branch carries the same name."""
    return {"number": number, "id": number, "state": "open", "title": "fork",
            "head": {"ref": name, "label": f"someone:{name}", "sha": "e" * 40,
                     "repo": {"full_name": "someone/fork"}}}


def _unrelated(world: TrainWorld, count: int) -> None:
    """*count* closed PRs on ordinary branches: native history discovery must never read."""
    for number in range(20000, 20000 + count):
        world.forge.pulls[number] = {
            "state": "closed", "title": "feat: unrelated", "head": f"task/unrelated-{number}",
            "head_sha": "e" * 40, "merged": False, "mergeable": True, "labels": []}


def _stem_with_a_tombstone(world: TrainWorld) -> tuple[list[tuple[int, str]], str]:
    """Build [11, 13] and drop its PR: a deterministic owner ref with no PR, on the stem of [11, 12, 13]."""
    members = _v2_members(world)
    built = world.build([members[0], members[2]])
    world.forge.pulls.pop(int(built["train_pr"]))
    return members, str(built["train_branch"])


def test_generation_is_the_documented_digest_of_the_base_and_the_sorted_heads():
    """The byte layout is a contract. The expected values were computed outside the gate's code
    with `hashlib` alone: the tag, NUL, the full base SHA, NUL, then the head SHAs sorted as text
    and joined by newlines, every SHA as its 40 ASCII hex characters, first 32 hex digits."""
    generation = train_operations._train_generation

    assert generation(_G_BASE, _G_HEADS) == "84259a697ada546b280bc9ec43d0fa95"
    # Member order is irrelevant; the full base and every one head are not.
    assert generation(_G_BASE, reversed(_G_HEADS)) == "84259a697ada546b280bc9ec43d0fa95"
    assert generation(_G_BASE[:-1] + "8", _G_HEADS) == "656f38df6649323d91b263c1f699d46d"
    assert generation(_G_BASE, [*_G_HEADS[:2], "b" * 39 + "d"]) == "b0499f532a372bfa993a7a68ff504839"
    assert generation(_G_BASE, _G_HEADS[:2]) == "d4c0642c11b28d00a430fd1411a11b74"
    # The raw 20-byte form of the same SHAs is a different byte string.
    assert generation(_G_BASE, _G_HEADS) != "c54e357087c11d49fd19a5b9afb181cf"


def test_variants_are_one_owner_name_per_kept_member_with_the_own_first():
    kept = [{"pr": pr, "head": head} for pr, head in ((12, "c" * 40), (11, "a" * 40), (13, "b" * 40))]

    names = train_operations._train_variants(_G_BASE, kept)

    assert names == [f"train/0123456789ab-{pr}-g84259a697ada546b280bc9ec43d0fa95"
                     for pr in (12, 11, 13)]


def test_v2_names_the_owner_by_its_kept_members(world_v2):
    members = _v2_members(world_v2)
    world_v2.forge.pulls[11]["labels"] = ["gate/hold"]

    built = world_v2.build(members)

    assert [member["pr"] for member in built["members"]] == [12, 13]
    assert built["train_branch"] == _owner_name(
        world_v2.forge.main(), 12, [members[1][1], members[2][1]])


def test_v2_reordered_request_finds_an_open_owner_through_its_variant_name(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    pulls, descriptions = set(world_v2.forge.pulls), dict(world_v2.forge.descriptions)
    world_v2.forge.head_lookups.clear()
    reordered = list(reversed(members))

    with pytest.raises(GateError) as refused:
        world_v2.build(reordered)

    assert refused.value.context["step"] == "closed_batch_active"
    assert refused.value.context["park"] is True
    assert refused.value.context["branch"] == first["train_branch"]
    # One lookup per variant, the request's own first; nothing was published.
    assert world_v2.forge.head_lookups == _variant_names(world_v2, reordered)
    assert set(world_v2.forge.pulls) == pulls
    assert world_v2.forge.descriptions == descriptions


@pytest.mark.parametrize("remnant", ["ref-without-pr", "open-pr-without-ref"])
def test_v2_another_variant_with_a_ref_or_an_open_pr_parks_the_reordered_batch(world_v2, remnant):
    """The reordered batch has another variant name as its own, so its own name is free; the
    earlier owner is found through its variant name, whichever of its two records survived."""
    members = _v2_members(world_v2)
    if remnant == "ref-without-pr":
        # A crash before the owner PR existed: the ref stays and no PR records the name.
        built = world_v2.build(members)
        world_v2.forge.pulls.pop(int(built["train_pr"]))
        branch = str(built["train_branch"])
    else:
        _owner, branch, _pull = _open_owner_with_deleted_ref(world_v2, members)
    pulls = set(world_v2.forge.pulls)
    refs = git(["for-each-ref", "--format=%(refname:short)", "refs/heads/train"],
               world_v2.origin).stdout.split()

    with pytest.raises(GateError) as refused:
        world_v2.build(list(reversed(members)))

    assert refused.value.context["step"] == "closed_batch_active"
    assert refused.value.context["park"] is True
    assert refused.value.context["branch"] == branch
    # Nothing was published and nothing was retired.
    assert set(world_v2.forge.pulls) == pulls
    assert len(world_v2.forge.descriptions) == 1
    assert git(["for-each-ref", "--format=%(refname:short)", "refs/heads/train"],
               world_v2.origin).stdout.split() == refs


def _train_heads(world: TrainWorld) -> dict[str, str]:
    """Every owner ref on origin with its tip: what a refused build must leave as it found it."""
    listed = git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/train"],
                 world.origin).stdout.splitlines()
    return dict(line.split() for line in listed)


def _closed_owner_keeping_its_branch(world: TrainWorld, monkeypatch):
    """An owner whose PR a person closed after its strict red, leaving the branch in place.

    The branch still names the owner candidate, so the forge reports the PR under that branch
    name, not under `refs/pull/<n>/head`, and the live tip is the closed PR's head."""
    members = _v2_members(world)
    built = world.build(members)
    owner = int(built["train_pr"])
    world.green(built)
    world.failed_runs.add(owner)
    world.forge.close_pr(None, world.checkout, REPOSITORY, owner)
    # As for `_retired_v2`: only the positive nonmatching base verdict is supplied here.
    monkeypatch.setattr(world.gate, "_train_base_failure_match", lambda *args: False)
    assert world.forge.pulls[owner]["state"] == "closed"
    assert world.origin_has(str(built["train_branch"]))
    return members, built


@pytest.mark.parametrize("reordered", [False, True], ids=["own-variant", "another-variant"])
@pytest.mark.parametrize("evidence", ["red", "lost", "unqualified"])
def test_v2_a_closed_owner_that_kept_its_branch_gets_the_closed_batch_outcome(
        world_v2, monkeypatch, evidence, reordered):
    """A ref no longer says that its owner is live once the PR is closed. Whether the rebuilt
    batch gives the owner its own variant name or another one (a reordered request), the PR
    decides, as it does for a deleted branch: a strict red or a lost run returns the set to the
    serial gates and any other evidence parks. The ref is never adopted, and nothing is created,
    retired or deleted."""
    world = world_v2
    members, old = _closed_owner_keeping_its_branch(world, monkeypatch)
    if evidence == "lost":
        _owner_run(world, old, status="completed", conclusion="cancelled")
    elif evidence == "unqualified":
        # A live run may still give a verdict: wait for it.
        _owner_run(world, old, status="queued", conclusion=None)
    pulls, descriptions = set(world.forge.pulls), dict(world.forge.descriptions)
    heads = _train_heads(world)
    request = list(reversed(members)) if reordered else members
    world.forge.head_lookups.clear()

    with pytest.raises(GateError) as refused:
        world.build(request)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["step"] == "closed_batch"
    assert context["train_pr"] == old["train_pr"]
    if evidence == "unqualified":
        assert context["park"] is True
        assert context.get("serial_only") is not True
    else:
        assert context["serial_only"] is True
        assert context.get("park") is not True
    # The owner was found through its variant name, and by no other read.
    assert world.forge.head_lookups == _variant_names(world, request)
    # No new PR, and no ref pushed or deleted: the branch is as the owner left it.
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions
    assert _train_heads(world) == heads
    assert world.forge.pulls[int(old["train_pr"])]["state"] == "closed"


@pytest.mark.parametrize("reordered", [False, True], ids=["own-variant", "another-variant"])
def test_v2_a_closed_owner_whose_branch_moved_fails_the_native_proof_and_parks(
        world_v2, monkeypatch, reordered):
    """The forge reports a live branch's tip as the PR's head while `refs/pull/<n>/head` still
    holds the owner candidate, so a branch that moved after the close is not the owner: the
    proof fails `REMOTE_HEAD_MISMATCH`, the build parks, and the branch is left alone."""
    world = world_v2
    members, old = _closed_owner_keeping_its_branch(world, monkeypatch)
    name = str(old["train_branch"])
    git(["push", "-q", "--force", "origin", f"{world.forge.main()}:refs/heads/{name}"],
        world.author)
    pulls, heads = set(world.forge.pulls), _train_heads(world)

    with pytest.raises(GateError) as refused:
        world.build(list(reversed(members)) if reordered else members)

    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == "owner_proof_failed"
    assert refused.value.context["cause"] == "REMOTE_HEAD_MISMATCH"
    assert refused.value.context["train_pr"] == old["train_pr"]
    assert set(world.forge.pulls) == pulls
    assert len(world.forge.descriptions) == 1
    assert _train_heads(world) == heads
    assert rev_parse(world.origin, f"refs/heads/{name}") == world.forge.main()


@pytest.mark.parametrize("reordered", [False, True])
def test_v2_backdated_retired_owner_is_found_by_name_among_ten_thousand_unrelated_prs(
        world_v2, monkeypatch, reordered):
    """Created before the base commit existed and never updated since: a Git commit time is not
    native chronology, and no listing order or page position can hide the owner."""
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    owner = int(old["train_pr"])
    world.forge.pulls[owner].update(created_at="2010-01-01T00:00:00Z",
                                    updated_at="2010-01-01T00:00:00Z")
    _unrelated(world, 10001)
    request = list(reversed(members)) if reordered else members
    world.forge.head_lookups.clear()

    with pytest.raises(GateError) as refused:
        world.build(request)

    assert refused.value.context["serial_only"] is True
    assert refused.value.context["train_pr"] == owner
    assert world.forge.head_lookups == _variant_names(world, request)
    assert len(world.forge.descriptions) == 1


def test_v2_ten_thousand_unrelated_prs_cost_no_history_read_and_at_most_three_locked_lookups(
        world_v2, monkeypatch):
    world = world_v2
    members = _v2_members(world)
    _unrelated(world, 10001)
    locked: list[bool | None] = []
    history: list[tuple[object, ...]] = []
    original = world.forge.list_pulls_by_head

    def watched(runner, cwd, repository, head):
        locked.append(_lock_busy_probe(world.policy, contracts.GATE_LOCK_DIR,
                                       contracts.GATE_LOCK_PARENT))
        return original(runner, cwd, repository, head)

    monkeypatch.setattr(world.forge, "list_pulls_by_head", watched)
    # The fake has no paged history reader any more; a call would be a census read.
    monkeypatch.setattr(world.forge, "list_closed_pulls",
                        lambda *args, **kwargs: history.append(args), raising=False)

    built = world.build(members)

    assert built["code"] == "TRAIN_BUILT"
    assert history == []
    assert world.forge.head_lookups == _variant_names(world, members)
    assert len(world.forge.head_lookups) <= 3
    # Every lookup ran under the repository lock.
    assert locked == [True] * len(world.forge.head_lookups)


def test_v2_an_unrelated_pr_that_appears_around_the_locked_build_parks_nothing(world_v2, monkeypatch):
    """the census parked every build whose repository gained a PR between the read that
    prepared it and the locked revalidation (`closed_history_changed`). A lookup by exact owner
    name has no repository-wide count or frontier to change, so a new PR on an ordinary branch
    cannot reach the build at any seam: after the executor chose the members, while the gate
    admits them, when the guard looks the owner names up, and just before it publishes."""
    world = world_v2
    members = _v2_members(world)
    appeared: list[str] = []
    numbers: list[int] = []

    def appear(seam: str) -> None:
        """Open one more PR on an ordinary task branch, as a person does while the gate works."""
        number = 20001 + len(numbers)
        branch = f"task/unrelated-{number}"
        git(["push", "-q", "origin", f"{world.forge.main()}:refs/heads/{branch}"], world.author)
        world.forge.add(number, branch, title="feat: unrelated")
        appeared.append(seam)
        numbers.append(number)

    def appears_before_the_first(method: str, seam: str) -> None:
        original = getattr(world.forge, method)
        fired: list[bool] = []

        def wrapper(*args, **kwargs):
            if not fired:
                fired.append(True)
                appear(seam)
            return original(*args, **kwargs)

        monkeypatch.setattr(world.forge, method, wrapper)

    appear("selection")
    appears_before_the_first("read_pr_state", "admission")
    appears_before_the_first("list_pulls_by_head", "guard")
    appears_before_the_first("create_pr", "publication")

    try:
        built = world.build(members)
    except GateError as refused:
        pytest.fail(f"a new unrelated PR refused the build: {refused.code} {refused.context}")

    assert appeared == ["selection", "admission", "guard", "publication"]
    assert built["code"] == "TRAIN_BUILT"
    assert not {"park", "step", "serial_only"} & built.keys()
    assert [member["pr"] for member in built["members"]] == [pr for pr, _head in members]
    assert world.forge.pulls[int(built["train_pr"])]["state"] == "open"
    assert world.forge.head_lookups == _variant_names(world, members)
    assert all(world.forge.pulls[number]["state"] == "open" for number in numbers)


def test_v2_reused_owner_name_fails_the_native_proof_and_parks(world_v2, monkeypatch):
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    name = str(old["train_branch"])
    # A new branch takes the retired name: the forge now reports its tip as the closed
    # PR's head, while `refs/pull/<n>/head` still holds the owner candidate.
    git(["push", "-q", "origin", f"{world.forge.main()}:refs/heads/{name}"], world.checkout)
    pulls = set(world.forge.pulls)

    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == "owner_proof_failed"
    assert refused.value.context["cause"] == "REMOTE_HEAD_MISMATCH"
    assert refused.value.context["train_pr"] == old["train_pr"]
    assert set(world.forge.pulls) == pulls
    assert len(world.forge.descriptions) == 1
    assert rev_parse(world.origin, f"refs/heads/{name}") == world.forge.main()


def test_v2_two_same_repository_prs_on_one_owner_name_park(world_v2, monkeypatch):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    name = str(first["train_branch"])
    original = world_v2.forge.list_pulls_by_head

    def doubled(runner, cwd, repository, head):
        rows = json.loads(original(runner, cwd, repository, head).stdout)
        if rows:
            rows.append({**rows[0], "number": 5000, "id": 5000})
        return _by_head(rows)

    monkeypatch.setattr(world_v2.forge, "list_pulls_by_head", doubled)
    pulls = set(world_v2.forge.pulls)

    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == "owner_lookup_ambiguous"
    assert refused.value.context["branch"] == name
    assert set(world_v2.forge.pulls) == pulls
    assert world_v2.forge.pulls[int(first["train_pr"])]["state"] == "open"


def test_v2_a_pr_from_another_repository_on_an_owner_name_is_ignored(world_v2, monkeypatch):
    members = _v2_members(world_v2)
    original = world_v2.forge.list_pulls_by_head

    def with_a_fork(runner, cwd, repository, head):
        rows = json.loads(original(runner, cwd, repository, head).stdout)
        return _by_head([*rows, _fork_row(6000, head)])

    monkeypatch.setattr(world_v2.forge, "list_pulls_by_head", with_a_fork)

    built = world_v2.build(members)
    # A real owner PR next to the fork's is the one same-repository PR on its name.
    again = world_v2.build(members)

    assert built["code"] == "TRAIN_BUILT"
    assert again["train_pr"] == built["train_pr"]


_LOOKUP_REFUSALS = [
    pytest.param(_done("[]", f"{_OK}X-Total-Count: 0\n", 1), "owner_lookup_unavailable",
                 id="failed-read"),
    pytest.param(_done("[]", "HTTP/2.0 403 Forbidden\nX-Total-Count: 0\n"),
                 "owner_lookup_unavailable", id="status-other-than-200"),
    pytest.param(_done("[]", _OK), "owner_lookup_unavailable", id="missing-total"),
    pytest.param(_done("[]", f"{_OK}X-Total-Count: 0\nX-Total-Count: 0\n"),
                 "owner_lookup_unavailable", id="repeated-total"),
    # The fork rows are ignored, so only the page-limit check can refuse these.
    pytest.param(_by_head([_fork_row(7000 + n, "task/other") for n in range(contracts.PAGE_SIZE + 1)]),
                 "owner_lookup_malformed", id="total-above-the-page-limit"),
    pytest.param(_by_head([_fork_row(7000, "task/other")], total=2),
                 "owner_lookup_malformed", id="total-differs-from-the-rows"),
    pytest.param(_done("not json", f"{_OK}X-Total-Count: 0\n"), "owner_lookup_malformed",
                 id="not-json"),
    pytest.param(_done('{"message": "unreadable"}', f"{_OK}X-Total-Count: 0\n"),
                 "owner_lookup_malformed", id="not-a-list"),
    pytest.param(_done('[{"number": 7}]', f"{_OK}X-Total-Count: 1\n"), "owner_lookup_malformed",
                 id="row-without-a-head"),
    pytest.param(_by_head([{**_fork_row(7000, "task/other"), "state": "merged"}]),
                 "owner_lookup_malformed", id="unknown-state"),
]


@pytest.mark.parametrize(("answer", "step"), _LOOKUP_REFUSALS)
def test_v2_an_unavailable_owner_lookup_parks_before_anything_is_published(
        world_v2, monkeypatch, answer, step):
    members = _v2_members(world_v2)
    monkeypatch.setattr(world_v2.forge, "list_pulls_by_head", lambda *args: answer)

    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == step
    assert world_v2.forge.descriptions == {}
    assert git(["for-each-ref", "refs/heads/train"], world_v2.origin).stdout == ""


@pytest.mark.parametrize("failure", ["timeout", "os-error", "gate-error", "no-reader"])
def test_v2_an_unreachable_or_missing_owner_reader_parks(world_v2, monkeypatch, failure):
    members = _v2_members(world_v2)

    def unreachable(*args):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("tea", 1)
        if failure == "os-error":
            raise OSError("tea is gone")
        contracts.fail("COMMAND_TIMEOUT")

    if failure == "no-reader":
        monkeypatch.delattr(TrainForge, "list_pulls_by_head")
    else:
        monkeypatch.setattr(world_v2.forge, "list_pulls_by_head", unreachable)

    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == "owner_lookup_unavailable"
    assert world_v2.forge.descriptions == {}
    assert git(["for-each-ref", "refs/heads/train"], world_v2.origin).stdout == ""


def _lists_exact_owner_names(argv: list[str]) -> bool:
    """The guard's one `git ls-remote` of the variant names, never a stem wildcard or a family listing."""
    return (argv[1:4] == ["ls-remote", "--heads", "origin"] and len(argv) > 4
            and all(len(ref.rpartition("-g")[2]) == 32 for ref in argv[4:]))


@pytest.mark.parametrize("failure", ["exit-code", "timeout", "unexpected-name", "malformed-line"])
def test_v2_an_unreadable_variant_ref_listing_parks_before_anything_is_published(
        world_v2, monkeypatch, failure):
    members = _v2_members(world_v2)
    runner = world_v2.gate._runner
    listed: list[list[str]] = []

    def listing(argv, cwd):
        if not _lists_exact_owner_names(argv):
            return runner(argv, cwd)
        listed.append(argv)
        if failure == "timeout":
            contracts.fail("COMMAND_TIMEOUT")
        if failure == "exit-code":
            return _done(code=2)
        if failure == "unexpected-name":
            return _done(f"{'a' * 40}\trefs/heads/train/not-asked\n")
        return _done("not a ref line\n")

    monkeypatch.setattr(world_v2.gate, "_runner", listing)

    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == "owner_refs_unavailable"
    assert [ref.removeprefix("refs/heads/") for ref in listed[0][4:]] == _variant_names(
        world_v2, members)
    assert world_v2.forge.descriptions == {}
    assert git(["for-each-ref", "refs/heads/train"], world_v2.origin).stdout == ""


def test_v2_same_first_member_in_another_order_is_a_spent_generation(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    pulls = set(world_v2.forge.pulls)

    # Same heads, same first PR: the same owner name, whose chain has another order.
    with pytest.raises(GateError) as refused:
        world_v2.build([members[0], members[2], members[1]])

    assert refused.value.context["step"] == "generation_spent"
    assert refused.value.context["serial_only"] is True
    assert refused.value.context["branch"] == first["train_branch"]
    assert world_v2.forge.pulls[int(first["train_pr"])]["state"] == "open"
    assert world_v2.origin_has(str(first["train_branch"]))
    assert set(world_v2.forge.pulls) == pulls


def test_v2_open_owner_without_its_ref_parks_as_hidden_and_publishes_nothing(world_v2):
    members = _v2_members(world_v2)
    owner, branch, pull = _open_owner_with_deleted_ref(world_v2, members)
    pulls, descriptions = set(world_v2.forge.pulls), dict(world_v2.forge.descriptions)

    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.context["step"] == "generation_hidden"
    assert refused.value.context["park"] is True
    assert refused.value.context["train_pr"] == owner
    assert pull["state"] == "open"
    assert set(world_v2.forge.pulls) == pulls
    assert world_v2.forge.descriptions == descriptions
    assert not world_v2.origin_has(branch)


def _vanish_before_the_stem_is_listed(world: TrainWorld, monkeypatch, built, *, open_pr: bool):
    """Delete the owner ref after the guard's reads and just before `_train_generation_refs`
    lists the stem: what an asynchronous forge deletion or another writer does in that gap.

    A PR that is still open then reports its head as `refs/pull/<n>/head`. Returns what the
    guard had looked up when the ref went (one entry: the stem is listed once)."""
    branch, owner = str(built["train_branch"]), int(built["train_pr"])
    listing = world.gate._train_generation_refs
    lookups: list[list[str]] = []

    def vanishing(context, stem):
        if not lookups:
            lookups.append(list(world.forge.head_lookups))
            git(["update-ref", "-d", f"refs/heads/{branch}"], world.origin)
            if open_pr:
                world.forge.pulls[owner].update(
                    head_sha=str(built["candidate_sha"]), head=f"refs/pull/{owner}/head",
                    native_head_ref=f"refs/pull/{owner}/head", native_label=branch)
        return listing(context, stem)

    monkeypatch.setattr(world.gate, "_train_generation_refs", vanishing)
    return lookups


@pytest.mark.parametrize("remnant", ["open-pr", "ref-without-pr"])
def test_v2_an_owner_ref_that_vanishes_after_the_guard_parks_and_publishes_nothing(
        world_v2, monkeypatch, remnant):
    """The guard read the own name as used, and its ref was deleted before the stem was listed.
    The absent-ref creation lease admits only a name that was never used, so the build parks
    (`generation_vanished`) instead of pushing and opening a second owner under the same
    name, and it retires nothing on the way to the park.

    This proves one pass. An owner PR still records the name for the next pass
    (`generation_hidden`); a ref that no PR records leaves nothing behind once it is gone."""
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    branch, owner = str(built["train_branch"]), int(built["train_pr"])
    if remnant == "ref-without-pr":
        # A crash before the owner PR existed: the ref stays and no PR records the name.
        world.forge.pulls.pop(owner)
    # An earlier crash's random-generation owner: retirable, so a build that retired before it
    # parked would delete it.
    retirable = f"{branch.split('-g')[0]}-g{'0' * 32}"
    git(["push", "-q", "origin", f"{built['candidate_sha']}:refs/heads/{retirable}"], world.checkout)
    pulls, descriptions, heads = set(world.forge.pulls), dict(world.forge.descriptions), _train_heads(world)
    world.forge.head_lookups.clear()
    lookups = _vanish_before_the_stem_is_listed(world, monkeypatch, built,
                                                open_pr=remnant == "open-pr")

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["step"] == "generation_vanished"
    assert context["park"] is True
    assert context["branch"] == branch
    assert context.get("serial_only") is not True
    # The guard had read every variant name when the ref went.
    assert lookups == [_variant_names(world, members)]
    # Nothing was created, retired or deleted: the own ref is still absent, the retirable
    # generation stands, and no PR or description was added.
    assert not world.origin_has(branch)
    assert world.origin_has(retirable)
    assert _train_heads(world) == {ref: tip for ref, tip in heads.items()
                                   if ref != f"refs/heads/{branch}"}
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions
    if remnant == "open-pr":
        assert world.forge.pulls[owner]["state"] == "open"


def _retirable_sibling(world: TrainWorld, built) -> str:
    """An earlier crash's random-generation owner under the same stem: a build retires it."""
    branch = str(built["train_branch"])
    sibling = f"{branch.split('-g')[0]}-g{'0' * 32}"
    git(["push", "-q", "origin", f"{built['candidate_sha']}:refs/heads/{sibling}"], world.checkout)
    return sibling


def _after_the_guard(world: TrainWorld, monkeypatch, act: Callable[[], object]) -> None:
    """Run *act* once, after the guard's reads and before the stem is listed."""
    listing = world.gate._train_generation_refs
    fired: list[bool] = []

    def listed(context, stem):
        if not fired:
            fired.append(True)
            act()
        return listing(context, stem)

    monkeypatch.setattr(world.gate, "_train_generation_refs", listed)


@pytest.mark.parametrize("change", ["closed", "read-back-fails"])
def test_v2_an_own_owner_that_changed_after_the_guard_parks_before_any_write(
        world_v2, monkeypatch, change):
    """The guard proved the batch's own open owner. Before adoption the owner PR is closed
    with its branch left in place, or the exact-name read-back fails. An open-only listing
    would find no PR on the name, but the name has one: adoption re-reads it by exact name
    before anything is retired, finds no match for the guard's answer, and parks. No second
    PR is opened, and the retirable generation under the stem is left as it was."""
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    owner = int(built["train_pr"])
    sibling = _retirable_sibling(world, built)
    lookup = world.forge.list_pulls_by_head
    failing: list[bool] = []

    def read_back(runner, cwd, forge_repo, head):
        if failing:
            return _done("[]", f"{_OK}X-Total-Count: 0\n", 1)
        return lookup(runner, cwd, forge_repo, head)

    def act():
        if change == "closed":
            world.forge.pulls[owner]["state"] = "closed"
        else:
            failing.append(True)

    monkeypatch.setattr(world.forge, "list_pulls_by_head", read_back)
    _after_the_guard(world, monkeypatch, act)
    pulls, descriptions, heads = set(world.forge.pulls), dict(world.forge.descriptions), _train_heads(world)
    comments = list(world.forge.comments)

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["park"] is True
    assert context.get("serial_only") is not True
    if change == "closed":
        assert context["step"] == "owner_proof_failed"
        assert context["train_pr"] == owner
    else:
        assert context["step"] == "owner_lookup_unavailable"
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions
    assert world.forge.comments == comments
    assert _train_heads(world) == heads
    assert world.origin_has(sibling)


@pytest.mark.parametrize("failure", ["exit-code", "timeout"])
def test_v2_a_stem_listing_that_fails_after_the_guard_parks(world_v2, monkeypatch, failure):
    """The guard proved the own open owner, and the stem listing that follows fails. The stem
    may hold that live owner, so the refusal parks instead of returning the members to serial
    CI beside it."""
    world = world_v2
    members = _v2_members(world)
    world.build(members)
    runner = world.gate._runner

    def listing(argv, cwd):
        if argv[1:4] == ["ls-remote", "--heads", "origin"] and argv[-1].endswith("-g*"):
            if failure == "timeout":
                contracts.fail("COMMAND_TIMEOUT")
            return _done("", "fatal: unreachable", 1)
        return runner(argv, cwd)

    monkeypatch.setattr(world.gate, "_runner", listing)
    pulls, heads = set(world.forge.pulls), _train_heads(world)

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["park"] is True
    assert context.get("serial_only") is not True
    if failure == "timeout":
        assert (context["step"], context["cause"]) == ("reconcile_failed", "COMMAND_TIMEOUT")
    else:
        assert (context["step"], context["cause"]) == ("list_refs", "TRAIN_BUILD_FAILED")
    assert set(world.forge.pulls) == pulls
    assert _train_heads(world) == heads


def test_v2_an_own_owner_whose_title_changed_after_the_guard_parks(world_v2, monkeypatch):
    """The guard proved the own open owner; its title is edited before the read-back. The
    owner is still open, so the refusal parks rather than letting serial CI start beside it."""
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    owner = int(built["train_pr"])

    def retitle():
        world.forge.pulls[owner]["title"] = f"{train_operations.TRAIN_TITLE_PREFIX} edited"

    _after_the_guard(world, monkeypatch, retitle)
    pulls, heads = set(world.forge.pulls), _train_heads(world)

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert (context["step"], context["cause"]) == ("owner_proof_failed", "TRAIN_INVALID")
    assert context["park"] is True
    assert context["train_pr"] == owner
    assert set(world.forge.pulls) == pulls
    assert _train_heads(world) == heads


def _same_chain_new_tip(world: TrainWorld, tip: str) -> str:
    """The commit *tip* would be had the gate linked it at another time: the same tree,
    parents, message and train identity, so its chain proves the same batch."""
    parents = git(["log", "-1", "--format=%P", tip], world.origin).stdout.split()
    message = git(["log", "-1", "--format=%B", tip], world.origin).stdout
    tree = git(["rev-parse", f"{tip}^{{tree}}"], world.origin).stdout.strip()
    ident = {"GIT_AUTHOR_NAME": "merge-train", "GIT_AUTHOR_EMAIL": "merge-train@example.invalid",
             "GIT_COMMITTER_NAME": "merge-train",
             "GIT_COMMITTER_EMAIL": "merge-train@example.invalid",
             "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"}
    made = subprocess.run(
        ["git", "commit-tree", tree, *(arg for parent in parents for arg in ("-p", parent))],
        cwd=world.origin, input=message, text=True, capture_output=True, check=True,
        env={**os.environ, **ident})
    return made.stdout.strip()


def test_v2_an_own_ref_swapped_after_the_guard_is_not_adopted(world_v2, monkeypatch):
    """The guard proved the own open owner at its candidate. An outside writer then replaces
    the own ref with another commit that carries the same chain. The PR number and state are
    unchanged, so only the tip shows the change: the stem listing no longer matches the tip the
    guard proved, and the build parks before it adopts, retires or publishes."""
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    branch, tip = str(built["train_branch"]), str(built["candidate_sha"])
    sibling = _retirable_sibling(world, built)
    swapped = _same_chain_new_tip(world, tip)
    assert swapped != tip
    _after_the_guard(world, monkeypatch, lambda: git(
        ["update-ref", f"refs/heads/{branch}", swapped], world.origin))
    pulls, descriptions = set(world.forge.pulls), dict(world.forge.descriptions)
    heads = {**_train_heads(world), f"refs/heads/{branch}": swapped}

    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["step"] == "generation_changed"
    assert refused.value.context["park"] is True
    assert refused.value.context["branch"] == branch
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions
    assert _train_heads(world) == heads
    assert world.origin_has(sibling)


@pytest.mark.parametrize("move", ["after-the-listing", "before-the-lease"])
def test_v2_a_retirement_whose_ref_moves_during_the_cleanup_parks(world_v2, monkeypatch, move):
    """The cleanup listed a retirable generation at the classified tip, and an outside writer
    moves it after that listing, or just before the leased deletion. The fetch check or the
    lease protects the new tip, and the stopped retirement parks the build
    (`generation_retire_failed`) instead of returning an ordinary refusal that the executor
    would answer with serial CI beside a possibly live generation."""
    world = world_v2
    members = _v2_members(world)
    built = world.build([members[0], members[2]])
    world.forge.pulls.pop(int(built["train_pr"]))
    git(["push", "-q", "origin", f":refs/heads/{built['train_branch']}"], world.checkout)
    sibling = _retirable_sibling(world, built)
    moved = world.forge.main()

    def move_sibling():
        git(["update-ref", f"refs/heads/{sibling}", moved], world.origin)

    if move == "after-the-listing":
        listing = world.gate._train_remote_refs

        def listed(context, family):
            refs = listing(context, family)
            if family == sibling:
                move_sibling()
            return refs

        monkeypatch.setattr(world.gate, "_train_remote_refs", listed)
    else:
        runner = world.gate._runner

        def leased(argv, cwd):
            if argv[-1] == f":refs/heads/{sibling}":
                move_sibling()
            return runner(argv, cwd)

        monkeypatch.setattr(world.gate, "_runner", leased)
    pulls, descriptions = set(world.forge.pulls), dict(world.forge.descriptions)

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["step"] == "generation_retire_failed"
    assert context["park"] is True
    assert context["branch"] == sibling
    assert context["cause"] == "TRAIN_CLOSE_FAILED"
    assert context.get("serial_only") is not True
    assert rev_parse(world.origin, f"refs/heads/{sibling}") == moved
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions


def test_v2_a_fork_pr_cannot_anchor_the_deletion_of_a_tombstone(world_v2):
    """A deterministic owner ref without a PR is a tombstone. A PR from a fork that targets
    main with the same head branch name and a train title is no record of that name: the close
    it anchors refuses before it deletes anything, and the tombstone remains."""
    world = world_v2
    _members, tombstone = _stem_with_a_tombstone(world)
    tip = rev_parse(world.origin, f"refs/heads/{tombstone}")
    fork = 30001
    world.forge.pulls[fork] = {
        "state": "open", "title": f"{train_operations.TRAIN_TITLE_PREFIX} #11, #13",
        "head": tombstone, "head_sha": tip, "head_repo": "someone/fork", "merged": False,
        "mergeable": True, "labels": []}

    with pytest.raises(GateError) as refused:
        world.execute(contracts.TRAIN_CLOSE, train_pr=fork, reason="TRAIN_ORPHANED")

    assert refused.value.code == "TRAIN_INVALID"
    assert refused.value.context["step"] == "generation_tombstone"
    assert rev_parse(world.origin, f"refs/heads/{tombstone}") == tip
    assert world.forge.pulls[fork]["state"] == "open"


@pytest.mark.parametrize("hidden", ["moved", "missing"])
def test_v2_an_open_own_owner_whose_hidden_head_disagrees_parks_without_adoption(
        world_v2, hidden):
    """An open own owner's live branch and its API head agree, but `refs/pull/<n>/head` is
    missing or names another commit: the hidden head was never proven, so the build parks
    before it adopts, retires or publishes anything."""
    world = world_v2
    members = _v2_members(world)
    built = world.build(members)
    owner = int(built["train_pr"])
    if hidden == "moved":
        git(["update-ref", f"refs/pull/{owner}/head", world.forge.main()], world.origin)
    else:
        git(["update-ref", "-d", f"refs/pull/{owner}/head"], world.origin)
    pulls, descriptions, heads = set(world.forge.pulls), dict(world.forge.descriptions), _train_heads(world)

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["step"] == "owner_proof_failed"
    assert context["park"] is True
    assert context["train_pr"] == owner
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions
    assert _train_heads(world) == heads
    assert world.forge.pulls[owner]["state"] == "open"


@pytest.mark.parametrize("identity", ["", "no-owner-component", "a/b/c"])
def test_v2_a_malformed_head_repository_on_a_retired_owner_parks(world_v2, monkeypatch, identity):
    """A closed owner whose branch was deleted is found only by its exact-name row. A row whose
    `head.repo.full_name` is not a repository identity is malformed evidence, not a foreign
    PR: the build parks instead of recreating the retired name."""
    world = world_v2
    members, old = _retired_v2(world, monkeypatch)
    assert not world.origin_has(str(old["train_branch"]))
    lookup = world.forge.list_pulls_by_head

    def damaged(runner, cwd, forge_repo, head):
        answer = lookup(runner, cwd, forge_repo, head)
        rows = json.loads(answer.stdout)
        for row in rows:
            row["head"]["repo"]["full_name"] = identity
        return _by_head(rows)

    monkeypatch.setattr(world.forge, "list_pulls_by_head", damaged)
    pulls, heads = set(world.forge.pulls), _train_heads(world)

    with pytest.raises(GateError) as refused:
        world.build(members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["step"] == "owner_lookup_malformed"
    assert refused.value.context["park"] is True
    assert set(world.forge.pulls) == pulls
    assert _train_heads(world) == heads


def test_v2_a_random_generation_owner_under_the_stem_is_retired_through_the_cleanup(
        world_v2, monkeypatch):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    # The owner of an earlier crash: the same stem and chain under a random suffix.
    random_owner = f"{str(first['train_branch']).split('-g')[0]}-g{'c' * 32}"
    git(["push", "-q", "origin", f"{first['candidate_sha']}:refs/heads/{random_owner}"],
        world_v2.checkout)
    context = world_v2.gate.managed_checkout_context(ALIAS)
    world_v2.gate._train_cleanup_family(context, str(first["train_branch"]), "fixture")
    cleaned: list[tuple[str, str]] = []
    inner = world_v2.gate._train_cleanup_family

    def recording(context, family, reason, **kwargs):
        cleaned.append((family, reason))
        return inner(context, family, reason, **kwargs)

    monkeypatch.setattr(world_v2.gate, "_train_cleanup_family", recording)

    second = world_v2.build([members[0], members[2]])

    assert second["code"] == "TRAIN_BUILT"
    assert cleaned == [(random_owner, "TRAIN_BUILD_RECOVERY")]
    assert not world_v2.origin_has(random_owner)


def test_v2_a_tombstone_under_the_stem_neither_stops_the_build_nor_is_deleted(world_v2):
    members, tombstone = _stem_with_a_tombstone(world_v2)

    built = world_v2.build(members)

    assert built["code"] == "TRAIN_BUILT"
    assert built["train_branch"] != tombstone
    assert world_v2.origin_has(tombstone)
    # The tombstone's PR state was read by its exact name before anything was retired.
    assert tombstone in world_v2.forge.head_lookups


@pytest.mark.parametrize("reason", ["TRAIN_ORPHANED", "merged #11, #12, #13"])
def test_v2_closing_an_owner_never_deletes_a_tombstone_of_its_stem(world_v2, reason):
    members, tombstone = _stem_with_a_tombstone(world_v2)
    built = world_v2.build(members)

    closed = world_v2.execute(contracts.TRAIN_CLOSE, train_pr=built["train_pr"], reason=reason)

    assert closed["code"] == "TRAIN_CLOSED"
    assert closed["deleted_branches"] == [built["train_branch"]]
    assert world_v2.forge.pulls[int(built["train_pr"])]["state"] == "closed"
    assert world_v2.origin_has(tombstone)


@pytest.mark.parametrize("swap", ["before-the-cleanup", "during-the-read"])
def test_v2_a_retirement_is_bound_to_the_tip_that_was_classified(world_v2, monkeypatch, swap):
    """An outside writer replaces another generation's ref after the gate decided about it.

    A name that carries another batch's chain is random-generation and is retired. Once the
    writer force-pushes the name's own batch to it, the name is a tombstone, which no path may
    delete. Both rows end with that tombstone chain at the name. They differ in when the writer
    acted: after the gate read the other chain (the cleanup then lists the new tip), or only
    while the gate read it (the cleanup lists the tip that the gate listed first)."""
    world = world_v2
    members = _v2_members(world)
    other = world.build([members[0], members[1]])
    named = world.build([members[0], members[2]])  # retires the first owner
    world.forge.pulls.pop(int(named["train_pr"]))  # its ref has no PR: a tombstone
    name = str(named["train_branch"])
    tip_other, tip_named = str(other["candidate_sha"]), str(named["candidate_sha"])

    def point_at(tip: str) -> None:
        git(["update-ref", f"refs/heads/{name}", tip], world.origin)

    fired: list[str] = []
    if swap == "before-the-cleanup":
        point_at(tip_other)  # the gate classifies a random generation, then the writer acts
        cleanup = world.gate._train_cleanup_family

        def replaced_before(context, family, reason, **kwargs):
            if not fired:
                fired.append(family)
                point_at(tip_named)
            return cleanup(context, family, reason, **kwargs)

        monkeypatch.setattr(world.gate, "_train_cleanup_family", replaced_before)
    else:
        read = world.gate._train_generation_chain

        def replaced_while_read(context, branch, **kwargs):
            if branch != name or fired:
                return read(context, branch, **kwargs)
            fired.append(branch)
            point_at(tip_other)
            try:
                return read(context, branch, **kwargs)
            finally:
                point_at(tip_named)

        monkeypatch.setattr(world.gate, "_train_generation_chain", replaced_while_read)
    pulls, descriptions = set(world.forge.pulls), dict(world.forge.descriptions)

    with pytest.raises(GateError) as refused:
        world.build(members)

    context = refused.value.context
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert context["step"] == "generation_changed"
    assert context["park"] is True
    assert context["branch"] == name
    assert context.get("serial_only") is not True
    assert fired == [name]
    # Nothing was deleted or published: the name still carries its own chain, and there is no
    # new owner ref, PR or description.
    assert _train_heads(world) == {f"refs/heads/{name}": tip_named}
    assert set(world.forge.pulls) == pulls
    assert world.forge.descriptions == descriptions
    # The park is not a deadlock: the writer is done, so the next build skips the tombstone.
    again = world.build(members)
    assert again["code"] == "TRAIN_BUILT"
    assert world.origin_has(name)


@pytest.mark.parametrize("which", ["own", "other"])
def test_v2_an_unreadable_generation_parks_and_deletes_nothing(world_v2, which):
    members = _v2_members(world_v2)
    own = _variant_names(world_v2, members)[0]
    unreadable = own if which == "own" else f"{own.split('-g')[0]}-g{'d' * 32}"
    # The tip of main is no train chain, so the gate cannot validate this name.
    git(["push", "-q", "origin", f"{world_v2.forge.main()}:refs/heads/{unreadable}"],
        world_v2.author)
    pulls = set(world_v2.forge.pulls)

    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.context["step"] == "generation_unreadable"
    assert refused.value.context["park"] is True
    assert refused.value.context["branch"] == unreadable
    assert world_v2.origin_has(unreadable)
    assert set(world_v2.forge.pulls) == pulls
    assert world_v2.forge.descriptions == {}
    listed = git(["for-each-ref", "--format=%(refname:short)", "refs/heads/train"], world_v2.origin)
    assert listed.stdout.split() == [unreadable]


@pytest.mark.parametrize("unread", ["chain", "pr_state"])
def test_v2_nothing_is_retired_until_every_other_generation_has_been_read(
        world_v2, monkeypatch, unread):
    members = _v2_members(world_v2)
    sibling = world_v2.build(members)
    stem = str(sibling["train_branch"]).split("-g")[0]
    # An earlier crash's random-generation owner. It is retirable and sorts before every
    # other generation, so a loop that deleted as it went would delete it first.
    retirable = f"{stem}-g{'0' * 32}"
    git(["push", "-q", "origin", f"{sibling['candidate_sha']}:refs/heads/{retirable}"],
        world_v2.checkout)
    if unread == "chain":
        # The tip of main is no train chain, so this generation (it sorts last) cannot be read.
        unreadable = f"{stem}-g{'f' * 32}"
        git(["push", "-q", "origin", f"{world_v2.forge.main()}:refs/heads/{unreadable}"],
            world_v2.author)
        step = "generation_unreadable"
    else:
        original = world_v2.forge.list_pulls_by_head

        def refuse_the_sibling(runner, cwd, repository, head):
            if head == sibling["train_branch"]:
                return _done(code=1)
            return original(runner, cwd, repository, head)

        monkeypatch.setattr(world_v2.forge, "list_pulls_by_head", refuse_the_sibling)
        step = "owner_lookup_unavailable"
    pulls = set(world_v2.forge.pulls)

    with pytest.raises(GateError) as refused:
        world_v2.build([members[0], members[2]])

    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == step
    assert world_v2.origin_has(retirable)
    assert world_v2.origin_has(str(sibling["train_branch"]))
    assert world_v2.forge.pulls[int(sibling["train_pr"])]["state"] == "open"
    assert set(world_v2.forge.pulls) == pulls
    assert len(world_v2.forge.descriptions) == 1


def test_v2_a_prefix_ref_of_another_generation_stops_the_build_before_it_retires_anything(world_v2):
    members = _v2_members(world_v2)
    sibling = world_v2.build(members)
    owner = str(sibling["train_branch"])
    prefix = f"{owner}-prefix-1"
    # This version creates no prefix, so one under another generation is unproven inventory.
    git(["push", "-q", "origin", f"{sibling['candidate_sha']}:refs/heads/{prefix}"],
        world_v2.checkout)
    pulls = set(world_v2.forge.pulls)

    with pytest.raises(GateError) as refused:
        world_v2.build([members[0], members[2]])

    # After the guard an unproven inventory parks: the stem may hold a live owner.
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["park"] is True
    assert refused.value.context["step"] == "generation_inventory"
    assert refused.value.context["cause"] == "TRAIN_INVALID"
    assert refused.value.context["refs"] == [owner, prefix]
    assert world_v2.origin_has(owner)
    assert world_v2.origin_has(prefix)
    assert world_v2.forge.pulls[int(sibling["train_pr"])]["state"] == "open"
    assert set(world_v2.forge.pulls) == pulls
    assert len(world_v2.forge.descriptions) == 1


def test_v2_inspect_reports_full_mode_and_the_owner_run(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    context = world_v2.gate.managed_checkout_context(ALIAS)

    observed = world_v2.gate.inspect_train_by_index(context, int(train["train_pr"]))

    assert observed["mode"] == "full"
    assert [record["pr"] for record in observed["prefixes"]] == [train["train_pr"]] * 3
    assert observed["ci_state"] == "green"
    assert observed["main_on_chain"] is True


def test_v2_interim_main_is_visible_from_trailers(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)

    def interim() -> str | None:
        world_v2.tree("origin/main")  # fetch the pushed main into the checkout
        head = rev_parse(world_v2.checkout, "refs/remotes/origin/main")
        return world_v2.gate._interim_train_commit(str(world_v2.checkout), head)

    world_v2.merge(train, *members[0])
    assert interim() is not None
    world_v2.merge(train, *members[1])
    assert interim() is not None
    world_v2.merge(train, *members[2])
    assert interim() is None


def test_v2_malformed_train_trailers_do_not_clear_the_interim_block(world_v2):
    """A train-shaped commit whose trailer block cannot be read refuses the deploy."""
    world_v2.tree("origin/main")  # fetch main into the managed checkout
    main = rev_parse(world_v2.checkout, "refs/remotes/origin/main")
    tree = git(["rev-parse", f"{main}^{{tree}}"], world_v2.checkout).stdout.strip()
    candidate, base, final = "a" * 40, "b" * 40, "c" * 40

    def synthetic(message: str) -> str:
        return git(["commit-tree", tree, "-p", main, "-m", message],
                   world_v2.checkout).stdout.strip()

    unreadable = {
        "not-the-closing-paragraph":
            f"Merge pull request #9\n\nTrain-Candidate: {candidate}\n"
            f"Train-Base: {base}\nplain tail",
        "unknown-trailer": f"Train-Candidate: {candidate}\nTrain-Base: {base}\nTrain-Other: x",
        "duplicate-trailer":
            f"Train-Candidate: {candidate}\nTrain-Candidate: {candidate}\nTrain-Base: {base}",
        "value-not-a-sha": f"Train-Candidate: not-a-sha\nTrain-Base: {base}",
    }
    for name, body in unreadable.items():
        commit = synthetic(body)
        assert world_v2.gate._interim_train_commit(
            str(world_v2.checkout), commit) == commit, name

    incomplete = {
        "owner-without-final":
            f"Train-Candidate: {candidate}\nTrain-Base: {base}\nTrain-Owner: 1",
        "final-without-owner":
            f"Train-Candidate: {candidate}\nTrain-Base: {base}\nTrain-Final: {final}",
        "final-outside-the-block":
            f"Train-Final: {final}\n\nTrain-Candidate: {candidate}\nTrain-Base: {base}",
        "owner-outside-the-block":
            f"Train-Owner: 1\n\nTrain-Candidate: {candidate}\nTrain-Base: {base}",
        # non-ASCII digits pass `str.isdigit`; `int("²")` raises.
        **{f"owner-{name}": f"Train-Candidate: {candidate}\nTrain-Base: {base}\n"
                            f"Train-Final: {final}\nTrain-Owner: {owner}"
           for name, owner in (("superscript", "²"), ("arabic-indic", "٣"),
                               ("fullwidth", "１"), ("zero", "0"))},
    }
    for name, body in incomplete.items():
        commit = synthetic(body)
        assert world_v2.gate._interim_train_commit(
            str(world_v2.checkout), commit) == commit, name

    # Controls: a legacy Candidate/Base pair is a tested prefix, a readable
    # interim block refuses, and a complete final block clears.
    legacy = synthetic(f"Train-Candidate: {candidate}\nTrain-Base: {base}")
    assert world_v2.gate._interim_train_commit(str(world_v2.checkout), legacy) is None
    interim = synthetic(f"Train-Candidate: {candidate}\nTrain-Base: {base}\n"
                        f"Train-Final: {final}\nTrain-Owner: 1")
    assert world_v2.gate._interim_train_commit(str(world_v2.checkout), interim) == interim
    # A final marker clears only when the landed tree really is the named
    # candidate's; use a real commit (main) whose tree this commit carries.
    settled = synthetic(f"Train-Candidate: {main}\nTrain-Base: {main}\n"
                        f"Train-Final: {main}\nTrain-Owner: 1")
    assert world_v2.gate._interim_train_commit(str(world_v2.checkout), settled) is None



@pytest.mark.parametrize("owner,readable", [
    pytest.param("7", True, id="ascii"),
    pytest.param("123456789", True, id="nine-digits"),
    pytest.param("\u00b2", False, id="superscript"),
    pytest.param("\u0663", False, id="arabic-indic"),
    pytest.param("\uff11", False, id="fullwidth"),
    pytest.param("1\u0663", False, id="ascii-then-arabic-indic"),
    pytest.param("1\uff11", False, id="ascii-then-fullwidth"),
    pytest.param("0", False, id="zero"),
    pytest.param("07", False, id="leading-zero"),
    pytest.param("1234567890", False, id="ten-digits"),
])
def test_v2_train_owner_trailer_is_ascii_digits_only(owner, readable):
    """the parser itself refuses a non-ASCII or padded owner, never raising."""
    sha = "a" * 40
    body = (f"Merge train\n\nTrain-Candidate: {sha}\nTrain-Base: {sha}\n"
            f"Train-Final: {sha}\nTrain-Owner: {owner}\n")

    trailers = deployment_operations._train_trailers(body)

    assert (trailers is not None) is readable
    if readable:
        assert trailers["Train-Owner"] == owner

def test_v2_deploy_refuses_interim_main(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.merge(train, *members[0])

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"


def test_v2_unreadable_history_search_refuses_the_deploy(world_v2, monkeypatch):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.merge(train, *members[0])
    world_v2.tree("origin/main")
    real = world_v2.gate._runner
    calls: list[list[str]] = []

    def failing(argv, cwd):
        calls.append(list(argv))
        if len(argv) > 1 and argv[1] == "log" and any(
                str(arg).startswith("--grep=^Train-") for arg in argv):
            return _done(code=1)
        return real(argv, cwd)

    monkeypatch.setattr(world_v2.gate, "_runner", failing)
    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert refused.value.context.get("reason") == "history_search_failed"
    assert not [argv for argv in calls if len(argv) > 1 and argv[1] == "merge"]


def test_v2_empty_history_search_does_not_block_the_deploy(world_v2):
    world_v2.tree("origin/main")
    head = rev_parse(world_v2.checkout, "refs/remotes/origin/main")

    assert world_v2.gate._interim_train_commit(str(world_v2.checkout), head) is None
    deployed = world_v2.gate.deploy(ALIAS)
    assert deployed["state"] in {"DEPLOY_SYNCED", "DEPLOY_CURRENT"}


def test_v2_changed_later_claim_blocks_the_first_merge(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.replace_claim(*members[1])

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "TRAIN_INVALID"
    assert refused.value.context.get("step") == "member_claim_changed"
    assert world_v2.forge.merges == []


def test_v2_changed_later_head_blocks_the_first_merge(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.move_member_head(*members[1], "moved\n")

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "EXPECTED_REFS_CHANGED"
    assert world_v2.forge.merges == []


def test_v2_changed_later_review_provenance_blocks_the_first_merge(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.replace_provenance(*members[1])

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "TRAIN_INVALID"
    assert refused.value.context.get("step") == "member_review_changed"
    assert world_v2.forge.merges == []


def test_v2_held_member_is_not_admitted(world_v2):
    members = _v2_members(world_v2)
    world_v2.forge.pulls[12]["labels"] = ["gate/hold"]

    train = world_v2.build(members)

    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "PR_QUEUE_HOLD"}]


def test_v2_hold_added_after_green_is_refused(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.forge.pulls[11]["labels"] = ["gate/hold"]

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "TRAIN_INVALID"
    assert refused.value.context.get("step") == "member_held"
    assert world_v2.forge.merges == []


def test_v2_next_member_hold_blocks_the_first_merge(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.forge.pulls[12]["labels"] = ["gate/hold"]

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "TRAIN_INVALID"
    assert refused.value.context.get("step") == "member_held"
    assert world_v2.forge.merges == []


def test_v2_unreadable_label_state_is_not_admitted(world_v2):
    members = _v2_members(world_v2)
    world_v2.forge.pulls[12]["labels"] = [{"name": ""}]

    train = world_v2.build(members)

    # The existing fail-closed label reader refuses before admission.
    assert [member["pr"] for member in train["members"]] == [11, 13]
    assert train["dropped"] == [{"pr": 12, "reason": "PR_HARNESS_LABEL_READ_FAILED"}]


def test_v2_changed_member_claim_cannot_be_adopted(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    world_v2.replace_claim(*members[1])
    pulls = set(world_v2.forge.pulls)

    # The same heads on the same base name the same owner, whose frozen chain
    # carries the old claim. That name is spent: the gate returns the set to
    # serial, never retires the owner and never creates its name again. Nothing
    # here is terminal-red CI proof for another owner.
    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context["step"] == "generation_spent"
    assert refused.value.context["serial_only"] is True
    assert refused.value.context.get("park") is not True
    assert refused.value.context["branch"] == first["train_branch"]
    assert world_v2.forge.pulls[int(first["train_pr"])]["state"] == "open"
    assert world_v2.origin_has(str(first["train_branch"]))
    assert set(world_v2.forge.pulls) == pulls
    assert len(world_v2.forge.descriptions) == 1


def test_v2_closed_later_member_blocks_the_first_merge(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.forge.pulls[12]["state"] = "closed"

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "PR_SELECTION_MISMATCH"
    assert world_v2.forge.merges == []


def test_v2_draft_later_member_blocks_the_first_merge(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.forge.pulls[12]["title"] = "WIP: member 12"

    with pytest.raises(GateError) as refused:
        world_v2.merge(train, *members[0])

    assert refused.value.code == "PR_DRAFT"
    assert world_v2.forge.merges == []


def test_v2_incomplete_v2_proof_without_candidate_refuses_the_deploy(world_v2):
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Base: {'a' * 40}\n"
        f"Train-Final: {'c' * 40}\nTrain-Owner: 1")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def test_v2_malformed_candidate_in_v2_proof_refuses_the_deploy(world_v2):
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Candidate: not-a-sha\n"
        f"Train-Base: {'b' * 40}\nTrain-Final: {'c' * 40}\nTrain-Owner: 1")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def test_v2_legacy_pair_does_not_block_the_deploy(world_v2):
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Candidate: {'a' * 40}\nTrain-Base: {'b' * 40}")

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] in {"DEPLOY_SYNCED", "DEPLOY_CURRENT"}


def test_v2_earlier_final_marker_above_an_interim_merge_refuses_the_deploy(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.merge(train, *members[0])
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Final: {train['candidate_sha']}\n\n"
        f"Train-Candidate: {members[0][1]}\nTrain-Base: {train['base_sha']}")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def test_v2_copied_final_marker_above_an_interim_merge_refuses_the_deploy(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.merge(train, *members[0])
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Candidate: {train['candidate_sha']}\n"
        f"Train-Base: {train['base_sha']}\n"
        f"Train-Final: {train['candidate_sha']}\nTrain-Owner: {train['train_pr']}")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def test_v2_legacy_pair_above_an_interim_merge_refuses_the_deploy(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.merge(train, *members[0])
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Candidate: {train['candidate_sha']}\n"
        f"Train-Base: {train['base_sha']}")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def test_v2_nonexistent_legacy_locator_above_an_interim_refuses_the_deploy(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    world_v2.merge(train, *members[0])
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Candidate: {'a' * 40}\n"
        f"Train-Base: {train['base_sha']}")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def test_v2_unreadable_earlier_train_body_blocks_a_legacy_pair(world_v2):
    # No v2 interim: an unreadable train-shaped commit alone, then a legacy pair.
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Base: {'b' * 40}")
    world_v2.push_main_with_message(
        f"Merge branch 'main'\n\nTrain-Candidate: {'a' * 40}\n"
        f"Train-Base: {'b' * 40}")
    before = rev_parse(world_v2.checkout, "HEAD")

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world_v2.checkout, "HEAD") == before


def _marker(kind: str, candidate: str, base: str, owner: object = 1) -> str:
    """The closing trailer block of a legacy pair or of a v2 final marker."""
    block = f"Train-Candidate: {candidate}\nTrain-Base: {base}"
    if kind == "final":
        block += f"\nTrain-Final: {candidate}\nTrain-Owner: {owner}"
    return block


def _interim_main(world: TrainWorld) -> tuple[dict[str, object], str]:
    """Merge only member 1 of a green three-member batch.

    Returns the train and the interim main tip, whose tree is the first
    prefix candidate's, an untested prefix.
    """
    members = _v2_members(world)
    train = world.build(members)
    world.green(train)
    world.merge(train, *members[0])
    world.tree("origin/main")
    return train, rev_parse(world.checkout, "refs/remotes/origin/main")


def _repair_member(world: TrainWorld, interim: str, index: int = 14) -> tuple[int, str, str]:
    """A repair member cut from *interim* and the candidate that merges it there.

    Like a repair train's candidate, it descends from *interim* and carries
    exactly the tree the native merge of the member will land.
    """
    git(["fetch", "-q", "origin"], world.author)
    head = world.member(index, "repair.txt", "repair\n", from_ref=interim)
    tree = git(["merge-tree", "--write-tree", interim, head], world.author).stdout.split()[0]
    candidate = git(["commit-tree", tree, "-p", interim, "-p", head, "-m", "repair candidate"],
                    world.author).stdout.strip()
    git(["push", "-q", "origin", f"{candidate}:refs/heads/repair-candidate"], world.author)
    git(["fetch", "-q", "origin", "repair-candidate"], world.checkout)
    return index, head, candidate


def _refused_interim(world: TrainWorld) -> GateError:
    """The deploy refusal; the checkout stays where it was."""
    before = rev_parse(world.checkout, "HEAD")
    with pytest.raises(GateError) as refused:
        world.gate.deploy(ALIAS)
    assert refused.value.code == "TRAIN_INTERIM"
    assert rev_parse(world.checkout, "HEAD") == before
    return refused.value


@pytest.mark.parametrize("scan", ["failed", "empty", "malformed", "truncated"])
def test_v2_unreadable_history_scan_refuses_the_deploy(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch, scan: str) -> None:
    train, interim = _interim_main(world_v2)
    real = world_v2.gate._runner

    def unreadable(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if "--format=%H%x00%B%x00" not in argv:
            return real(argv, cwd)
        if scan == "failed":
            return _done(code=1)
        if scan == "empty":
            return _done()
        if scan == "malformed":
            return _done("not-a-sha\0Train-Base: unreadable\0")
        base = str(train["base_sha"])
        return _done(f"{base}\0{_marker('legacy', base, base)}\0")

    monkeypatch.setattr(world_v2.gate, "_runner", unreadable)

    refused = _refused_interim(world_v2)

    if scan == "failed":
        assert refused.context.get("reason") == "history_scan_failed"
    else:
        assert refused.context.get("interim_commit") == interim


@pytest.mark.parametrize("kind", ["legacy", "final"])
@pytest.mark.parametrize("named", ["first-prefix", "interim"])
def test_v2_marker_naming_an_untested_batch_tree_refuses_the_deploy(world_v2, kind, named):
    # The first prefix candidate and the interim commit both carry the interim
    # tree, so a tree and base proof alone would clear the block. The named
    # candidate must cover the interim instead.
    train, interim = _interim_main(world_v2)
    candidate = train["members"][0]["candidate"] if named == "first-prefix" else interim
    world_v2.push_main_with_message(
        "Merge branch 'main'\n\n"
        + _marker(kind, candidate, train["base_sha"], train["train_pr"]))

    refused = _refused_interim(world_v2)

    assert refused.context.get("interim_commit") == interim
    # Moving a rejected marker into history must not make it authoritative.
    for next_kind in ("legacy", "final", "legacy"):
        world_v2.push_main_with_message(
            "Merge branch 'main'\n\n"
            + _marker(next_kind, candidate, train["base_sha"], train["train_pr"]))
        refused = _refused_interim(world_v2)
        assert refused.context.get("interim_commit") == interim


@pytest.mark.parametrize("kind", ["legacy", "final"])
def test_v2_marker_naming_a_repair_candidate_clears_the_interim_block(world_v2, kind):
    train, interim = _interim_main(world_v2)
    pr, head, candidate = _repair_member(world_v2, interim)
    world_v2.forge.merge_pr(None, None, REPOSITORY, pr, head,
                            merge_message=_marker(kind, candidate, interim, train["train_pr"]))

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] in {"DEPLOY_SYNCED", "DEPLOY_CURRENT"}


@pytest.mark.parametrize("cut_from", ["marker", "interim"])
@pytest.mark.parametrize("named", ["final", "first-prefix"])
def test_v2_serial_repair_after_a_copied_marker_clears_the_interim_block(
        world_v2, named, cut_from):
    # The copied marker refuses on its own, and a later merge whose branch
    # contains the interim and lands exactly its own tree still repairs it.
    train, interim = _interim_main(world_v2)
    candidate = train["candidate_sha"] if named == "final" else train["members"][0]["candidate"]
    for kind in ("final", "legacy", "final"):
        marker = world_v2.push_main_with_message(
            "Merge branch 'main'\n\n"
            + _marker(kind, candidate, train["base_sha"], train["train_pr"]))
        refused = _refused_interim(world_v2)
        assert refused.context.get("interim_commit") == interim
    git(["fetch", "-q", "origin"], world_v2.author)
    head = world_v2.member(14, "repair.txt", "repair\n",
                           from_ref=marker if cut_from == "marker" else interim)
    world_v2.forge.merge_pr(None, None, REPOSITORY, 14, head)

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] in {"DEPLOY_SYNCED", "DEPLOY_CURRENT"}


def test_v2_genuine_final_marker_clears_the_block(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    world_v2.green(train)
    for member in members:
        world_v2.merge(train, *member)
    world_v2.tree("origin/main")
    head = rev_parse(world_v2.checkout, "refs/remotes/origin/main")

    assert world_v2.gate._interim_train_commit(str(world_v2.checkout), head) is None
    deployed = world_v2.gate.deploy(ALIAS)
    assert deployed["state"] in {"DEPLOY_SYNCED", "DEPLOY_CURRENT"}


def _completed_train(world: TrainWorld) -> dict[str, object]:
    """Build a green three-member v2 batch and merge every member."""
    members = _v2_members(world)
    train = world.build(members)
    world.green(train)
    for member in members:
        world.merge(train, *member)
    return train


def _candidate_held(checkout: Path, train: dict[str, object]) -> bool:
    """Whether *checkout* holds the train's final candidate commit."""
    return git(["cat-file", "-e", f"{train['candidate_sha']}^{{commit}}"], checkout,
               check=False).returncode == 0


def _landed_on_a_deploy_host(
        world: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], Path]:
    """A merged v2 train and a checkout that never fetched its candidate.

    The checkout is cloned before the train exists and a deploy fetches only
    main, as on a host that did not run the train. The train branch is then
    deleted, as the forge does after the merge, so the final candidate
    survives only under ``refs/pull/<owner>/head``. The gate is pointed at the
    deploy checkout last: the train itself runs in the managed one.
    """
    deploy = world.checkout.parent / "deploy-host"
    git(["clone", "-q", str(world.origin), str(deploy)], world.checkout.parent)
    train = _completed_train(world)
    git(["push", "-q", "origin", f"{train['candidate_sha']}:refs/pull/{train['train_pr']}/head"],
        world.checkout)
    git(["push", "-q", "origin", "--delete", str(train["train_branch"])], world.checkout)
    # Main's history is all the deploy preamble brings; it must not carry the candidate.
    git(["fetch", "-q", "origin", "main:refs/remotes/origin/main"], deploy)
    assert not _candidate_held(deploy, train)
    monkeypatch.setattr(
        catalog, "CLOSED_REPOSITORIES",
        {REPOSITORY: replace(world.policy, managed_checkout=str(deploy))})
    return train, deploy


def _pull_ref_fetches(
        world: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record every gate git command that names a ``refs/pull/`` ref."""
    seen: list[tuple[str, ...]] = []
    inner = world.gate._runner

    def counting(argv: list[str], cwd: Path, *args: object,
                 **kwargs: object) -> subprocess.CompletedProcess[str]:
        if any(str(part).startswith("refs/pull/") for part in argv):
            seen.append(tuple(str(part) for part in argv))
        return inner(argv, cwd, *args, **kwargs)

    monkeypatch.setattr(world.gate, "_runner", counting)
    return seen


def test_v2_deploy_fetches_a_final_candidate_the_checkout_lacks(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """main never contains the train head and the forge deletes its branch."""
    train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    fetches = _pull_ref_fetches(world_v2, monkeypatch)

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] == "DEPLOY_SYNCED"
    assert rev_parse(deploy) == world_v2.forge.main()
    assert [argv[-1] for argv in fetches] == [f"refs/pull/{train['train_pr']}/head"]
    assert _candidate_held(deploy, train)


def test_v2_candidate_fetch_ignores_configured_ref_mappings(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote.origin.fetch mapping cannot turn the objects-only fetch into a ref update."""
    train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    main = rev_parse(deploy, "refs/remotes/origin/main")
    for mapping in ("+refs/pull/*/head:refs/remotes/origin/pr/*",
                    f"+refs/pull/{train['train_pr']}/head:refs/remotes/origin/main"):
        git(["config", "--add", "remote.origin.fetch", mapping], deploy)
    git(["fetch", "-q", "origin", "main:refs/remotes/origin/main"], deploy)
    assert rev_parse(deploy, "refs/remotes/origin/main") == main

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] == "DEPLOY_SYNCED"
    assert rev_parse(deploy) == main
    assert rev_parse(deploy, "refs/remotes/origin/main") == main
    assert git(["for-each-ref", "refs/remotes/origin/pr/"], deploy).stdout == ""


def test_v2_deploy_does_not_fetch_a_pull_ref_for_a_candidate_it_holds(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate host built the candidate, so proving its final marker costs no network."""
    train = _completed_train(world_v2)
    assert _candidate_held(world_v2.checkout, train)
    fetches = _pull_ref_fetches(world_v2, monkeypatch)

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] == "DEPLOY_SYNCED"
    assert fetches == []


def test_v2_deploy_refuses_when_the_owner_pull_ref_is_gone(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    git(["update-ref", "-d", f"refs/pull/{train['train_pr']}/head"], world_v2.origin)
    fetches = _pull_ref_fetches(world_v2, monkeypatch)
    before = rev_parse(deploy)

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert len(fetches) == 1
    assert rev_parse(deploy) == before
    assert not _candidate_held(deploy, train)


def test_v2_deploy_refuses_when_the_owner_pull_ref_lacks_the_candidate(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pull head that does not contain the named candidate cannot prove it, on every attempt."""
    train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    candidate = str(train["candidate_sha"])
    # Same tree, no parent: a different commit that does not bring the candidate along.
    unrelated = git(["commit-tree", f"{candidate}^{{tree}}", "-m", "unrelated pull head"],
                    world_v2.checkout).stdout.strip()
    git(["push", "-q", "origin", f"+{unrelated}:refs/pull/{train['train_pr']}/head"],
        world_v2.checkout)
    fetches = _pull_ref_fetches(world_v2, monkeypatch)
    before = rev_parse(deploy)

    for _attempt in range(2):
        with pytest.raises(GateError) as refused:
            world_v2.gate.deploy(ALIAS)
        assert refused.value.code == "TRAIN_INTERIM"
    assert len(fetches) == 2
    assert rev_parse(deploy) == before
    assert not _candidate_held(deploy, train)


def test_v2_deploy_proves_a_candidate_a_descendant_pull_head_brings(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetch only acquires objects: the named candidate's tree, not the ref, is the proof."""
    train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    candidate = str(train["candidate_sha"])
    moved = git(["commit-tree", f"{candidate}^{{tree}}", "-p", candidate, "-m", "pull head moved on"],
                world_v2.checkout).stdout.strip()
    git(["push", "-q", "origin", f"{moved}:refs/pull/{train['train_pr']}/head"], world_v2.checkout)

    deployed = world_v2.gate.deploy(ALIAS)

    assert deployed["state"] == "DEPLOY_SYNCED"
    assert rev_parse(deploy) == world_v2.forge.main()


def test_v2_deploy_reads_a_timed_out_pull_ref_fetch_as_an_unproven_marker(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung forge refuses like a failed fetch; the runner's timeout does not escape the proof."""
    _train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    real = world_v2.gate._runner
    attempts: list[tuple[str, ...]] = []

    def hung(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if any(str(part).startswith("refs/pull/") for part in argv):
            attempts.append(tuple(argv))
            contracts.fail_with("COMMAND_TIMEOUT", timeout_phase="git.fetch")
        return real(argv, cwd)

    monkeypatch.setattr(world_v2.gate, "_runner", hung)
    before = rev_parse(deploy)

    with pytest.raises(GateError) as refused:
        world_v2.gate.deploy(ALIAS)

    assert refused.value.code == "TRAIN_INTERIM"
    assert len(attempts) == 1
    assert rev_parse(deploy) == before


def test_v2_final_marker_proof_fetches_only_for_a_canonical_owner(
        world_v2: TrainWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner names a refspec, so only a positive decimal with no sign or padding may reach it."""
    train, deploy = _landed_on_a_deploy_host(world_v2, monkeypatch)
    fetches = _pull_ref_fetches(world_v2, monkeypatch)
    owner = str(train["train_pr"])
    landed = rev_parse(deploy, "refs/remotes/origin/main")

    def proven(trailer_owner: str | None) -> bool:
        return world_v2.gate._final_marker_proven(
            str(deploy), landed, str(train["candidate_sha"]), str(train["base_sha"]), trailer_owner)

    malformed = {"absent": None, "padded": f"0{owner}", "not-decimal": f"{owner}x",
                 "signed": f"-{owner}"}
    for name, trailer_owner in malformed.items():
        assert proven(trailer_owner) is False, name
    assert fetches == []
    assert proven(owner) is True
    assert len(fetches) == 1


def test_v2_changed_review_provenance_cannot_be_adopted(world_v2):
    members = _v2_members(world_v2)
    first = world_v2.build(members)
    world_v2.replace_provenance(*members[1])
    pulls = set(world_v2.forge.pulls)

    # A replacement review is not a CI verdict for the owner, and its frozen
    # chain names the old provenance: the same spent name as a changed claim.
    with pytest.raises(GateError) as refused:
        world_v2.build(members)

    assert refused.value.context["step"] == "generation_spent"
    assert refused.value.context["serial_only"] is True
    assert refused.value.context.get("park") is not True
    assert world_v2.forge.pulls[int(first["train_pr"])]["state"] == "open"
    assert world_v2.origin_has(str(first["train_branch"]))
    assert set(world_v2.forge.pulls) == pulls
    assert len(world_v2.forge.descriptions) == 1


def test_v2_prefix_inventory_is_refused_before_mutation(world_v2):
    members = _v2_members(world_v2)
    train = world_v2.build(members)
    prefix = f"{train['train_branch']}-prefix-1"
    git(["push", "-q", "origin", f"{train['candidate_sha']}:refs/heads/{prefix}"],
        world_v2.checkout)

    context = world_v2.gate.managed_checkout_context(ALIAS)

    with pytest.raises(GateError) as refused:
        world_v2.gate.inspect_train_by_index(context, int(train["train_pr"]))
    assert refused.value.code == "TRAIN_INVALID"
    assert refused.value.context.get("step") == "generation_inventory"

    with pytest.raises(GateError) as refused_merge:
        world_v2.merge(train, *members[0])
    assert refused_merge.value.code == "TRAIN_INVALID"
    assert refused_merge.value.context.get("step") == "generation_inventory"
    assert world_v2.forge.merges == []

    with pytest.raises(GateError) as refused:
        world_v2.build(members)
    assert refused.value.code == "TRAIN_BUILD_FAILED"
    assert refused.value.context.get("park") is True
    assert refused.value.context.get("step") == "generation_inventory"
    assert refused.value.context.get("cause") == "TRAIN_INVALID"
    assert world_v2.origin_has(train["train_branch"])
    assert world_v2.origin_has(prefix)
    assert world_v2.forge.pulls[int(train["train_pr"])]["state"] == "open"


def test_in_process_train_dispatch_records_hold_and_bounded_phases(world_v2, tmp_path, monkeypatch):
    log_root = tmp_path / "train-logs"
    monkeypatch.setenv("OPERATION_LOG_ROOT", str(log_root))
    members = _v2_members(world_v2)
    built = world_v2.build(members)
    events = [json.loads(line) for path in (log_root / "gate").glob("*.jsonl")
              for line in path.read_text().splitlines()]
    phases = {event["attributes"].get("phase") for event in events
              if event["event_name"] == "GATE_LOCK_PHASE"}
    assert built["code"] == "TRAIN_BUILT"
    assert phases == {"train-base", "train-members", "train-history", "train-reconcile", "train-publish"}
    released = next(event for event in events if event["event_name"] == "GATE_LOCK_RELEASED")
    assert released["attributes"]["operation"] == "train-build"
    assert released["attributes"]["lock_hold_ms"] >= 0
