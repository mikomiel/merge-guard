"""Tests for the `merge_open_prs` module, the merge command's implementation.

Pinned here are the four things that would ship broken without saying so.

**The Renovate hold-back, and why the author is not the signal.** Renovate may
run under the same account as ordinary changes, in which case every pull
request in the forge - Renovate's and the gate's alike - has one author. An author
filter would therefore match all of them or none, and "none" looks exactly like
a working filter on a day with no Renovate PRs open. The signals are the
`renovate/` head branch and the `renovate` label, *either alone*; both are
Renovate configuration (`branchPrefix`, `labels`) and either could be changed
without the other, so a test that only ever supplies both would not notice the
day one of them stopped arriving.

**Repository resolution against the real catalog file.** Which repositories
exist, where their checkouts are and which forge each is writable on are facts
that file owns, and getting the forge wrong means reaching for `gh` against a
Gitea repository — which fails loudly — or the reverse. Read against the real
file, not a fixture, because the failure worth catching is the catalog moving
a field, and a fixture would keep passing through exactly that.

**Collision ordering, and the empty-path-set trap.** Ordering by collision
degree is the whole reason this tool exists, and it degrades silently: if the
changed-path sets come back empty, every pull request reads degree 0, the batch
reports "all independent", and the colliding cluster merges first — the failure
mode the ordering exists to prevent, wearing the output of success. That is not
hypothetical. A naive implementation reads the head commit from
`FETCH_HEAD` after fetching the base branch *second*, which overwrites it; every
`git diff base...base` returns empty and all open PRs report
independent. `changed_paths` raises rather than returning an empty set it
cannot vouch for, and that guard is pinned below.

**The CI classification staying wired.** Both Renovate signals feed one
classification output in the checks workflow, and a Renovate head gets the
same affected-group narrowing as any other PR. Nothing else references that
output, so an edit that drops either signal changes the narrowing silently
and no test would otherwise notice.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — < 3.11 venv fallback
    import tomli as tomllib  # type: ignore[no-redef]

from gitfixtures import DETERMINISTIC_GIT_ENV, commit_file, git, make_origin
from merge_open_prs_fixtures import (
    QUEUE_STORE_TYPE,
    REPO_ROOT,
    FakeGateApplication,
    _core_repo,
    _in_progress,
    _queue_inspect,
    _queue_store,
    _train_repository,
    merge_open_prs,
)

CHECKS_WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "checks.yml"
PYPROJECT = REPO_ROOT / "platform" / "pyproject.toml"











def _pull(number: int, *, head="task/whatever", labels=(), draft=False, sha="a" * 40):
    return merge_open_prs.Pull(
        number=number,
        title=f"pull {number}",
        head_ref=head,
        head_sha=sha,
        base_ref="main",
        labels=tuple(labels),
        is_draft=draft,
    )


# ------------------------------------------------------------ forge resolution


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@forge.example.org:acme/core-repo.git", "acme/core-repo"),
        ("git@forge.example.org:acme/docs-repo.git",
         "acme/docs-repo"),
        ("git@forge.example.org:acme/shop-repo.git", "acme/shop-repo"),
        ("https://forge.example.org/acme/shop-repo.git", "acme/shop-repo"),
        ("https://github.com/example-org/core-repo", "example-org/core-repo"),
    ],
)
def test_forge_slug_handles_both_remote_spellings(remote, expected):
    assert merge_open_prs.forge_slug(remote) == expected


def test_repositories_come_from_the_real_catalog():
    """Both sections are read, and each repository's forge is what it declares.

    Asserted against the live file rather than a fixture: a fixture would keep
    passing on the day the catalog renames its forge or path field,
    which is the change this is here to catch.
    """
    records = merge_open_prs.load_repositories()
    assert records["core-repo"].forge == "forgejo"
    assert records["core-repo"].path == Path("/work/core-repo")
    # docs-repo is a managed repository; shop-repo below is a product
    # repository, so it never exercises the managed repositories' own
    # identity path.
    assert records["docs-repo"].forge == "forgejo"
    assert records["docs-repo"].path == Path("/work/docs-repo")
    # From the second catalog section, not the first -- both sections are read.
    # shop-repo is a product repository rather than a managed one, and has
    # no CI onboarding.
    assert records["shop-repo"].forge == "forgejo"
    assert records["shop-repo"].path == Path("/work/shop-repo")


def test_every_repository_has_a_forge_backend():
    """A catalog forge nothing can drive is a run that fails at the end."""
    for record in merge_open_prs.load_repositories().values():
        assert record.forge in merge_open_prs.FORGES


def test_bare_merge_takes_the_default_repositories_in_order():
    """`shop-repo` stays out: shipping the main product must not merge unrelated work.

    The order matches the other commands, which both advance
    core-repo before docs-repo.
    """
    selected = merge_open_prs.resolve_selection([], merge_open_prs.load_repositories())
    assert [record.name for record in selected] == ["core-repo", "docs-repo"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (["core"], ["core-repo"]),
        (["docs"], ["docs-repo"]),
        (["shop-repo"], ["shop-repo"]),
        (["docs", "core"], ["docs-repo", "core-repo"]),
        # An alias and its full name name one repository, not two merges of it.
        (["core", "core-repo"], ["core-repo"]),
    ],
)
def test_selection_resolves_aliases_and_keeps_the_requested_order(requested, expected):
    records = merge_open_prs.load_repositories()
    selected = merge_open_prs.resolve_selection(requested, records)
    assert [record.name for record in selected] == expected


def test_an_unknown_repository_fails_closed_and_names_the_alternatives():
    with pytest.raises(RuntimeError) as excinfo:
        merge_open_prs.resolve_selection(["nope"], merge_open_prs.load_repositories())
    message = str(excinfo.value)
    assert "nope" in message
    assert "core-repo" in message and "shop-repo" in message


# ------------------------------------------------------- merge postconditions


class _MergeForge:
    def __init__(self, events: list[str]) -> None:
        self.repository = SimpleNamespace(path=Path("/managed"))
        self.events = events

    def mergeable(self, _pull):
        return "MERGEABLE"

    def not_green_reason(self, _pull):
        return None

    def merge(self, _pull):
        self.events.append("merge")
        return 0, ""


def test_merge_success_carries_monotonic_main_receipt(monkeypatch):
    events: list[str] = []
    shas = iter(["b" * 40, "c" * 40])
    ancestry: list[tuple[str, str, str]] = []

    def fresh(_cwd):
        events.append("fetch")
        return next(shas), None

    monkeypatch.setattr(merge_open_prs, "_fresh_remote_main", fresh)
    monkeypatch.setattr(
        merge_open_prs,
        "_is_ancestor",
        lambda cwd, ancestor, descendant: (
            ancestry.append((cwd, ancestor, descendant)) or True
        ),
    )

    result = merge_open_prs.merge_one(_pull(7), _MergeForge(events), False)

    assert events == ["fetch", "merge", "fetch"]
    assert result == {
        "pr": 7,
        "outcome": "merged",
        "pre_merge_origin_main_sha": "b" * 40,
        "selected_head_sha": "a" * 40,
        "origin_main_sha": "c" * 40,
    }
    assert ancestry == [
        ("/managed", "b" * 40, "c" * 40),
        ("/managed", "a" * 40, "c" * 40),
    ]


def test_backend_success_fails_when_fresh_main_omits_pre_merge_main(monkeypatch):
    shas = iter(["b" * 40, "c" * 40])
    monkeypatch.setattr(
        merge_open_prs,
        "_fresh_remote_main",
        lambda _cwd: (next(shas), None),
    )
    monkeypatch.setattr(
        merge_open_prs,
        "_is_ancestor",
        lambda _cwd, ancestor, _descendant: ancestor != "b" * 40,
    )

    result = merge_open_prs.merge_one(_pull(7), _MergeForge([]), False)

    assert result["outcome"] == "failed"
    assert "pre-merge main" in result["reason"]
    assert result["pre_merge_origin_main_sha"] == "b" * 40
    assert result["origin_main_sha"] == "c" * 40


def test_backend_success_fails_when_fresh_main_omits_selected_head(monkeypatch):
    shas = iter(["b" * 40, "c" * 40])
    monkeypatch.setattr(
        merge_open_prs,
        "_fresh_remote_main",
        lambda _cwd: (next(shas), None),
    )
    monkeypatch.setattr(
        merge_open_prs,
        "_is_ancestor",
        lambda _cwd, ancestor, _descendant: ancestor != "a" * 40,
    )

    result = merge_open_prs.merge_one(_pull(7), _MergeForge([]), False)

    assert result["outcome"] == "failed"
    assert result["authority_integrity_failed"] is True
    assert "selected head" in result["reason"]
    assert result["selected_head_sha"] == "a" * 40


def test_backend_success_fails_when_post_merge_main_cannot_be_fetched(monkeypatch):
    replies = iter([("b" * 40, None), (None, "cannot fetch canonical main")])
    monkeypatch.setattr(
        merge_open_prs, "_fresh_remote_main", lambda _cwd: next(replies)
    )

    result = merge_open_prs.merge_one(_pull(7), _MergeForge([]), False)

    assert result == {
        "pr": 7,
        "outcome": "failed",
        "reason": "cannot fetch canonical main",
        "authority_integrity_failed": True,
        "pre_merge_origin_main_sha": "b" * 40,
        "selected_head_sha": "a" * 40,
    }


def test_a_head_that_moved_after_selection_is_never_merged():
    """`Gate.mergeable()` re-reads the forge's current head at merge time, not
    selection time. A PR whose head moved between the two -- a push landing
    after `collision_order` ran but before its turn in the batch -- must be
    left for the next pass rather than merged against the head the batch
    ordering was computed for. This is the `run_one`/`merge_one` path (not
    `--automerge`'s `_automerge_candidate`, which never calls `mergeable`).

    `merge_one`'s own `HEAD_CHANGED` branch (`~805-811`) reports this outcome
    as `"failed"` with a human-readable reason, not as `"retained"` with the
    bare state name -- `"retained"` is `_automerge_candidate`'s vocabulary, a
    different code path. The behaviour this test actually pins, and the one
    that matters here, is that `Gate.mergeable()` deciding `HEAD_CHANGED`
    ends the attempt before `forge.merge()` -- so the backend's `merge_pr`
    is never invoked against a head the batch no longer agrees on.
    """
    class _HeadMovedForge:
        def __init__(self) -> None:
            self.repository = SimpleNamespace(path=Path("/managed"))
            self.merge_calls = 0

        def mergeable(self, _pull):
            return "HEAD_CHANGED"

        def not_green_reason(self, _pull):
            return None

        def merge(self, _pull):
            self.merge_calls += 1
            return 0, ""

    forge = _HeadMovedForge()

    result = merge_open_prs.merge_one(_pull(7), forge, False)

    assert result == {
        "pr": 7,
        "outcome": "failed",
        "reason": "pull-request head changed after batch selection",
    }
    assert forge.merge_calls == 0


def test_batch_stops_after_authority_postcondition_failure(monkeypatch, tmp_path):
    pulls = [_pull(7), _pull(8, sha="d" * 40)]

    class BatchForge:
        repository = SimpleNamespace(path=tmp_path)

        def open_pulls(self):
            return pulls

    attempted: list[int] = []

    def merge_one(pull, _forge, _require_green):
        attempted.append(pull.number)
        return {
            "pr": pull.number,
            "outcome": "failed",
            "reason": "canonical main omits selected head",
            "authority_integrity_failed": True,
        }

    monkeypatch.setattr(merge_open_prs, "forge_for", lambda _repository: BatchForge())
    monkeypatch.setattr(merge_open_prs, "ensure_head_local", lambda _pull, _cwd: True)
    monkeypatch.setattr(merge_open_prs, "changed_paths", lambda pull, _cwd: {str(pull.number)})
    monkeypatch.setattr(merge_open_prs, "merge_one", merge_one)
    repository = merge_open_prs.Repository("test", tmp_path, "forgejo", "test/repo")
    args = SimpleNamespace(
        include_drafts=False,
        include_renovate=False,
        dry_run=False,
        json=True,
        require_green=False,
    )

    summary = merge_open_prs.run_one(repository, args)

    assert attempted == [7]
    assert summary["authority_integrity_failed"] is True


# -------------------------------------------------------------------- Renovate


@pytest.mark.parametrize(
    ("head", "labels", "expected"),
    [
        # Either signal alone is enough; neither is required to bring the other.
        ("renovate/example-stack", (), True),
        ("some/other-branch", ("renovate",), True),
        ("renovate/container-images", ("renovate", "major"), True),
        # A gate lane is never Renovate, and neither is a lookalike name.
        ("task/example-lane-0123456789ab-abcdef012345", (), False),
        ("task/renovate-docs", (), False),
        ("main", ("major",), False),
    ],
)
def test_renovate_is_identified_by_branch_or_label_but_never_by_author(
    head, labels, expected
):
    assert _pull(1, head=head, labels=labels).is_renovate is expected


def test_renovate_pulls_are_held_back_by_default_and_named():
    pulls = [
        _pull(6, head="renovate/container-images", labels=("renovate",)),
        _pull(8, head="task/interface-type"),
    ]
    batch, notes = merge_open_prs._held_back(
        pulls, include_drafts=False, include_renovate=False
    )
    assert [pull.number for pull in batch] == [8]
    assert len(notes) == 1
    # Named, not just counted: a bare count cannot be acted on.
    assert "#6" in notes[0] and "--include-renovate" in notes[0]


def test_include_renovate_merges_them():
    pulls = [_pull(6, head="renovate/example-stack", labels=("renovate",))]
    batch, notes = merge_open_prs._held_back(
        pulls, include_drafts=False, include_renovate=True
    )
    assert [pull.number for pull in batch] == [6]
    assert notes == []


def test_a_draft_renovate_pull_is_reported_once():
    """Otherwise the two hold-back notes sum to more than the batch."""
    pulls = [_pull(6, head="renovate/x", labels=("renovate",), draft=True)]
    batch, notes = merge_open_prs._held_back(
        pulls, include_drafts=False, include_renovate=False
    )
    assert batch == []
    assert len(notes) == 1 and "draft" in notes[0]


def test_holding_everything_back_leaves_an_empty_batch_not_a_failure():
    """An all-Renovate forge is "nothing to merge", which must still exit 0."""
    pulls = [_pull(n, head=f"renovate/{n}", labels=("renovate",)) for n in (6, 7)]
    batch, notes = merge_open_prs._held_back(
        pulls, include_drafts=False, include_renovate=False
    )
    assert batch == []
    assert notes and "#7" in notes[0]


# ------------------------------------------------------------------- ordering


def test_collision_order_puts_independent_pulls_first():
    a = _pull(1)
    b = _pull(2)
    c = _pull(3)
    paths = {
        1: {"shared.yml"},
        2: {"shared.yml"},
        3: {"alone.yml"},
    }
    ordered = merge_open_prs.collision_order([a, b, c], paths)
    assert [pull.number for pull, _ in ordered] == [3, 1, 2]
    assert [degree for _, degree in ordered] == [0, 1, 1]


def test_collision_order_is_reproducible_within_a_degree():
    pulls = [_pull(9), _pull(4), _pull(7)]
    paths = {9: {"a"}, 4: {"b"}, 7: {"c"}}
    ordered = merge_open_prs.collision_order(pulls, paths)
    assert [pull.number for pull, _ in ordered] == [4, 7, 9]


def test_changed_paths_refuses_a_pull_with_no_head_commit():
    """The guard on the empty-set trap described in this module's docstring.

    An empty changed-path set and "could not work out what this PR changes" are
    the same value but not the same fact, and only one of them may quietly
    order a batch.
    """
    with pytest.raises(RuntimeError):
        merge_open_prs.changed_paths(_pull(1, sha=""), "/nonexistent")


def _git_commit(repo, message):
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fetched_checkout(tmp_path):
    """A clone (configured `+refs/heads/*` refspec) of an origin whose main
    holds two commits and whose PR 7 head sits on top of the second."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    first = _git_commit(origin, "first")
    second = _git_commit(origin, "second")
    _git(origin, "checkout", "-q", "-b", "lane")
    head = _git_commit(origin, "pull request")
    _git(origin, "update-ref", "refs/pull/7/head", head)
    _git(origin, "checkout", "-q", "main")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", "--single-branch", "-b", "main",
                    str(origin), str(checkout)], check=True, capture_output=True)
    assert _git(checkout, "rev-parse", "origin/main") == second
    return origin, checkout, first, second, head


def test_ensure_head_local_never_moves_origin_main_onto_a_rewritten_main(fetched_checkout):
    """The gate recognises a rewritten main only while `origin/main` still holds
    what the checkout recorded; a manual run must not force it onto the rewrite."""
    origin, checkout, first, second, head = fetched_checkout
    _git(origin, "reset", "-q", "--hard", first)
    rewritten = _git_commit(origin, "rewritten second")
    pull = _pull(7, sha=head)

    with pytest.raises(RuntimeError, match="cannot fast-forward origin/main"):
        merge_open_prs.ensure_head_local(pull, str(checkout))

    assert _git(checkout, "rev-parse", "origin/main") == second
    # Discovery control: the configured refspec this checkout carries is the
    # forced one, so the old bare `git fetch origin main` accepted the rewrite.
    _git(checkout, "fetch", "-q", "origin", "main")
    assert _git(checkout, "rev-parse", "origin/main") == rewritten


def test_ensure_head_local_fast_forwards_the_base_and_fetches_the_head(fetched_checkout):
    origin, checkout, _first, _second, head = fetched_checkout
    advanced = _git_commit(origin, "third")
    pull = _pull(7, sha=head)

    assert merge_open_prs.ensure_head_local(pull, str(checkout)) is True

    assert _git(checkout, "rev-parse", "origin/main") == advanced
    _git(checkout, "cat-file", "-e", f"{head}^{{commit}}")


# ------------------------------------------------------------- CI exclusion


def test_checks_workflow_classifies_renovate_once_and_narrows_it_like_any_pr():
    """Both Renovate signals feed one trusted classification output.

    That output no longer branches the ordinary jobs --
    a Renovate head gets the same affected-group narrowing as any other PR.
    It still routes the additional `example-lane` lane.
    """
    workflow = yaml.safe_load(CHECKS_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    classifier = "\n".join(
        str(step.get("run", "")) + str(step.get("env", ""))
        for step in jobs["classify"]["steps"]
    )
    assert (
        f"startsWith(github.head_ref, '{merge_open_prs.RENOVATE_BRANCH_PREFIX}')"
        in classifier
    )
    assert (
        "contains(github.event.pull_request.labels.*.name, "
        f"'{merge_open_prs.RENOVATE_LABEL}')" in classifier
    )
    for name in (
        "leaf-01",
        "leaf-02",
        "leaf-03",
        "leaf-04",
        "leaf-05",
        "leaf-06",
        "leaf-07",
        "leaf-08",
        "leaf-13",
        "leaf-14",
        "leaf-15",
        "leaf-16",
        "leaf-17",
        "leaf-test-1",
        "leaf-test-2",
        "leaf-test-3",
        "leaf-18",
        "leaf-19",
        "leaf-20",
    ):
        job = jobs[name]
        condition = job["if"]
        assert "needs.classify.outputs.renovate" not in condition, name
        assert "\n" not in condition, name
    assert "needs.classify.outputs.renovate == 'true'" in jobs[
        "example-lane"
    ]["if"]


# ------------------------------------------------------------ type coverage


def test_ty_actually_checks_this_script():
    """`uv run ty check` must be evidence about this file, not merely near it.

    ty's `src.include` *is* the checked surface, so a module outside it is
    invisible: a script left out of the list is never read, and the suite
    reports `Success` regardless.
    A green run that structurally cannot see the code under review is worse
    than no run, because it is quoted as though it could.

    The tea engine backend is asserted alongside it for a second reason.
    This script imports it for its pinned binary path, and under mypy --
    which follows imports and reports outside its `files` list -- naming
    only this script would have let that unlisted module turn the config red
    from outside the declared surface. `src.include` is a hard boundary, so
    ty inverts the failure: an unnamed module is never reported at all, and
    the tea engine would silently return to being checked by nothing. Both
    names are load-bearing under either checker, for opposite reasons.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    include = config["tool"]["ty"]["src"]["include"]
    assert "scripts/merge_open_prs.py" in include
    assert "scripts/git_gate/gate_backend_tea.py" in include


# ------------------------------------------------------------------- preflight


def _git(repo, *argv):
    """Run git in *repo*, failing loudly rather than returning a bad answer."""
    done = subprocess.run(
        ["git", "-C", str(repo), *argv],
        capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def batch_repo(tmp_path):
    """A real repository whose two branches conflict only *in sequence*.

    Built rather than mocked because the whole claim under test is what `git`
    does, and a mock of git can only ever restate the expectation. `main` holds
    a three-line file; two branches each rewrite the same line differently, so
    both merge cleanly onto `main` today and the second cannot merge once the
    first has landed. That is precisely the case the forge calls MERGEABLE for
    both and `collision_order` can only guess at.

    A third branch touches a different file and must stay clean throughout, so
    a test that passed by declaring everything conflicted would fail here.
    """
    repo = tmp_path / "batch"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "shared.txt").write_text("top\nMIDDLE\nbottom\n")
    (repo / "other.txt").write_text("untouched\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    # `origin/main` is what the code resolves against, and a bare remote is not
    # needed to create it -- the simulation only ever reads.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    base = _git(repo, "rev-parse", "HEAD")

    heads = {}
    for name, path, body in (
        ("one", "shared.txt", "top\nONE\nbottom\n"),
        ("two", "shared.txt", "top\nTWO\nbottom\n"),
        ("solo", "other.txt", "changed\n"),
    ):
        _git(repo, "checkout", "-q", "-b", name, base)
        (repo / path).write_text(body)
        _git(repo, "commit", "-qam", name)
        heads[name] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return repo, heads


def test_preflight_catches_the_conflict_the_forge_cannot_see(batch_repo):
    """The module docstring's opening failure, now predicted instead of ordered around.

    Both branches merge cleanly onto `main`, so a forge asked today reports
    MERGEABLE for both -- and the second still fails the moment the first
    lands. The simulation says so before anything is attempted, and names the
    file.
    """
    repo, heads = batch_repo
    one = _pull(1, sha=heads["one"])
    two = _pull(2, sha=heads["two"])

    # The premise: each is genuinely clean against the base on its own.
    assert merge_open_prs.conflicting_paths(one, str(repo)) == []
    assert merge_open_prs.conflicting_paths(two, str(repo)) == []

    records = merge_open_prs.preflight_batch([(one, 1), (two, 1)], str(repo))

    assert [r["verdict"] for r in records] == ["clean", "conflict"]
    assert records[1]["conflicting_paths"] == ["shared.txt"]
    # Named, because "conflicts" without "with what" is the silence this tool exists to end.
    assert records[1]["after"] == [1]


def test_preflight_does_not_cry_conflict_over_a_merely_shared_file(batch_repo):
    """Shared path is not collision, and this is the over-report that proves it.

    `collision_order` would place both of these last as a colliding cluster.
    Only one of them actually collides.
    """
    repo, heads = batch_repo
    solo = _pull(3, sha=heads["solo"])
    one = _pull(1, sha=heads["one"])

    records = merge_open_prs.preflight_batch([(solo, 0), (one, 1)], str(repo))

    assert [r["verdict"] for r in records] == ["clean", "clean"]


def test_a_conflicting_pull_does_not_advance_the_projected_base(batch_repo):
    """A PR that cannot merge does not land, so it must not shadow the ones behind it.

    Were the conflicting head folded into the base anyway, `solo` would be
    judged against a tree that will never exist -- and the operator would be
    warned off a PR that merges fine.
    """
    repo, heads = batch_repo
    records = merge_open_prs.preflight_batch(
        [
            (_pull(1, sha=heads["one"]), 1),
            (_pull(2, sha=heads["two"]), 1),
            (_pull(3, sha=heads["solo"]), 0),
        ],
        str(repo),
    )

    assert [r["verdict"] for r in records] == ["clean", "conflict", "clean"]
    # #2 conflicted, so only #1 is behind #3.
    assert records[2]["after"] == [1]


def test_an_unmergeable_input_is_unproven_rather_than_clean(batch_repo):
    """`error` and `clean` are different answers and only one may be reported as safe.

    `conflicting_paths` collapses them -- any exit code other than 1 becomes
    "no conflict found locally" -- which is how a check stops being able to
    fail. The preflight keeps them apart.
    """
    repo, _ = batch_repo
    ghost = _pull(4, sha="b" * 40)  # a well-formed SHA naming no commit

    records = merge_open_prs.preflight_batch([(ghost, 0)], str(repo))

    assert records[0]["verdict"] == "error"
    assert "merge-tree" in records[0]["detail"]
    # The pre-existing helper's blind spot, pinned so the difference is deliberate.
    assert merge_open_prs.conflicting_paths(ghost, str(repo)) == []


def test_the_simulation_creates_no_refs_and_leaves_the_tree_clean(batch_repo):
    """Safe to run against a working checkout while other work is in flight.

    The objects it writes are unreferenced and `git gc` reaps them; what would
    hurt is a stray ref or a dirtied working tree, so both are asserted.
    """
    repo, heads = batch_repo
    before = _git(repo, "show-ref")

    merge_open_prs.preflight_batch(
        [(_pull(1, sha=heads["one"]), 1), (_pull(2, sha=heads["two"]), 1)],
        str(repo),
    )

    assert _git(repo, "show-ref") == before
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")


# -------------------------------------------------------------- automerge




def _stale_base(conclusion="success", **overrides):
    """An inspect answer for a PR whose trusted run judged an older base.

    `ci_state` is `pending`, the shape the live gate reports for it."""
    return {"ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "ci_state": "pending", "review_state": "approved",
            "blocking_reason": "CI_BASE_STALE", "ci_reason": "CI_BASE_STALE",
            "next_action": "refresh_task",
            "ci_run_conclusion": conclusion, **overrides}


class _ApprovedAfterReview(FakeGateApplication):
    """The gate reads a PR as approved once a review of it has run.

    Selection reads a PR before its turn, so a scripted list of reads
    would be spent by the ranking; this models the review's effect instead."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reviewed = set()

    def review(self, repository, pr, *args, **kwargs):
        self.reviewed.add((repository, pr))
        return super().review(repository, pr, *args, **kwargs)

    def inspect(self, repository, pr):
        read = super().inspect(repository, pr)
        if (repository, pr) in self.reviewed and read.get("ok"):
            return {**read, "review_state": "approved"}
        return read






def test_candidate_inspect_exception_keeps_other_pr_and_repository_progress(tmp_path):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]

    class BrokenInspect(FakeGateApplication):
        def inspect(self, repository, pr):
            if (repository, pr) == ("core-repo", 1):
                raise KeyError("review read failed")
            return super().inspect(repository, pr)

    gate = BrokenInspect({"core-repo": [1, 2], "docs-repo": [3]})
    summaries = merge_open_prs.run_automerge_pass(repositories, gate)
    assert summaries[0]["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CANDIDATE_ERROR:KeyError"},
        {"pr": 2, "outcome": "merged"},
    ]
    assert summaries[1]["results"] == [{"pr": 3, "outcome": "merged"}]


def test_listing_exception_is_repository_local(tmp_path):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]

    class BrokenListing(FakeGateApplication):
        def candidates(self, name):
            if name == "core-repo":
                raise KeyError("listing")
            return super().candidates(name)

    summaries = merge_open_prs.run_automerge_pass(
        repositories, BrokenListing({"docs-repo": [2]}))
    assert summaries[0]["error"] == "PR_LIST_ERROR:KeyError"
    assert summaries[1]["results"] == [{"pr": 2, "outcome": "merged"}]


@pytest.mark.parametrize("boundary", ["listing", "parked", "readback"])
def test_authority_refusal_stops_all_repositories_at_read_boundary(tmp_path, boundary):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    store = _queue_store(tmp_path)
    if boundary == "parked":
        with store.locked() as state:
            state["repositories"]["core-repo"] = {
                "active": None, "parked": {"1": {
                    "reason": "changes_requested", "head": "a" * 40,
                    "run": [11, 1], "review": [None, "none"],
                }},
            }
            store.save(state)

    class AuthorityRefusal(FakeGateApplication):
        reads = 0

        def candidates(self, name):
            if boundary == "listing" and name == "core-repo":
                return {"ok": False, "code": "EXECUTOR_HOST_REQUIRED"}
            return super().candidates(name)

        def inspect(self, name, pr):
            if name == "core-repo" and pr == 1:
                self.reads += 1
                if boundary == "parked" or (boundary == "readback" and self.reads == 2):
                    return {"ok": False, "code": "EXECUTOR_HOST_REQUIRED"}
                if boundary == "readback":
                    # only a comment re-review reads the PR again.
                    return _comment_due()
            return super().inspect(name, pr)

    gate = AuthorityRefusal({"core-repo": [1, 2], "docs-repo": [3]})
    summaries = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store)
    assert summaries[0]["authority_integrity_failed"] is True
    assert summaries[1]["results"] == []
    assert not any(call[0] == "merge" for call in gate.calls)


def test_active_inspect_exception_waits_then_parks_without_blocking_sibling(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]

    class BrokenActive(FakeGateApplication):
        broken = False

        def inspect(self, name, pr):
            if self.broken and pr == 1:
                raise KeyError("transport")
            return super().inspect(name, pr)

    gate = BrokenActive({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    gate.broken = True
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               now=lambda: clock[0])[0]
    assert first["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "CANDIDATE_ERROR:KeyError"}]
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"][0]["reason"] == "INSPECT_STALLED"
    assert second["results"][-1] == {"pr": 2, "outcome": "merged"}


def test_parked_inspect_and_rejection_readback_exceptions_are_candidate_local(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    rejected = _queue_inspect(review="changes_requested")

    class BrokenReadback(FakeGateApplication):
        broken = False

        def inspect(self, name, pr):
            if self.broken and pr == 1:
                raise KeyError("readback")
            return super().inspect(name, pr)

    gate = BrokenReadback({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): rejected,
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, run=12,
            blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    gate.broken = True
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert [row["pr"] for row in result["results"]] == [2, 1, 3]
    assert result["results"][-1] == {"pr": 3, "outcome": "merged"}


def test_rejection_readback_exception_keeps_other_candidates_available(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    class BrokenRejectionReadback(FakeGateApplication):
        reads = 0

        def inspect(self, name, pr):
            if pr == 1:
                self.reads += 1
                if self.reads == 2:
                    raise KeyError("rejection readback")
            return super().inspect(name, pr)

    # only a comment re-review reads the rejected PR again.
    gate = BrokenRejectionReadback({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _comment_due(),
    })
    result = merge_open_prs.run_automerge_pass([repository], gate)[0]
    assert [row["pr"] for row in result["results"]] == [1, 1, 2]
    assert result["results"][0]["comment_rereview"] is True
    assert result["results"][1:] == [
        {"pr": 1, "outcome": "retained", "reason": "CANDIDATE_ERROR:KeyError"},
        {"pr": 2, "outcome": "merged"},
    ]


def test_product_dispatch_exception_does_not_hide_managed_repository(tmp_path):
    repositories = [
        merge_open_prs.Repository("billing-repo", tmp_path, "forgejo", "acme/billing-repo",
                                  gate_profile="product-pr"),
        merge_open_prs.Repository("docs-repo", tmp_path, "forgejo",
                                  "acme/docs-repo"),
    ]

    class BrokenProduct(FakeGateApplication):
        def inspect(self, name, pr):
            if name == "billing-repo":
                raise KeyError("product")
            return super().inspect(name, pr)

    gate = BrokenProduct({"billing-repo": [1], "docs-repo": [2]})
    summaries = merge_open_prs.run_automerge_pass(repositories, gate)
    assert summaries[0]["results"] == [{"pr": 1, "outcome": "retained",
                                        "reason": "CANDIDATE_ERROR:KeyError"}]
    assert summaries[1]["results"] == [{"pr": 2, "outcome": "merged"}]


@pytest.mark.parametrize("reason", ["CI_API_UNAVAILABLE", "CI_HEAD_STALE"])
def test_transient_ci_evidence_keeps_active_until_deadline(tmp_path, reason):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="unavailable", conclusion=None, blocking_reason=reason),
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               now=lambda: clock[0])[0]
    # the green PR 2 is elected first; PR 1 keeps the slot after it.
    assert first["results"] == [{"pr": 2, "outcome": "merged"},
                                {"pr": 1, "outcome": "retained", "reason": reason}]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"][0]["reason"] == "CI_STALLED"
    assert second["results"][-1] == {"pr": 2, "outcome": "merged"}


@pytest.mark.parametrize("blocker,mergeable", [
    ("PR_MERGE_FAILED", True), ("PR_HEAD_STALE", True),
    ("PR_MERGEABILITY_UNKNOWN", None),
])
def test_no_progress_merge_blocker_parks_after_deadline(tmp_path, blocker, mergeable):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(mergeable=mergeable),
        ("merge", "core-repo", 1): {"ok": False, "code": blocker},
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               now=lambda: clock[0])[0]
    assert first["results"][0]["reason"] == blocker
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"][0]["reason"] == "PR_STALLED"
    assert second["results"][-1] == {"pr": 2, "outcome": "merged"}


@pytest.mark.parametrize("code", [
    "PR_REFRESH_FAILED", "PR_REFRESH_STATE_CHANGED", "PR_REFRESH_NOT_ELIGIBLE",
])
def test_refused_refresh_parks_after_deadline_and_queue_advances(tmp_path, code):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _stale_base(),
        ("refresh", "core-repo", 1): {"ok": False, "code": code},
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               now=lambda: clock[0])[0]
    # the green PR 2 is elected first; PR 1 keeps the slot after it.
    assert first["results"] == [{"pr": 2, "outcome": "merged"},
                                {"pr": 1, "outcome": "retained", "reason": code}]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"][0] == {"pr": 1, "outcome": "retained", "reason": "PR_STALLED"}
    assert second["results"][-1] == {"pr": 2, "outcome": "merged"}
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["reason"] == "gate_blocked"


def test_queue_save_os_error_is_a_named_state_error(tmp_path, monkeypatch):
    from git_gate import automerge_queue
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)

    def full(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(automerge_queue.os, "replace", full)
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_WRITE_FAILED"):
        store.save(automerge_queue.empty_state())
    assert not list(store.path.parent.glob(".automerge-*"))


def test_queue_lock_sweeps_temporaries_left_by_a_killed_writer(tmp_path):
    store = _queue_store(tmp_path)
    stale = store.path.parent / ".automerge-killed"
    stale.write_bytes(b"{}")
    with store.locked():
        assert not stale.exists()


def test_aged_merge_authority_refusal_is_not_rewritten_as_stall(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("merge", "core-repo", 1): {"ok": False, "code": "EXECUTOR_HOST_REQUIRED"},
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    clock[0] += 3601
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "EXECUTOR_HOST_REQUIRED",
                                  "authority_integrity_failed": True}]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


def test_parked_unknown_mergeability_reenters_when_resolved(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(mergeable=None),
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    clock[0] += 3601
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(mergeable=True)
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect()
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert [row["pr"] for row in result["results"]] == [2, 1]


def test_queue_lock_preserves_body_errors_and_refuses_missing_directory(tmp_path):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    for error in (FileNotFoundError, PermissionError):
        with pytest.raises(error):
            with store.locked():
                raise error("body")
        with store.locked():
            pass  # The previous exception released the file lock.
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_DIRECTORY_UNAVAILABLE"):
        with QUEUE_STORE_TYPE(tmp_path / "absent" / "automerge.json",
                              repositories=("core-repo",)).locked():
            pass


def test_serial_queue_refreshes_only_after_previous_pr_finishes(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [27, 29, 30]}, responses={
        ("inspect", "core-repo", 27): _queue_inspect(),
        ("inspect", "core-repo", 29): _stale_base(ci_run_id=19, ci_run_attempt=1),
        ("inspect", "core-repo", 30): _stale_base(ci_run_id=20, ci_run_attempt=1),
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               max_reviews=1)[0]
    assert [row["outcome"] for row in first["results"]] == ["merged", "refreshed"]
    assert [call[2] for call in gate.calls if call[0] == "refresh"] == [29]
    # Review-ahead inspects the stale-base PR 30 and leaves it alone.
    assert not any(call[0] in {"refresh", "merge", "review"} and call[2] == 30
                   for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 29

    gate.by_repository["core-repo"] = [29, 30]
    gate.responses[("inspect", "core-repo", 29)] = _queue_inspect(
        head="c", base="d", ci="pending", run=21, conclusion=None,
        blocking_reason="CI_RUN_IN_PROGRESS")
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                max_reviews=1)[0]
    assert second["results"] == [{"pr": 29, "outcome": "retained",
                                   "reason": "CI_RUN_IN_PROGRESS"}]
    assert not any(call[0] in {"refresh", "merge", "review"} and call[2] == 30
                   for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 29

    gate.responses[("inspect", "core-repo", 29)] = [
        _queue_inspect(head="c", base="d", run=21, review="not_approved"),
        _queue_inspect(head="c", base="d", run=21),
    ]
    third = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               max_reviews=1)[0]
    assert [row["outcome"] for row in third["results"]] == ["merged", "refreshed"]
    assert [call[2] for call in gate.calls if call[0] == "refresh"] == [29, 30]
    assert [call[2] for call in gate.calls if call[0] == "review"] == [29]


def test_active_stale_base_candidate_is_refreshed_not_waited_into_a_stall(tmp_path):
    """A green run on a moved base is refreshed on every pass, never CI_STALLED.

    The gate reports it as `pending`. A serial queue could wait on it
    until CI_STALLED, unpark it at once and wait again, so the queue never
    reached a later PR."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _stale_base(ci_run_id=11, ci_run_attempt=1),
        ("inspect", "core-repo", 2): _stale_base(ci_run_id=12, ci_run_attempt=1),
    })
    results = []
    for _ in range(3):
        results += merge_open_prs.run_automerge_pass(
            [repository], gate, queue_store=store, now=lambda: clock[0])[0]["results"]
        clock[0] += merge_open_prs.CI_WAIT_SECONDS + 1
    assert [call[2] for call in gate.calls if call[0] == "refresh"] == [1, 1, 1]
    assert all(row["outcome"] == "refreshed" for row in results)


def test_parked_ci_failure_does_not_reenter_on_base_movement(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    failed = _queue_inspect(ci="red", conclusion="failure", run=11,
                            blocking_reason="CI_RUN_FAILED")
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): failed,
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, run=12,
            blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    gate.responses[("inspect", "core-repo", 1)] = {**failed, "base_sha": "c" * 40}
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert second["results"] == [{"pr": 2, "outcome": "merged"}]
    assert not any(call[:3] == ("merge", "core-repo", 1) for call in gate.calls)


@pytest.mark.parametrize("first_reason", ["CI_EVIDENCE_INVALID", "CI_RUN_IN_PROGRESS"])
def test_parked_evidence_or_stall_recovers_on_same_run(tmp_path, first_reason):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    first = _queue_inspect(
        ci="unavailable" if first_reason == "CI_EVIDENCE_INVALID" else "pending",
        conclusion=None, run=11, blocking_reason=first_reason)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): first,
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, run=12,
            blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    if first_reason == "CI_RUN_IN_PROGRESS":
        clock[0] += 3601
        merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                           now=lambda: clock[0])
    expected = "evidence_unavailable" if first_reason == "CI_EVIDENCE_INVALID" else "ci_stalled"
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == expected
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(run=11)
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert [row["pr"] for row in result["results"]] == [2, 1]
    assert [row["outcome"] for row in result["results"]] == ["merged", "merged"]


def test_unchanged_review_rejection_survives_base_movement(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    rejected = _queue_inspect(
        review="changes_requested", latest_delegated_review_id=31,
        latest_delegated_review_state="REQUEST_CHANGES")
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): rejected,
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    gate.responses[("inspect", "core-repo", 1)] = {**rejected, "base_sha": "c" * 40,
                                                    "review_state": "not_approved"}
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect()
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert result["results"] == [{"pr": 2, "outcome": "merged"}]
    assert not any(call[:3] == ("review", "core-repo", 1) for call in gate.calls)


def test_corrected_claim_requeues_same_head_with_same_green_ci(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    old = _queue_inspect(
        review="changes_requested", claim_sha="1" * 64,
        latest_delegated_review_id=31,
        latest_delegated_review_state="REQUEST_CHANGES",
    )

    class ClaimGate(FakeGateApplication):
        def review(self, repository, pr, head, base, executor_run, retry_of=None):
            super().review(repository, pr, head, base, executor_run, retry_of)
            self.responses[("inspect", repository, pr)] = _queue_inspect(
                review="changes_requested", claim_sha="2" * 64,
                latest_delegated_review_id=32,
                latest_delegated_review_state="REQUEST_CHANGES",
            )
            return {"ok": True, "verdict": "CHANGES_REQUIRED"}

    gate = ClaimGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): old,
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["claim_sha"] == "1" * 64
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert not any(call[0] == "review" for call in gate.calls)

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        review="not_approved", claim_sha="2" * 64,
    )
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert [call[3:5] for call in gate.calls if call[0] == "review"] == [
        ("a" * 40, "b" * 40)
    ]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["claim_sha"] == "2" * 64


def test_unavailable_claim_parks_author_and_corrected_claim_reenters_after_active(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="not_approved", claim_sha=None),
        ("review", "core-repo", 1): {"ok": False, "code": "PR_CLAIM_UNAVAILABLE"},
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved", run=12),
    })

    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert first["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_CLAIM_UNAVAILABLE"},
        {"pr": 2, "outcome": "retained", "reason": "PR_REVIEW_NOT_APPROVED"},
    ]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "claim_unavailable"
    assert row["parked"]["1"]["claim_sha"] is None
    assert row["active"]["pr"] == 2
    assert gate.statuses[-1][1][1] == (
        "queue=parked reason=claim_unavailable owner=author pos=-")
    assert not any(call[:3] == ("merge", "core-repo", 1) for call in gate.calls)

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        review="not_approved", claim_sha="1" * 64)
    reviews_before = [call[2] for call in gate.calls if call[0] == "review"]
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert second["results"] == [
        {"pr": 2, "outcome": "retained", "reason": "PR_REVIEW_NOT_APPROVED"},
    ]
    assert [call[2] for call in gate.calls if call[0] == "review"] == reviews_before + [2]

    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    gate.responses[("review", "core-repo", 1)] = {
        "ok": False, "code": "REVIEW_INCOMPLETE"}
    third = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert third["results"] == [
        {"pr": 2, "outcome": "merged"},
        {"pr": 1, "outcome": "retained", "reason": "REVIEW_INCOMPLETE"},
    ]
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1, 2, 2, 1]
    assert not any(call[:3] == ("merge", "core-repo", 1) for call in gate.calls)


def _outdated_claim(**extra):
    """A green PR whose claim names the head before an operator's "Update branch"."""
    return _queue_inspect(review="not_approved", claim_sha=None,
                          claim_error="PR_CLAIM_OUTDATED_HEAD", **extra)


def test_outdated_claim_head_refreshes_before_parking(tmp_path):
    """The gate refresh re-projects a claim left on the head before a
    tree-identical update merge; parking it would wait hours on an author.
    Any other claim error still parks at once, with no refresh."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="not_approved", claim_sha=None, claim_error="PR_CLAIM_MISSING"),
        ("inspect", "core-repo", 2): _outdated_claim(),
        # Not approved: an approved PR 3 would be elected ahead of both.
        ("inspect", "core-repo", 3): _queue_inspect(run=12, review="not_approved")})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert [call[2:5] for call in gate.calls if call[0] == "refresh"] == [
        (2, "a" * 40, "b" * 40)]
    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_CLAIM_UNAVAILABLE",
         "claim_error": "PR_CLAIM_MISSING"},
        {"pr": 2, "outcome": "refreshed", "head_sha": "c" * 40}]
    row = store.read()["repositories"]["core-repo"]
    assert set(row["parked"]) == {"1"}
    assert row["active"]["pr"] == 2
    assert row["active"]["refreshed"] == "a" * 40
    assert not any(call[0] in {"review", "merge"} for call in gate.calls)


@pytest.mark.parametrize("uncertain", [False, True])
def test_outdated_claim_head_refreshes_once_per_head_then_parks(tmp_path, uncertain):
    """A refresh that left the claim outdated on the same head is not
    repeated: the next pass parks it for its author as before."""
    store = _queue_store(tmp_path)
    repositories = [_core_repo(tmp_path), merge_open_prs.Repository(
        "docs-repo", tmp_path / "docs-repo", "forgejo", "acme/docs-repo")]
    gate = FakeGateApplication({"core-repo": [1, 2], "docs-repo": [3]}, responses={
        ("inspect", "core-repo", 1): _outdated_claim(),
        ("inspect", "core-repo", 2): _queue_inspect(run=12),
        ("inspect", "docs-repo", 3): _stale_base(),
        ("refresh", "core-repo", 1): {"ok": True, "code": "PR_REFRESHED"}})
    original_refresh = gate.refresh
    persisted_before_dispatch = []

    def refresh(repository, pr, head, base):
        if repository == "core-repo" and pr == 1:
            persisted_before_dispatch.append(store.read()["repositories"][repository]["active"]["refreshed"])
            result = original_refresh(repository, pr, head, base)
            if uncertain:
                raise TimeoutError("the branch update response was lost")
            return result
        return original_refresh(repository, pr, head, base)

    gate.refresh = refresh
    first = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store)
    assert [call[1:3] for call in gate.calls if call[0] == "refresh"] == [
        ("core-repo", 1), ("docs-repo", 3)]
    assert persisted_before_dispatch == ["a" * 40], "the attempt must be saved before an uncertain dispatch"
    gate.responses[("inspect", "docs-repo", 3)] = _in_progress(head="c")

    summary = merge_open_prs.run_automerge_pass(
        repositories, gate, queue_store=store)[0]

    assert [call[1:3] for call in gate.calls if call[0] == "refresh"] == [
        ("core-repo", 1), ("docs-repo", 3)]
    if uncertain:
        # the approved PR 2 is elected, and merged, before PR 1.
        assert first[0]["results"][0] == {"pr": 2, "outcome": "merged"}
        assert first[0]["results"][1] == {
            "pr": 1, "outcome": "retained", "reason": "PR_CLAIM_UNAVAILABLE",
            "claim_error": "PR_CLAIM_OUTDATED_HEAD", "refresh_refused": "CANDIDATE_ERROR:TimeoutError",
        }
    else:
        assert summary["results"][0] == {"pr": 1, "outcome": "retained",
                                         "reason": "PR_CLAIM_UNAVAILABLE",
                                         "claim_error": "PR_CLAIM_OUTDATED_HEAD"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "claim_unavailable")
    assert {"pr": 2, "outcome": "merged"} in summary["results"]


def test_a_refused_claim_refresh_parks_in_the_same_pass(tmp_path):
    """Today's gate refuses the refresh: the PR parks exactly as before."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _outdated_claim(),
        ("inspect", "core-repo", 2): _queue_inspect(run=12),
        ("refresh", "core-repo", 1): {"ok": False, "code": "PR_REFRESH_NOT_ELIGIBLE"}})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    # the approved PR 2 is elected, and merged, before PR 1.
    assert summary["results"] == [
        {"pr": 2, "outcome": "merged"},
        {"pr": 1, "outcome": "retained", "reason": "PR_CLAIM_UNAVAILABLE",
         "claim_error": "PR_CLAIM_OUTDATED_HEAD", "refresh_refused": "PR_REFRESH_NOT_ELIGIBLE"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "claim_unavailable")


def test_missing_active_pr_is_reconciled_before_next_selection(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    gate.by_repository["core-repo"] = [2]  # merged or closed after the crash
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert result["results"] == [{"pr": 2, "outcome": "merged"}]
    assert store.read()["repositories"]["core-repo"]["active"] is None


def test_queue_state_rejects_corruption_and_overlapping_pass(tmp_path):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {}}
        store.save(state)
        with pytest.raises(QueueStateError, match="AUTOMERGE_PASS_BUSY"):
            with store.locked():
                pass
    store.path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
        with store.locked():
            pass


def test_queue_state_round_trips_a_product_row_and_rejects_an_unmanaged_one(tmp_path):
    """the queue validates against the executor's own selection.

    A `product-pr` repository is scheduled through the same persisted queue as
    the two gate-managed checkouts, so its row must survive a save and a read.
    A row for a repository outside the selection still invalidates the state.
    """
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path, repositories=("core-repo", "docs-repo", "billing-repo"))
    row = {
        "active": {"pr": 67, "head": "a" * 40, "base": "b" * 40,
                   "run": [11, 1], "wait_since": 1000.0},
        "parked": {"68": {"reason": "conflict", "head": "c" * 40,
                          "run": [12, None], "review": [None, "none"]}},
    }
    with store.locked() as state:
        state["repositories"]["billing-repo"] = row
        store.save(state)
    assert store.read()["repositories"]["billing-repo"] == row

    platform_only = QUEUE_STORE_TYPE(store.path, repositories=("core-repo", "docs-repo"))
    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
        platform_only.read()

    before = store.path.read_bytes()
    with store.locked() as state:
        state["repositories"]["shop-repo"] = {"active": None, "parked": {}}
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)
    assert store.path.read_bytes() == before


def test_draft_oldest_and_active_becoming_draft_do_not_hold_queue(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): {"ok": True, "pr_state": "draft"},
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    # the green PR 3 is elected first; PR 2 holds the slot after it.
    assert first["results"] == [{"pr": 3, "outcome": "merged"},
                                {"pr": 2, "outcome": "retained",
                                 "reason": "CI_RUN_IN_PROGRESS"}]
    gate.responses[("inspect", "core-repo", 2)] = {"ok": True, "pr_state": "draft"}
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert second["results"] == [{"pr": 3, "outcome": "merged"}]


def test_inspect_failure_skips_unselected_but_active_has_bounded_wait(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): {"ok": False, "code": "TEA_REVIEW_PROBE_FAILED"},
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               now=lambda: clock[0])[0]
    # the green PR 3 is elected, and merged, before PR 2 takes the slot.
    assert [row["pr"] for row in first["results"]] == [1, 3, 2]
    gate.responses[("inspect", "core-repo", 2)] = {"ok": False, "code": "TEA_REVIEW_PROBE_FAILED"}
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"][0]["reason"] == "INSPECT_STALLED"
    assert second["results"][-1] == {"pr": 3, "outcome": "merged"}
    assert store.read()["repositories"]["core-repo"]["parked"]["2"]["reason"] == "evidence_unavailable"


def test_missing_run_does_not_restart_active_wait_and_late_stale_success_reenters(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    pending = _queue_inspect(ci="pending", conclusion=None, run=11,
                             blocking_reason="CI_RUN_IN_PROGRESS")
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): pending,
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, run=12,
            blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    gate.responses[("inspect", "core-repo", 1)] = {**pending, "ci_run_id": None,
                                                     "ci_run_attempt": None}
    clock[0] += 3601
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["reason"] == "ci_stalled"
    assert parked["run"] == [11, 1]
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    gate.responses[("inspect", "core-repo", 1)] = _stale_base(
        ci_run_id=11, ci_run_attempt=1)
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert [row["outcome"] for row in result["results"]] == ["merged", "refreshed"]


def test_human_rejection_dismissal_reenters_without_a_new_head(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            review="changes_requested", latest_delegated_review_id=31,
            latest_delegated_review_state="APPROVED"),
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect()
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        review="approved", latest_delegated_review_id=31,
        latest_delegated_review_state="APPROVED")
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]
    assert [row["pr"] for row in result["results"]] == [2, 1]


def test_fix_push_after_delegated_rejection_gets_a_fresh_review(tmp_path):
    """The whole re-review loop: a delegated rejection parks the PR, an
    unchanged head keeps it parked, and a fix push buys a review of the new
    head. Forgejo only marks the old rejection stale, so the gate row for the
    new head carries no `latest_delegated_*` fields (the gate projection)."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)

    class RejectingGate(FakeGateApplication):
        def review(self, repository, pr, head, base, executor_run, retry_of=None):
            super().review(repository, pr, head, base, executor_run, retry_of)
            self.responses[("inspect", repository, pr)] = _queue_inspect(
                head=head[0], review="not_approved", latest_delegated_review_id=31,
                latest_delegated_review_state="REQUEST_CHANGES")
            return {"ok": True, "verdict": "CHANGES_REQUIRED"}

    gate = RejectingGate({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved")})

    def reviewed_heads():
        return [call[3] for call in gate.calls if call[0] == "review"]

    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["reason"] == "changes_requested"
    assert parked["head"] == "a" * 40
    assert reviewed_heads() == ["a" * 40]

    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert reviewed_heads() == ["a" * 40]

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        head="c", review="not_approved")
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert reviewed_heads() == ["a" * 40, "c" * 40]
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["head"] == "c" * 40


def test_product_repository_gets_first_review_turn_on_rotated_pass(tmp_path):
    store = _queue_store(tmp_path, repositories=("core-repo", "billing-repo"))
    core = merge_open_prs.Repository("core-repo", tmp_path, "forgejo", "acme/core-repo")
    product = merge_open_prs.Repository("billing-repo", tmp_path, "forgejo",
                                        "acme/billing-repo", gate_profile="product-pr")
    gate = FakeGateApplication({"core-repo": [1], "billing-repo": [2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS"),
        ("inspect", "billing-repo", 2): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
    })
    with store.locked() as state:
        state["cursor"] = 1
        store.save(state)
    merge_open_prs.run_automerge_pass([core, product], gate, queue_store=store,
                                       max_reviews=1)
    assert [call[1] for call in gate.calls if call[0] == "review"] == ["billing-repo"]


@pytest.mark.parametrize("result", [
    {"ok": False, "code": "REVIEW_STATE_CHANGED"},
    {"ok": False, "code": "PR_CI_NOT_GREEN"},
    {"ok": False, "code": "UNEXPECTED_DISPATCH_RESULT"},
])
def test_ambiguous_review_result_spends_paid_slot(result):
    assert merge_open_prs._review_attempt_started(result) is True


def test_a_refusal_the_review_owner_marks_undispatched_spends_no_slot():
    assert merge_open_prs._review_attempt_started(
        {"ok": False, "code": "PR_CI_NOT_GREEN", "dispatched": False}) is False


def test_uncertain_fresh_review_with_unproved_cleanup_spends_paid_slot():
    marker = "gate-review-" + "a" * 24
    assert merge_open_prs._review_attempt_started({
        "ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
        "run_marker": marker, "cleanup_proven": False,
    }) is True
    assert merge_open_prs._review_attempt_started({
        "ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
        "run_marker": marker, "cleanup_proven": True,
    }) is False


def test_paid_budget_blocks_insufficient_profile_dispatch_in_second_repository(tmp_path):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 1): {"ok": True, "verdict": "CHANGES_REQUIRED"},
        ("inspect", "docs-repo", 2): _queue_inspect(
            ci="unavailable", review="not_approved", conclusion=None,
            blocking_reason="CI_PROFILE_INSUFFICIENT"),
    })
    summaries = merge_open_prs.run_automerge_pass(repositories, gate, max_reviews=1)
    assert [call[1] for call in gate.calls if call[0] == "review"] == ["core-repo"]
    assert summaries[1]["results"] == [{
        "pr": 2, "outcome": "retained", "reason": "REVIEW_BUDGET_EXHAUSTED"}]


def _deadline_pass(tmp_path, deadline):
    """One rate-limited pass on a fake clock where every review takes 1700 s:
    core-repo #1 needs a review, docs-repo #2 is approved and #3 needs
    a review."""
    clock = [1_000_000.0]

    class SlowReviewGate(FakeGateApplication):
        def review(self, *args, **kwargs):
            clock[0] += 1700
            return super().review(*args, **kwargs)

    store = _queue_store(tmp_path)
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    gate = SlowReviewGate({"core-repo": [1], "docs-repo": [2, 3]}, responses={
        ("inspect", "core-repo", 1): [_queue_inspect(review="not_approved"), _queue_inspect()],
        ("inspect", "docs-repo", 3): _queue_inspect(review="not_approved"),
    })
    summaries = merge_open_prs.run_automerge_pass(
        repositories, gate, queue_store=store, max_reviews=3, max_reviews_per_repo=1,
        now=lambda: clock[0], pass_deadline_seconds=deadline)
    return gate, store, summaries


@pytest.mark.parametrize(("deadline", "reviewed"), [
    # 1700 s spent on the first review: 1300 s left is under 1330 + 60.
    (3000, ["core-repo"]),
    # 1400 s left still fits one more hard-bound review.
    (3100, ["core-repo", "docs-repo"]),
    (None, ["core-repo", "docs-repo"]),
])
def test_pass_deadline_starts_no_review_that_could_outlast_it(tmp_path, deadline, reviewed):
    gate, _store, _summaries = _deadline_pass(tmp_path, deadline)
    assert [call[1] for call in gate.calls if call[0] == "review"] == reviewed
    assert [call[2] for call in gate.calls if call[0] == "merge"][:2] == [1, 2]


@pytest.mark.parametrize(("status_seconds", "dispatched"), [(60, True), (61, False)])
def test_review_status_publication_preserves_the_dispatch_time_reserve(status_seconds, dispatched):
    clock = [0]
    gate = FakeGateApplication({"core-repo": [1]})
    budget = merge_open_prs._ReviewBudget(gate, 1, 1, deadline=1450, now=lambda: clock[0])

    def publish_status(*_args):
        clock[0] += status_seconds

    budget.before_review = publish_status
    result = budget.review("core-repo", 1, "a" * 40, "b" * 40, "deadline-status-test")

    assert clock[0] == status_seconds
    assert bool([call for call in gate.calls if call[0] == "review"]) is dispatched
    assert budget.review_count == int(dispatched)
    if not dispatched:
        assert result == {"ok": False, "code": "PASS_DEADLINE"}


def test_a_review_deferred_by_the_pass_deadline_keeps_the_pr_active_and_unannounced(tmp_path):
    """The deferred PR is retained with its own code, stays active for the
    next pass without a park, and is never published as reviewing; the
    approved PR before it still merges."""
    gate, store, summaries = _deadline_pass(tmp_path, 3000)
    assert [call[2] for call in gate.calls if call[0] == "merge"] == [1, 2]
    assert summaries[1]["results"] == [
        {"pr": 2, "outcome": "merged"},
        {"pr": 3, "outcome": "retained", "reason": "PASS_DEADLINE"}]
    row = store.read()["repositories"]["docs-repo"]
    assert row["active"]["pr"] == 3
    assert row["parked"] == {}
    assert not any("reason=reviewing" in descriptions.get(3, "")
                   for repository, descriptions, _ in gate.statuses
                   if repository == "docs-repo")


def test_cleanup_proven_receipt_reread_allows_one_linked_paid_retry(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    marker = "gate-review-" + "c" * 24
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
        ("review", "core-repo", 1): [
            {"ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
             "cleanup_proven": True, "run_marker": marker, "recovery_action": "retry"},
            {"ok": True, "code": "REVIEW_PUBLISHED", "verdict": "ACCEPT_STATIC_CHANGE"},
        ],
    })
    summary = merge_open_prs.run_automerge_pass([repository], gate, max_reviews=1)[0]
    assert summary["results"] == [{"pr": 1, "outcome": "merged"}]
    assert [call[6] for call in gate.calls if call[0] == "review"] == [None, marker]


_PRIMARY_NOT_STARTED = {"worker": "reviewer", "error": "NONZERO_EXIT(1)"}


def test_a_review_whose_primary_never_started_logs_one_alertable_outage_line(tmp_path, caplog):
    """the primary route outage is one stable, bounded journal line."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
        ("review", "core-repo", 1): {
            "ok": True, "code": "REVIEW_PUBLISHED", "verdict": "ACCEPT_STATIC_CHANGE",
            "dispatched": True, "primary_not_started": _PRIMARY_NOT_STARTED,
            "primary_review_error": "NONZERO_EXIT(1)",
            "primary_review_error_detail": "private detail"},
    })
    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        merge_open_prs.run_automerge_pass([repository], gate, max_reviews=1)
    outage = [record for record in caplog.records
              if "review_primary_not_started" in record.getMessage()]
    assert [(record.levelno, record.getMessage()) for record in outage] == [(
        logging.ERROR,
        "automerge: review_primary_not_started core-repo#1 head=" + "a" * 40
        + " route=reviewer err=NONZERO_EXIT(1)")]
    assert "private detail" not in caplog.text


def test_a_refusal_after_a_never_started_primary_logs_one_outage_line(tmp_path, caplog):
    """a failure after the fallback review still names the outage."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
        ("review", "core-repo", 1): {
            "ok": False, "code": "PR_CI_NOT_GREEN", "dispatched": True,
            "primary_not_started": _PRIMARY_NOT_STARTED},
    })
    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        merge_open_prs.run_automerge_pass([repository], gate, max_reviews=1)
    outage = [record for record in caplog.records
              if "review_primary_not_started" in record.getMessage()]
    assert [(record.levelno, record.getMessage()) for record in outage] == [(
        logging.ERROR,
        "automerge: review_primary_not_started core-repo#1 head=" + "a" * 40
        + " route=reviewer err=NONZERO_EXIT(1)")]


@pytest.mark.parametrize("marks", [{"dispatched": False}, {}])
def test_a_resumed_publication_logs_no_second_outage_line(tmp_path, caplog, marks):
    """a receipt that resumes without a dispatch still counts as an attempt."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
        ("review", "core-repo", 1): {
            "ok": False, "code": "REVIEW_PUBLICATION_UNCERTAIN", **marks,
            "primary_not_started": _PRIMARY_NOT_STARTED},
    })
    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        merge_open_prs.run_automerge_pass([repository], gate, max_reviews=1)
    assert "review_primary_not_started" not in caplog.text


def test_a_receipt_reread_or_an_ordinary_review_logs_no_outage_line(tmp_path, caplog):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
        # The receipt of an earlier outage, read again: not a new attempt.
        ("review", "core-repo", 1): {
            "ok": True, "code": "REVIEW_REUSED", "verdict": "ACCEPT_STATIC_CHANGE",
            "primary_not_started": _PRIMARY_NOT_STARTED},
    })
    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        merge_open_prs.run_automerge_pass([repository], gate, max_reviews=1)
    assert "review_primary_not_started" not in caplog.text


def test_carried_approval_id_loss_after_base_move_does_not_unpark_stalled_ci(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    pending = _queue_inspect(
        ci="pending", conclusion=None, blocking_reason="CI_RUN_IN_PROGRESS",
        review_id=77, approval_basis="clean_refresh")
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): pending,
        ("inspect", "core-repo", 2): _queue_inspect(
            ci="pending", conclusion=None, run=12,
            blocking_reason="CI_RUN_IN_PROGRESS"),
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    clock[0] += 3601
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                       now=lambda: clock[0])
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    gate.responses[("inspect", "core-repo", 1)] = {
        **pending, "base_sha": "c" * 40, "review_state": "not_approved",
        "review_id": None,
    }
    result = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert result["results"] == [{"pr": 2, "outcome": "merged"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_stalled"


def test_automerge_visits_a_descending_listing_oldest_first(tmp_path):
    """Forgejo lists newest first; the pass must not inherit that."""
    application = FakeGateApplication({"core-repo": [44, 42, 43],
                                       "docs-repo": [9, 8]})
    repositories = [merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
                    for name in ("core-repo", "docs-repo")]

    summaries = merge_open_prs.run_automerge_pass(repositories, application)

    inspected = [(call[1], call[2]) for call in application.calls if call[0] == "inspect"]
    first_visits = list(dict.fromkeys(inspected))
    assert first_visits == [("core-repo", 42), ("core-repo", 43),
                            ("core-repo", 44), ("docs-repo", 8),
                            ("docs-repo", 9)]
    assert [record["pr"] for record in summaries[0]["results"]] == [42, 43, 44]
    assert [record["pr"] for record in summaries[1]["results"]] == [8, 9]


@pytest.mark.parametrize("ci_state", ["pending", "unavailable"])
def test_automerge_refreshes_only_the_oldest_eligible_stale_base_candidate(
        tmp_path, ci_state):
    application = FakeGateApplication(
        {"core-repo": [7, 5, 6]},
        responses={
            ("inspect", "core-repo", 5): _stale_base(ci_state=ci_state),
            ("inspect", "core-repo", 6): _stale_base(ci_state=ci_state),
            # Not approved: an approved PR 7 would be elected ahead.
            ("inspect", "core-repo", 7): _queue_inspect(review="not_approved"),
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    # Selection reads PR 6 to rank it; only PR 5 is refreshed.
    assert [call for call in application.calls if call[0] == "refresh"] == [
        ("refresh", "core-repo", 5, "a" * 40, "b" * 40)]
    assert result["results"] == [
        {"pr": 5, "outcome": "refreshed", "head_sha": "c" * 40},
    ]
    assert not any(call[0] in {"review", "merge"} for call in application.calls)


def test_automerge_refreshes_a_stale_product_pull_request_like_a_platform_one(tmp_path):
    """A stale product base is refreshed, not left to deadlock."""
    application = FakeGateApplication(
        {"billing-repo": [5]}, responses={("inspect", "billing-repo", 5): _stale_base()})
    repository = merge_open_prs.Repository(
        "billing-repo", tmp_path, "forgejo", "acme/billing-repo", gate_profile="product-pr")

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert [call for call in application.calls if call[0] == "refresh"] == [
        ("refresh", "billing-repo", 5, "a" * 40, "b" * 40)]
    assert result["results"] == [
        {"pr": 5, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert not any(call[0] in {"review", "merge"} for call in application.calls)


def test_green_ci_on_live_base_refreshes_a_head_forked_before_main(tmp_path):
    inspected = {**_stale_base(), "ci_state": "green",
                 "blocking_reason": "PR_BASE_STALE"}
    application = FakeGateApplication(
        {"core-repo": [5]}, responses={("inspect", "core-repo", 5): inspected})
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=0)[0]

    assert result["results"] == [
        {"pr": 5, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert [call[0] for call in application.calls if call[0] in {"refresh", "review", "merge"}] == [
        "refresh"]


@pytest.mark.parametrize("inspected", [
    _stale_base(conclusion="failure"),
    _stale_base(conclusion="cancelled"),
    _stale_base(conclusion=None),
    {**_stale_base(), "ci_run_conclusion": "success",
     "blocking_reason": "CI_HEAD_STALE", "next_action": "correct_ci_evidence"},
    {**_stale_base(), "ci_state": "pending", "blocking_reason": "CI_RUN_QUEUED",
     "next_action": "wait_for_trusted_ci"},
])
def test_automerge_does_not_refresh_an_ineligible_candidate(tmp_path, inspected):
    application = FakeGateApplication(
        {"core-repo": [5]}, responses={("inspect", "core-repo", 5): inspected})
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert not any(call[0] == "refresh" for call in application.calls)
    assert result["results"] == [
        {"pr": 5, "outcome": "retained", "reason": inspected["blocking_reason"]}]


@pytest.mark.parametrize("code", [
    "PR_REFRESH_CONFLICT", "PR_REFRESH_STATE_CHANGED", "PR_REFRESH_NOT_ELIGIBLE",
    "PR_REFRESH_FAILED",
])
def test_automerge_retains_a_refused_refresh_and_spends_the_budget(tmp_path, code):
    """A refusal spends only its repository's slot, including after a park."""
    application = FakeGateApplication(
        {"core-repo": [5, 6], "docs-repo": [7]},
        responses={
            ("inspect", "core-repo", 5): _stale_base(),
            ("inspect", "core-repo", 6): _stale_base(),
            ("inspect", "docs-repo", 7): _stale_base(),
            ("refresh", "core-repo", 5): {"ok": False, "code": code},
        },
    )
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]

    results = merge_open_prs.run_automerge_pass(repositories, application)
    result = results[0]

    assert [call[1:3] for call in application.calls if call[0] == "refresh"] == [
        ("core-repo", 5), ("docs-repo", 7)]
    assert result["results"][0] == {"pr": 5, "outcome": "retained", "reason": code}
    assert all(record["outcome"] != "refreshed" for record in result["results"])
    assert result["authority_integrity_failed"] is False
    assert results[1]["results"] == [{"pr": 7, "outcome": "refreshed", "head_sha": "c" * 40}]


def test_automerge_refresh_keeps_the_review_budget_for_a_later_pass(tmp_path):
    """A refresh waits for its own CI before the next PR is merged.

    The unspent review slot may still review the next green PR ahead."""
    application = _ApprovedAfterReview(
        {"core-repo": [5, 6, 7]},
        responses={
            ("inspect", "core-repo", 5): _stale_base(),
            ("inspect", "core-repo", 6): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
            ("inspect", "core-repo", 7): _queue_inspect(review="not_approved"),
            ("review", "core-repo", 6): {"ok": True, "code": "REVIEW_PUBLISHED"},
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1)[0]

    # The unused slot goes to a review-ahead of 6; 6 is not merged.
    assert result["results"] == [
        {"pr": 5, "outcome": "refreshed", "head_sha": "c" * 40},
        {"pr": 6, "outcome": "reviewed_ahead"},
    ]
    assert [call[2] for call in application.calls if call[0] == "review"] == [6]
    assert not any(call[0] == "merge" for call in application.calls)


def test_each_repository_refreshes_its_green_stale_active_head(tmp_path):
    application = FakeGateApplication(
        {"core-repo": [5], "docs-repo": [7]},
        responses={
            ("inspect", "core-repo", 5): _stale_base(),
            ("inspect", "docs-repo", 7): _queue_inspect(
                review="not_approved", blocking_reason="PR_BASE_STALE",
            ),
        },
    )
    repositories = [
        merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")
    ]

    result = merge_open_prs.run_automerge_pass(repositories, application)

    assert result[0]["results"][0]["outcome"] == "refreshed"
    assert result[1]["results"] == [
        {"pr": 7, "outcome": "refreshed", "head_sha": "c" * 40},
    ]
    assert [call[1:3] for call in application.calls if call[0] == "refresh"] == [
        ("core-repo", 5), ("docs-repo", 7)]
    assert not any(call[0] in {"review", "merge"} and call[1] == "docs-repo"
                   for call in application.calls)


@pytest.mark.parametrize("code", ["EXECUTOR_HOST_REQUIRED", "INTERNAL_ERROR"])
def test_refresh_authority_or_integrity_refusal_stops_the_next_repository(tmp_path, code):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [2]}, responses={
        ("inspect", "core-repo", 1): _stale_base(),
        ("inspect", "docs-repo", 2): _stale_base(),
        ("refresh", "core-repo", 1): {"ok": False, "code": code},
    })

    results = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=_queue_store(tmp_path))

    assert results[0]["authority_integrity_failed"] is True
    assert results[1]["results"] == []
    assert [call[1:3] for call in gate.calls if call[0] == "refresh"] == [("core-repo", 1)]
    assert ("inspect", "docs-repo", 2) not in gate.calls
    assert gate.statuses == []


def test_automerge_sweeps_an_eligible_candidate_ranked_after_the_review(tmp_path):
    """The review budget must not swallow the refresh.

    With `--max-reviews 1`, #1 starts and finishes the one review; #2, ranked
    after it, is `CI_BASE_STALE` with a green stale run. Without the sweep
    the pass would stop at #1 and never look at #2."""
    application = _ApprovedAfterReview(
        {"core-repo": [1, 2]},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
            ("review", "core-repo", 1): {"ok": True, "code": "REVIEW_PUBLISHED"},
            # Not approved: an approved #2 would be elected ahead of #1.
            ("inspect", "core-repo", 2): _stale_base(review_state="not_approved"),
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1)[0]

    assert [call[2] for call in application.calls if call[0] == "refresh"] == [2]
    assert not any(call[0] in {"review", "merge"} and call[2] == 2
                   for call in application.calls)
    assert result["results"] == [
        {"pr": 1, "outcome": "merged"},
        {"pr": 2, "outcome": "refreshed", "head_sha": "c" * 40},
    ]


def test_automerge_review_budget_does_not_skip_a_green_candidate(tmp_path):
    """After #1's review, #2 holds its place; #3 cannot refresh ahead."""
    application = _ApprovedAfterReview(
        {"core-repo": [1, 2, 3, 4]},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
            ("review", "core-repo", 1): {"ok": True, "code": "REVIEW_PUBLISHED"},
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            # Not approved: an approved #3 or #4 would be elected ahead.
            ("inspect", "core-repo", 3): _stale_base(review_state="not_approved"),
            ("inspect", "core-repo", 4): _queue_inspect(review="not_approved"),
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1)[0]

    assert not any(call[0] == "refresh" for call in application.calls)
    assert not any(call[0] in {"review", "merge"} and call[2] in {2, 3, 4}
                   for call in application.calls)
    assert result["results"] == [{"pr": 1, "outcome": "merged"}]


def test_automerge_integrity_stop_does_not_trigger_the_sweep(tmp_path):
    """An integrity/authority stop ends the whole pass; the sweep never runs."""
    application = FakeGateApplication(
        {"core-repo": [1, 2, 3]},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 1): {
                "ok": False, "code": "INTERNAL_ERROR",
            },
            # Not approved: an approved #2 would be elected ahead of #1.
            ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
            ("inspect", "core-repo", 3): _queue_inspect(review="not_approved"),
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1)[0]

    assert result["authority_integrity_failed"] is True
    # Selection read the later PRs to rank them; the stop ended every write.
    assert [call[:3] for call in application.calls
            if call[0] in {"review", "refresh", "merge"}] == [("review", "core-repo", 1)]


def test_automerge_refresh_holds_next_candidate_even_with_review_budget(tmp_path):
    """A refresh keeps the same active PR until its replacement CI settles."""
    application = _ApprovedAfterReview(
        {"core-repo": [1, 2, 3]},
        responses={
            ("inspect", "core-repo", 1): _stale_base(),
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
            ("inspect", "core-repo", 3): _queue_inspect(review="not_approved"),
            ("review", "core-repo", 2): {"ok": True, "code": "REVIEW_PUBLISHED"},
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1)[0]

    assert [call[2] for call in application.calls if call[0] == "refresh"] == [1]
    # 2 is only reviewed ahead: never merged while 1 is the active PR.
    assert result["results"] == [
        {"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40},
        {"pr": 2, "outcome": "reviewed_ahead"},
    ]
    assert not any(call[0] == "merge" for call in application.calls)
    assert not any(call[0] == "review" and call[2] == 3 for call in application.calls)


def test_automerge_refresh_budget_is_fresh_for_each_pass(tmp_path):
    application = FakeGateApplication(
        {"core-repo": [5]}, responses={("inspect", "core-repo", 5): _stale_base()})
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    for _ in range(2):
        merge_open_prs.run_automerge_pass([repository], application)

    assert [call[2] for call in application.calls if call[0] == "refresh"] == [5, 5]


def test_automerge_candidate_without_a_refresh_budget_never_refreshes(tmp_path):
    """Direct callers of one candidate keep the behaviour from before the refresh budget."""
    application = FakeGateApplication(
        {"core-repo": [5]}, responses={("inspect", "core-repo", 5): _stale_base()})
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 5)

    assert result == {"pr": 5, "outcome": "retained", "reason": "CI_BASE_STALE"}
    assert not any(call[0] == "refresh" for call in application.calls)


def test_automerge_enumerates_repository_prs_without_local_lanes(tmp_path):
    application = FakeGateApplication({"core-repo": [42, 43]})
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert [call[:3] for call in application.calls if call[0] == "merge"] == [
        ("merge", "core-repo", 42), ("merge", "core-repo", 43)]
    assert [record["outcome"] for record in result["results"]] == [
        "merged",
        "merged",
    ]


def test_automerge_retains_stale_pr_and_continues(tmp_path):
    application = FakeGateApplication(
        {"core-repo": [42, 43, 44]},
        responses={("merge", "core-repo", 43): {"ok": False, "code": "STALE_BRANCH"}},
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert result["results"] == [
        {"pr": 42, "outcome": "merged"},
        {"pr": 43, "outcome": "retained", "reason": "STALE_BRANCH"},
    ]
    assert result["authority_integrity_failed"] is False


@pytest.mark.parametrize("code", [
    "PR_MERGE_POSTCONDITION_FAILED", "GATE_LOCK_DIR_UNSAFE", "INTERNAL_ERROR",
])
def test_automerge_stops_after_integrity_failure(tmp_path, code):
    application = FakeGateApplication(
        {"core-repo": [42, 43]},
        responses={("merge", "core-repo", 42): {
            "ok": False, "code": code}},
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert result["authority_integrity_failed"] is True
    assert not any(call[:3] == ("inspect", "core-repo", 43)
                   for call in application.calls)


def test_automerge_interleaves_repositories_and_treats_changes_required_as_retained(tmp_path):
    application = FakeGateApplication(
        {"core-repo": [1, 2], "docs-repo": [8, 9]},
        responses={
            ("inspect", "core-repo", 1): [
                {"ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                 "ci_state": "green", "review_state": "not_approved"},
                {"ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                 "ci_state": "green", "review_state": "changes_requested",
                 "blocking_reason": "PR_REVIEW_CHANGES_REQUESTED",
                 "latest_delegated_review_id": 41,
                 "latest_delegated_review_state": "REQUEST_CHANGES"}],
            ("review", "core-repo", 1): {"ok": True, "verdict": "CHANGES_REQUIRED"},
            # Not approved: an approved PR 2 would be elected ahead of PR 1.
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
        },
    )
    repositories = [merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
                    for name in ("core-repo", "docs-repo")]

    summaries = merge_open_prs.run_automerge_pass(repositories, application)

    candidate_order = []
    for call in application.calls:
        identity = (call[1], call[2]) if call[0] == "inspect" else None
        if identity is not None and identity not in candidate_order:
            candidate_order.append(identity)
    assert candidate_order == [("core-repo", 1), ("core-repo", 2),
                               ("docs-repo", 8), ("docs-repo", 9)]
    assert summaries[0]["results"][0] == {
        "pr": 1, "outcome": "retained", "reason": "CHANGES_REQUIRED"}
    assert not any(call[:3] == ("merge", "core-repo", 1) for call in application.calls)


def test_automerge_does_not_repeat_review_while_findings_are_current(tmp_path):
    application = FakeGateApplication(
        {"core-repo": [4]},
        responses={("inspect", "core-repo", 4): {
            "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "ci_state": "green", "review_state": "changes_requested",
            "blocking_reason": "PR_REVIEW_CHANGES_REQUESTED",
            "next_action": "address_review_findings",
        }},
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert result["results"] == [{
        "pr": 4, "outcome": "retained", "reason": "PR_REVIEW_CHANGES_REQUESTED"}]
    assert not any(call[0] in {"review", "merge"} for call in application.calls)


def test_automerge_candidate_retains_review_error_evidence_when_present():
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 42): {
                "ok": False, "code": "REVIEW_INCOMPLETE",
                "review_error": "TIMEOUT",
                "review_error_detail": "the reviewer exceeded 900s time limit",
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 42)

    assert result == {
        "pr": 42, "outcome": "retained", "reason": "REVIEW_INCOMPLETE",
        "review_error": "TIMEOUT",
        "review_error_detail": "the reviewer exceeded 900s time limit",
    }


def test_automerge_retries_an_uncertain_review_once_cleanup_is_proven():
    """REVIEW_EXECUTION_UNCERTAIN with cleanup proven -- the reread
    of a receipt a prior pass's failed run left behind -- must not sit
    retained forever; a successor attempt is safe and the gate already
    supports it (`retry_of`), so this pass uses it once."""
    predecessor_marker = "gate-review-" + "a" * 24
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): [
                {"ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                 "ci_state": "green", "review_state": "not_approved"},
                {"ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                 "ci_state": "green", "review_state": "approved"},
            ],
            ("review", "core-repo", 42): [
                {"ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
                 "cleanup_proven": True, "run_marker": predecessor_marker, "recovery_action": "retry"},
                {"ok": True, "code": "REVIEW_PUBLISHED",
                 "verdict": "ACCEPT_STATIC_CHANGE"},
            ],
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 42)

    review_calls = [call for call in application.calls if call[0] == "review"]
    assert len(review_calls) == 2
    assert review_calls[0][6] is None
    assert review_calls[1][6] == predecessor_marker
    assert result == {"pr": 42, "outcome": "merged"}


def test_automerge_keeps_the_original_failure_when_the_gate_refuses_the_retry():
    """A previously authorized retry may be refused after the gate rechecks
    the receipt or native publication. Keep the original failure and report
    the refusal instead of concealing the predecessor's failure."""
    predecessor_marker = "gate-review-" + "c" * 24
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 42): [
                {"ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
                 "cleanup_proven": True, "run_marker": predecessor_marker, "recovery_action": "retry"},
                {"ok": False, "code": "REVIEW_RETRY_PUBLICATION_UNCERTAIN",
                 "cleanup_proven": True, "run_marker": predecessor_marker, "recovery_action": "retry"},
            ],
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 42)

    review_calls = [call for call in application.calls if call[0] == "review"]
    assert len(review_calls) == 2
    assert review_calls[1][6] == predecessor_marker
    assert result == {"pr": 42, "outcome": "retained",
                      "reason": "REVIEW_EXECUTION_UNCERTAIN",
                      "retry_refused": "REVIEW_RETRY_PUBLICATION_UNCERTAIN", "recovery_action": "operator"}


def test_automerge_does_not_retry_a_review_that_is_already_a_successor():
    """At most one successor per head: a receipt that already carries
    `previous_run_marker` is itself a retry, so leave it retained rather than
    chaining a second successor onto it."""
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 42): {
                "ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
                "cleanup_proven": True, "run_marker": "gate-review-" + "b" * 24,
                "previous_run_marker": "gate-review-" + "a" * 24,
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 42)

    review_calls = [call for call in application.calls if call[0] == "review"]
    assert len(review_calls) == 1
    assert result == {"pr": 42, "outcome": "retained",
                       "reason": "REVIEW_EXECUTION_UNCERTAIN"}


def test_automerge_does_not_retry_a_fresh_review_incomplete_in_the_same_pass():
    """a fresh failure in this pass returns REVIEW_INCOMPLETE, even
    with cleanup proven -- retrying inline would start a second up-to-
    REVIEW_TIMEOUT execution right behind the first, doubling the pass
    and risking its own timeout. The retry is left for the next pass's
    reread, which returns REVIEW_EXECUTION_UNCERTAIN instead."""
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 42): {
                "ok": False, "code": "REVIEW_INCOMPLETE",
                "cleanup_proven": True, "run_marker": "gate-review-" + "a" * 24,
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 42)

    review_calls = [call for call in application.calls if call[0] == "review"]
    assert len(review_calls) == 1
    assert result == {"pr": 42, "outcome": "retained",
                       "reason": "REVIEW_INCOMPLETE"}


def test_automerge_does_not_retry_an_uncertain_review_with_cleanup_unproven():
    """Cleanup not proven is exactly the case the gate's own retry-of refusal
    (`REVIEW_RETRY_CLEANUP_NOT_PROVEN`) protects; the pass must not attempt
    one either."""
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 42): {
                "ok": False, "code": "REVIEW_EXECUTION_UNCERTAIN",
                "cleanup_proven": False, "run_marker": "gate-review-" + "a" * 24,
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")

    result = merge_open_prs._automerge_candidate(application, repository, 42)

    review_calls = [call for call in application.calls if call[0] == "review"]
    assert len(review_calls) == 1
    assert result == {"pr": 42, "outcome": "retained",
                       "reason": "REVIEW_EXECUTION_UNCERTAIN"}


def test_automerge_logs_review_execution_failures_at_error_and_verdicts_at_info(
    monkeypatch, tmp_path, capsys, caplog
):
    repositories = {
        "core-repo": merge_open_prs.Repository(
            "core-repo", tmp_path / "core-repo", "forgejo", "acme/core-repo"),
        "docs-repo": merge_open_prs.Repository(
            "docs-repo", tmp_path / "docs-repo", "forgejo", "acme/docs-repo"),
    }
    application = FakeGateApplication(
        {"core-repo": [1, 2, 3], "docs-repo": []},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 1): {
                "ok": False, "code": "REVIEW_INCOMPLETE",
                "review_error": "TIMEOUT",
                "review_error_detail": "the reviewer exceeded 900s time limit",
            },
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 2): {"ok": True, "verdict": "CHANGES_REQUIRED"},
            ("inspect", "core-repo", 3): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "pending", "blocking_reason": "PR_CI_NOT_GREEN",
            },
        },
    )
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["executor_run"] == merge_open_prs.DEFAULT_EXECUTOR_RUN_PREFIX
    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].message == (
        "automerge review failed: core-repo#1 REVIEW_INCOMPLETE "
        "TIMEOUT: the reviewer exceeded 900s time limit"
    )
    assert not any(
        record.levelno == logging.ERROR and "CHANGES_REQUIRED" in record.message
        for record in caplog.records
    )
    assert not any(
        record.levelno == logging.ERROR and "PR_CI_NOT_GREEN" in record.message
        for record in caplog.records
    )


_SYMLINK_MESSAGE = "snapshot tree contains a symlink or unsupported entry"


@pytest.mark.parametrize(("refusal", "record"), [
    ({"ok": False, "code": "REVIEW_SNAPSHOT_UNSUPPORTED",
      "review_error_detail": _SYMLINK_MESSAGE},
     {"pr": 1, "outcome": "retained", "reason": "REVIEW_SNAPSHOT_UNSUPPORTED",
      "review_error_detail": _SYMLINK_MESSAGE}),
    # What a review owner without the stable code returned: the free text itself.
    ({"ok": False, "code": _SYMLINK_MESSAGE},
     {"pr": 1, "outcome": "retained", "reason": "REVIEW_EXECUTION_FAILED",
      "review_error": _SYMLINK_MESSAGE}),
])
def test_review_execution_failure_parks_alerts_and_releases_the_slot(
    monkeypatch, tmp_path, capsys, caplog, refusal, record
):
    repositories = {
        name: merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")
    }
    application = FakeGateApplication({"core-repo": [1, 2], "docs-repo": []}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 1): refusal,
    })
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    summary = json.loads(capsys.readouterr().out)["repositories"][0]
    # the approved PR 2 is elected, and merged, before PR 1.
    assert summary["results"] == [{"pr": 2, "outcome": "merged"}, record]
    assert [entry.message for entry in caplog.records if entry.levelno == logging.ERROR] == [
        f"automerge review failed: core-repo#1 {record['reason']} {_SYMLINK_MESSAGE}"]
    queue = merge_open_prs.QueueStore(repositories=list(repositories)).read()
    row = queue["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "review_execution_failed"

    # The next pass leaves the unchanged head parked: no second review.
    application.by_repository["core-repo"] = [1]
    caplog.clear()
    assert merge_open_prs.main(["--automerge", "--json"]) == 0
    capsys.readouterr()
    assert len([call for call in application.calls if call[0] == "review"]) == 1
    assert not any(entry.levelno == logging.ERROR for entry in caplog.records)
    row = merge_open_prs.QueueStore(repositories=list(repositories)).read()[
        "repositories"]["core-repo"]
    assert row["active"] is None and "1" in row["parked"]


_TOO_LARGE_DETAIL = ("151 files, 90000 bytes (counting stops above the limit); "
                     "limits 150 files, 1000000 bytes")


def test_oversized_diff_parks_the_pr_and_the_queue_continues(
    monkeypatch, tmp_path, capsys, caplog
):
    """REVIEW_DIFF_TOO_LARGE is a review execution failure, so the PR
    parks for the operator instead of staying at the head of the queue."""
    repositories = {
        name: merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")
    }
    application = FakeGateApplication({"core-repo": [1, 2], "docs-repo": []}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 1): {"ok": False, "code": "REVIEW_DIFF_TOO_LARGE",
                                     "review_error_detail": _TOO_LARGE_DETAIL,
                                     "dispatched": False},
    })
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    summary = json.loads(capsys.readouterr().out)["repositories"][0]
    # the approved PR 2 is elected, and merged, before PR 1.
    assert summary["results"] == [
        {"pr": 2, "outcome": "merged"},
        {"pr": 1, "outcome": "retained", "reason": "REVIEW_DIFF_TOO_LARGE",
         "review_error_detail": _TOO_LARGE_DETAIL}]
    queue = merge_open_prs.QueueStore(repositories=list(repositories)).read()
    row = queue["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "review_execution_failed"

    # The next pass leaves the unchanged head parked: no second review.
    application.by_repository["core-repo"] = [1]
    assert merge_open_prs.main(["--automerge", "--json"]) == 0
    capsys.readouterr()
    assert len([call for call in application.calls if call[0] == "review"]) == 1


def test_unavailable_private_filter_parks_the_pr_and_the_queue_continues(
    monkeypatch, tmp_path, capsys
):
    """unreadable base path-filter rules need the operator, so the PR
    parks instead of holding the queue or spending a review slot every pass."""
    detail = "path-rules.yaml is absent at the base commit"
    repositories = {
        name: merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")
    }
    application = FakeGateApplication({"core-repo": [], "docs-repo": [1, 2]}, responses={
        ("inspect", "docs-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "docs-repo", 1): {"ok": False,
                                          "code": "REVIEW_PRIVATE_FILTER_UNAVAILABLE",
                                          "review_error_detail": detail, "dispatched": False},
    })
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    summary = json.loads(capsys.readouterr().out)["repositories"][1]
    # the approved PR 2 is elected, and merged, before PR 1.
    assert summary["results"] == [
        {"pr": 2, "outcome": "merged"},
        {"pr": 1, "outcome": "retained", "reason": "REVIEW_PRIVATE_FILTER_UNAVAILABLE",
         "review_error_detail": detail}]
    row = merge_open_prs.QueueStore(repositories=list(repositories)).read()[
        "repositories"]["docs-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "review_execution_failed"


def test_gate_review_refusal_waits_then_parks_after_deadline(tmp_path):
    code = "STALE_BRANCH"  # A gate code no review-refusal set handles.
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 1): {"ok": False, "code": code},
    })
    first = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                               now=lambda: clock[0])[0]
    # the green PR 2 is elected first; PR 1 keeps the slot after it.
    assert first["results"] == [{"pr": 2, "outcome": "merged"},
                                {"pr": 1, "outcome": "retained", "reason": code}]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    assert store.read()["repositories"]["core-repo"]["parked"] == {}
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"][0] == {"pr": 1, "outcome": "retained", "reason": "PR_STALLED"}
    assert second["results"][-1] == {"pr": 2, "outcome": "merged"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "gate_blocked"


@pytest.mark.parametrize("code", [
    # The next reread decides retry or park.
    "REVIEW_INCOMPLETE",
    # Self-resolving: a timer of their own or the next pass's re-inspection.
    "REVIEW_ATTEMPT_BUSY", "REVIEW_STATE_CHANGED", "PR_CI_NOT_GREEN", "REVIEW_CANCELLED",
])
def test_self_resolving_review_refusal_keeps_its_slot_past_the_deadline(
        tmp_path, code):
    """A long CI wait must not turn these into an operator-owned park."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 1): {"ok": False, "code": code},
    })
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                      now=lambda: clock[0])
    clock[0] += 3601
    second = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                now=lambda: clock[0])[0]
    assert second["results"] == [{"pr": 1, "outcome": "retained", "reason": code}]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1 and row["parked"] == {}


def test_pass_budget_exhaustion_is_not_a_candidate_review_refusal():
    """The pass's own budget is not a stall."""
    application = FakeGateApplication({"core-repo": [42]}, responses={
        ("inspect", "core-repo", 42): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 42): {"ok": False, "code": "REVIEW_BUDGET_EXHAUSTED"},
    })
    repository = merge_open_prs.Repository(
        "core-repo", Path("/tmp/core-repo"), "forgejo", "acme/core-repo")
    result = merge_open_prs._automerge_candidate(application, repository, 42)
    assert result == {"pr": 42, "outcome": "retained", "reason": "REVIEW_BUDGET_EXHAUSTED"}


def test_journal_priority_formatter_emits_syslog_priority_prefix():
    formatter = merge_open_prs._JournalPriorityFormatter("%(levelname)s %(message)s")
    error_record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), None)
    info_record = logging.LogRecord("x", logging.INFO, __file__, 1, "ok", (), None)
    assert formatter.format(error_record) == "<3>ERROR boom"
    assert formatter.format(info_record) == "<6>INFO ok"


@pytest.mark.parametrize(("journal_stream", "expect_prefix"), [("1", True), (None, False)])
def test_automerge_logging_prefixes_only_under_journal_stream(
    monkeypatch, tmp_path, journal_stream, expect_prefix
):
    if journal_stream is None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    else:
        monkeypatch.setenv("JOURNAL_STREAM", journal_stream)
    repositories = {
        "core-repo": merge_open_prs.Repository(
            "core-repo", tmp_path / "core-repo", "forgejo", "acme/core-repo"),
        "docs-repo": merge_open_prs.Repository(
            "docs-repo", tmp_path / "docs-repo", "forgejo", "acme/docs-repo"),
    }
    application = FakeGateApplication(
        {"core-repo": [1], "docs-repo": []},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 1): {"ok": False, "code": "REVIEW_INCOMPLETE"},
        },
    )
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    for handler in saved_handlers:
        root_logger.removeHandler(handler)
    try:
        assert merge_open_prs.main(["--automerge", "--json"]) == 0
        handler = root_logger.handlers[0]
        line = handler.formatter.format(
            logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), None))
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in saved_handlers:
            root_logger.addHandler(handler)
    assert line.startswith("<3>") is expect_prefix


def test_automerge_rejects_repository_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--automerge", "shop-repo"])

    assert exc.value.code == 2
    assert ("always selects the configured repositories"
            in capsys.readouterr().err)


def test_automerge_json_prints_summary_and_keeps_diagnostics_in_logs(
    monkeypatch, tmp_path, capsys, caplog
):
    repositories = {
        name: merge_open_prs.Repository(
            name, tmp_path / name, "forgejo", f"acme/{name}"
        )
        for name in ("core-repo", "docs-repo")
    }
    application = FakeGateApplication({"core-repo": [42], "docs-repo": [42]})

    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "executor_run": merge_open_prs.DEFAULT_EXECUTOR_RUN_PREFIX,
        "ok": True,
        "repositories": [
            {
                "repository": "core-repo",
                "results": [
                    {
                        "pr": 42,
                        "outcome": "merged",
                    }
                ],
                "authority_integrity_failed": False,
                "elected": [{"pr": 42, "tier": 0}],
                "queue_depth": 0,
                "head_wait_s": 0,
                "park_wait_s": 0,
            },
            {
                "repository": "docs-repo",
                "results": [
                    {
                        "pr": 42,
                        "outcome": "merged",
                    }
                ],
                "authority_integrity_failed": False,
                "elected": [{"pr": 42, "tier": 0}],
                "queue_depth": 0,
                "head_wait_s": 0,
                "park_wait_s": 0,
            },
        ]
    }
    assert [call[:3] for call in application.calls if call[0] == "merge"] == [
        ("merge", "core-repo", 42), ("merge", "docs-repo", 42)]
    assert "automerge result" in caplog.text
    # The journal line format is a contract.
    assert '"head_wait_s":0' in caplog.text
    assert '"park_wait_s":0' in caplog.text
    assert "automerge result" not in captured.out


def test_automerge_cli_reports_enumeration_failure_after_other_repository_runs(
    monkeypatch, tmp_path, capsys
):
    repositories = {
        name: merge_open_prs.Repository(name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")
    }
    application = FakeGateApplication({"docs-repo": [7]})
    original_candidates = application.candidates
    application.candidates = lambda repository: (
        {"ok": False, "code": "PR_LIST_FAILED"}
        if repository == "core-repo" else original_candidates(repository)
    )
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)

    assert merge_open_prs.main(["--automerge", "--json"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["repositories"][0]["error"] == "PR_LIST_FAILED"
    assert payload["repositories"][1]["results"] == [{"pr": 7, "outcome": "merged"}]


def test_automerge_cli_skips_busy_pass_without_error_alert(monkeypatch, tmp_path, capsys, caplog):
    from git_gate.automerge_queue import QueueStateError

    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)

    def busy(*args, **kwargs):
        raise QueueStateError("AUTOMERGE_PASS_BUSY")

    monkeypatch.setattr(merge_open_prs, "run_automerge_pass", busy)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    assert merge_open_prs.main(["--automerge", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repositories"][0]["reason"] == (
        "AUTOMERGE_PASS_BUSY")
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def _automerge_cli_with_marker(monkeypatch, tmp_path, *, content: str | None,
                               age_seconds: float = 60):
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    passes = []

    def ran(selected, **kwargs):
        passes.append([repository.name for repository in selected])
        return [{"repository": repository.name, "results": []} for repository in selected]

    monkeypatch.setattr(merge_open_prs, "run_automerge_pass", ran)
    # A held pass publishes `queue=held` through the gate API; no
    # open PR here, so it writes nothing.
    monkeypatch.setattr(merge_open_prs, "_gate_application",
                        lambda: FakeGateApplication({name: [] for name in repositories}))
    marker = merge_open_prs.DEPLOY_MARKER_PATH
    if content is not None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(content)
        stamp = time.time() - age_seconds
        os.utime(marker, (stamp, stamp))
    return passes


_MARKER_JSON = json.dumps({"schema": "deploy-marker.v1", "origin": "dev-host",
                           "plan_id": "0123abcd", "started_at": "2020-01-01T00:00:00+00:00"})


def test_automerge_skips_while_a_fresh_deploy_marker_is_present(
        monkeypatch, tmp_path, capsys, caplog):
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=_MARKER_JSON)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == []
    deploy = {"origin": "dev-host", "plan_id": "0123abcd",
              "started_at": "2020-01-01T00:00:00+00:00"}
    assert json.loads(capsys.readouterr().out) == {
        "executor_run": merge_open_prs.DEFAULT_EXECUTOR_RUN_PREFIX, "ok": True,
        "repositories": [
            {"repository": name, "skipped": True, "reason": "DEPLOY_IN_PROGRESS", "deploy": deploy}
            for name in ("core-repo", "docs-repo")],
    }
    # The journal line format is a contract; a paused pass still logs it.
    assert any(record.message.startswith("automerge result ") for record in caplog.records)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


_HELD = "queue=held reason=deploy_in_progress owner=operator pos=-"


def test_a_held_pass_publishes_the_hold_on_each_queued_pr(monkeypatch, tmp_path, capsys):
    _automerge_cli_with_marker(monkeypatch, tmp_path, content=_MARKER_JSON)
    gate = FakeGateApplication({"core-repo": [4, 5], "docs-repo": [9]})
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: gate)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    capsys.readouterr()
    assert [(repository, descriptions) for repository, descriptions, _ in gate.statuses] == [
        ("core-repo", {4: _HELD, 5: _HELD}), ("docs-repo", {9: _HELD})]
    assert not any(call[0] in {"inspect", "review", "merge", "refresh"} for call in gate.calls)


def test_a_deploy_hold_is_published_once_and_lifted_by_the_next_pass(tmp_path):
    store = _queue_store(tmp_path, repositories=("core-repo",))
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="changes_requested"),
        ("inspect", "core-repo", 2): _in_progress(),
        # Not approved: an approved PR 3 would be elected ahead of PR 2.
        ("inspect", "core-repo", 3): _queue_inspect(review="not_approved")})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    normal = {1: "queue=parked reason=changes_requested owner=author pos=-",
              2: "queue=active reason=waiting_ci owner=queue pos=1",
              3: "queue=eligible reason=- owner=queue pos=2"}
    assert gate.statuses[-1][1] == normal
    deploy = {"reason": "DEPLOY_IN_PROGRESS", "deploy": {"plan_id": "0123abcd"}}

    merge_open_prs._publish_hold([_core_repo(tmp_path)], deploy, gate, queue_store=store)

    # The author still owns its park; everything the queue owns is held.
    assert gate.statuses[-1][1] == {1: normal[1], 2: _HELD, 3: _HELD}
    row = store.read()["repositories"]["core-repo"]
    assert row["held"] == "0123abcd"
    assert row["published"]["2"][1] == row["published"]["3"][1] == _HELD
    assert row["active"]["pr"] == 2 and set(row["parked"]) == {"1"}, "the queue is untouched"

    # The same hold on the next minute's pass: no forge call at all.
    calls, statuses = len(gate.calls), len(gate.statuses)
    merge_open_prs._publish_hold([_core_repo(tmp_path)], deploy, gate, queue_store=store)
    assert (len(gate.calls), len(gate.statuses)) == (calls, statuses)

    # The hold ends: the next pass rewrites each held status, though the
    # queue itself did not change, from the held pairs it recorded.
    statuses_before = len(gate.statuses)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    _repository, descriptions, _published = gate.statuses[-1]
    assert descriptions == normal
    published = gate.statuses[statuses_before][2]
    assert published["2"][1] == published["3"][1] == _HELD
    row = store.read()["repositories"]["core-repo"]
    assert "held" not in row
    assert _HELD not in {description for _head, description in row["published"].values()}

    # A later hold, even under the same plan, is published again.
    merge_open_prs._publish_hold([_core_repo(tmp_path)], deploy, gate, queue_store=store)
    assert gate.statuses[-1][1] == {1: normal[1], 2: _HELD, 3: _HELD}


@pytest.mark.parametrize("local_hold", [True, False])
def test_all_deploy_holds_are_reconciled_before_the_first_paid_review(tmp_path, local_hold):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [2, 3]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("inspect", "docs-repo", 2): _in_progress(),
        ("inspect", "docs-repo", 3): _queue_inspect(review="changes_requested"),
    })
    merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store, max_reviews=0)
    with store.locked() as state:
        state["repositories"]["docs-repo"]["parked"]["3"] = {
            "reason": "changes_requested", "head": "a" * 40, "run": [11, 1],
            "review": [None, "none"], "since": 1000.0,
        }
        store.save(state)
    merge_open_prs._publish_hold(repositories, {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    with store.locked() as state:
        row = state["repositories"]["docs-repo"]
        # The previously active PR vanished during the hold. Its old write
        # must finish on its recorded head, without reserving position 1.
        row["active"]["pr"] = 99
        row["published"]["99"] = ["d" * 40, _HELD]
        if not local_hold:
            for row in state["repositories"].values():
                row.pop("held", None)  # A previous version cleared this too early.
        store.save(state)
    observed = []
    observed_heads = []

    def review(repository, pr, head, base, executor_run, retry_of=None):
        index = next(index for index in reversed(range(len(gate.statuses)))
                     if gate.statuses[index][0] == "docs-repo")
        observed.append(gate.statuses[index][1])
        observed_heads.append(gate.parked_heads[index])
        return {"ok": True, "verdict": "CHANGES_REQUIRED"}

    gate.review = review
    # The setup pass rotated the cursor; this pass must review core-repo first.
    with store.locked() as state:
        state["cursor"] = 0
        store.save(state)
    merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store, max_reviews=1)

    assert observed == [{
        2: "queue=eligible reason=- owner=queue pos=1",
        3: "queue=parked reason=changes_requested owner=author pos=-",
        99: _QUEUE_DONE,
    }], "the second repository must be released while the first review is still running"
    assert observed_heads == [{
        3: "a" * 40, 99: "d" * 40,
    }]
    assert all("held" not in row for row in store.read()["repositories"].values())


@pytest.mark.parametrize("code", ["PR_LIST_FAILED", "EXECUTOR_HOST_REQUIRED", "INTERNAL_ERROR"])
def test_an_unread_listing_retains_its_deploy_hold(tmp_path, code):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "docs-repo", 2): _in_progress(),
    })
    merge_open_prs._publish_hold(repositories, {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    original_candidates = gate.candidates
    gate.candidates = lambda name: ({"ok": False, "code": code} if name == "docs-repo"
                                    else original_candidates(name))
    gate.statuses.clear()

    merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store)

    row = store.read()["repositories"]["docs-repo"]
    assert row["held"] == "-"
    assert row["published"]["2"][1] == _HELD
    assert all(name != "docs-repo" for name, _, _ in gate.statuses)
    if code != "PR_LIST_FAILED":
        assert gate.statuses == [], "an authority or integrity stop must prevent every release"
        assert store.read()["repositories"]["core-repo"]["held"] == "-"


@pytest.mark.parametrize("local_hold", [True, False])
@pytest.mark.parametrize("failure", ["whole", "partial"])
def test_an_unproven_hold_release_is_retried_before_the_next_review(tmp_path, local_hold, failure):
    repository = _core_repo(tmp_path)
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
    })
    merge_open_prs._publish_hold([repository], {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    if not local_hold:
        with store.locked() as state:
            state["repositories"]["core-repo"].pop("held")
            store.save(state)
    gate.responses[("queue_status", "core-repo")] = (
        {"ok": False, "code": "QUEUE_STATUS_UNAVAILABLE"} if failure == "whole"
        else {"ok": True, "published": {"1": None}})

    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    row = store.read()["repositories"]["core-repo"]
    assert row["published"]["1"][1] == _HELD, "failed release must retain its retry evidence"
    assert "held" not in row, "an attempted release invalidates the hold's dedup marker"
    del gate.responses[("queue_status", "core-repo")]
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(review="not_approved")
    observed = []

    def review(repository, pr, head, base, executor_run, retry_of=None):
        observed.append(dict(store.read()["repositories"]["core-repo"]))
        return {"ok": True, "verdict": "CHANGES_REQUIRED"}

    gate.review = review
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)

    assert len(observed) == 1
    assert "held" not in observed[0]
    assert observed[0]["published"]["1"][1] == "queue=active reason=reviewing owner=queue pos=1"


def test_an_interrupted_hold_release_reasserts_the_next_hold_on_the_forge(tmp_path, monkeypatch):
    from git_gate import gate_backend_forgejo
    from git_gate.operations import GitMaintainerGate

    store = _queue_store(tmp_path)
    repository = _core_repo(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
    })
    publisher = GitMaintainerGate(lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publisher, "_refresh_forge_auth", lambda *_args: None)
    context = SimpleNamespace(cwd=tmp_path, policy=repository)
    writes = []
    monkeypatch.setattr(gate_backend_forgejo, "read_pr_state", lambda *_args: subprocess.CompletedProcess(
        [], 0, json.dumps({"head": {"sha": "a" * 40}}), ""))

    def create_status(_runner, _cwd, _repository, head, state, context, description):
        writes.append((head, state, context, description))
        return subprocess.CompletedProcess([], 0, "", "HTTP/1.1 201 Created\r\n")

    monkeypatch.setattr(gate_backend_forgejo, "create_commit_status", create_status)

    def queue_status(repository, descriptions, published):
        return {"ok": True, "published": publisher.publish_queue_statuses(context, descriptions, published)}

    gate.queue_status = queue_status
    merge_open_prs._publish_hold([repository], {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    assert writes == [("a" * 40, "pending", "gate/queue", _HELD)]

    def lost_response(repository, descriptions, published):
        queue_status(repository, descriptions, published)
        raise SystemExit("the process stopped after status publication")

    gate.queue_status = lost_response
    with pytest.raises(SystemExit, match="process stopped"):
        merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert "held" not in row, "a new hold must not skip publication after an uncertain release"
    assert row["published"]["1"][1] == _HELD, "the next normal pass still owes the release"
    assert writes[-1][3] == "queue=eligible reason=- owner=queue pos=1"

    gate.queue_status = queue_status
    merge_open_prs._publish_hold([repository], {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    assert [write[3] for write in writes] == [
        _HELD, "queue=eligible reason=- owner=queue pos=1", _HELD,
    ], "the stale local held pair must not deduplicate the next held POST"
    assert store.read()["repositories"]["core-repo"]["held"] == "-"

    merge_open_prs._publish_hold([repository], {"reason": "DEPLOY_IN_PROGRESS"},
                                 gate, queue_store=store)
    assert len(writes) == 3, "a proven continuing hold still publishes once"


def test_an_unproven_hold_write_is_retried_on_the_next_held_pass(tmp_path, caplog):
    store = _queue_store(tmp_path, repositories=("core-repo",))
    gate = FakeGateApplication({"core-repo": [1]})
    gate.responses[("queue_status", "core-repo")] = {"ok": True, "published": {"1": None}}
    deploy = {"reason": "DEPLOY_IN_PROGRESS"}  # an unreadable marker names no plan

    merge_open_prs._publish_hold([_core_repo(tmp_path)], deploy, gate, queue_store=store)
    assert "held" not in store.read()["repositories"]["core-repo"]

    del gate.responses[("queue_status", "core-repo")]
    merge_open_prs._publish_hold([_core_repo(tmp_path)], deploy, gate, queue_store=store)
    assert [descriptions for _, descriptions, _ in gate.statuses] == [{1: _HELD}, {1: _HELD}]
    assert store.read()["repositories"]["core-repo"]["held"] == "-"


def test_a_hold_never_fails_the_pass_it_rides_on(tmp_path, caplog):
    store = _queue_store(tmp_path, repositories=("core-repo",))

    class Unlisted(FakeGateApplication):
        def candidates(self, repository):
            raise RuntimeError("forge down")

    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    merge_open_prs._publish_hold([_core_repo(tmp_path)], {"reason": "DEPLOY_IN_PROGRESS"},
                                 Unlisted({"core-repo": [1]}), queue_store=store)

    assert [record.message for record in caplog.records if record.levelno >= logging.WARNING] == [
        "automerge hold not published: RuntimeError"]
    with store.locked():
        # A pass holding the lock: the hold retries next minute, silently.
        caplog.clear()
        merge_open_prs._publish_hold([_core_repo(tmp_path)], {"reason": "DEPLOY_IN_PROGRESS"},
                                     FakeGateApplication({"core-repo": [1]}), queue_store=store)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_automerge_skips_on_a_fresh_marker_it_cannot_parse(monkeypatch, tmp_path, capsys):
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content="{truncated")

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == []
    assert json.loads(capsys.readouterr().out)["repositories"][0] == {
        "repository": "core-repo", "skipped": True, "reason": "DEPLOY_IN_PROGRESS"}


def test_automerge_runs_past_a_stale_marker_and_warns(monkeypatch, tmp_path, capsys, caplog):
    age = merge_open_prs.DEPLOY_MARKER_TTL_SECONDS + 60
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=_MARKER_JSON,
                                        age_seconds=age)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    before = merge_open_prs.DEPLOY_MARKER_PATH.read_bytes()

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == [["core-repo", "docs-repo"]]
    capsys.readouterr()
    warnings = [record.message for record in caplog.records
                if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "stale deploy marker" in warnings[0]
    assert str(merge_open_prs.DEPLOY_MARKER_PATH) in warnings[0]
    assert merge_open_prs.DEPLOY_MARKER_PATH.read_bytes() == before, "read-only"


def _iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat(
        timespec="seconds")


def test_automerge_runs_past_a_failed_deploy_marker_and_warns(
        monkeypatch, tmp_path, capsys, caplog):
    """a deploy that stopped without COMPLETE keeps its marker for
    the other origin, but it is not running, so automerge does not pause."""
    content = json.dumps({"schema": "deploy-marker.v1", "origin": "dev-host",
                          "plan_id": "0123abcd", "state": "failed",
                          "started_at": _iso(3600), "claimed_at": _iso(60),
                          "failed_at": "2020-01-01T07:00:12+00:00"})
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=content)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    before = merge_open_prs.DEPLOY_MARKER_PATH.read_bytes()

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == [["core-repo", "docs-repo"]]
    capsys.readouterr()
    warnings = [record.message for record in caplog.records
                if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "records a failed deploy" in warnings[0]
    for detail in ("dev-host", "0123abcd", "2020-01-01T07:00:12+00:00",
                   str(merge_open_prs.DEPLOY_MARKER_PATH)):
        assert detail in warnings[0]
    assert merge_open_prs.DEPLOY_MARKER_PATH.read_bytes() == before, "read-only"


def test_automerge_ages_a_running_marker_from_its_claim_not_its_mtime(
        monkeypatch, tmp_path, capsys, caplog):
    """Livelock: a marker rewritten moments ago (fresh mtime) whose
    claim is older than the TTL is stale; a fresh claim pauses."""
    def marker(claimed_at: str) -> str:
        return json.dumps({"schema": "deploy-marker.v1", "origin": "dev-host",
                           "plan_id": "0123abcd", "state": "running",
                           "started_at": "2020-01-01T00:00:00+00:00",
                           "claimed_at": claimed_at})

    old = merge_open_prs.DEPLOY_MARKER_TTL_SECONDS + 60
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=marker(_iso(old)),
                                        age_seconds=60)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    assert merge_open_prs.main(["--automerge", "--json"]) == 0
    assert passes == [["core-repo", "docs-repo"]]
    capsys.readouterr()
    warnings = [record.message for record in caplog.records
                if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "stale deploy marker" in warnings[0]

    claimed = _iso(60)
    merge_open_prs.DEPLOY_MARKER_PATH.write_text(marker(claimed))
    stamp = time.time() - old
    os.utime(merge_open_prs.DEPLOY_MARKER_PATH, (stamp, stamp))
    passes.clear()
    assert merge_open_prs.main(["--automerge", "--json"]) == 0
    assert passes == []
    deploy = json.loads(capsys.readouterr().out)["repositories"][0]["deploy"]
    assert deploy == {"origin": "dev-host", "plan_id": "0123abcd", "state": "running",
                      "started_at": "2020-01-01T00:00:00+00:00", "claimed_at": claimed}


def _applied_marker(**overrides) -> str:
    return json.dumps({"schema": "deploy-marker.v1", "origin": "dev-host",
                       "plan_id": "0123abcd", "state": "running",
                       "started_at": _iso(3600), "claimed_at": _iso(60),
                       "applied_at": _iso(30), **overrides})


def test_automerge_resumes_once_a_running_marker_records_applied_source(
        monkeypatch, tmp_path, capsys, caplog):
    """The steps after the source apply and the Python environment
    reconcile leave the executor's code alone, so the still-running
    marker no longer pauses a fresh pass."""
    content = _applied_marker(applied_at="2020-01-01T00:30:00+00:00")
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=content)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    before = merge_open_prs.DEPLOY_MARKER_PATH.read_bytes()

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == [["core-repo", "docs-repo"]]
    capsys.readouterr()
    notes = [record.message for record in caplog.records
             if "records applied source" in record.message]
    assert len(notes) == 1
    for detail in ("dev-host", "0123abcd", "2020-01-01T00:30:00+00:00",
                   "later deploy steps still running"):
        assert detail in notes[0]
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert merge_open_prs.DEPLOY_MARKER_PATH.read_bytes() == before, "read-only"


@pytest.mark.parametrize("overrides", [
    {"applied_at": None}, {"applied_at": ""}, {"applied_at": 1577836800},
    {"applied_at": ["2020-01-01T00:30:00+00:00"]}, {"applied_at": "soon"},
    {"applied_at": "2020-01-01T00:30:00"},
    {"schema": "deploy-marker.v0"}, {"state": None}, {"state": "orphaned"},
], ids=["null", "empty", "number", "list", "not-iso", "no-offset", "other-schema",
        "no-state", "unknown-state"])
def test_automerge_keeps_pausing_unless_applied_at_is_a_timestamp_on_a_running_marker(
        monkeypatch, tmp_path, capsys, overrides):
    content = json.loads(_applied_marker())
    content.update(overrides)
    content = {key: value for key, value in content.items() if value is not None}
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=json.dumps(content))

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == []
    assert json.loads(capsys.readouterr().out)["repositories"][0]["reason"] == (
        "DEPLOY_IN_PROGRESS")


def test_automerge_pauses_on_a_running_marker_without_applied_at(
        monkeypatch, tmp_path, capsys):
    content = json.loads(_applied_marker())
    del content["applied_at"]
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=json.dumps(content))

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == []
    assert json.loads(capsys.readouterr().out)["repositories"][0]["reason"] == (
        "DEPLOY_IN_PROGRESS")


def test_a_failed_marker_that_recorded_applied_source_still_warns_as_failed(
        monkeypatch, tmp_path, capsys, caplog):
    content = _applied_marker(state="failed", failed_at="2020-01-01T07:00:12+00:00")
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=content)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == [["core-repo", "docs-repo"]]
    capsys.readouterr()
    warnings = [record.message for record in caplog.records
                if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "records a failed deploy" in warnings[0]


@pytest.mark.parametrize("action", ["merge", "refresh", "review", "train_build", "train_member_merge"])
def test_remote_deploy_intent_during_inspection_prevents_the_next_action(
        tmp_path, monkeypatch, action):
    marker = tmp_path / "marker.json"
    monkeypatch.setattr(merge_open_prs, "DEPLOY_MARKER_PATH", marker)
    monkeypatch.setattr(merge_open_prs, "probe_deploy_lock", lambda: ("free", ""))
    store = _queue_store(tmp_path)
    repository = _core_repo(tmp_path)
    if action in {"train_build", "train_member_merge"}:
        gate = _train_gate(numbers=(1, 2))
        repository = _train_repository(tmp_path)
        if action == "train_member_merge":
            _train_pass(tmp_path, gate, store)
            gate.calls.clear()
        inspected_name = "train_inspect" if action == "train_member_merge" else "inspect"
    else:
        result = (_stale_base() if action == "refresh" else
                  _queue_inspect(review="missing" if action == "review" else "approved"))
        gate = FakeGateApplication({"core-repo": [1]}, responses={
            ("inspect", "core-repo", 1): result})
        inspected_name = "inspect"
    original = getattr(gate, inspected_name)

    def inspect(*args, **kwargs):
        observed = original(*args, **kwargs)
        marker.write_text(json.dumps({
            "schema": "deploy-marker.v1", "state": "running", "origin": "remote-origin",
            "plan_id": "remote-plan", "claimed_at": datetime.now(timezone.utc).isoformat(),
        }))
        return observed

    setattr(gate, inspected_name, inspect)
    summaries = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)

    assert summaries[0]["hold"]["reason"] == "DEPLOY_WAITING"
    assert summaries[0]["ok"] is True and summaries[0]["skipped"] is True
    assert not any(call[0] in {"merge", "refresh", "review", "train_build",
                              "train_member_merge", "train_close"} for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["active"] is not None


def test_automerge_runs_without_a_marker(monkeypatch, tmp_path, capsys, caplog):
    passes = _automerge_cli_with_marker(monkeypatch, tmp_path, content=None)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert passes == [["core-repo", "docs-repo"]]
    capsys.readouterr()
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_automerge_cli_reports_candidate_error_and_continues(monkeypatch, tmp_path, capsys):
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}

    class BrokenCandidate(FakeGateApplication):
        def inspect(self, name, pr):
            if name == "core-repo":
                raise KeyError("candidate")
            return super().inspect(name, pr)

    gate = BrokenCandidate({"core-repo": [1], "docs-repo": [2]})
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: gate)
    assert merge_open_prs.main(["--automerge", "--json"]) == 0
    summaries = json.loads(capsys.readouterr().out)["repositories"]
    assert summaries[0]["results"][0]["reason"] == "CANDIDATE_ERROR:KeyError"
    assert summaries[1]["results"] == [{"pr": 2, "outcome": "merged"}]


@pytest.mark.parametrize("code", [
    "REVIEW_SUPERVISOR_UNAVAILABLE", "REVIEW_SUPERVISOR_INVALID",
])
def test_automerge_cli_stops_after_shared_review_supervisor_failure(
    monkeypatch, tmp_path, capsys, code
):
    repositories = {
        name: merge_open_prs.Repository(
            name, tmp_path / name, "forgejo", f"acme/{name}"
        )
        for name in ("core-repo", "docs-repo")
    }
    application = FakeGateApplication(
        {"core-repo": [42, 43], "docs-repo": [7]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "ci_state": "green",
                "review_state": "not_approved",
            },
            ("review", "core-repo", 42): {"ok": False, "code": code},
            # Not approved: an approved PR 43 would be elected ahead of PR 42.
            ("inspect", "core-repo", 43): _queue_inspect(review="not_approved"),
        },
    )
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)

    assert merge_open_prs.main(["--automerge", "--json"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["repositories"][0]["results"] == [{
        "pr": 42,
        "outcome": "retained",
        "reason": code,
        "authority_integrity_failed": True,
    }]
    assert payload["repositories"][1]["results"] == []
    # Selection read PR 43 to rank it; the stop ended every later read.
    assert not any(
        call[0] == "inspect" and (call[1], call[2]) not in {("core-repo", 42), ("core-repo", 43)}
        for call in application.calls
    )


def _automerge_errors(monkeypatch, tmp_path, caplog, application, capsys):
    """Run `main --automerge --json`; return its exit code, payload and ERROR lines."""
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    code = merge_open_prs.main(["--automerge", "--json"])
    payload = json.loads(capsys.readouterr().out)
    errors = [record.message for record in caplog.records if record.levelno == logging.ERROR]
    return code, payload, errors


def test_a_repository_whose_listing_raises_logs_one_error_and_the_other_still_merges(
    monkeypatch, tmp_path, caplog, capsys
):
    """Error-level lines are the alertable signal: this exit-2 cause must log one."""
    class BrokenListing(FakeGateApplication):
        def candidates(self, repository):
            if repository == "core-repo":
                raise ValueError("repository must be a non-empty catalog alias")
            return super().candidates(repository)

    code, payload, errors = _automerge_errors(
        monkeypatch, tmp_path, caplog, BrokenListing({"docs-repo": [7]}), capsys)

    assert code == 2
    assert errors == ["automerge repository error: core-repo PR_LIST_ERROR:ValueError"]
    assert payload["repositories"][0]["error"] == "PR_LIST_ERROR:ValueError"
    assert payload["repositories"][1]["results"] == [{"pr": 7, "outcome": "merged"}]


@pytest.mark.parametrize("failure", [
    OSError("catalog unreadable"),
    yaml.YAMLError("catalog malformed"),
    RuntimeError("automerge selection names an undeclared repository"),
])
def test_an_unloadable_catalog_logs_one_error_before_any_pass(
    monkeypatch, caplog, capsys, failure
):
    """The FATAL stderr line reaches the service log at info, so an error-level record is needed too."""
    def load_repositories():
        raise failure

    monkeypatch.setattr(merge_open_prs, "load_repositories", load_repositories)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 2
    assert [record.message for record in caplog.records
            if record.levelno == logging.ERROR] == [f"automerge: {failure}"]
    assert f"FATAL: {failure}" in capsys.readouterr().err


@pytest.mark.parametrize(("refusal", "expected"), [
    ({"ok": False, "code": "FORGE_UNREACHABLE"}, "FORGE_UNREACHABLE"),
    ({"ok": False}, "PR_LIST_FAILED"),
])
def test_a_refused_listing_logs_one_error_with_the_gate_code(
    monkeypatch, tmp_path, caplog, capsys, refusal, expected
):
    application = FakeGateApplication({"docs-repo": [7]})
    original = application.candidates
    application.candidates = lambda repository: (
        refusal if repository == "core-repo" else original(repository))

    code, payload, errors = _automerge_errors(monkeypatch, tmp_path, caplog, application, capsys)

    assert code == 2
    assert errors == [f"automerge repository error: core-repo {expected}"]
    assert payload["repositories"][1]["results"] == [{"pr": 7, "outcome": "merged"}]


def test_an_integrity_stop_on_a_listing_is_logged_once_as_an_integrity_stop(
    monkeypatch, tmp_path, caplog, capsys
):
    application = FakeGateApplication({"core-repo": [], "docs-repo": [7]})
    application.candidates = lambda repository: {
        "ok": False, "code": "GATE_LOCK_DIR_UNSAFE"}

    code, payload, errors = _automerge_errors(monkeypatch, tmp_path, caplog, application, capsys)

    assert code == 2
    assert payload["repositories"][0]["error"] == "GATE_LOCK_DIR_UNSAFE"
    assert errors == ["automerge integrity stop: core-repo GATE_LOCK_DIR_UNSAFE",
                      "automerge integrity stop: docs-repo GATE_LOCK_DIR_UNSAFE"]


@pytest.mark.parametrize("failure", ["runtime", "queue-state"])
def test_a_pass_level_failure_keeps_its_one_existing_error_line(
    monkeypatch, tmp_path, caplog, capsys, failure
):
    from git_gate.automerge_queue import QueueStateError

    def failing(*args, **kwargs):
        raise RuntimeError("pass failed") if failure == "runtime" else QueueStateError(
            "AUTOMERGE_STATE_INVALID")

    monkeypatch.setattr(merge_open_prs, "run_automerge_pass", failing)
    code, payload, errors = _automerge_errors(
        monkeypatch, tmp_path, caplog, FakeGateApplication({}), capsys)

    assert code == 2
    expected = "pass failed" if failure == "runtime" else "AUTOMERGE_STATE_INVALID"
    assert [summary["error"] for summary in payload["repositories"]] == [expected, expected]
    assert errors == [f"automerge: {expected}"]


def test_a_candidate_level_integrity_stop_adds_no_repository_error_line(
    monkeypatch, tmp_path, caplog, capsys
):
    application = FakeGateApplication(
        {"core-repo": [42], "docs-repo": []},
        responses={
            ("inspect", "core-repo", 42): _queue_inspect(review="not_approved"),
            ("review", "core-repo", 42): {
                "ok": False, "code": "REVIEW_SUPERVISOR_UNAVAILABLE"},
        })

    code, _payload, errors = _automerge_errors(monkeypatch, tmp_path, caplog, application, capsys)

    assert code == 2
    # The two lines this stop has always left; no third, repository-level one.
    assert errors == ["automerge review failed: core-repo#42 REVIEW_SUPERVISOR_UNAVAILABLE",
                      "automerge integrity stop: core-repo#42 REVIEW_SUPERVISOR_UNAVAILABLE"]


class _AcceptingOperations:
    """The gate operations behind the gate API, answering every read."""

    def candidates(self, repository):
        return {"ok": True, "pull_requests": []}

    def inspect(self, repository, pull_request):
        return {"ok": True}


def test_every_automerge_alias_passes_the_gate_and_review_watch_validators():
    """A catalog alias that either validator refuses fails every pass,
    so check the real catalog, not a list."""
    from gate_server.application import GateApplication
    from git_gate.catalog import policy_for

    records = merge_open_prs.load_repositories()
    repositories = merge_open_prs.resolve_selection(
        merge_open_prs.automerge_selection(records), records)
    aliases = [repository.name for repository in repositories]

    assert aliases
    assert any("." in alias for alias in aliases)  # example.org
    monitor_spec = importlib.util.spec_from_file_location(
        "ci_status_monitor_alias_contract",
        REPO_ROOT / "tools-config" / "scripts" / "harness_ci_status.py")
    assert monitor_spec is not None and monitor_spec.loader is not None
    monitor = importlib.util.module_from_spec(monitor_spec)
    monitor_spec.loader.exec_module(monitor)
    application = GateApplication(_AcceptingOperations())
    # The validators are live: a malformed alias is refused by both.
    with pytest.raises(ValueError):
        application.candidates("a..b")
    assert monitor._review_target(
        {"ci_evidence": {"repository": "acme/a..b", "pull_request": 1}}) is None

    for repository in repositories:
        alias = repository.name
        assert policy_for(alias).name.rsplit("/", 1)[-1] == alias
        assert application.candidates(alias)["ok"] is True
        assert application.inspect(alias, 1)["ok"] is True
        assert monitor._review_target(
            {"ci_evidence": {"repository": repository.forge_repository,
                             "pull_request": 1}}) == (alias, 1)


def test_max_reviews_requires_automerge(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--max-reviews", "1"])
    assert exc.value.code == 2
    assert "--max-reviews requires --automerge" in capsys.readouterr().err


def test_max_reviews_per_repo_requires_automerge(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--max-reviews-per-repo", "1"])
    assert exc.value.code == 2
    assert "--max-reviews-per-repo requires --automerge" in capsys.readouterr().err


def test_max_reviews_per_repo_rejects_non_positive_values(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--automerge", "--max-reviews-per-repo", "0"])
    assert exc.value.code == 2


@pytest.mark.parametrize("argv,expected", [
    ([], {}),
    (["--max-reviews", "3"], {"max_reviews": 3, "max_reviews_per_repo": 1}),
    (["--max-reviews", "3", "--max-reviews-per-repo", "2"],
     {"max_reviews": 3, "max_reviews_per_repo": 2}),
    (["--max-reviews-per-repo", "2"], {"max_reviews_per_repo": 2}),
    (["--pass-deadline-seconds", "3000"], {"pass_deadline_seconds": 3000}),
])
def test_max_reviews_per_repo_defaults_to_one_only_on_a_bounded_pass(
        monkeypatch, tmp_path, argv, expected):
    """The unbounded timer pass keeps no per-repository cap by default."""
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    seen = []

    def ran(selected, **kwargs):
        seen.append(kwargs)
        return [{"repository": repository.name, "results": []} for repository in selected]

    monkeypatch.setattr(merge_open_prs, "run_automerge_pass", ran)
    assert merge_open_prs.main(["--automerge", "--json", *argv]) == 0
    assert seen == [expected]


def test_executor_run_requires_automerge(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--executor-run", "executor:acme/core-repo:1"])
    assert exc.value.code == 2
    assert "--executor-run requires --automerge" in capsys.readouterr().err


def _automerge_main_kwargs(monkeypatch, tmp_path, argv):
    """Run `main(["--automerge", "--json", *argv])`; return each pass's kwargs."""
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_deploy_in_progress", lambda: None)
    seen = []

    def ran(selected, **kwargs):
        seen.append(kwargs)
        return [{"repository": repository.name, "results": []} for repository in selected]

    monkeypatch.setattr(merge_open_prs, "run_automerge_pass", ran)
    assert merge_open_prs.main(["--automerge", "--json", *argv]) == 0
    return seen


@pytest.mark.parametrize(("environment", "argv", "expected"), [
    # The CI adapter's fixed environment carries the deadline.
    ("3000", [], {"pass_deadline_seconds": 3000}),
    # An explicit option wins over the environment.
    ("3000", ["--pass-deadline-seconds", "1200"], {"pass_deadline_seconds": 1200}),
    (None, [], {}),
])
def test_automerge_takes_its_pass_deadline_default_from_the_environment(
        monkeypatch, tmp_path, environment, argv, expected):
    monkeypatch.delenv("GATE_AUTOMERGE_PASS_DEADLINE_SECONDS", raising=False)
    if environment is not None:
        monkeypatch.setenv("GATE_AUTOMERGE_PASS_DEADLINE_SECONDS", environment)
    assert _automerge_main_kwargs(monkeypatch, tmp_path, argv) == [expected]


@pytest.mark.parametrize("environment", ["soon", "0", "-5", ""])
def test_a_malformed_pass_deadline_environment_is_ignored_with_a_warning(
        monkeypatch, tmp_path, caplog, environment):
    """Never a non-zero exit: the pass runs as it did before the variable."""
    monkeypatch.setenv("GATE_AUTOMERGE_PASS_DEADLINE_SECONDS", environment)
    with caplog.at_level(logging.WARNING):
        assert _automerge_main_kwargs(monkeypatch, tmp_path, []) == [{}]
    assert [record.getMessage() for record in caplog.records
            if "GATE_AUTOMERGE_PASS_DEADLINE_SECONDS" in record.getMessage()] == [
        f"automerge: ignoring GATE_AUTOMERGE_PASS_DEADLINE_SECONDS={environment!r}: "
        "not a positive integer; the pass runs without a deadline"]


def test_the_pass_deadline_environment_is_read_only_under_automerge(
        monkeypatch, tmp_path, caplog):
    """An interactive merge neither honours nor warns about it."""
    monkeypatch.setenv("GATE_AUTOMERGE_PASS_DEADLINE_SECONDS", "soon")
    repositories = {"core-repo": merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "run_one", lambda repository, args: {
        "repository": repository.name, "notes": [], "results": []})
    with caplog.at_level(logging.WARNING):
        assert merge_open_prs.main(["--json", "core-repo"]) == 0
    assert not any("GATE_AUTOMERGE_PASS_DEADLINE_SECONDS" in record.getMessage()
                   for record in caplog.records)


def test_pass_deadline_requires_automerge_and_a_positive_value(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--pass-deadline-seconds", "3000"])
    assert exc.value.code == 2
    assert "--pass-deadline-seconds requires --automerge" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--automerge", "--pass-deadline-seconds", "0"])
    assert exc.value.code == 2


def test_level_prefix_requires_automerge(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--level-prefix"])
    assert exc.value.code == 2
    assert "--level-prefix requires --automerge" in capsys.readouterr().err


def test_max_reviews_rejects_non_positive_values(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--automerge", "--max-reviews", "0"])
    assert exc.value.code == 2


def test_executor_run_rejects_malformed_prefix(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--automerge", "--executor-run", "bad value"])
    assert exc.value.code == 2


def test_max_reviews_stops_the_pass_after_n_finished_reviews(tmp_path):
    """An already approved PR needs no paid review: the readiness order merges it first."""
    application = _ApprovedAfterReview(
        {"core-repo": [1, 2, 3, 4]},
        responses={
            # #1 needs no review: already approved, merges straight away.
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "approved",
            },
            # #2 and #3 each invoke review once.
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
            ("inspect", "core-repo", 3): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=2,
    )[0]

    # #4 is approved too, so it is elected ahead of #2 and #3, which need a review.
    assert [record["pr"] for record in result["results"]] == [1, 4, 2, 3]
    assert result["results"][1] == {"pr": 4, "outcome": "merged"}
    assert [call[2] for call in application.calls if call[0] == "review"] == [2, 3]


def test_max_reviews_ignores_an_instant_refusal_but_keeps_the_active_pr(tmp_path):
    """A busy attempt lock spends no budget, but cannot let #2 overtake."""
    application = FakeGateApplication(
        {"core-repo": [1, 2]},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 1): {"ok": False, "code": "REVIEW_ATTEMPT_BUSY"},
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 2): {
                "ok": True, "verdict": "CHANGES_REQUIRED",
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1,
    )[0]

    # A busy review of the active PR is not a CI wait: no review-ahead.
    # Selection read #2 to rank it; no write was made for it.
    assert [record["pr"] for record in result["results"]] == [1]
    assert result["results"][0]["reason"] == "REVIEW_ATTEMPT_BUSY"
    assert not any(call[0] in {"review", "refresh", "merge"} and call[2] == 2
                   for call in application.calls)


def test_max_reviews_stops_after_one_started_review_even_when_it_fails(tmp_path):
    """A rejected paid review spends the budget and #2 waits for a new pass."""
    application = FakeGateApplication(
        {"core-repo": [1, 2]},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
            ("review", "core-repo", 1): {
                "ok": True, "code": "REVIEW_PUBLISHED", "verdict": "CHANGES_REQUIRED",
            },
            # #1 now parks in its own pass (changes_requested), so the loop
            # reaches #2; it needs a review the spent budget must refuse.
            ("inspect", "core-repo", 2): {
                "ok": True, "head_sha": "c" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1,
    )[0]

    assert [record["pr"] for record in result["results"]][:1] == [1]
    assert not any(call[0] in {"review", "merge"} and call[2] == 2
                   for call in application.calls)


def test_max_reviews_default_pass_behavior_is_unaffected(tmp_path):
    application = FakeGateApplication({"core-repo": [42, 43]})
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert [record["outcome"] for record in result["results"]] == ["merged", "merged"]
    assert result["results"] == [{"pr": 42, "outcome": "merged"},
                                  {"pr": 43, "outcome": "merged"}]


def test_executor_run_prefix_overrides_the_default(tmp_path):
    application = FakeGateApplication(
        {"core-repo": [42]},
        responses={
            ("inspect", "core-repo", 42): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    merge_open_prs.run_automerge_pass(
        [repository], application,
        executor_run_prefix="executor:acme/core-repo:9",
    )

    review_calls = [call for call in application.calls if call[0] == "review"]
    assert review_calls[0][5] == "executor:acme/core-repo:9:core-repo:42"
    assert review_calls[0][6] is None


def test_ordinary_json_still_prints_summary(monkeypatch, tmp_path, capsys):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path / "core-repo", "forgejo", "acme/core-repo"
    )

    class EmptyForge:
        def open_pulls(self):
            return []

    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: {"core-repo": repository})
    monkeypatch.setattr(merge_open_prs, "forge_for", lambda _repository: EmptyForge())

    assert merge_open_prs.main(["--json", "core-repo"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "repositories": [
            {"repository": "core-repo", "notes": [], "results": []}
        ]
    }


def test_automerge_records_and_logs_a_reused_clean_refresh_approval(
    monkeypatch, tmp_path, capsys, caplog
):
    """an approval carried across a clean refresh spends no review
    slot, and the journal says so.

    The automerge pass has a bounded review budget per tick, so the
    saving only exists if the pass genuinely skips `review` -- asserted here
    rather than inferred from the outcome. The `automerge result` line is the
    single source of truth for why a merge proceeded without a fresh review
    round, so the basis and the reviewed pair have to survive into it.
    """
    reused = {
        "ok": True,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "ci_state": "green",
        "review_state": "approved",
        "approval_basis": "clean_refresh",
        "reviewed_head_sha": "c" * 40,
        "reviewed_base_sha": "d" * 40,
    }
    repositories = {
        "core-repo": merge_open_prs.Repository(
            "core-repo", tmp_path / "core-repo", "forgejo", "acme/core-repo"
        ),
        "docs-repo": merge_open_prs.Repository(
            "docs-repo", tmp_path / "docs-repo", "forgejo",
            "acme/docs-repo",
        ),
    }
    application = FakeGateApplication(
        {"core-repo": [42], "docs-repo": []},
        responses={("inspect", "core-repo", 42): [reused, reused]},
    )
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    assert not [call for call in application.calls if call[0] == "review"]
    assert [call[:3] for call in application.calls if call[0] == "merge"] == [
        ("merge", "core-repo", 42)
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["repositories"][0]["results"] == [
        {
            "pr": 42,
            "outcome": "merged",
            "approval_basis": "clean_refresh",
            "reviewed_head_sha": "c" * 40,
            "reviewed_base_sha": "d" * 40,
        }
    ]
    assert '"approval_basis":"clean_refresh"' in caplog.text


def test_automerge_leaves_an_exact_approval_record_unadorned(
    monkeypatch, tmp_path, capsys, caplog
):
    """The contrary state for the test above: an ordinary exact-head approval
    still merges, and its journal record gains neither a reviewed pair nor a
    `clean_refresh` basis."""
    exact = {
        "ok": True,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "ci_state": "green",
        "review_state": "approved",
        "approval_basis": "exact",
    }
    repositories = {
        "core-repo": merge_open_prs.Repository(
            "core-repo", tmp_path / "core-repo", "forgejo", "acme/core-repo"
        ),
        "docs-repo": merge_open_prs.Repository(
            "docs-repo", tmp_path / "docs-repo", "forgejo",
            "acme/docs-repo",
        ),
    }
    application = FakeGateApplication(
        {"core-repo": [42], "docs-repo": []},
        responses={("inspect", "core-repo", 42): [exact, exact]},
    )
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)

    assert merge_open_prs.main(["--automerge", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["repositories"][0]["results"] == [
        {"pr": 42, "outcome": "merged", "approval_basis": "exact"}
    ]
    assert "clean_refresh" not in caplog.text


def test_review_attempt_started_excludes_a_reused_approval():
    """`ok` alone cannot separate these two, so the code is what decides.

    A published review and a reused approval both return `ok`. Only the
    first launched anything, and only the first may spend `--max-reviews`.
    """
    assert merge_open_prs._review_attempt_started({"ok": True}) is True
    assert merge_open_prs._review_attempt_started(
        {"ok": True, "code": "REVIEW_NATIVE_REUSED"}
    ) is False
    assert merge_open_prs._review_attempt_started(
        {"ok": True, "code": "REVIEW_APPROVAL_REUSED"}
    ) is False


def test_a_reused_approval_merges_without_spending_the_review_budget(tmp_path):
    """Clean-refresh reuse end to end through the pass, including the budget.

    The first `inspect` cannot see the freshly pushed head --
    `inspect_pr_readonly_by_index` does not fetch -- so the PR reads
    `not_approved` and the pass calls `review`. The executor fetches, answers
    `REVIEW_APPROVAL_REUSED` without dispatching a model, and the re-inspect
    then sees the approval because the objects are local. The merge proceeds,
    the journal record names the reused pair, and the review slot is still
    free for the next candidate -- asserted by PR 2 getting its review under
    `--max-reviews 1`.
    """
    not_approved = {
        "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
        "ci_state": "green", "review_state": "not_approved",
    }
    reused = {
        "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
        "ci_state": "green", "review_state": "approved",
        "approval_basis": "clean_refresh",
        "reviewed_head_sha": "c" * 40, "reviewed_base_sha": "d" * 40,
    }
    application = FakeGateApplication(
        {"core-repo": [1, 2]},
        responses={
            ("inspect", "core-repo", 1): [not_approved, reused],
            ("review", "core-repo", 1): {
                "ok": True, "code": "REVIEW_APPROVAL_REUSED",
                "verdict": "ACCEPT_STATIC_CHANGE",
                "approval_basis": "clean_refresh",
                "reviewed_head_sha": "c" * 40, "reviewed_base_sha": "d" * 40,
            },
            ("inspect", "core-repo", 2): not_approved,
            ("review", "core-repo", 2): {"ok": True, "verdict": "CHANGES_REQUIRED"},
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass(
        [repository], application, max_reviews=1,
    )[0]

    assert result["results"][0] == {
        "pr": 1,
        "outcome": "merged",
        "approval_basis": "clean_refresh",
        "reviewed_head_sha": "c" * 40,
        "reviewed_base_sha": "d" * 40,
    }
    assert [call[:3] for call in application.calls if call[0] == "merge"] == [
        ("merge", "core-repo", 1)
    ]
    # The reuse did not consume the single review slot, so PR 2 was still
    # reviewed. Without the `_review_attempt_started` exclusion the pass would
    # have stopped after PR 1 and this list would be one entry long.
    assert [call[:3] for call in application.calls if call[0] == "review"] == [
        ("review", "core-repo", 1), ("review", "core-repo", 2)
    ]
    assert [record["pr"] for record in result["results"]] == [1, 2]
    assert result["results"][1]["reason"] == "CHANGES_REQUIRED"


def test_the_executor_basis_survives_a_re_inspection_that_lost_it(tmp_path):
    """The journal keeps the reused pair even when the re-inspect cannot.

    The re-inspection goes through the non-fetching read, so on a host that
    has since lost the objects it can report `approved` without repeating the
    basis. The executor is the one caller that certainly had them, so its
    answer stands in rather than the journal falling silent about why this
    merge needed no review.
    """
    approved_without_basis = {
        "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
        "ci_state": "green", "review_state": "approved",
    }
    application = FakeGateApplication(
        {"core-repo": [1]},
        responses={
            ("inspect", "core-repo", 1): [
                {**approved_without_basis, "review_state": "not_approved"},
                approved_without_basis,
            ],
            ("review", "core-repo", 1): {
                "ok": True, "code": "REVIEW_APPROVAL_REUSED",
                "verdict": "ACCEPT_STATIC_CHANGE",
                "approval_basis": "clean_refresh",
                "reviewed_head_sha": "c" * 40, "reviewed_base_sha": "d" * 40,
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert result["results"][0]["outcome"] == "merged"
    assert result["results"][0]["approval_basis"] == "clean_refresh"
    assert result["results"][0]["reviewed_head_sha"] == "c" * 40


def test_a_reused_approval_that_the_re_inspection_refuses_is_not_merged(tmp_path):
    """Fail closed: the re-inspection is still the merge decision.

    If the objects are not local for the re-inspect, `review_state` stays
    `not_approved` and the PR is retained. The executor's answer records why
    a review was skipped; it never becomes the merge authority by itself.
    """
    application = FakeGateApplication(
        {"core-repo": [1]},
        responses={
            ("inspect", "core-repo", 1): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved",
                "blocking_reason": "PR_REVIEW_NOT_APPROVED",
            },
            ("review", "core-repo", 1): {
                "ok": True, "code": "REVIEW_APPROVAL_REUSED",
                "verdict": "ACCEPT_STATIC_CHANGE",
                "approval_basis": "clean_refresh",
                "reviewed_head_sha": "c" * 40, "reviewed_base_sha": "d" * 40,
            },
        },
    )
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"
    )

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert result["results"][0]["outcome"] == "retained"
    assert result["results"][0]["reason"] == "PR_REVIEW_NOT_APPROVED"
    assert not [call for call in application.calls if call[0] == "merge"]


# ------------------------------------ product-pr: uniform merge


def _product_repository(tmp_path):
    return merge_open_prs.Repository(
        "billing-repo", tmp_path, "forgejo", "acme/billing-repo",
        gate_profile="product-pr",
    )


def test_product_pull_request_is_reviewed_and_merged_through_the_serial_queue(tmp_path):
    """A product PR takes the same review-then-merge path as a platform PR.

    It is selected through the persisted queue, reviewed on its exact head,
    re-inspected, and merged at that head with outcome `merged`.
    """
    store = _queue_store(tmp_path, repositories=("billing-repo",))
    application = FakeGateApplication(
        {"billing-repo": [12]},
        responses={("inspect", "billing-repo", 12): [
            _queue_inspect(review="not_approved"),
            _queue_inspect(review="approved"),
        ]},
    )

    result = merge_open_prs.run_automerge_pass(
        [_product_repository(tmp_path)], application, queue_store=store)[0]

    assert [call[0] for call in application.calls] == [
        "candidates", "inspect", "review", "inspect", "merge",
    ]
    assert application.calls[-1] == ("merge", "billing-repo", 12, "a" * 40, "b" * 40)
    assert result["results"] == [{"pr": 12, "outcome": "merged"}]
    assert store.read()["repositories"]["billing-repo"] == {
        "active": None, "parked": {}, "published": {}, "agent_streak": 1}


def test_an_already_approved_product_pull_request_merges_without_review(tmp_path):
    application = FakeGateApplication({"billing-repo": [12]})

    result = merge_open_prs.run_automerge_pass([_product_repository(tmp_path)], application)[0]

    assert [call[0] for call in application.calls] == [
        "candidates", "inspect", "inspect", "merge",
    ]
    assert result["results"] == [{"pr": 12, "outcome": "merged"}]


def test_a_refused_product_merge_is_retained_with_the_gate_code(tmp_path):
    """A forge that refuses the merge (e.g. branch protection) fails closed."""
    application = FakeGateApplication(
        {"billing-repo": [12]},
        responses={("merge", "billing-repo", 12): {"ok": False, "code": "PR_MERGE_FAILED"}},
    )

    result = merge_open_prs.run_automerge_pass([_product_repository(tmp_path)], application)[0]

    assert result["results"] == [
        {"pr": 12, "outcome": "retained", "reason": "PR_MERGE_FAILED"}
    ]
    assert result["authority_integrity_failed"] is False


def test_automerge_selection_appends_product_repositories_after_the_defaults(tmp_path):
    """`--automerge` widens; the interactive default selection does not.

    `DEFAULT_REPOSITORIES` also drives the bare merge command and the
    interactive menu, so a product repository joining the automerge pass must
    not join those.
    """
    fixture = tmp_path / "repos.yaml"
    fixture.write_text(
        "canonical_repos:\n"
        "  - name: core-repo\n"
        "    remote: git@forge.example.org:acme/core-repo.git\n"
        "    writable_forge: forgejo\n"
        "    deploy_path: /tmp/core-repo\n"
        "  - name: docs-repo\n"
        "    remote: git@forge.example.org:acme/docs-repo.git\n"
        "    writable_forge: forgejo\n"
        "    deploy_path: /tmp/docs-repo\n"
        "standalone_repos:\n"
        "  - name: shop-repo\n"
        "    remote: git@forge.example.org:acme/shop-repo.git\n"
        "    writable_forge: forgejo\n"
        "    deploy_path: /tmp/shop-repo\n"
        "  - name: billing-repo\n"
        "    remote: git@forge.example.org:acme/billing-repo.git\n"
        "    writable_forge: forgejo\n"
        "    deploy_path: /tmp/billing-repo\n"
        "    gate_profile: product-pr\n",
        encoding="utf-8",
    )
    records = merge_open_prs.load_repositories(fixture)

    assert records["billing-repo"].gate_profile == "product-pr"
    assert records["shop-repo"].gate_profile is None
    assert merge_open_prs.automerge_selection(records) == [
        "core-repo", "docs-repo", "billing-repo",
    ]
    assert [record.name for record in
            merge_open_prs.resolve_selection([], records)] == [
        "core-repo", "docs-repo",
    ]


def test_the_live_catalog_puts_product_repositories_in_the_automerge_pass_only():
    """Read against the real file: a fixture alone could not catch the day
    the catalog drops a product's profile marker, which is the single fact its
    automerge selection is keyed on."""
    records = merge_open_prs.load_repositories()

    assert records["billing-repo"].gate_profile == "product-pr"
    assert records["example.org"].gate_profile == "product-pr"
    assert records["example.com"].gate_profile == "product-pr"
    assert records["example.net"].gate_profile == "product-pr"
    assert records["demo-repo"].gate_profile == "product-pr"
    assert merge_open_prs.automerge_selection(records) == [
        "core-repo", "docs-repo", "example.org", "billing-repo", "example.com",
        "example.net", "demo-repo",
    ]
    assert merge_open_prs.DEFAULT_REPOSITORIES == ("core-repo", "docs-repo")


def test_a_product_review_refusal_cannot_halt_the_merging_repositories(tmp_path):
    """One repository's refusal must not stop the pass for the others.

    A product repository is visited by the same pass that merges `core-repo`
    and `docs-repo`. Only the integrity and authority code sets stop
    every repository; an ordinary review refusal is retained and the pass
    carries on, so a product-side problem cannot withhold core merges.
    """
    repositories = [
        merge_open_prs.Repository(
            "core-repo", tmp_path / "core-repo", "forgejo", "acme/core-repo"
        ),
        _product_repository(tmp_path / "billing-repo"),
    ]
    application = FakeGateApplication(
        {"core-repo": [42], "billing-repo": [12]},
        responses={
            ("inspect", "billing-repo", 12): {
                "ok": True, "head_sha": "a" * 40, "base_sha": "b" * 40,
                "ci_state": "green", "review_state": "not_approved"},
            ("review", "billing-repo", 12): {
                "ok": False, "code": "REPOS_YAML_MALFORMED"},
        },
    )

    summaries = merge_open_prs.run_automerge_pass(repositories, application)

    assert summaries[0]["results"] == [{"pr": 42, "outcome": "merged"}]
    assert summaries[1]["results"] == [
        {"pr": 12, "outcome": "retained", "reason": "REPOS_YAML_MALFORMED"}
    ]
    assert not any(summary["authority_integrity_failed"] for summary in summaries)
    assert ("merge", "core-repo", 42, "a" * 40, "b" * 40) in application.calls


def test_clean_refresh_reuse_closes_the_loop_for_a_product_repository(tmp_path):
    """Clean-refresh reuse merges a refreshed product PR without a second review.

    After a merge-of-main refresh the readonly inspection reports no exact
    approval on the new head. The executor answers REVIEW_APPROVAL_REUSED
    from the clean refresh, the re-inspection reports the carried approval,
    and the pass merges the new head with outcome `merged`.
    """
    new_head, new_base = "c" * 40, "d" * 40
    application = FakeGateApplication(
        {"billing-repo": [12]},
        responses={
            ("inspect", "billing-repo", 12): [
                {"ok": True, "head_sha": new_head, "base_sha": new_base,
                 "ci_state": "green", "review_state": "not_approved"},
                {"ok": True, "head_sha": new_head, "base_sha": new_base,
                 "ci_state": "green", "review_state": "approved",
                 "approval_basis": "clean_refresh",
                 "reviewed_head_sha": "a" * 40, "reviewed_base_sha": "b" * 40},
            ],
            ("review", "billing-repo", 12): {
                "ok": True, "code": "REVIEW_APPROVAL_REUSED",
                "approval_basis": "clean_refresh",
                "reviewed_head_sha": "a" * 40, "reviewed_base_sha": "b" * 40},
        },
    )

    summaries = merge_open_prs.run_automerge_pass(
        [_product_repository(tmp_path)], application, max_reviews=1,
    )

    assert [call[0] for call in application.calls] == [
        "candidates", "inspect", "review", "inspect", "merge",
    ]
    assert application.calls[-1] == ("merge", "billing-repo", 12, new_head, new_base)
    assert summaries[0]["results"] == [{
        "pr": 12, "outcome": "merged",
        "approval_basis": "clean_refresh",
        "reviewed_head_sha": "a" * 40, "reviewed_base_sha": "b" * 40,
    }]


# --------------------------- forge mergeability is meaningful for every repo


def _unmergeable(review="not_approved", **extra):
    return _queue_inspect(review=review, mergeable=False, **extra)


@pytest.mark.parametrize("name", ["core-repo", "billing-repo"])
def test_unmergeable_pull_request_is_retained_without_review(tmp_path, name):
    """A product PR is never a WIP draft, so its forge `mergeable=False`
    means a conflict, as it does for an ordinary PR."""
    repository = merge_open_prs.Repository(
        name, tmp_path, "forgejo", f"acme/{name}",
        gate_profile="product-pr" if name == "billing-repo" else None)
    application = FakeGateApplication({name: [1]}, responses={
        ("inspect", name, 1): _unmergeable()})

    result = merge_open_prs.run_automerge_pass([repository], application)[0]

    assert result["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "PR_NOT_MERGEABLE"}]
    assert not any(call[0] in {"review", "merge"} for call in application.calls)


def _settle_pass(repository, gate, store, clock, at):
    clock[0] = at
    return merge_open_prs.run_automerge_pass(
        [repository], gate, queue_store=store, now=lambda: clock[0])[0]


def test_unmergeable_product_pull_request_is_parked_in_the_queue(tmp_path):
    """One `false` read holds the slot; the same head and base still
    unmergeable after the settle window park as an author-owned conflict."""
    store = _queue_store(tmp_path, repositories=("billing-repo",))
    application = FakeGateApplication({"billing-repo": [66]}, responses={
        ("inspect", "billing-repo", 66): _unmergeable()})
    clock = [1000.0]
    repository = _product_repository(tmp_path)

    _settle_pass(repository, application, store, clock, 1000.0)
    assert "66" not in store.read()["repositories"]["billing-repo"]["parked"]

    _settle_pass(repository, application, store, clock,
                 1000.0 + merge_open_prs.MERGEABLE_SETTLE_SECONDS)

    assert store.read()["repositories"]["billing-repo"]["parked"]["66"]["reason"] == "conflict"


def test_conflict_with_green_run_on_moved_base_parks_without_waiting_on_ci(tmp_path):
    """The gate reports a conflict ahead of a stale-base green run, so the
    row reads `pending` with `PR_NOT_MERGEABLE`. Waiting on that CI would
    hold the slot for CI_WAIT_SECONDS; it holds at most the settle window,
    then parks as an author-owned conflict, and the next PR proceeds."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _unmergeable(
            ci="pending", conclusion="success", blocking_reason="PR_NOT_MERGEABLE",
            next_action="resolve_conflict"),
        # Not approved, like PR 1: an approved PR 2 would be elected ahead.
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
    })
    clock = [1000.0]
    repository = _core_repo(tmp_path)
    settle = merge_open_prs.MERGEABLE_SETTLE_SECONDS

    held = _settle_pass(repository, gate, store, clock, 1000.0)
    assert held["results"] == [{"pr": 1, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1 and row["parked"] == {}
    # The slot is held: selection read PR 2 to rank it, and nothing was written for it.
    assert not any(call[0] in {"review", "refresh", "merge"} and call[2] == 2
                   for call in gate.calls)

    held = _settle_pass(repository, gate, store, clock, 1000.0 + settle - 1)
    assert held["results"] == [{"pr": 1, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"}]
    assert store.read()["repositories"]["core-repo"]["parked"] == {}

    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect()  # Approved by now.
    result = _settle_pass(repository, gate, store, clock, 1000.0 + settle)
    assert result["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"},
        {"pr": 2, "outcome": "merged"},
    ]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "conflict"
    assert row["active"] is None
    assert settle < merge_open_prs.CI_WAIT_SECONDS
    assert not any(call[0] in {"refresh", "review", "merge"} and call[2] == 1
                   for call in gate.calls)


def test_post_merge_false_read_is_never_parked_and_the_next_pass_refreshes(tmp_path):
    """Forgejo answers `mergeable: false` while it rechecks open PRs after a
    merge, and the pass inspects the next PR right after one. PR 2, green on
    the old base, reads `PR_NOT_MERGEABLE` once: it keeps the active slot and
    is not parked; the next pass reads it mergeable and refreshes it."""
    store = _queue_store(tmp_path)
    recheck = _queue_inspect(ci="pending", conclusion="success", mergeable=False,
                             blocking_reason="PR_NOT_MERGEABLE",
                             next_action="resolve_conflict")
    settled = _queue_inspect(ci="pending", conclusion="success", mergeable=True,
                             blocking_reason="CI_BASE_STALE", next_action="refresh_task")
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(),
        ("inspect", "core-repo", 2): [recheck, settled, settled, settled],
    })
    clock = [1000.0]
    first = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]

    assert first["results"] == [
        {"pr": 1, "outcome": "merged"},
        {"pr": 2, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"},
    ]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert row["active"]["pr"] == 2
    assert row["active"]["unmergeable"] == ["a" * 40, "b" * 40, 1000.0]

    gate.by_repository["core-repo"] = [2]  # PR 1 merged and left the listing.
    clock[0] += 10
    second = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert "unmergeable" not in row["active"]  # a settled read ends the window
    assert [result["outcome"] for result in second["results"]] == ["refreshed"]
    assert ("refresh", "core-repo", 2, "a" * 40, "b" * 40) in gate.calls


def test_parked_managed_conflict_with_unchanged_evidence_stays_parked(tmp_path):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo")
    store = _queue_store(tmp_path)
    application = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _unmergeable()})
    clock = [1000.0]
    settle = merge_open_prs.MERGEABLE_SETTLE_SECONDS
    _settle_pass(repository, application, store, clock, 1000.0)
    _settle_pass(repository, application, store, clock, 1000.0 + settle)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "conflict"
    _settle_pass(repository, application, store, clock, 1000.0 + 2 * settle)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "conflict"
    assert not any(call[0] in {"review", "merge"} for call in application.calls)


def test_false_read_inside_the_settle_window_keeps_the_slot(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        # Approved, like PR 2: the forge flag decides before the review state does,
        # and readiness keeps PR 1 at the head of the queue.
        ("inspect", "core-repo", 1): _unmergeable(review="approved"),
        ("inspect", "core-repo", 2): _queue_inspect(),
    })
    clock = [1000.0]
    repository = _core_repo(tmp_path)
    _settle_pass(repository, gate, store, clock, 1000.0)

    result = _settle_pass(repository, gate, store, clock, 1000.0 + 30)

    assert result["results"] == [{"pr": 1, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert row["active"]["pr"] == 1
    # The window runs from the first read, not from the latest one.
    assert row["active"]["unmergeable"] == ["a" * 40, "b" * 40, 1000.0]
    assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)


@pytest.mark.parametrize("moved", ["head", "base"])
def test_a_moved_head_or_base_restarts_the_settle_window(tmp_path, moved):
    store = _queue_store(tmp_path)
    first_read = _unmergeable()
    moved_read = _unmergeable(**{moved: "c"})
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [first_read, moved_read, moved_read, moved_read]})
    clock = [1000.0]
    repository = _core_repo(tmp_path)
    settle = merge_open_prs.MERGEABLE_SETTLE_SECONDS
    _settle_pass(repository, gate, store, clock, 1000.0)
    _settle_pass(repository, gate, store, clock, 1030.0)

    pair = {"head": ("c" * 40, "b" * 40), "base": ("a" * 40, "c" * 40)}[moved]
    assert store.read()["repositories"]["core-repo"]["active"]["unmergeable"] == [*pair, 1030.0]

    # Past the old first read plus the window, inside the restarted one.
    _settle_pass(repository, gate, store, clock, 1000.0 + settle + 1)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {} and row["active"]["pr"] == 1

    _settle_pass(repository, gate, store, clock, 1030.0 + settle)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "conflict"


def test_a_mergeable_read_clears_the_settle_marker(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): [
            _unmergeable(), _in_progress(mergeable=True), _unmergeable()]})
    clock = [1000.0]
    repository = _core_repo(tmp_path)
    settle = merge_open_prs.MERGEABLE_SETTLE_SECONDS
    _settle_pass(repository, gate, store, clock, 1000.0)
    _settle_pass(repository, gate, store, clock, 1010.0)
    assert "unmergeable" not in store.read()["repositories"]["core-repo"]["active"]

    # A later false read counts from its own first sight, not from 1000.
    _settle_pass(repository, gate, store, clock, 1000.0 + settle + 1)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert row["active"]["unmergeable"] == ["a" * 40, "b" * 40, 1000.0 + settle + 1]


def test_an_active_row_without_the_settle_marker_loads_and_runs(tmp_path):
    """Written by the previous executor: an active row with no `unmergeable`.
    A row that carries the marker round-trips through the JSON state file."""
    store = _queue_store(tmp_path)
    row = {"active": {"pr": 7, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                      "wait_since": 900.0}, "parked": {}}
    store.path.write_text(json.dumps({"version": 1, "cursor": 0, "repositories": {
        "core-repo": row, "docs-repo": {"active": None, "parked": {}}}}),
        encoding="utf-8")
    store.path.chmod(0o600)
    gate = FakeGateApplication({"core-repo": [7]}, responses={
        ("inspect", "core-repo", 7): _unmergeable()})
    clock = [1000.0]
    repository = _core_repo(tmp_path)

    _settle_pass(repository, gate, store, clock, 1000.0)
    marked = store.read()["repositories"]["core-repo"]["active"]
    assert marked["unmergeable"] == ["a" * 40, "b" * 40, 1000.0]

    _settle_pass(repository, gate, store, clock, 1000.0 + merge_open_prs.MERGEABLE_SETTLE_SECONDS)
    assert store.read()["repositories"]["core-repo"]["parked"]["7"]["reason"] == "conflict"


@pytest.mark.parametrize("marker", [
    ["a" * 40, "b" * 40], ["a" * 40, "b" * 40, -1.0], ["a", "b" * 40, 1.0],
    ["a" * 40, "b" * 40, 1.0, 2.0], "a" * 40,
])
def test_a_malformed_settle_marker_is_corrupt_state(tmp_path, marker):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": {
            "pr": 7, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
            "wait_since": 900.0, "unmergeable": marker}, "parked": {}}
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)






@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_active_stale_base_with_failed_run_parks_without_waiting(tmp_path, conclusion):
    """A failed run on a moved base will not turn green by waiting; park it
    in the same pass so the queue reaches the next PR."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _stale_base(conclusion, ci_run_id=11, ci_run_attempt=1),
        # Waiting on CI, like PR 1: a green PR 2 would be elected ahead.
        ("inspect", "core-repo", 2): _in_progress(),
    })
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]
    assert result["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_BASE_STALE"},
        {"pr": 2, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"},
    ]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    assert not any(call[0] == "refresh" for call in gate.calls)


def test_queued_ci_parks_after_start_wait(tmp_path):
    # Missing CI has its shorter recovery deadline, covered separately below.
    never_started = _queue_inspect(ci="pending", conclusion=None, blocking_reason="CI_RUN_QUEUED")
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): never_started,
        ("inspect", "core-repo", 2): _queue_inspect(),
    })

    def run():
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]

    run()
    clock[0] += merge_open_prs.CI_START_WAIT_SECONDS - 1
    assert run()["results"] == [{"pr": 1, "outcome": "retained",
                                 "reason": never_started["blocking_reason"]}]
    clock[0] += 2
    assert run()["results"] == [{"pr": 1, "outcome": "retained", "reason": "CI_STALLED"},
                                {"pr": 2, "outcome": "merged"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_stalled"


def test_in_progress_ci_still_waits_the_full_hour(tmp_path):
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(),
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: clock[0])
    clock[0] += merge_open_prs.CI_START_WAIT_SECONDS + 1
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]
    assert result["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert store.read()["repositories"]["core-repo"]["parked"] == {}


def test_queued_run_that_starts_keeps_its_wait_since_and_hour_bound(tmp_path):
    store = _queue_store(tmp_path)
    clock = [1000.0]
    queued = _queue_inspect(ci="pending", conclusion=None, blocking_reason="CI_RUN_QUEUED")
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): queued})

    def run():
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]

    run()
    clock[0] += 900
    run()
    gate.responses[("inspect", "core-repo", 1)] = _in_progress()
    clock[0] += merge_open_prs.CI_START_WAIT_SECONDS + 1 - 900
    started = clock[0]
    assert run()["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    # the hour counts from the run's last state change, its start.
    clock[0] = started + merge_open_prs.CI_WAIT_SECONDS - 1
    assert run()["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    clock[0] = started + merge_open_prs.CI_WAIT_SECONDS
    assert run()["results"] == [{"pr": 1, "outcome": "retained", "reason": "CI_STALLED"}]


def _missing_ci(**extra):
    """No run at all for the head: no `pull_request` event ever fired."""
    return _queue_inspect(ci="pending", conclusion=None, run=None, attempt=None,
                          blocking_reason="CI_RUN_MISSING", **extra)


def _missing_world(tmp_path, first):
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): first,
        ("inspect", "core-repo", 2): _queue_inspect(run=12)})

    def run():
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]
    return store, gate, clock, run


def test_incomplete_ci_evidence_uses_the_existing_bounded_wait(tmp_path: Path) -> None:
    store, gate, clock, run = _missing_world(tmp_path, _queue_inspect(
        ci="pending", conclusion=None, run=None, attempt=None,
        blocking_reason="CI_RESULT_AMBIGUOUS"))
    # PR 2 is not green yet, so nothing outranks PR 1.
    gate.responses[("inspect", "core-repo", 2)] = _in_progress()

    for elapsed in (0, 601, 1201, 3599):
        clock[0] = 1000.0 + elapsed
        assert run()["results"] == [
            {"pr": 1, "outcome": "retained", "reason": "CI_RESULT_AMBIGUOUS"}]
        row = store.read()["repositories"]["core-repo"]
        assert row["parked"] == {}
        assert row["active"]["pr"] == 1
        assert row["active"]["wait_since"] == 1000.0
        assert "ci_missing" not in row["active"]
        assert "retained" not in row["active"]

    clock[0] = 4600.0
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect(run=12)
    assert run()["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_STALLED"},
        {"pr": 2, "outcome": "merged"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_stalled"
    assert not any(call[0] == "refresh" for call in gate.calls)


def test_missing_ci_under_ten_minutes_keeps_the_slot_and_times_the_head(tmp_path):
    store, gate, clock, run = _missing_world(tmp_path, _missing_ci())

    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS - 1
    assert run()["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_MISSING"}]

    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1
    assert row["active"]["ci_missing"] == ["a" * 40, 1000.0]
    assert row["parked"] == {}
    assert not any(call[0] == "refresh" for call in gate.calls)


def test_missing_ci_after_ten_minutes_on_a_moved_base_is_refreshed_once(tmp_path):
    """The "Update branch" action moved the head without the hooks, so the PR's
    recorded base (`merge_base`) lags main. A gate refresh is a real push."""
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="9" * 40))
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    refreshed = run()

    assert refreshed["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert [call[2:5] for call in gate.calls if call[0] == "refresh"] == [
        (1, "a" * 40, "b" * 40)]
    assert store.read()["repositories"]["core-repo"]["active"]["refreshed"] == "a" * 40

    # The forge still shows the same head with no run: never a second refresh.
    clock[0] += 60
    parked = run()
    assert len([call for call in gate.calls if call[0] == "refresh"]) == 1
    assert parked["results"][0] == {"pr": 1, "outcome": "retained",
                                    "reason": "CI_RUN_MISSING_STALLED"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_run_missing"


def test_missing_ci_after_ten_minutes_on_a_current_base_parks_for_the_operator(tmp_path):
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="b" * 40))
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    summary = run()

    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_MISSING_STALLED"},
        {"pr": 2, "outcome": "merged"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "ci_run_missing"
    assert row["parked"]["1"]["head"] == "a" * 40
    assert row["active"] is None
    assert not any(call[0] == "refresh" for call in gate.calls)
    assert gate.statuses[-1][1][1] == "queue=parked reason=ci_run_missing owner=operator pos=-"
    assert summary["park_wait_s"] == 0  # Operator-owned: counted from now on.


def test_a_refused_missing_ci_refresh_parks_in_the_same_pass(tmp_path):
    """Today's gate refuses a refresh unless the base is stale under a green
    run, so the PR goes to the operator instead of holding the slot."""
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="9" * 40))
    gate.responses[("refresh", "core-repo", 1)] = {
        "ok": False, "code": "PR_REFRESH_NOT_ELIGIBLE"}
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    summary = run()

    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_MISSING_STALLED",
         "refresh_refused": "PR_REFRESH_NOT_ELIGIBLE"},
        {"pr": 2, "outcome": "merged"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_run_missing"


def test_a_stale_pull_ref_on_a_moved_base_is_repaired_by_the_one_refresh(tmp_path):
    """The head moved without the forge's post-receive step, so
    `refs/pull/<n>/head` lags it. Main moved too: the refresh is a real push."""
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="9" * 40, pull_ref_sha="8" * 40))
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    refreshed = run()

    assert refreshed["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert [call[2:5] for call in gate.calls if call[0] == "refresh"] == [
        (1, "a" * 40, "b" * 40)]
    assert store.read()["repositories"]["core-repo"]["parked"] == {}

    # The same head still shows a stale ref: never a second refresh for it.
    clock[0] += 60
    parked = run()
    assert len([call for call in gate.calls if call[0] == "refresh"]) == 1
    assert parked["results"][0] == {"pr": 1, "outcome": "retained",
                                    "reason": "PULL_REF_STALE"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "pull_ref_stale"


def test_a_stale_pull_ref_with_nothing_to_merge_parks_with_its_own_reason(tmp_path):
    """The branch already holds main (the operator's "Update branch"), so no
    push the queue may make exists: say what is wrong instead of `ci_run_missing`."""
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="b" * 40, pull_ref_sha="8" * 40))
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    summary = run()

    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PULL_REF_STALE"},
        {"pr": 2, "outcome": "merged"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "pull_ref_stale"
    assert row["parked"]["1"]["head"] == "a" * 40
    assert row["active"] is None
    assert not any(call[0] == "refresh" for call in gate.calls)
    assert gate.statuses[-1][1][1] == "queue=parked reason=pull_ref_stale owner=operator pos=-"
    assert summary["park_wait_s"] == 0  # Operator-owned: counted from now on.


def test_a_stale_pull_ref_whose_refresh_is_refused_parks_with_its_own_reason(tmp_path):
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="9" * 40, pull_ref_sha="8" * 40))
    gate.responses[("refresh", "core-repo", 1)] = {
        "ok": False, "code": "PR_REFRESH_NOT_ELIGIBLE"}
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    summary = run()

    assert summary["results"][0] == {
        "pr": 1, "outcome": "retained", "reason": "PULL_REF_STALE",
        "refresh_refused": "PR_REFRESH_NOT_ELIGIBLE"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "pull_ref_stale"


@pytest.mark.parametrize("extra", [
    {"pull_ref_sha": "a" * 40},  # The ref is current: the run is missing for another reason.
    {},                          # The gate could not read the ref.
])
def test_a_current_or_unread_pull_ref_keeps_the_ci_run_missing_park(tmp_path, extra):
    store, gate, clock, run = _missing_world(
        tmp_path, _missing_ci(recorded_base_sha="b" * 40, **extra))
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS

    summary = run()

    assert summary["results"][0] == {
        "pr": 1, "outcome": "retained", "reason": "CI_RUN_MISSING_STALLED"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_run_missing"
    assert not any(call[0] == "refresh" for call in gate.calls)


@pytest.mark.parametrize(("still", "appeared"), [
    (_missing_ci(pull_ref_sha="8" * 40), _missing_ci(head="d")),  # The operator pushed.
    (_missing_ci(pull_ref_sha="8" * 40), _in_progress()),
    (_missing_ci(pull_ref_sha="8" * 40), None),  # Nothing changed.
    # Main moved: the queue's own recovery refresh can now push the repair.
    (_missing_ci(pull_ref_sha="8" * 40),
     _missing_ci(pull_ref_sha="8" * 40, base="c", recorded_base_sha="b" * 40)),
])
def test_a_pull_ref_stale_park_ends_as_a_ci_run_missing_park_does(tmp_path, still, appeared):
    store, gate, clock, run = _missing_world(tmp_path, still)
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS
    run()
    gate.by_repository["core-repo"] = [1]  # PR 2 merged.
    clock[0] += 60
    run()
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "pull_ref_stale"

    gate.responses[("inspect", "core-repo", 1)] = appeared or still
    clock[0] += 60
    run()

    row = store.read()["repositories"]["core-repo"]
    if appeared is None:
        assert row["parked"]["1"]["reason"] == "pull_ref_stale"
        assert row["active"] is None
    else:
        assert row["parked"] == {}
        assert row["active"]["pr"] == 1
    if appeared is not None and "recorded_base_sha" in appeared:
        # Main moved past the recorded base: the released PR gets the one
        # recovery refresh, the push that repairs the ref.
        clock[0] += merge_open_prs.CI_MISSING_SECONDS
        run()

        assert ("refresh", "core-repo", 1) == next(
            call[:3] for call in gate.calls if call[0] == "refresh")
        assert store.read()["repositories"]["core-repo"]["parked"] == {}


def _contained_world(tmp_path, monkeypatch, shape):
    """A real repository whose `main` tip already holds, or merely equals, a head.

    Returns the repository, the head and base SHAs. The placeholder SHAs of the
    other tests name no object here, so a PR that keeps them still reads as
    having something to land.
    """
    for key, value in DETERMINISTIC_GIT_ENV.items():
        monkeypatch.setenv(key, value)  # Restored after the test.
    repo = make_origin(tmp_path).clone
    git(["checkout", "-q", "-b", "side"], repo)
    stale = commit_file(repo, "feature.txt", "feature\n", "the PR head")
    git(["checkout", "-q", "main"], repo)
    if shape == "ancestor":
        git(["merge", "-q", "--ff-only", "side"], repo)  # Merged by hand.
        base = commit_file(repo, "other.txt", "other\n", "later work on main")
        return _core_repo(repo), stale, base
    if shape == "noop_merge":
        commit_file(repo, "feature.txt", "feature\n", "the same change, landed another way")
        base = commit_file(repo, "other.txt", "other\n", "later work on main")
        head = git(["commit-tree", f"{base}^{{tree}}", "-p", stale, "-p", base,
                    "-m", "Merge main into the PR"], repo).stdout.strip()
        assert git(["merge-base", "--is-ancestor", head, base], repo, check=False).returncode != 0
        return _core_repo(repo), head, base
    base = commit_file(repo, "other.txt", "other\n", "later work on main")  # "changed"
    return _core_repo(repo), stale, base


def _contained_inspect(head, base, **extra):
    return {**_queue_inspect(**extra), "head_sha": head, "base_sha": base}


def _no_landing_calls(gate, pr=1):
    return not [call for call in gate.calls
                if call[0] in {"refresh", "review", "merge"} and call[2] == pr]


@pytest.mark.parametrize("shape", ["ancestor", "noop_merge"])
def test_a_head_that_adds_nothing_to_main_parks_before_any_refresh_review_or_merge(
        tmp_path, monkeypatch, shape):
    """A head that is already in main, while the forge still lists the PR
    as open, parks before any refresh, review or merge: the queue must not
    spend a refresh, a CI run or a paid review on it."""
    repository, head, base = _contained_world(tmp_path, monkeypatch, shape)
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _contained_inspect(head, base, review="not_approved")})

    summary = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]

    # the approved PR 2 is elected, and merged, before PR 1.
    assert summary["results"] == [
        {"pr": 2, "outcome": "merged"},
        {"pr": 1, "outcome": "retained", "reason": "PR_ALREADY_IN_BASE"}]
    assert _no_landing_calls(gate)
    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "already_in_base"
    assert (row["parked"]["1"]["head"], row["parked"]["1"]["base"]) == (head, base)

    # Unchanged head: the next pass leaves it parked and spends nothing on it.
    gate.by_repository["core-repo"] = [1]
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert _no_landing_calls(gate)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "already_in_base"


def test_a_head_with_a_real_change_against_main_is_processed_as_before(tmp_path, monkeypatch):
    repository, head, base = _contained_world(tmp_path, monkeypatch, "changed")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _contained_inspect(head, base)})

    summary = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]

    assert summary["results"] == [{"pr": 1, "outcome": "merged"}]
    assert ("merge", "core-repo", 1, head, base) in gate.calls
    assert store.read()["repositories"]["core-repo"]["parked"] == {}


def test_an_active_pr_whose_head_turns_out_to_add_nothing_parks_on_the_next_pass(
        tmp_path, monkeypatch):
    repository, head, base = _contained_world(tmp_path, monkeypatch, "noop_merge")
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress()})
    merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    gate.calls.clear()

    gate.responses[("inspect", "core-repo", 1)] = _contained_inspect(head, base)
    summary = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store)[0]

    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_ALREADY_IN_BASE"}]
    assert not [call for call in gate.calls if call[0] in {"refresh", "review", "merge"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "already_in_base"


def test_head_adds_nothing_fails_open_when_git_cannot_tell(tmp_path, monkeypatch):
    repository, head, base = _contained_world(tmp_path, monkeypatch, "noop_merge")
    # Positive control: the check can answer True here.
    assert merge_open_prs._head_adds_nothing(
        repository, {"head_sha": head, "base_sha": base}) is True

    plain = tmp_path / "not-a-repository"
    plain.mkdir()
    elsewhere = merge_open_prs.Repository("core-repo", plain, "forgejo", "acme/core-repo")
    assert merge_open_prs._head_adds_nothing(
        elsewhere, {"head_sha": head, "base_sha": base}) is False
    assert merge_open_prs._head_adds_nothing(
        repository, {"head_sha": "d" * 40, "base_sha": base}) is False  # Not in the repository.
    for malformed in ("not-a-sha", head[:12], None, ""):
        assert merge_open_prs._head_adds_nothing(
            repository, {"head_sha": malformed, "base_sha": base}) is False
        assert merge_open_prs._head_adds_nothing(
            repository, {"head_sha": head, "base_sha": malformed}) is False
    assert merge_open_prs._head_adds_nothing(repository, {}) is False


@pytest.mark.parametrize("appeared", [
    _in_progress(),
    _queue_inspect(ci="pending", conclusion=None, blocking_reason="CI_RUN_QUEUED"),
    _missing_ci(head="d"),
])
def test_a_ci_run_missing_park_ends_when_ci_appears_or_the_head_changes(tmp_path, appeared):
    store, gate, clock, run = _missing_world(tmp_path, _missing_ci())
    run()
    clock[0] += merge_open_prs.CI_MISSING_SECONDS
    run()
    gate.by_repository["core-repo"] = [1]  # PR 2 merged.
    clock[0] += 60
    run()
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_run_missing"

    gate.responses[("inspect", "core-repo", 1)] = appeared
    clock[0] += 60
    run()

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert row["active"]["pr"] == 1


@pytest.mark.parametrize("field,value", [
    ("ci_missing", ["a" * 40]), ("ci_missing", ["a", 1.0]), ("ci_missing", ["a" * 40, -1.0]),
    ("refreshed", "a"), ("refreshed", None),
])
def test_recovery_markers_round_trip_and_malformed_ones_are_corrupt(tmp_path, field, value):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    active = {"pr": 7, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
              "wait_since": 900.0, "ci_missing": ["a" * 40, 900.0], "refreshed": "a" * 40}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": active, "parked": {}}
        store.save(state)
        assert store.read()["repositories"]["core-repo"]["active"] == active
        active[field] = value
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


def _stuck_world(tmp_path, responses):
    """PR 1 at the head of an `core-repo` queue behind which PR 2 would merge."""
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses=responses)

    def run():
        clock[0] += 60  # Well inside every 20- and 60-minute deadline.
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]
    return store, gate, run


def _head_of_line_parked_as_stuck(tmp_path):
    store, gate, run = _stuck_world(tmp_path, {
        ("inspect", "core-repo", 1): _queue_inspect(),
        ("merge", "core-repo", 1): {"ok": False, "code": "FETCH_FAILED"}})
    for passes in range(1, merge_open_prs.STUCK_PASS_LIMIT):
        assert run()["results"] == [
            {"pr": 1, "outcome": "retained", "reason": "FETCH_FAILED"}]
        assert store.read()["repositories"]["core-repo"]["active"]["retained"] == [
            "FETCH_FAILED", passes]
    return store, gate, run


def test_ten_passes_retaining_the_head_with_one_code_park_it_as_stuck(tmp_path):
    """A merge refused `FETCH_FAILED` on every pass would hold a PR at the head of
    the queue indefinitely; the tenth such pass parks it and the queue moves."""
    store, gate, run = _head_of_line_parked_as_stuck(tmp_path)

    tenth = run()

    assert tenth["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_STUCK", "code": "FETCH_FAILED"},
        {"pr": 2, "outcome": "merged"}]
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert (parked["reason"], parked["code"]) == ("stuck", "FETCH_FAILED")
    assert gate.statuses[-1][1][1] == "queue=parked reason=stuck owner=operator pos=-"


def test_a_stuck_park_ends_only_on_a_head_change(tmp_path):
    """A moved base must not return a stuck PR to the head of the queue after
    every merge, where it would block ten more passes."""
    store, gate, run = _head_of_line_parked_as_stuck(tmp_path)
    run()
    gate.by_repository["core-repo"] = [1]

    for inspected in (_queue_inspect(), _queue_inspect(base="c"),
                      _queue_inspect(base="c", run=12)):
        gate.responses[("inspect", "core-repo", 1)] = inspected
        before = len(gate.calls)
        run()  # Unchanged head: still parked, never merged again.
        assert [call[0] for call in gate.calls[before:]] == ["candidates", "inspect"]
        assert "1" in store.read()["repositories"]["core-repo"]["parked"]

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d", base="c")
    assert run()["results"] == [{"pr": 1, "outcome": "retained", "reason": "FETCH_FAILED"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert (row["active"]["pr"], row["active"]["retained"]) == (1, ["FETCH_FAILED", 1])


def test_a_changed_retaining_code_restarts_the_count(tmp_path):
    store, gate, run = _head_of_line_parked_as_stuck(tmp_path)
    gate.responses[("merge", "core-repo", 1)] = {"ok": False, "code": "REMOTE_HEAD_MISMATCH"}

    assert run()["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "REMOTE_HEAD_MISMATCH"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["retained"] == ["REMOTE_HEAD_MISMATCH", 1]
    assert row["parked"] == {}


@pytest.mark.parametrize("responses,reason", [
    ({("inspect", "core-repo", 1): _in_progress()}, "CI_RUN_IN_PROGRESS"),
    ({("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
      ("review", "core-repo", 1): {"ok": False, "code": "REVIEW_ATTEMPT_BUSY"}},
     "REVIEW_ATTEMPT_BUSY"),
    ({("merge", "core-repo", 1): {"ok": False, "code": "PR_MERGEABILITY_UNKNOWN"}},
     "PR_MERGEABILITY_UNKNOWN"),
    ({("inspect", "core-repo", 1): _queue_inspect(
        review="not_approved", blocking_reason="PR_REVIEW_NOT_APPROVED")},
     "PR_REVIEW_NOT_APPROVED"),
])
def test_ci_waits_and_the_review_family_never_park_as_stuck(tmp_path, responses, reason):
    # PR 2 reads as ready as PR 1, so PR 1 keeps the head of the queue.
    first = responses.get(("inspect", "core-repo", 1))
    if first is not None:
        responses = {**responses, ("inspect", "core-repo", 2): first}
    store, gate, run = _stuck_world(tmp_path, responses)
    for _ in range(merge_open_prs.STUCK_PASS_LIMIT + 1):
        assert run()["results"] == [{"pr": 1, "outcome": "retained", "reason": reason}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert row["active"]["pr"] == 1 and "retained" not in row["active"]


@pytest.mark.parametrize("field,value", [
    ("retained", ["fetch failed", 1]), ("retained", ["FETCH_FAILED", 0]),
    ("retained", "FETCH_FAILED"), ("code", "fetch failed"),
])
def test_a_malformed_stuck_count_or_code_is_corrupt_state(tmp_path, field, value):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    row = {"active": {"pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                      "wait_since": 1000.0, "retained": ["FETCH_FAILED", 3]},
           "parked": {"2": {"reason": "stuck", "code": "FETCH_FAILED", "head": "c" * 40,
                            "run": [12, None], "review": [None, "none"]}}}
    with store.locked() as state:
        state["repositories"]["core-repo"] = row
        store.save(state)
    assert store.read()["repositories"]["core-repo"] == row
    target = row["active"] if field == "retained" else row["parked"]["2"]
    target[field] = value
    with store.locked() as state:
        state["repositories"]["core-repo"] = row
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


@pytest.mark.parametrize("cursor", [0, 1])
def test_review_ahead_never_takes_the_slot_from_a_later_repository(tmp_path, cursor):
    store = _queue_store(tmp_path)
    docs_repo = merge_open_prs.Repository(
        "docs-repo", tmp_path, "forgejo", "acme/docs-repo")
    gate = FakeGateApplication({"core-repo": [1, 2], "docs-repo": [5]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("inspect", "docs-repo", 5): [
            _queue_inspect(review="not_approved"), _queue_inspect()],
    })
    with store.locked() as state:
        state["cursor"] = cursor
        store.save(state)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path), docs_repo], gate,
                                       queue_store=store, max_reviews=1)
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("docs-repo", 5)]


def test_review_ahead_reviews_next_green_pr_without_queue_mutation(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(head="e", base="f", review="not_approved"),
    })
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)[0]
    assert [call for call in gate.calls if call[0] == "review"] == [
        ("review", "core-repo", 2, "e" * 40, "f" * 40,
         "merge-guard-automerge:core-repo:2", None)]
    assert result["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"},
        {"pr": 2, "outcome": "reviewed_ahead"},
    ]
    assert not any(call[0] in {"merge", "refresh", "ready"} for call in gate.calls)
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1
    assert row["parked"] == {}


def test_no_review_ahead_when_active_pr_spent_the_slot(tmp_path):
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path), max_reviews=1)
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1]


def test_no_review_ahead_on_an_unbounded_pass(tmp_path):
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))
    assert not any(call[0] == "review" for call in gate.calls)
    # Only selection read PR 2; a review ahead would read it again.
    assert [call[:3] for call in gate.calls].count(("inspect", "core-repo", 2)) == 1


def test_review_ahead_skips_parked_and_stale_base_and_caps_inspections(tmp_path):
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"2": {
            "reason": "changes_requested", "head": "a" * 40,
            "run": [11, 1], "review": [None, "none"],
        }}}
        store.save(state)
    responses = {("inspect", "core-repo", 1): _in_progress(),
                 ("inspect", "core-repo", 2): _queue_inspect(review="not_approved")}
    for pr in range(3, 8):
        responses[("inspect", "core-repo", pr)] = _stale_base(review_state="not_approved")
    gate = FakeGateApplication({"core-repo": list(range(1, 8))}, responses=responses)
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)[0]
    inspected = [call[2] for call in gate.calls if call[0] == "inspect"]
    # Selection ranks every listed PR, the parked one read as always; the
    # review ahead then skips the parked and the active PR and stops at its cap of 3.
    assert inspected[:7] == [1, 2, 3, 4, 5, 6, 7]
    assert inspected[7:] == [3, 4, 5]
    assert not any(call[0] in {"review", "refresh", "merge"} for call in gate.calls)
    assert result["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]


def test_review_ahead_reviews_a_pr_green_on_its_recorded_base_over_that_base(tmp_path):
    """A moved main no longer keeps a green PR from being reviewed ahead."""
    gate = FakeGateApplication({"core-repo": [1, 2, 3, 4]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        # Failed on its recorded base, or no recorded base to review on.
        ("inspect", "core-repo", 2): _stale_base(
            "failure", review_state="not_approved", recorded_base_sha="d" * 40),
        ("inspect", "core-repo", 3): _stale_base(review_state="not_approved"),
        ("inspect", "core-repo", 4): _stale_base(
            review_state="not_approved", recorded_base_sha="d" * 40),
    })
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), max_reviews=1)[0]
    assert [call for call in gate.calls if call[0] == "review"] == [
        ("review", "core-repo", 4, "a" * 40, "d" * 40,
         "merge-guard-automerge:core-repo:4", None)]
    assert result["results"][-1] == {"pr": 4, "outcome": "reviewed_ahead"}
    assert not any(call[0] in {"merge", "refresh"} for call in gate.calls)


@pytest.mark.parametrize(("refusal", "reviewed"), [
    ({"ok": False, "code": "PR_CI_NOT_GREEN", "dispatched": False}, [2, 3]),
    ({"ok": False, "code": "PR_CI_NOT_GREEN"}, [2]),
])
def test_review_ahead_tries_the_next_pr_after_an_undispatched_refusal(
        tmp_path, refusal, reviewed):
    """A refusal before any dispatch leaves the repository's review ahead
    unused, so the next candidate takes it within the inspection cap. A
    refusal that may have followed a dispatch keeps ending the turn."""
    gate = FakeGateApplication({"core-repo": [1, 2, 3, 4]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _stale_base(
            review_state="not_approved", recorded_base_sha="d" * 40),
        ("inspect", "core-repo", 3): _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 4): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 2): refusal,
    })
    store = _queue_store(tmp_path)
    # One paid review for the pass: PR 3 is reviewed only if PR 2 spent none.
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)[0]
    assert [call[2] for call in gate.calls if call[0] == "review"] == reviewed
    assert result["results"][1] == {"pr": 2, "outcome": "retained",
                                    "reason": "PR_CI_NOT_GREEN", "review_ahead": True}
    if reviewed == [2, 3]:
        assert result["results"][-1] == {"pr": 3, "outcome": "reviewed_ahead"}
    assert not any(call[0] in {"merge", "refresh"} for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


def test_review_ahead_skips_green_current_base_when_head_lacks_it(tmp_path):
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): {
            **_queue_inspect(review="not_approved"),
            "blocking_reason": "PR_BASE_STALE",
            "recorded_base_sha": "d" * 40,
            "ci_run_conclusion": "success",
        },
    })

    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), max_reviews=1)[0]

    assert result["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert not any(call[0] in {"review", "refresh", "merge"} for call in gate.calls)


@pytest.mark.parametrize("later", [
    _queue_inspect(review="not_approved", latest_delegated_review_state="APPROVED"),
    _stale_base(review_state="not_approved", recorded_base_sha="d" * 40,
                latest_delegated_review_state="APPROVED"),
])
def test_review_ahead_skips_a_pr_whose_head_is_already_approved(tmp_path, later):
    # PR 1 is seated first: an approved PR 2 would otherwise be elected.
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): later,
    })
    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    gate.by_repository["core-repo"] = [1, 2]
    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    assert ("inspect", "core-repo", 2) in gate.calls
    assert not any(call[0] == "review" for call in gate.calls)


def test_review_ahead_integrity_code_stops_the_pass(tmp_path):
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 2): {"ok": False, "code": "INTERNAL_ERROR"},
    })
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), max_reviews=1)[0]
    assert result["authority_integrity_failed"] is True
    assert result["results"][-1] == {
        "pr": 2, "outcome": "retained", "reason": "INTERNAL_ERROR",
        "review_ahead": True, "authority_integrity_failed": True}


def _repositories(tmp_path, *names):
    return [merge_open_prs.Repository(name, tmp_path, "forgejo", f"acme/{name}")
            for name in names]


def _needs_review_then_approved():
    """Inspect answers for an active PR that one review approves."""
    return [_queue_inspect(review="not_approved"), _queue_inspect()]


def test_per_repo_cap_gives_each_repository_one_review_in_one_pass(tmp_path):
    """Two slots and one per repository: core-repo' second green PR waits and
    docs-repo's active PR takes the other slot."""
    gate = FakeGateApplication({"core-repo": [1, 2], "docs-repo": [5]}, responses={
        ("inspect", "core-repo", 1): _needs_review_then_approved(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("inspect", "docs-repo", 5): _needs_review_then_approved(),
    })
    summaries = merge_open_prs.run_automerge_pass(
        _repositories(tmp_path, "core-repo", "docs-repo"), gate,
        queue_store=_queue_store(tmp_path), max_reviews=2, max_reviews_per_repo=1)
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("core-repo", 1), ("docs-repo", 5)]
    assert summaries[0]["results"] == [{"pr": 1, "outcome": "merged"}]
    assert summaries[1]["results"] == [{"pr": 5, "outcome": "merged"}]


def test_per_repo_cap_stops_only_its_own_repository(tmp_path):
    """A spare global slot is not spent on a repository at its own cap."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2], "docs-repo": [5]}, responses={
        ("inspect", "core-repo", 1): _needs_review_then_approved(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("inspect", "docs-repo", 5): _needs_review_then_approved(),
    })
    merge_open_prs.run_automerge_pass(
        _repositories(tmp_path, "core-repo", "docs-repo"), gate,
        queue_store=store, max_reviews=3, max_reviews_per_repo=1)
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("core-repo", 1), ("docs-repo", 5)]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


def test_global_cap_refuses_a_third_repository(tmp_path):
    names = ("core-repo", "docs-repo", "billing-repo")
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [5], "billing-repo": [7]},
                               responses={
        ("inspect", "core-repo", 1): _needs_review_then_approved(),
        ("inspect", "docs-repo", 5): _needs_review_then_approved(),
        ("inspect", "billing-repo", 7): _queue_inspect(review="not_approved"),
    })
    store = _queue_store(tmp_path, repositories=names)
    summaries = merge_open_prs.run_automerge_pass(
        _repositories(tmp_path, *names), gate,
        queue_store=store, max_reviews=2, max_reviews_per_repo=1)
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("core-repo", 1), ("docs-repo", 5)]
    assert summaries[2]["results"] == []
    assert store.read()["repositories"]["billing-repo"]["active"]["pr"] == 7


def test_review_budget_refuses_per_repository_without_spending_the_pass():
    gate = FakeGateApplication({})
    budget = merge_open_prs._ReviewBudget(gate, 3, 1)
    assert budget.review("core-repo", 1, "a" * 40, "b" * 40, "run")["ok"] is True
    assert budget.review("core-repo", 2, "a" * 40, "b" * 40, "run") == {
        "ok": False, "code": "REVIEW_BUDGET_EXHAUSTED"}
    assert budget.exhausted("core-repo") and not budget.exhausted("docs-repo")
    assert budget.review("docs-repo", 5, "a" * 40, "b" * 40, "run")["ok"] is True
    assert [call[1:3] for call in gate.calls] == [("core-repo", 1), ("docs-repo", 5)]
    assert budget.review_count == 2 and not budget.spent()


def test_review_ahead_lends_a_slot_from_an_active_pr_blocked_on_its_merge(tmp_path):
    """An approved active PR whose merge is refused needs no review: its idle
    slot reviews the next green PR, which still never merges ahead of it."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(),
        ("merge", "core-repo", 1): {"ok": False, "code": "PR_MERGE_REFUSED"},
        ("inspect", "core-repo", 2): _queue_inspect(head="e", review="not_approved"),
    })
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)[0]
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [("core-repo", 2)]
    assert result["results"][-1] == {"pr": 2, "outcome": "reviewed_ahead"}
    assert [call[2] for call in gate.calls if call[0] == "merge"] == [1]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


def test_review_ahead_spends_leftover_slots_in_each_lending_repository(tmp_path):
    gate = FakeGateApplication({"core-repo": [1, 2], "docs-repo": [5, 6]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("inspect", "docs-repo", 5): _in_progress(),
        ("inspect", "docs-repo", 6): _queue_inspect(review="not_approved"),
    })
    summaries = merge_open_prs.run_automerge_pass(
        _repositories(tmp_path, "core-repo", "docs-repo"), gate,
        queue_store=_queue_store(tmp_path), max_reviews=3, max_reviews_per_repo=1)
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("core-repo", 2), ("docs-repo", 6)]
    assert summaries[0]["results"][-1] == {"pr": 2, "outcome": "reviewed_ahead"}
    assert summaries[1]["results"][-1] == {"pr": 6, "outcome": "reviewed_ahead"}
    assert not any(call[0] == "merge" for call in gate.calls)


def test_review_ahead_skips_a_repository_at_its_own_cap(tmp_path):
    """docs-repo spent its one review on its active PR, which now waits
    on a refused merge; the spare global slot does not review its next PR."""
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [5, 6]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "docs-repo", 5): _needs_review_then_approved(),
        ("merge", "docs-repo", 5): {"ok": False, "code": "PR_MERGE_REFUSED"},
        ("inspect", "docs-repo", 6): _queue_inspect(review="not_approved"),
    })
    store = _queue_store(tmp_path)
    merge_open_prs.run_automerge_pass(
        _repositories(tmp_path, "core-repo", "docs-repo"), gate,
        queue_store=store, max_reviews=3, max_reviews_per_repo=1)
    # Selection reads PR 6 to rank it; the cap bars only its review.
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("docs-repo", 5)]
    assert store.read()["repositories"]["docs-repo"]["active"]["pr"] == 5


def test_a_progressing_run_waits_past_the_hour_and_a_frozen_one_parks(tmp_path):
    """a multi-stage run reports CI_RUN_QUEUED again between stages.
    Once it has been seen running, the 20-minute start wait no longer applies.
    The hour counts from the last state change, so a run that keeps
    moving between queued and running is never parked as `ci_stalled`; one
    whose state stops changing for the hour still is."""
    store = _queue_store(tmp_path)
    clock = [1000.0]
    queued = _queue_inspect(ci="pending", conclusion=None, blocking_reason="CI_RUN_QUEUED")
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 2): _queue_inspect()})

    def run(inspected):
        gate.responses[("inspect", "core-repo", 1)] = inspected
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]

    for step in range(19):  # 0..90 minutes, stage boundaries every 5 minutes
        run(queued if step % 2 == 0 else _in_progress())
        clock[0] += 300
    assert clock[0] - 1000.0 > merge_open_prs.CI_WAIT_SECONDS
    changed = clock[0] - 300  # The last pass saw the run queued again.
    assert run(queued)["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_QUEUED"}]
    assert store.read()["repositories"]["core-repo"]["parked"] == {}
    assert store.read()["repositories"]["core-repo"]["active"]["progress"] == [
        "CI_RUN_QUEUED", changed]
    clock[0] = changed + merge_open_prs.CI_WAIT_SECONDS - 1
    assert run(queued)["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_QUEUED"}]
    clock[0] = changed + merge_open_prs.CI_WAIT_SECONDS
    result = run(queued)
    assert result["results"][0] == {"pr": 1, "outcome": "retained", "reason": "CI_STALLED"}
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_stalled"


def test_a_run_frozen_in_progress_parks_an_hour_after_it_started(tmp_path):
    """without a state change the budget is the hour from the wait start."""
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress()})

    def run():
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]

    run()
    clock[0] += 600
    run()
    clock[0] = 1000.0 + merge_open_prs.CI_WAIT_SECONDS - 1
    assert run()["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    clock[0] = 1000.0 + merge_open_prs.CI_WAIT_SECONDS
    assert run()["results"] == [{"pr": 1, "outcome": "retained", "reason": "CI_STALLED"}]


def test_dead_evidence_does_not_block_the_queue_and_retries_once_per_base_move(tmp_path):
    store = _queue_store(tmp_path)
    dead = _queue_inspect(ci="unavailable", conclusion=None, blocking_reason="CI_EVIDENCE_INVALID")
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): dead,
        ("inspect", "core-repo", 2): _in_progress(),
        ("inspect", "core-repo", 3): _in_progress(),  # Not green: nothing outranks PR 1.
    })

    def run(listing):
        gate.by_repository["core-repo"] = listing
        return merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store)[0]["results"]

    assert run([1, 2, 3]) == [
        {"pr": 1, "outcome": "retained", "reason": "CI_EVIDENCE_INVALID"},
        {"pr": 2, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["base"] == "b" * 40
    gate.responses[("inspect", "core-repo", 2)] = _queue_inspect()
    gate.responses[("inspect", "core-repo", 3)] = _queue_inspect()
    assert run([1, 2, 3]) == [{"pr": 2, "outcome": "merged"}, {"pr": 3, "outcome": "merged"}]

    gate.responses[("inspect", "core-repo", 1)] = {**dead, "base_sha": "c" * 40}
    # The released park ranks below the green PR 4, so PR 4 merges first.
    assert run([1, 4]) == [
        {"pr": 4, "outcome": "merged"},
        {"pr": 1, "outcome": "retained", "reason": "CI_EVIDENCE_INVALID"}]
    row = store.read()["repositories"]["core-repo"]
    assert (row["active"], row["parked"]["1"]["base"]) == (None, "c" * 40)
    assert run([1, 5]) == [{"pr": 5, "outcome": "merged"}]


def test_review_ahead_rotates_past_the_prs_it_already_inspected(tmp_path):
    store = _queue_store(tmp_path)
    responses = {("inspect", "core-repo", 1): _in_progress(),
                 ("inspect", "core-repo", 5): _queue_inspect(review="not_approved")}
    for pr in (2, 3, 4, 6):
        responses[("inspect", "core-repo", pr)] = _stale_base(review_state="not_approved")
    gate = FakeGateApplication({"core-repo": [1, 2, 3, 4, 5, 6]}, responses=responses)

    reviewed = []
    for _ in range(2):
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                           queue_store=store, max_reviews=1)
        reviewed.append([call[2] for call in gate.calls if call[0] == "review"])
    # The first window holds only review-blocked heads; the second rotates past them.
    assert reviewed == [[], [5]]
    assert store.read()["repositories"]["core-repo"]["review_ahead"] == 5


def test_queue_status_names_state_reason_owner_and_position(tmp_path):
    store = _queue_store(tmp_path)

    def parked(reason):
        return {"reason": reason, "head": "a" * 40, "run": [11, 1], "review": [None, "none"]}

    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": {"pr": 3, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": 1000.0},
            "parked": {"1": parked("changes_requested"), "2": parked("ci_stalled"),
                       "4": parked("gate_blocked")},
        }
        store.save(state)
    gate = FakeGateApplication({"core-repo": [1, 2, 3, 4, 5, 6]}, responses={
        ("inspect", "core-repo", 3): _in_progress()})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1250.0)[0]

    assert gate.statuses == [("core-repo", {
        1: "queue=parked reason=changes_requested owner=author pos=-",
        2: "queue=parked reason=ci_stalled owner=queue pos=-",
        3: "queue=active reason=waiting_ci owner=queue pos=1",
        4: "queue=parked reason=gate_blocked owner=operator pos=-",
        5: "queue=eligible reason=- owner=queue pos=2",
        6: "queue=eligible reason=- owner=queue pos=3",
    }, {})]
    assert (summary["queue_depth"], summary["head_wait_s"]) == (6, 250)


def test_a_parked_pr_carries_the_head_it_was_parked_on_while_another_is_active(tmp_path):
    """with PR 3 active nothing re-checks PRs 1 and 2 this pass, so
    the forge write needs each park's head to tell a new head from the old."""
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": {"pr": 3, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": 1000.0},
            "parked": {
                "1": {"reason": "changes_requested", "head": "d" * 40, "run": [11, 1],
                      "review": [None, "none"]},
                "2": {"reason": "ci_stalled", "head": "e" * 40, "run": [11, 1],
                      "review": [None, "none"]},
            },
        }
        store.save(state)
    gate = FakeGateApplication({"core-repo": [1, 2, 3, 4]}, responses={
        ("inspect", "core-repo", 3): _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1250.0)

    assert [call for call in gate.calls if call[0] == "inspect"] == [("inspect", "core-repo", 3)]
    assert gate.parked_heads == [{1: "d" * 40, 2: "e" * 40}]


def test_park_wait_counts_only_parks_the_queue_or_operator_must_clear(tmp_path):
    """`park_wait_s` is the age of the oldest park nobody but the
    queue or the operator can clear. An operator-owned park always counts. A
    queue-owned park counts only with no active PR: while one is active,
    `head_wait_s` covers a stall and the park is re-checked once the queue is
    idle. An author-owned park never counts."""
    store = _queue_store(tmp_path)

    def parked(reason, since):
        return {"reason": reason, "head": "a" * 40, "run": [11, 1],
                "review": [None, "none"], "since": since}

    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": {"pr": 3, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": 1000.0},
            "parked": {"1": parked("changes_requested", 100.0), "2": parked("ci_stalled", 200.0),
                       "4": parked("gate_blocked", 400.0)},
        }
        state["repositories"]["docs-repo"] = {
            "active": None,
            "parked": {"5": parked("conflict", 100.0), "6": parked("ci_stalled", 300.0)},
        }
        store.save(state)
    # Unchanged evidence on an unfinished run: the idle candidate loop re-checks
    # PRs 5 and 6 and leaves both parked.
    gate = FakeGateApplication({"core-repo": [1, 2, 3, 4], "docs-repo": [5, 6]}, responses={
        ("inspect", "core-repo", 3): _in_progress(),
        ("inspect", "docs-repo", 5): _in_progress(),
        ("inspect", "docs-repo", 6): _in_progress()})
    repositories = [_core_repo(tmp_path), merge_open_prs.Repository(
        "docs-repo", tmp_path, "forgejo", "acme/docs-repo")]

    summaries = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store,
                                                   now=lambda: 1250.0)

    rows = store.read()["repositories"]
    assert (rows["docs-repo"]["active"], set(rows["docs-repo"]["parked"])) == (
        None, {"5", "6"})
    assert [summary["park_wait_s"] for summary in summaries] == [850, 950]


def test_a_park_from_before_since_is_timed_from_first_sight(tmp_path):
    """a record written before `since` existed loads, is stamped with
    the pass time, and ages from there."""
    store = _queue_store(tmp_path)
    store.path.write_text(json.dumps({"version": 1, "cursor": 0, "repositories": {
        "core-repo": {
            "active": {"pr": 3, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": 4900.0},
            "parked": {"1": {"reason": "gate_blocked", "head": "a" * 40,
                             "run": [11, 1], "review": [None, "none"]}},
        },
    }}), encoding="utf-8")
    store.path.chmod(0o600)
    gate = FakeGateApplication({"core-repo": [1, 3]}, responses={
        ("inspect", "core-repo", 3): _in_progress()})

    first = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                               now=lambda: 5000.0)[0]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["since"] == 5000.0
    second = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                                now=lambda: 5600.0)[0]
    assert (first["park_wait_s"], second["park_wait_s"]) == (0, 600)


@pytest.mark.parametrize("since", ["1000", -1.0, float("nan"), True])
def test_queue_state_rejects_a_malformed_park_time(tmp_path, since):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"1": {
            "reason": "ci_stalled", "head": "a" * 40, "run": [11, 1],
            "review": [None, "none"], "since": since}}}
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


def test_a_new_park_records_when_it_was_parked(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="changes_requested")})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 4321.0)

    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert (parked["reason"], parked["since"]) == ("changes_requested", 4321.0)


@pytest.mark.parametrize(("inspected", "responses", "reason"), [
    (_stale_base(), {}, "refreshing"),
    (_queue_inspect(), {("merge", "core-repo", 1): {"ok": False, "code": "PR_MERGE_FAILED"}},
     "merging"),
    (_queue_inspect(review="not_approved"),
     {("review", "core-repo", 1): {"ok": False, "code": "REVIEW_INCOMPLETE"}}, "waiting_review"),
])
def test_active_queue_status_names_what_the_queue_waits_on(tmp_path, inspected, responses, reason):
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): inspected, **responses,
        # Not reviewed yet: PR 2 never outranks the active PR.
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved")})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))
    assert gate.statuses[-1][1] == {
        1: f"queue=active reason={reason} owner=queue pos=1",
        2: "queue=eligible reason=- owner=queue pos=2"}


def test_queue_status_feeds_back_what_was_published_and_drops_unproven_writes(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): _in_progress(), ("inspect", "core-repo", 2): _in_progress(),
        ("inspect", "core-repo", 3): _in_progress()})
    gate.heads[("core-repo", 3)] = "c" * 40

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    first = store.read()["repositories"]["core-repo"]["published"]
    assert first["3"] == ["c" * 40, "queue=eligible reason=- owner=queue pos=3"]

    gate.responses[("queue_status", "core-repo")] = {"ok": True, "published": {
        **first, "2": None}}
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert gate.statuses[1][2] == first  # the last published pair, per PR
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1", "3"}

    gate.by_repository["core-repo"] = [1, 2]  # PR 3 closed
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    # The closed PR's record reaches the gate once more, with its done write,
    # and leaves the state afterwards.
    assert set(gate.statuses[2][2]) == {"1", "3"}
    assert gate.statuses[2][1][3] == _QUEUE_DONE
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1"}


@pytest.mark.parametrize(("failure", "code"), [
    ({"ok": False, "code": "EXECUTOR_HOST_REQUIRED"}, "EXECUTOR_HOST_REQUIRED"),
    # A gate API without `queue_status`.
    (AttributeError("queue_status"), "CANDIDATE_ERROR:AttributeError"),
])
def test_a_failed_queue_status_write_does_not_stop_the_pass(tmp_path, caplog, failure, code):
    repositories = [_core_repo(tmp_path), merge_open_prs.Repository(
        "docs-repo", tmp_path, "forgejo", "acme/docs-repo")]
    store = _queue_store(tmp_path)

    class FailingStatus(FakeGateApplication):
        def queue_status(self, repository, descriptions, published):
            if repository != "core-repo":
                return super().queue_status(repository, descriptions, published)
            if isinstance(failure, Exception):
                raise failure
            return failure

    gate = FailingStatus({"core-repo": [1, 2, 3], "docs-repo": [4]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _stale_base(review_state="not_approved"),
        ("inspect", "core-repo", 3): _stale_base(review_state="not_approved"),
    })
    caplog.set_level(logging.WARNING, logger=merge_open_prs.__name__)

    summaries = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store,
                                                   max_reviews=1)

    assert summaries[1]["results"] == [{"pr": 4, "outcome": "merged"}]
    assert not any(summary["authority_integrity_failed"] for summary in summaries)
    rows = store.read()["repositories"]
    assert "published" not in rows["core-repo"]
    assert rows["core-repo"]["review_ahead"] == 3  # the pass still saved its state
    assert f"queue status not published: core-repo {code}" in caplog.text


_QUEUE_DONE = "queue=done reason=- owner=- pos=-"


def test_a_pr_that_leaves_the_queue_gets_one_done_status(tmp_path):
    """A merged or closed PR keeps its last `gate/queue` status forever
    unless the executor writes a final one: without it the PR page reads
    "in progress" after merge."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): _in_progress(), ("inspect", "core-repo", 2): _in_progress(),
        ("inspect", "core-repo", 3): _in_progress()})
    gate.heads[("core-repo", 3)] = "c" * 40
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1", "2", "3"}

    gate.by_repository["core-repo"] = [1, 2]  # PR 3 merged or closed
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert gate.statuses[1][1][3] == _QUEUE_DONE
    assert summary["queue_depth"] == 2
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1", "2"}

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert 3 not in gate.statuses[2][1]  # written once, never again


def test_a_pr_the_pass_itself_merges_gets_its_done_status_in_that_pass(tmp_path):
    """The common case: the executor merges the active PR. It is still listed
    when the pass starts, so only the published record marks it finished."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(), ("inspect", "core-repo", 2): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1", "2"}

    del gate.responses[("inspect", "core-repo", 1)]
    gate.responses[("inspect", "core-repo", 2)] = _in_progress()
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert {"pr": 1, "outcome": "merged"} in summary["results"]
    assert "1" in gate.statuses[1][2]  # the head the done status is written on
    assert gate.statuses[1][1][1] == _QUEUE_DONE
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"2"}


def test_a_repository_whose_last_pr_left_still_writes_its_done_status(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    gate.by_repository["core-repo"] = []
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert gate.statuses[1][1] == {1: _QUEUE_DONE}
    assert summary["queue_depth"] == 0
    assert store.read()["repositories"]["core-repo"]["published"] == {}


def test_done_status_targets_recorded_head_after_branch_reuse(tmp_path):
    """Renovate reuses a branch name for a new PR: the old PR's done status
    must land on the head the executor published it on, not the branch tip."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 3]}, responses={
        ("inspect", "core-repo", 1): _in_progress(), ("inspect", "core-repo", 3): _in_progress()})
    gate.heads[("core-repo", 3)] = "c" * 40
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["published"]["3"][0] == "c" * 40

    gate.by_repository["core-repo"] = [1]  # PR 3 closed; its branch now heads another PR.
    gate.heads[("core-repo", 3)] = "d" * 40
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert gate.statuses[-1][1][3] == _QUEUE_DONE
    assert gate.parked_heads[-1] == {3: "c" * 40}


def test_a_failed_done_status_is_logged_and_never_retried(tmp_path, caplog):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(), ("inspect", "core-repo", 2): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    caplog.set_level(logging.WARNING, logger=merge_open_prs.__name__)

    gate.by_repository["core-repo"] = [1]
    gate.responses[("queue_status", "core-repo")] = {"ok": True, "published": {
        "1": ["a" * 40, "queue=active reason=waiting_ci owner=queue pos=1"], "2": None}}
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert gate.statuses[1][1][2] == _QUEUE_DONE
    assert "queue status not published: core-repo#2" in caplog.text
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1"}

    del gate.responses[("queue_status", "core-repo")]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert 2 not in gate.statuses[2][1]


def test_integrity_stop_publishes_no_queue_status(tmp_path):
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("merge", "core-repo", 1): {"ok": False, "code": "INTERNAL_ERROR"}})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))
    assert gate.statuses == []


def test_an_integrity_stop_keeps_the_done_write_for_a_later_pass(tmp_path, caplog):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(), ("inspect", "core-repo", 2): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert set(store.read()["repositories"]["core-repo"]["published"]) == {"1", "2"}

    gate.by_repository["core-repo"] = [1]  # PR 2 merged, then the pass stops
    del gate.responses[("inspect", "core-repo", 1)]
    gate.responses[("merge", "core-repo", 1)] = {"ok": False, "code": "INTERNAL_ERROR"}
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len(gate.statuses) == 1  # the stop skipped every write
    assert "2" in store.read()["repositories"]["core-repo"]["published"]

    del gate.responses[("merge", "core-repo", 1)]
    gate.responses[("inspect", "core-repo", 1)] = _in_progress()
    caplog.set_level(logging.WARNING, logger=merge_open_prs.__name__)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert gate.statuses[-1][1][2] == _QUEUE_DONE
    assert "2" not in store.read()["repositories"]["core-repo"]["published"]
    assert "not published" not in caplog.text  # a written done status is no failure


def test_a_state_file_from_before_the_new_fields_loads_and_runs(tmp_path):
    """Written by the previous executor: no `started`, `base`, `published`
    or `review_ahead`. A parked `evidence_unavailable` record without a base
    is retried once, then carries the base it was parked on."""
    store = _queue_store(tmp_path)
    store.path.write_text(json.dumps({"version": 1, "cursor": 0, "repositories": {
        "core-repo": {
            "active": None,
            "parked": {"1": {"reason": "evidence_unavailable", "head": "a" * 40,
                             "run": [11, 1], "review": [None, "none"]}},
        },
        "docs-repo": {
            "active": {"pr": 7, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": 1000.0},
            "parked": {},
        },
    }}), encoding="utf-8")
    store.path.chmod(0o600)
    dead = _queue_inspect(ci="unavailable", conclusion=None, blocking_reason="CI_EVIDENCE_INVALID")
    gate = FakeGateApplication({"core-repo": [1], "docs-repo": [7]}, responses={
        ("inspect", "core-repo", 1): dead,
        ("inspect", "docs-repo", 7): _in_progress()})
    repositories = [_core_repo(tmp_path), merge_open_prs.Repository(
        "docs-repo", tmp_path, "forgejo", "acme/docs-repo")]

    first = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store,
                                               now=lambda: 1100.0)
    assert first[0]["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "CI_EVIDENCE_INVALID"}]
    state = store.read()["repositories"]
    assert state["core-repo"]["parked"]["1"]["base"] == "b" * 40
    assert state["docs-repo"]["active"]["started"] is True
    second = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store,
                                                now=lambda: 1200.0)
    assert second[0]["results"] == []


def _queue_cli(monkeypatch, tmp_path):
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    store = merge_open_prs.QueueStore(repositories=list(repositories))
    parked = {"reason": "ci_stalled", "head": "a" * 40, "run": [11, 1], "review": [None, "none"]}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {
            "1": parked, "2": {**parked, "reason": "conflict"}, "3": parked}}
        state["repositories"]["docs-repo"] = {"active": None, "parked": {"9": parked}}
        store.save(state)
    return store, parked


def test_queue_show_prints_the_selected_rows_without_changing_them(monkeypatch, tmp_path, capsys):
    store, _parked = _queue_cli(monkeypatch, tmp_path)
    before = store.path.read_bytes()

    assert merge_open_prs.main(["--queue-show", "docs"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "repositories": {"docs-repo": store.read()["repositories"]["docs-repo"]}}
    assert store.path.read_bytes() == before


def test_unpark_removes_only_the_named_parked_entries(monkeypatch, tmp_path, capsys):
    store, parked = _queue_cli(monkeypatch, tmp_path)

    assert merge_open_prs.main(["--unpark", "1", "--unpark", "2", "--unpark", "5",
                                "core-repo"]) == 0

    assert json.loads(capsys.readouterr().out) == {"repositories": [{
        "repository": "core-repo",
        "unparked": {"1": parked, "2": {**parked, "reason": "conflict"}}}]}
    rows = store.read()["repositories"]
    assert (rows["core-repo"]["parked"], rows["docs-repo"]["parked"]) == (
        {"3": parked}, {"9": parked})

    assert merge_open_prs.main(["--unpark-all", "core-repo"]) == 0
    assert store.read()["repositories"]["core-repo"]["parked"] == {}


def test_unpark_while_a_pass_holds_the_lock_changes_nothing(monkeypatch, tmp_path, capsys):
    store, _parked = _queue_cli(monkeypatch, tmp_path)
    before = store.path.read_bytes()

    with store.locked():
        assert merge_open_prs.main(["--unpark-all", "core-repo"]) == 1

    assert json.loads(capsys.readouterr().out) == {"repositories": [
        {"repository": "core-repo", "skipped": True, "reason": "AUTOMERGE_PASS_BUSY"}]}
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("argv", [
    ["--unpark", "1"], ["--unpark-all", "core-repo", "docs-repo"],
    ["--unpark", "1", "--queue-show", "core-repo"], ["--automerge", "--queue-show"],
])
def test_queue_commands_refuse_an_ambiguous_selection(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(argv)
    assert exc.value.code == 2


# ------------------------------------------------ Renovate queue fairness


def _selected(gate):
    return [call[2] for call in gate.calls if call[0] == "inspect"]


def test_a_lower_numbered_renovate_pr_queues_behind_an_agent_pr(tmp_path):
    store = _queue_store(tmp_path)
    # Equally ready: readiness outranks the slot, which orders within a tier.
    gate = FakeGateApplication(
        {"core-repo": [1, 2]}, head_refs={"core-repo": {1: "renovate/example-app", 2: "task/x"}},
        responses={("inspect", "core-repo", 1): _in_progress(),
                   ("inspect", "core-repo", 2): _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


def test_a_renovate_pr_is_selected_when_no_other_pr_is_eligible(tmp_path):
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/example-app"}},
        responses={("inspect", "core-repo", 1): _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))

    assert _selected(gate) == [1]


def test_an_active_renovate_pr_keeps_its_slot_when_an_agent_pr_appears(tmp_path):
    store = _queue_store(tmp_path)
    heads = {"core-repo": {1: "renovate/example-app"}}
    gate = FakeGateApplication({"core-repo": [1]}, head_refs=heads,
                               responses={("inspect", "core-repo", 1): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = [1, 2]
    heads["core-repo"][2] = "task/x"
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert _selected(gate) == [1]
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


@pytest.mark.parametrize("head_refs", [None, {"core-repo": {1: None, 2: 7}}])
def test_without_a_renovate_head_the_queue_is_plain_ascending(tmp_path, head_refs):
    store = _queue_store(tmp_path)
    # Equally ready: the position decides.
    gate = FakeGateApplication({"core-repo": [2, 1]}, head_refs=head_refs,
                               responses={("inspect", "core-repo", 1): _in_progress(),
                                          ("inspect", "core-repo", 2): _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1


def test_queue_positions_put_renovate_prs_after_agent_prs(tmp_path):
    gate = _slot_gate()  # Equally ready: the queue order decides.

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))

    assert gate.statuses[-1][1] == {
        1: "queue=eligible reason=- owner=queue pos=3",
        2: "queue=active reason=waiting_ci owner=queue pos=1",
        3: "queue=eligible reason=- owner=queue pos=2",
    }


def test_a_rejected_renovate_pr_parks_for_the_operator(tmp_path):
    """Its bot author never answers a review, so the park is operator-owned
    and counts toward `park_wait_s` while another PR is active."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1, 2]}, head_refs={"core-repo": {1: "task/x", 2: "renovate/a"}},
        responses={("inspect", "core-repo", 1): _queue_inspect(review="changes_requested"),
                   ("inspect", "core-repo", 2): _queue_inspect(review="changes_requested")})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    parked = store.read()["repositories"]["core-repo"]["parked"]
    assert {pr: record["reason"] for pr, record in parked.items()} == {
        "1": "changes_requested", "2": "renovate_changes_requested"}
    assert gate.statuses[-1][1] == {
        1: "queue=parked reason=changes_requested owner=author pos=-",
        2: "queue=parked reason=renovate_changes_requested owner=operator pos=-",
    }

    gate.by_repository["core-repo"] = [1, 2, 3]
    gate.head_refs["core-repo"][3] = "task/y"
    gate.responses[("inspect", "core-repo", 3)] = _in_progress()
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1250.0)[0]

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 3
    assert summary["park_wait_s"] == 250


def test_review_ahead_rotates_over_queue_order_and_wraps(tmp_path):
    store = _queue_store(tmp_path)
    responses = {("inspect", "core-repo", 3): _in_progress()}
    for pr in (1, 2, 4):
        responses[("inspect", "core-repo", pr)] = _stale_base(review_state="not_approved")
    gate = FakeGateApplication(
        {"core-repo": [1, 2, 3, 4]}, responses=responses,
        head_refs={"core-repo": {1: "renovate/a", 2: "renovate/b", 3: "task/x", 4: "task/y"}})
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {}, "review_ahead": 1}
        store.save(state)

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=store, max_reviews=1)

    # Queue order is 3, 4, 1, 2. Selection ranks all four; the review
    # ahead then resumes at 2 after the cursor on 1 and wraps to 4, skipping 3.
    assert _selected(gate)[:4] == [3, 4, 1, 2]
    assert _selected(gate)[4:] == [2, 4, 1]


def test_queue_state_accepts_a_renovate_park(tmp_path):
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {"1": {
            "reason": "renovate_changes_requested", "head": "a" * 40, "run": [11, 1],
            "review": [None, "none"], "since": 1.0}}}
        store.save(state)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_changes_requested")


def _seed_agent_streak(store, streak):
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": None, "parked": {}, "agent_streak": streak}
        store.save(state)


def _slot_gate(**kwargs):
    return FakeGateApplication(
        {"core-repo": [1, 2, 3]},
        head_refs={"core-repo": {1: "renovate/a", 2: "task/x", 3: "task/y"}},
        responses={("inspect", "core-repo", pr): _in_progress() for pr in (1, 2, 3)},
        **kwargs)


def test_a_due_renovate_slot_selects_the_renovate_pr_and_resets_the_streak(tmp_path):
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = _slot_gate()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (1, 0)


def test_an_agent_selection_before_the_slot_is_due_counts_toward_it(tmp_path):
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY - 1)
    gate = _slot_gate()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (2, merge_open_prs.RENOVATE_SLOT_EVERY)


def test_a_state_without_a_streak_selects_agent_prs_first_and_starts_counting(tmp_path):
    store = _queue_store(tmp_path)
    gate = _slot_gate()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 1)


def test_a_due_slot_with_only_a_held_renovate_pr_selects_an_agent_pr_and_keeps_counting(tmp_path):
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = _slot_gate(labels={"core-repo": {1: ["major", "renovate"], 2: [], 3: []}})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_major"
    assert (row["active"]["pr"], row["agent_streak"]) == (
        2, merge_open_prs.RENOVATE_SLOT_EVERY + 1)


def test_the_slot_is_spent_by_the_first_renovate_selection_of_the_same_pass(tmp_path):
    """The reset takes effect in the pass that selected the Renovate PR."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = FakeGateApplication(
        {"core-repo": [1, 2, 3]},
        head_refs={"core-repo": {1: "renovate/a", 2: "renovate/b", 3: "task/x"}},
        responses={("inspect", "core-repo", 1): _queue_inspect(),  # Merges, freeing the slot.
                   ("inspect", "core-repo", 2): _in_progress(),
                   ("inspect", "core-repo", 3): _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[2] for call in gate.calls if call[0] == "merge"] == [1]
    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (3, 1)


def test_selections_alternate_between_agent_and_renovate_prs(tmp_path):
    """One-half Renovate share: with both queues populated every second merge
    is Renovate. The expected order is literal, not derived from the constant."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1, 2, 3, 4, 5, 6]},
        head_refs={"core-repo": {1: "renovate/a", 2: "renovate/b", 3: "renovate/c",
                                 4: "task/x", 5: "task/y", 6: "task/z"}})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    merged = [call[2] for call in gate.calls if call[0] == "merge"]
    assert merged == [4, 1, 5, 2, 6, 3]
    kinds = ["renovate" if pr <= 3 else "agent" for pr in merged]
    assert kinds == ["agent", "renovate", "agent", "renovate", "agent", "renovate"]


def test_queue_positions_put_the_renovate_pr_first_when_the_slot_is_due(tmp_path):
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = _slot_gate()
    gate.responses[("inspect", "core-repo", 1)] = _in_progress()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert gate.statuses[-1][1] == {
        1: "queue=active reason=waiting_ci owner=queue pos=1",
        2: "queue=eligible reason=- owner=queue pos=2",
        3: "queue=eligible reason=- owner=queue pos=3",
    }


def test_queue_positions_put_a_waiting_renovate_pr_first_while_an_agent_pr_is_active(tmp_path):
    """The active PR is seeded, so selection is skipped and only the queue order can
    put the Renovate PR at position 2: ascending order would put it at 3."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    with store.locked() as state:
        state["repositories"]["core-repo"]["active"] = {
            "pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
            "wait_since": 1000.0,
        }
        store.save(state)
    gate = FakeGateApplication(
        {"core-repo": [1, 2, 3]},
        head_refs={"core-repo": {1: "task/x", 2: "task/y", 3: "renovate/a"}},
        responses={("inspect", "core-repo", 1): _in_progress()},
    )

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    assert gate.statuses[-1][1] == {
        1: "queue=active reason=waiting_ci owner=queue pos=1",
        3: "queue=eligible reason=- owner=queue pos=2",
        2: "queue=eligible reason=- owner=queue pos=3",
    }


@pytest.mark.parametrize("streak", [0, 3, 7])
def test_queue_state_accepts_a_non_negative_agent_streak(tmp_path, streak):
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, streak)
    assert store.read()["repositories"]["core-repo"]["agent_streak"] == streak


@pytest.mark.parametrize("streak", [-1, True, False, 1.0, "3", None])
def test_queue_state_rejects_an_invalid_agent_streak(tmp_path, streak):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": None, "parked": {}, "agent_streak": streak}
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


def test_queue_state_without_an_agent_streak_still_loads(tmp_path):
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {}}
        store.save(state)
    assert "agent_streak" not in store.read()["repositories"]["core-repo"]


# ------------------------------------------------ Renovate major updates


def _major_gate(first=None):
    responses = {} if first is None else {("inspect", "core-repo", 1): first}
    return FakeGateApplication(
        {"core-repo": [1, 2]}, head_refs={"core-repo": {1: "renovate/major-x", 2: "task/x"}},
        labels={"core-repo": {1: ["major", "renovate"], 2: []}},
        responses={("inspect", "core-repo", 2): _in_progress(), **responses})


def _reviewed_or_refreshed(gate, pr):
    return [call for call in gate.calls
            if call[0] in {"review", "refresh", "merge"} and call[2] == pr]


def test_a_major_renovate_pr_parks_for_the_operator_before_any_review(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate(_queue_inspect(review="not_approved"))

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "renovate_major"
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained", "reason": "RENOVATE_MAJOR"} in summary["results"]
    assert gate.statuses[-1][1][1] == "queue=parked reason=renovate_major owner=operator pos=-"


def test_a_major_park_survives_a_head_change_and_is_not_an_alerting_wait(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate()
    # PR 2 merges on the first pass; the second pass must read that state.
    gate.responses[("inspect", "core-repo", 2)] = [
        _queue_inspect(), _queue_inspect(), _queue_inspect(pr_state="merged")]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)
    gate.by_repository["core-repo"] = [1]
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d")
    gate.calls.clear()

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 90000.0)[0]

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "renovate_major"
    assert _selected(gate) == []
    assert summary["park_wait_s"] == 0
    # The forge write keeps it parked on the new head, not released.
    assert gate.parked_heads[-1] == {}
    assert gate.statuses[-1][1][1] == "queue=parked reason=renovate_major owner=operator pos=-"


def test_removing_the_major_label_releases_the_park(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = ["renovate"]
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_an_active_pr_labelled_major_gives_up_its_slot(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
        labels={"core-repo": {1: ["renovate"]}},
        responses={("inspect", "core-repo", 1): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    gate.labels["core-repo"][1] = ["major", "renovate"]

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "renovate_major"


def test_review_ahead_never_reviews_a_major_renovate_pr(tmp_path):
    gate = _major_gate(_queue_inspect(review="not_approved"))

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path), max_reviews=1)

    assert _reviewed_or_refreshed(gate, 1) == []


def test_a_major_label_on_an_agent_pr_does_not_park_it(tmp_path):
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "task/x"}},
        labels={"core-repo": {1: ["major"]}})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_the_renovate_label_alone_queues_a_pr_behind_agent_prs(tmp_path):
    gate = FakeGateApplication(
        {"core-repo": [1, 2]}, head_refs={"core-repo": {1: "deps/x", 2: "task/x"}},
        labels={"core-repo": {1: ["renovate"], 2: []}},
        responses={("inspect", "core-repo", 1): _in_progress(),  # Equally ready.
                   ("inspect", "core-repo", 2): _in_progress()})
    store = _queue_store(tmp_path)

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


def test_an_unreadable_major_pr_is_neither_selected_nor_advertised(tmp_path):
    gate = _major_gate({"ok": False, "code": "PR_INSPECT_FAILED"})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), max_reviews=1)[0]

    assert _reviewed_or_refreshed(gate, 1) == []
    assert 1 not in gate.statuses[-1][1]
    assert {"pr": 1, "outcome": "retained", "reason": "PR_INSPECT_FAILED"} in summary["results"]


@pytest.mark.parametrize("labels", [["x"], "major", {"1": "major"}, [7]])
def test_malformed_labels_mark_nothing(labels):
    listed = {"labels": {"1": labels}} if labels != ["x"] else {"labels": labels}
    assert merge_open_prs._renovate_holds(listed, {1}) == {}


@pytest.mark.parametrize(("labels", "reason"), [
    (["premerge", "renovate"], "renovate_premerge"),
    (["major", "premerge", "renovate"], "renovate_major"),
])
def test_a_premerge_renovate_pr_parks_for_the_operator(tmp_path, labels, reason):
    store = _queue_store(tmp_path)
    gate = _major_gate(_queue_inspect(review="not_approved"))
    gate.labels["core-repo"][1] = labels

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == reason
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained", "reason": reason.upper()} in summary["results"]
    assert summary["park_wait_s"] == 0


def test_removing_the_premerge_label_releases_the_park(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate()
    gate.labels["core-repo"][1] = ["premerge", "renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = ["renovate"]
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


# ------------------------------------- Renovate holds fail closed, bind a head


@pytest.mark.parametrize("labels", [
    {1: None, 2: []},  # the gate could not read them
    {1: 7, 2: []},
    {1: [7], 2: []},
    {1: "major", 2: []},
    {2: []},  # no entry for the Renovate PR at all
])
def test_unread_labels_park_a_renovate_pr_for_the_operator(tmp_path, labels):
    """An unread label list is not an empty one: a `major` or `premerge`
    update must not reach review or merge because the listing changed shape."""
    store = _queue_store(tmp_path)
    gate = _major_gate(_queue_inspect(review="not_approved"))
    gate.labels["core-repo"] = labels

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_labels_unknown"
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained",
            "reason": "RENOVATE_LABELS_UNKNOWN"} in summary["results"]
    assert gate.statuses[-1][1][1] == (
        "queue=parked reason=renovate_labels_unknown owner=operator pos=-")


def test_an_unread_labels_park_is_an_alerting_wait_and_ends_when_they_read(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate()
    gate.labels["core-repo"][1] = None
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)
    gate.by_repository["core-repo"] = [1]

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 12000.0)[0]
    assert summary["park_wait_s"] == 11000

    gate.labels["core-repo"][1] = ["renovate"]
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert "1" not in row["parked"]
    # No hold was ever read, so nothing was consented to.
    assert "consented" not in row
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


@pytest.mark.parametrize("unread", [None, 7])
def test_unread_labels_keep_an_existing_hold_park(tmp_path, unread):
    """An unread listing must not release any held park."""
    store = _queue_store(tmp_path)
    gate = _major_gate()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = unread
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "renovate_major"
    assert _reviewed_or_refreshed(gate, 1) == []

    # The operator removed the label meanwhile: that is still a consent.
    gate.labels["core-repo"][1] = ["renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_a_listing_that_no_longer_marks_a_held_pr_as_renovate_keeps_its_park(tmp_path):
    """No head branch and unread labels say nothing about the hold label:
    the park stays, and nothing is consented to."""
    store = _queue_store(tmp_path)
    gate = _major_gate()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = [1]
    gate.head_refs["core-repo"] = {}
    gate.labels["core-repo"][1] = None
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_major"
    assert "consented" not in row
    assert _reviewed_or_refreshed(gate, 1) == []


def _consented_gate(tmp_path, label="premerge"):
    """Pass one parks PR 1 on its hold label; pass two sees it removed while
    the PR's CI is still running, so it stays the active PR."""
    store = _queue_store(tmp_path)
    gate = _major_gate()
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = [label, "renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.labels["core-repo"][1] = ["renovate"]
    gate.responses[("inspect", "core-repo", 1)] = _in_progress()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.calls.clear()
    return store, gate


@pytest.mark.parametrize("label", ["premerge", "major"])
def test_removing_the_hold_label_records_the_consented_head(tmp_path, label):
    store, _gate = _consented_gate(tmp_path, label)

    row = store.read()["repositories"]["core-repo"]
    assert row["consented"] == {"1": ["a" * 40, f"renovate_{label}"]}
    assert "1" not in row["parked"]
    assert row["active"]["pr"] == 1


@pytest.mark.parametrize("label", ["premerge", "major"])
def test_a_new_renovate_head_after_consent_parks_the_pr_again(tmp_path, label):
    store, gate = _consented_gate(tmp_path, label)
    # Renovate pushes a newer version the operator never checked.
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d")

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == f"renovate_{label}"
    assert row["active"] is None
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained", "reason": "RENOVATE_CONSENT_STALE",
            "code": f"RENOVATE_{label.upper()}"} in summary["results"]
    assert gate.statuses[-1][1][1] == (
        "queue=parked reason=renovate_consent_stale owner=operator pos=- "
        f"consented={'a' * 40} hold={label}")

    # The label stays absent: that is no fresh consent, and neither is
    # `--unpark`.
    for unpark in (False, True):
        if unpark:
            with store.locked() as state:
                state["repositories"]["core-repo"]["parked"].pop("1")
                store.save(state)
        gate.calls.clear()
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                           max_reviews=1)
        row = store.read()["repositories"]["core-repo"]
        assert row["parked"]["1"]["reason"] == f"renovate_{label}"
        assert row["consented"] == {"1": ["a" * 40, f"renovate_{label}"]}
        assert _reviewed_or_refreshed(gate, 1) == []


@pytest.mark.parametrize("reads_before_push", [1, 2])
@pytest.mark.parametrize("next_state", ["green", "stale"])
def test_consent_is_checked_at_the_operation_head(tmp_path, reads_before_push, next_state):
    store, gate = _consented_gate(tmp_path)
    changed = (_queue_inspect(head="d") if next_state == "green"
               else _stale_base(head_sha="d" * 40))
    gate.responses[("inspect", "core-repo", 1)] = (
        [_queue_inspect(head="a") for _ in range(reads_before_push)]
        + [changed for _ in range(6)])

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert not _reviewed_or_refreshed(gate, 1)
    assert any(result.get("reason") == "RENOVATE_CONSENT_STALE"
               for result in summary["results"])
    row = store.read()["repositories"]["core-repo"]
    assert row["consented"]["1"][0] == "a" * 40
    assert row["parked"]["1"]["reason"] == "renovate_premerge"


def test_a_consented_pr_the_listing_no_longer_marks_as_renovate_gets_no_turn(tmp_path):
    store, gate = _consented_gate(tmp_path)
    gate.head_refs["core-repo"] = {}
    gate.labels["core-repo"][1] = None
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d")

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert _reviewed_or_refreshed(gate, 1) == []
    assert store.read()["repositories"]["core-repo"]["consented"] == {
        "1": ["a" * 40, "renovate_premerge"]}


def test_re_adding_and_removing_the_label_consents_to_the_new_head(tmp_path):
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d")
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    gate.labels["core-repo"][1] = ["premerge", "renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_premerge"
    assert "consented" not in row

    gate.labels["core-repo"][1] = ["renovate"]
    gate.responses[("inspect", "core-repo", 1)] = [_queue_inspect(head="d"), _in_progress(head="d")]
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert row["consented"] == {"1": ["d" * 40, "renovate_premerge"]}
    assert row["active"]["pr"] == 1


@pytest.mark.parametrize("already_parked", [False, True])
def test_proven_external_refresh_carries_consent_but_still_waits_for_ci(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_parked: bool) -> None:
    store, gate = _consented_gate(tmp_path)
    if already_parked:
        with store.locked() as state:
            row = state["repositories"]["core-repo"]
            row["active"] = None
            row["parked"]["1"] = {"reason": "renovate_premerge", "head": "d" * 40,
                                  "run": [11, 1], "review": [None, None]}
            store.save(state)
    gate.responses[("inspect", "core-repo", 1)] = _in_progress(head="d")
    proofs = []
    def proof(*args: object) -> bool:
        proofs.append(args)
        return True
    monkeypatch.setattr(gate, "consent_preserved", proof, raising=False)

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert row["consented"]["1"] == ["d" * 40, "renovate_premerge"]
    assert "1" not in row["parked"]
    assert row["active"]["pr"] == 1
    assert proofs == [("core-repo", 1, "a" * 40, "d" * 40, "b" * 40)]
    assert _reviewed_or_refreshed(gate, 1) == []


def test_production_application_exposes_the_consent_proof() -> None:
    """The fake application defines the proof; the real one must too.

    A missing method would raise AttributeError on the first externally
    refreshed consented head and abort the whole pass.
    """
    application = merge_open_prs._gate_application()
    assert callable(getattr(application, "consent_preserved", None))
    assert callable(getattr(application.operations, "consent_preserved", None))


def test_proven_external_refresh_still_needs_independent_review(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d", review="not_approved")
    monkeypatch.setattr(gate, "consent_preserved", lambda *_: True)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["review"]
    assert store.read()["repositories"]["core-repo"]["consented"]["1"][0] == "d" * 40


def test_failed_proof_is_bounded_but_a_new_head_or_unpark_retries_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = _in_progress(head="d")
    proofs = []
    def refuse(*args: object) -> bool:
        proofs.append(args)
        return False
    monkeypatch.setattr(gate, "consent_preserved", refuse)
    for _ in range(2):
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len(proofs) == 1
    with store.locked() as state:
        state["repositories"]["core-repo"]["parked"].pop("1")
        store.save(state)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len(proofs) == 2
    gate.responses[("inspect", "core-repo", 1)] = _in_progress(head="e")
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len(proofs) == 3
    row = store.read()["repositories"]["core-repo"]
    assert row["consented"]["1"][0] == "a" * 40
    assert row["parked"]["1"]["head"] == "e" * 40
    assert _reviewed_or_refreshed(gate, 1) == []


def test_a_push_after_the_proof_cannot_reach_review_or_merge(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = (
        [_in_progress(head="d")] + [_queue_inspect(head="e") for _ in range(10)])
    monkeypatch.setattr(gate, "consent_preserved", lambda *_: True)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert _reviewed_or_refreshed(gate, 1) == []
    row = store.read()["repositories"]["core-repo"]
    assert row["consented"]["1"][0] == "d" * 40
    assert row["parked"]["1"]["head"] == "e" * 40


def test_the_executor_refresh_keeps_the_consent(tmp_path):
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = _stale_base()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["refresh"]
    row = store.read()["repositories"]["core-repo"]
    assert row["consented"] == {"1": ["c" * 40, "renovate_premerge"]}

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="c")
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]
    assert "consented" not in store.read()["repositories"]["core-repo"]


def _bypass_records(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING
            and "merged outside the queue" in record.getMessage()]


@pytest.mark.parametrize(("label", "merged_by"), [
    ("major", "acme"), ("premerge", "acme"),
    # The executor never merges a held PR, whoever the forge names.
    ("major", None), ("premerge", merge_open_prs.FORGEJO_LOGIN),
])
def test_a_held_renovate_pr_merged_outside_the_queue_is_warned_once(
        tmp_path, caplog, label, merged_by):
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    store = _queue_store(tmp_path)
    gate = _major_gate()
    gate.labels["core-repo"][1] = [label, "renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = [2]
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        pr_state="merged", **({"merged_by": merged_by} if merged_by else {}))

    for _ in range(2):
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert _bypass_records(caplog) == [
        f"automerge: renovate hold merged outside the queue core-repo#1 by "
        f"{merged_by or 'unknown'}"]
    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]


def test_a_consented_renovate_pr_merged_by_hand_is_warned(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    store, gate = _consented_gate(tmp_path)
    gate.by_repository["core-repo"] = []
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        pr_state="merged", merged_by="acme")

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert _bypass_records(caplog) == [
        "automerge: renovate hold merged outside the queue core-repo#1 by acme"]
    assert "consented" not in store.read()["repositories"]["core-repo"]


@pytest.mark.parametrize("departed", [
    _queue_inspect(pr_state="merged", merged_by=merge_open_prs.FORGEJO_LOGIN),
    _queue_inspect(pr_state="closed"),
])
def test_no_bypass_warning_for_an_executor_merge_or_a_close(tmp_path, caplog, departed):
    """A consented PR the executor merged before a crash, or any closed PR."""
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    store, gate = _consented_gate(tmp_path)
    gate.by_repository["core-repo"] = []
    gate.responses[("inspect", "core-repo", 1)] = departed

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert _bypass_records(caplog) == []
    assert "consented" not in store.read()["repositories"]["core-repo"]


def test_an_executor_merge_of_a_consented_pr_is_no_bypass(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.by_repository["core-repo"] = []
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        pr_state="merged", merged_by="someone-else")

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[0] for call in gate.calls if call[0] == "merge"] == ["merge"]
    assert _bypass_records(caplog) == []


def test_queue_state_accepts_a_consent_and_rejects_a_malformed_one(tmp_path):
    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": None, "parked": {}, "consented": {"1": ["a" * 40, "renovate_major"]}}
        store.save(state)
    assert store.read()["repositories"]["core-repo"]["consented"] == {
        "1": ["a" * 40, "renovate_major"]}
    for consented in ([], {"x": ["a" * 40, "renovate_major"]}, {"1": ["a" * 40]},
                      {"1": ["z" * 40, "renovate_major"]}, {"1": ["a" * 40, "conflict"]},
                      {"1": ["a" * 40, ["renovate_major"]]}):
        with store.locked() as state:
            state["repositories"]["core-repo"]["consented"] = consented
            with pytest.raises(merge_open_prs.QueueStateError):
                store.save(state)


def test_consent_reasons_are_the_label_hold_reasons():
    from git_gate.automerge_queue import CONSENT_REASONS, PARK_OWNERS

    assert CONSENT_REASONS == merge_open_prs.RENOVATE_LABEL_HOLD_REASONS
    assert {PARK_OWNERS[reason] for reason in merge_open_prs.RENOVATE_HOLD_REASONS} == {
        "operator"}


def test_a_refresh_the_gate_refuses_keeps_the_consent_on_its_head(tmp_path):
    """The gate refuses a refresh whose readback is not its update merge
    (`PR_REFRESH_STATE_CHANGED`): the consent stays on the head it named."""
    store, gate = _consented_gate(tmp_path)
    gate.responses[("inspect", "core-repo", 1)] = _stale_base()
    gate.responses[("refresh", "core-repo", 1)] = {
        "ok": False, "code": "PR_REFRESH_STATE_CHANGED"}

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["refresh"]
    assert store.read()["repositories"]["core-repo"]["consented"] == {
        "1": ["a" * 40, "renovate_premerge"]}

    # Whatever the branch heads now was never consented to.
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="c")
    gate.calls.clear()
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert _reviewed_or_refreshed(gate, 1) == []
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_premerge")
    assert any(result.get("reason") == "RENOVATE_CONSENT_STALE"
               for result in summary["results"])


# ------------- a hold label gone before any pass parked the PR: no consent


def _unconsented_gate(history, labels=("renovate",)):
    """PR 1 is a Renovate PR whose forge timeline shows `history`, listed
    with `labels`: any hold label in `history` went before a pass saw it."""
    gate = _major_gate()
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = None if labels is None else list(labels)
    gate.label_history[("core-repo", 1)] = set(history)
    return gate


@pytest.mark.parametrize("label", ["premerge", "major"])
def test_a_hold_label_removed_before_any_park_keeps_the_pr_parked(tmp_path, label):
    """A PR opened during a deploy hold is never parked; the operator removing
    its label meanwhile recorded no consent, so it must not merge."""
    store = _queue_store(tmp_path)
    gate = _unconsented_gate({label, "renovate"})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_consent_missing"
    assert "consented" not in row
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained",
            "reason": "RENOVATE_CONSENT_MISSING"} in summary["results"]
    assert gate.statuses[-1][1][1] == (
        "queue=parked reason=renovate_consent_missing owner=operator pos=-")

    # The absent label is no consent on a later pass, and neither is `--unpark`.
    for unpark in (False, True):
        if unpark:
            with store.locked() as state:
                state["repositories"]["core-repo"]["parked"].pop("1")
                store.save(state)
        gate.calls.clear()
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                           max_reviews=1)
        row = store.read()["repositories"]["core-repo"]
        assert row["parked"]["1"]["reason"] == "renovate_consent_missing"
        assert "consented" not in row
        assert _reviewed_or_refreshed(gate, 1) == []


def test_a_missing_consent_is_given_by_adding_and_removing_the_label(tmp_path):
    store = _queue_store(tmp_path)
    gate = _unconsented_gate({"major", "renovate"})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    gate.labels["core-repo"][1] = ["major", "renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_major"

    gate.labels["core-repo"][1] = ["renovate"]
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d")
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]
    assert [call for call in gate.calls if call[0] == "merge"][0][3] == "d" * 40


def test_an_unread_listing_ends_in_a_consent_park_not_a_release(tmp_path):
    """The labels read unreadable while the hold label was still on; by the
    next readable listing the operator had removed it, consenting to nothing."""
    store = _queue_store(tmp_path)
    gate = _unconsented_gate({"premerge", "renovate"}, labels=None)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_labels_unknown")

    gate.labels["core-repo"][1] = ["renovate"]
    gate.calls.clear()
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_consent_missing"
    assert "consented" not in row
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained",
            "reason": "RENOVATE_CONSENT_MISSING"} in summary["results"]


@pytest.mark.parametrize("labels", [("renovate",), None])
def test_a_renovate_pr_that_never_carried_a_hold_label_still_merges(tmp_path, labels):
    store = _queue_store(tmp_path)
    gate = _unconsented_gate({"renovate"}, labels=labels)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.labels["core-repo"][1] = ["renovate"]
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert "1" not in row["parked"]
    assert "consented" not in row
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


@pytest.mark.parametrize("history", [None, "major", [7], {"major": True}])
def test_an_unreadable_label_history_holds_a_renovate_pr(tmp_path, history):
    store = _queue_store(tmp_path)
    gate = _unconsented_gate({"renovate"})
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(label_history=history)

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_consent_missing")
    assert _reviewed_or_refreshed(gate, 1) == []


def test_an_absent_label_history_cannot_be_told_from_a_held_one():
    assert merge_open_prs._consent_missing({"ok": True}) is True
    assert merge_open_prs._consent_missing({"label_history": ["renovate"]}) is False


@pytest.mark.parametrize(("history", "reviewed"), [
    ({"premerge", "renovate"}, False),
    ({"renovate"}, True),  # the control: review-ahead does reach PR 1
])
def test_review_ahead_spends_no_review_on_a_pr_without_consent(tmp_path, history, reviewed):
    gate = _unconsented_gate(history)
    gate.by_repository["core-repo"] = [1, 2]
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(review="not_approved")

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path), max_reviews=1)

    assert ([call[2] for call in gate.calls if call[0] == "review"] == [1]) is reviewed


# ------------------------------- Renovate branch freshness is the executor's


def _red_renovate_gate(conclusion="failure"):
    return FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
        labels={"core-repo": {1: ["renovate"]}},
        responses={("inspect", "core-repo", 1): _stale_base(
            conclusion, ci_state="red", ci_run_id=11, ci_run_attempt=1)})


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
@pytest.mark.parametrize(("retry_base", "retry_run", "retry_attempt"), [
    pytest.param("b", 13, 1, id="new_run"),
    pytest.param("b", 12, 2, id="new_attempt"),
])
def test_red_renovate_refresh_parks_until_new_evidence(
    tmp_path, conclusion, retry_base, retry_run, retry_attempt,
):
    """A red refresh result parks without a loop. A new trusted run/attempt
    can admit the executor-created head once again."""
    store = _queue_store(tmp_path)
    gate = _red_renovate_gate(conclusion)

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert [call[0] for call in gate.calls if call[0] in {"refresh", "review", "merge"}] == [
        "refresh"]
    assert {"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40} in summary["results"]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1 and row["active"]["refreshed_head"] == "c" * 40
    assert row["parked"] == {}

    gate.responses[("inspect", "core-repo", 1)] = _stale_base(
        conclusion, ci_state="red", head_sha="c" * 40, ci_run_id=12, ci_run_attempt=1)
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert not any(call[0] == "refresh" for call in gate.calls)
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["reason"] == "renovate_ci_failed"
    assert (parked["head"], parked["base"], parked["run"]) == (
        "c" * 40, "b" * 40, [12, 1])

    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert not any(call[0] == "refresh" for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"] == parked

    gate.responses[("inspect", "core-repo", 1)] = _stale_base(
        conclusion, ci_state="red", head_sha="c" * 40, base_sha=retry_base * 40,
        ci_run_id=retry_run, ci_run_attempt=retry_attempt)
    gate.responses[("refresh", "core-repo", 1)] = {
        "ok": True, "code": "PR_REFRESHED", "head_sha": "e" * 40}
    gate.calls.clear()
    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert [call for call in gate.calls if call[0] in {"refresh", "review", "merge"}] == [
        ("refresh", "core-repo", 1, "c" * 40, retry_base * 40)]
    assert {"pr": 1, "outcome": "refreshed", "head_sha": "e" * 40} in summary["results"]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["refreshed_head"] == "e" * 40
    assert row["parked"] == {}

    gate.responses[("inspect", "core-repo", 1)] = _stale_base(
        conclusion, ci_state="red", head_sha="e" * 40, base_sha=retry_base * 40,
        ci_run_id=14, ci_run_attempt=1)
    for _ in range(2):
        gate.calls.clear()
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
        assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)
        row = store.read()["repositories"]["core-repo"]
        assert row["active"] is None
        assert row["parked"]["1"]["reason"] == "renovate_ci_failed"
        assert (row["parked"]["1"]["head"], row["parked"]["1"]["base"],
                row["parked"]["1"]["run"]) == ("e" * 40, retry_base * 40, [14, 1])


def test_a_red_stale_base_pr_that_is_not_renovate_is_never_refreshed(tmp_path):
    """The protection is kept: a red agent PR's failure is its own."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "task/x"}},
        labels={"core-repo": {1: []}},
        responses={("inspect", "core-repo", 1): _stale_base(
            "failure", ci_state="red", ci_run_id=11, ci_run_attempt=1)})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert not any(call[0] == "refresh" for call in gate.calls)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"


@pytest.mark.parametrize("renovate", [True, False])
def test_main_advance_keeps_a_failed_pr_parked(tmp_path, renovate):
    store = _queue_store(tmp_path)
    gate = _red_renovate_gate()
    if not renovate:
        gate.head_refs["core-repo"][1] = "task/x"
        gate.labels["core-repo"][1] = []
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(
        ci_state="red", blocking_reason="CI_RUN_FAILED", ci_run_conclusion="failure",
        ci_run_id=11, ci_run_attempt=1)
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["reason"] == ("renovate_ci_failed" if renovate else "ci_failed")

    for base in ("d", "e", "f"):
        gate.calls.clear()
        gate.responses[("inspect", "core-repo", 1)] = _stale_base(
            "failure", base_sha=base * 40, ci_state="pending", ci_run_id=11, ci_run_attempt=1)
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
        assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)
        assert store.read()["repositories"]["core-repo"]["parked"]["1"] == parked


def _red_head(**extra):
    """A failed run on the current base: no catch-up refresh can help it."""
    return _queue_inspect(ci="red", conclusion="failure", blocking_reason="CI_RUN_FAILED",
                          **extra)


def _red_slot_gate(*, second="renovate/b"):
    """A red Renovate PR 1, a green-so-far PR 2, and an agent PR 3."""
    return FakeGateApplication(
        {"core-repo": [1, 2, 3]},
        head_refs={"core-repo": {1: "renovate/a", 2: second, 3: "task/y"}},
        labels={"core-repo": {1: ["renovate"], 2: ["renovate"] if "renovate" in second else [],
                              3: []}},
        responses={("inspect", "core-repo", 1): _red_head(),
                   ("inspect", "core-repo", 2): _in_progress(),
                   ("inspect", "core-repo", 3): _in_progress()})


def test_review_ahead_parks_a_renovate_pr_whose_current_head_failed_before_it_takes_a_slot(
        tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1, 2, 3, 4]},
        head_refs={"core-repo": {1: "task/x", 2: "renovate/a", 3: "task/y", 4: "renovate/b"}},
        labels={"core-repo": {1: [], 2: ["renovate"], 3: [], 4: ["renovate"]}},
        responses={("inspect", "core-repo", 1): _in_progress(),
                   ("inspect", "core-repo", 2): _red_head(),
                   ("inspect", "core-repo", 3): _in_progress(),
                   ("inspect", "core-repo", 4): _queue_inspect(head="e", base="f",
                                                               review="not_approved")})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1, now=lambda: 1000.0)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 1
    assert row["parked"]["2"]["reason"] == "renovate_ci_failed"
    assert (row["parked"]["2"]["head"], row["parked"]["2"]["run"]) == ("a" * 40, [11, 1])
    assert {"pr": 2, "outcome": "retained", "reason": "CI_RUN_FAILED",
            "review_ahead": True} in summary["results"]
    # The park spends no review: the next queued PR still gets the one review ahead.
    assert [call[2] for call in gate.calls if call[0] == "review"] == [4]
    assert not any(call[0] in {"refresh", "merge"} and call[2] == 2 for call in gate.calls)
    assert gate.statuses[-1][1][2] == "queue=parked reason=renovate_ci_failed owner=operator pos=-"


@pytest.mark.parametrize(("head_ref", "inspect"), [
    pytest.param("renovate/a", _stale_base("failure", ci_state="red", review_state="not_approved"),
                 id="renovate_red_on_a_moved_base_keeps_its_one_refresh"),
    pytest.param("task/x", _red_head(review="not_approved"), id="agent_pr_is_not_parked_early"),
])
def test_review_ahead_leaves_these_red_prs_unparked(tmp_path, head_ref, inspect):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1, 2]}, head_refs={"core-repo": {1: "task/y", 2: head_ref}},
        labels={"core-repo": {1: [], 2: ["renovate"] if head_ref.startswith("renovate/") else []}},
        responses={("inspect", "core-repo", 1): _in_progress(),
                   ("inspect", "core-repo", 2): inspect})

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1, now=lambda: 1000.0)

    assert store.read()["repositories"]["core-repo"]["parked"] == {}


def test_a_red_renovate_head_does_not_spend_the_renovate_slot(tmp_path):
    """Parking PR 1 resets nothing: the due slot goes on to the next Renovate PR."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = _red_slot_gate()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_ci_failed"
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 0)
    assert _reviewed_or_refreshed(gate, 3) == []


@pytest.mark.parametrize("ci_state", [
    pytest.param("red", id="red_projection"),
    pytest.param("pending", id="real_projection_ci_base_stale_pending"),
])
def test_a_red_renovate_pr_on_a_moved_base_keeps_its_refresh_and_the_slot(tmp_path, ci_state):
    """The real evaluator projects a run that failed on an older base as
    `CI_BASE_STALE` with `ci_state` pending and a failure conclusion."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
        labels={"core-repo": {1: ["renovate"]}},
        responses={("inspect", "core-repo", 1): _stale_base(
            "failure", ci_state=ci_state, ci_run_id=11, ci_run_attempt=1)})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert [call[0] for call in gate.calls if call[0] == "refresh"] == ["refresh"]
    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (1, merge_open_prs.RENOVATE_SLOT_EVERY)


@pytest.mark.parametrize("inspected", [
    pytest.param(_in_progress(), id="pending_current_run_without_a_conclusion"),
    pytest.param(_stale_base("success", ci_run_id=11, ci_run_attempt=1),
                 id="stale_base_pending_run_that_succeeded"),
])
def test_a_renovate_pr_without_a_failed_head_still_resets_the_streak(tmp_path, inspected):
    """Control: only a failure conclusion keeps the streak; any other pending
    Renovate selection spends the slot."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/x"}},
        labels={"core-repo": {1: ["renovate"]}}, responses={("inspect", "core-repo", 1): inspected})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (1, 0)


def _masked_stale_failure(**overrides):
    """The gate projection for a Renovate head whose trusted run failed on an
    older base while a review or conflict blocker outranks `CI_BASE_STALE`:
    `ci_reason` keeps the CI-level fact that `blocking_reason` hides."""
    return _stale_base("failure", ci_run_id=11, ci_run_attempt=1, **overrides)


@pytest.mark.parametrize(("masked", "settle"), [
    pytest.param({"blocking_reason": "PR_REVIEW_CHANGES_REQUESTED",
                  "review_state": "changes_requested",
                  "next_action": "address_review_findings"}, 0, id="standing_change_request"),
    pytest.param({"blocking_reason": "PR_NOT_MERGEABLE", "mergeable": False,
                  "next_action": "resolve_conflict"},
                 merge_open_prs.MERGEABLE_SETTLE_SECONDS + 1, id="unmergeable"),
])
def test_a_masked_red_renovate_head_does_not_spend_the_renovate_slot(tmp_path, masked, settle):
    """A review or conflict blocker outranks CI_BASE_STALE in `blocking_reason`,
    but the head still carries a failed trusted run: selecting it must leave the
    due slot to the next Renovate PR. An unmergeable head first settles, so its
    park comes on the pass after the window, with the streak untouched."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = FakeGateApplication(
        {"core-repo": [1, 2, 3]},
        head_refs={"core-repo": {1: "renovate/a", 2: "renovate/b", 3: "task/y"}},
        labels={"core-repo": {1: ["renovate"], 2: ["renovate"], 3: []}},
        responses={("inspect", "core-repo", 1): _masked_stale_failure(**masked),
                   ("inspect", "core-repo", 2): _in_progress(),
                   ("inspect", "core-repo", 3): _in_progress()})

    for at in ([1000.0, 1000.0 + settle] if settle else [1000.0]):
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                           now=lambda at=at: at)
        if settle and at == 1000.0:
            row = store.read()["repositories"]["core-repo"]
            assert (row["active"]["pr"], row["agent_streak"]) == (
                1, merge_open_prs.RENOVATE_SLOT_EVERY)

    row = store.read()["repositories"]["core-repo"]
    assert 1 in {int(pr) for pr in row["parked"]}
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 0)
    assert _reviewed_or_refreshed(gate, 3) == []


def test_a_masked_pending_run_without_a_stale_base_failure_still_spends_the_slot(tmp_path):
    """Control: a standing change request over a pending run that merely carries
    an earlier failure conclusion is not a trusted failure on an older base."""
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/a"}},
        labels={"core-repo": {1: ["renovate"]}},
        responses={("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", conclusion="failure", ci_reason="CI_RUN_IN_PROGRESS",
            blocking_reason="PR_REVIEW_CHANGES_REQUESTED", review_state="changes_requested")})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    # selection parks a standing change request and never elects it, so
    # it neither spends the slot nor counts as agent work, whatever its run shows.
    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "renovate_changes_requested"
    assert row["agent_streak"] == merge_open_prs.RENOVATE_SLOT_EVERY
    assert merge_open_prs._failed_head(gate.responses[("inspect", "core-repo", 1)]) is False


def test_a_red_renovate_head_does_not_reset_a_streak_that_is_not_due(tmp_path):
    """The red PR is only reached when it is the last candidate; the count keeps."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/a"}},
        labels={"core-repo": {1: ["renovate"]}}, responses={("inspect", "core-repo", 1): _red_head()})
    _seed_agent_streak(store, 0)

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_ci_failed"
    assert row["agent_streak"] == 0


# ------------------- an agent election that parks in its own pass spends no slot


def _unreadable_evidence(**extra):
    """The gate cannot read the CI evidence of the head."""
    return _queue_inspect(ci="unavailable", conclusion=None,
                          blocking_reason="CI_EVIDENCE_INVALID", **extra)


def _parked_election_gate(first):
    """PR 1 and PR 2 are agent PRs, PRs 3 and 4 are Renovate PRs. Every read but
    PR 1's is a pending run of the same readiness tier, so the slot alone picks
    the election that follows PR 1's park."""
    return FakeGateApplication(
        {"core-repo": [1, 2, 3, 4]},
        head_refs={"core-repo": {1: "task/a", 2: "task/b", 3: "renovate/a", 4: "renovate/b"}},
        labels={"core-repo": {1: [], 2: [], 3: ["renovate"], 4: ["renovate"]}},
        responses={("inspect", "core-repo", 1): first,
                   **{("inspect", "core-repo", pr): _in_progress() for pr in (2, 3, 4)}})


def test_a_base_move_does_not_let_a_reparked_agent_pr_starve_the_other_agent_prs(tmp_path):
    """a moved base releases an `evidence_unavailable` park once, and the
    same pass elects that PR first and parks it again. Counted as agent work, that
    election made the next one a Renovate PR after every base move, and no other
    agent PR reached the slot."""
    store = _queue_store(tmp_path)
    gate = _parked_election_gate(_unreadable_evidence())
    gate.by_repository["core-repo"] = [1]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "evidence_unavailable"
    # Main moves, and the last election was a Renovate PR: the count is 0.
    with store.locked() as state:
        state["repositories"]["core-repo"]["agent_streak"] = 0
        store.save(state)
    gate.by_repository["core-repo"] = [1, 2, 3, 4]
    gate.responses[("inspect", "core-repo", 1)] = _unreadable_evidence(base="c")

    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                                 queue_store=store, now=lambda: 1000.0)[0]

    row = store.read()["repositories"]["core-repo"]
    assert [elected["pr"] for elected in summary["elected"]] == [1, 2]
    assert row["parked"]["1"]["base"] == "c" * 40  # Parked again, on the moved base.
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 1)
    assert _reviewed_or_refreshed(gate, 3) == []


def test_an_agent_pr_parked_for_a_failed_run_in_the_pass_that_elected_it_spends_no_slot(
        tmp_path):
    store = _queue_store(tmp_path)
    gate = _parked_election_gate(_red_head())

    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                                 now=lambda: 1000.0)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "ci_failed"
    assert [elected["pr"] for elected in summary["elected"]] == [1, 2]
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 1)


def test_an_agent_pr_whose_head_adds_nothing_spends_no_slot(tmp_path, monkeypatch):
    repository, head, base = _contained_world(tmp_path, monkeypatch, "ancestor")
    store = _queue_store(tmp_path)
    gate = _parked_election_gate(_contained_inspect(head, base))

    summary = merge_open_prs.run_automerge_pass([repository], gate, queue_store=store,
                                                 now=lambda: 1000.0)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "already_in_base"
    assert [elected["pr"] for elected in summary["elected"]] == [1, 2]
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 1)


def test_an_agent_pr_that_held_the_slot_across_passes_keeps_its_count_when_it_parks(tmp_path):
    """Control: only an election parked in its own pass is given back. PR 2 held
    the slot while its run waited; its stall park undoes none of that work, so
    the due slot still goes to the Renovate PR."""
    store = _queue_store(tmp_path)
    gate = _slot_gate()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)
    row = store.read()["repositories"]["core-repo"]
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 1)

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0 + merge_open_prs.CI_WAIT_SECONDS + 1)

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["2"]["reason"] == "ci_stalled"
    assert (row["active"]["pr"], row["agent_streak"]) == (1, 0)  # A given-back count elects PR 3.


# ---------------------- a corrected CI result releases a CI-failure park


def _ci_failure_park(reason, *, head="a", run=11):
    """A legacy record: no claim hash and no run attempt."""
    return {"reason": reason, "head": head * 40, "base": "b" * 40,
            "run": [run, None], "review": [None, "none"], "since": 900.0}


def _persisted_park(tmp_path, parked):
    """Write the park as an earlier executor left it, then reload the store."""
    store = _queue_store(tmp_path)
    store.path.write_text(json.dumps({"version": 1, "cursor": 0, "repositories": {
        "core-repo": {"active": None, "parked": {"1": parked}},
        "docs-repo": {"active": None, "parked": {}}}}), encoding="utf-8")
    store.path.chmod(0o600)
    return QUEUE_STORE_TYPE(store.path, repositories=("core-repo", "docs-repo"))


def test_a_corrected_green_run_releases_a_persisted_ci_failed_park(tmp_path):
    """CI was read red, the park recorded [run, no attempt], and the
    gate later read the same run green. A corrected rerun need not expose an
    attempt, and a corrected forge result changes nothing else, so the park
    must release on the gate's verdict at the next reconciliation instead of
    waiting for an operator unpark."""
    parked = _ci_failure_park("ci_failed")
    store = _persisted_park(tmp_path, parked)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(run=11, attempt=None)})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert summary["results"] == [{"pr": 1, "outcome": "merged"}]
    assert store.read()["repositories"]["core-repo"]["parked"] == {}


@pytest.mark.parametrize("renovate", [True, False])
def test_a_corrected_green_run_releases_both_ci_failure_parks(tmp_path, renovate):
    """Renovate's park stays stricter about retrying a failed head: a refresh
    needs a changed head or trusted run. It is the same corrected-evidence
    defect when the gate reads the parked run green, and green is a verdict,
    not a retry request: the PR returns to the ordinary queue and no refresh is
    spent on this unchanged, current-base head. A stale base still refreshes,
    which test_a_released_green_head_behind_its_base_still_takes_the_refresh
    pins."""
    reason = "renovate_ci_failed" if renovate else "ci_failed"
    parked = _ci_failure_park(reason)
    store = _persisted_park(tmp_path, parked)
    gate = FakeGateApplication(
        {"core-repo": [1]},
        head_refs={"core-repo": {1: "renovate/x" if renovate else "task/x"}},
        labels={"core-repo": {1: ["renovate"] if renovate else []}},
        responses={("inspect", "core-repo", 1): _queue_inspect(run=11, attempt=None)})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert summary["results"] == [{"pr": 1, "outcome": "merged"}]
    assert not any(call[0] == "refresh" for call in gate.calls)


def test_a_released_green_head_behind_its_base_still_takes_the_refresh(tmp_path):
    """The release itself issues no refresh, but it does not promise that the
    pass spends none: a green head still behind its base is refresh-eligible,
    so the ordinary single catch-up refresh follows in the same pass. This pins
    the limit of the "spends no refresh" wording in the claim and documents."""
    parked = _ci_failure_park("ci_failed")
    store = _persisted_park(tmp_path, parked)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(
            run=11, attempt=None, blocking_reason="PR_BASE_STALE")})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert [call[0] for call in gate.calls].count("refresh") == 1
    assert summary["results"] == [
        {"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert store.read()["repositories"]["core-repo"]["parked"] == {}


def _stale_base_park_gate(renovate, conclusion, blocking_reason="CI_BASE_STALE"):
    """The gate reading the parked head and run after main moved:
    `pending` with a stale-base blocker, whatever the run concluded. The gate
    emits `PR_BASE_STALE` only under a green verdict, which already releases a park."""
    return FakeGateApplication(
        {"core-repo": [1]},
        head_refs={"core-repo": {1: "renovate/x" if renovate else "task/x"}},
        labels={"core-repo": {1: ["renovate"] if renovate else []}},
        responses={("inspect", "core-repo", 1): _queue_inspect(
            ci="pending", conclusion=conclusion, attempt=None,
            blocking_reason=blocking_reason, next_action="refresh_task")})


@pytest.mark.parametrize("renovate", [True, False])
def test_a_stale_base_success_on_the_parked_head_releases_a_ci_failure_park(
        tmp_path, renovate):
    """a Forgejo re-run keeps the run id, and once main has moved the
    gate reads the corrected success as `pending` with a stale-base blocker, so
    the green verdict never arrives and the stale-base refresh only follows a
    release: a deadlock until `--unpark`. The success on the parked head
    releases both CI-failure parks; the release itself issues no refresh, the
    ordinary catch-up refresh follows it, and nothing merges on the stale base."""
    reason = "renovate_ci_failed" if renovate else "ci_failed"
    store = _persisted_park(tmp_path, _ci_failure_park(reason))
    gate = _stale_base_park_gate(renovate, "success")

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert store.read()["repositories"]["core-repo"]["parked"] == {}
    assert [call[0] for call in gate.calls].count("refresh") == 1
    assert not any(call[0] == "merge" for call in gate.calls)
    assert summary["results"] == [
        {"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
@pytest.mark.parametrize("renovate", [True, False])
def test_a_stale_base_non_success_on_the_parked_head_keeps_a_ci_failure_park(
        tmp_path, renovate, conclusion):
    """The stale-base release needs a success: a failed or cancelled run, or
    one still without a conclusion, on the same head and run id is not
    recovery."""
    reason = "renovate_ci_failed" if renovate else "ci_failed"
    parked = _ci_failure_park(reason)
    store = _persisted_park(tmp_path, parked)
    gate = _stale_base_park_gate(renovate, conclusion)

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert summary["results"] == []
    assert store.read()["repositories"]["core-repo"]["parked"]["1"] == parked
    assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)


def test_a_pending_pr_base_stale_read_does_not_release_a_ci_failure_park(tmp_path):
    """The documented release matches `CI_BASE_STALE` only.

    The gate projects `PR_BASE_STALE` solely under a green verdict,
    which the first disjunct already releases, so a pending read carrying it is
    not the stale-base success the claim matched. Pinned so re-adding it cannot
    widen the release to a shape the gate does not emit today.
    """
    parked = _ci_failure_park("ci_failed")
    store = _persisted_park(tmp_path, parked)
    gate = _stale_base_park_gate(False, "success", blocking_reason="PR_BASE_STALE")

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert summary["results"] == []
    assert store.read()["repositories"]["core-repo"]["parked"]["1"] == parked
    assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)


@pytest.mark.parametrize("inspect", [
    pytest.param(_queue_inspect(ci="red", conclusion="failure", attempt=None,
                                blocking_reason="CI_RUN_FAILED"), id="red"),
    pytest.param(_queue_inspect(ci="pending", conclusion=None, attempt=None,
                                blocking_reason="CI_RUN_IN_PROGRESS"), id="pending"),
    pytest.param(_queue_inspect(ci="pending", conclusion=None, attempt=None,
                                blocking_reason="CI_RESULT_AMBIGUOUS"), id="ambiguous"),
    pytest.param(_queue_inspect(ci="unavailable", conclusion=None, attempt=None,
                                blocking_reason="CI_EVIDENCE_INVALID"), id="unavailable"),
])
def test_a_ci_failure_park_holds_without_a_green_verdict(tmp_path, inspect):
    """Red, pending, ambiguous and unavailable reads are not recovery. The run
    identity is the parked one -- no attempt recorded -- so the older
    comparisons do not admit the PR either."""
    parked = _ci_failure_park("ci_failed")
    store = _persisted_park(tmp_path, parked)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): inspect})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert summary["results"] == []
    assert store.read()["repositories"]["core-repo"]["parked"]["1"] == parked
    assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)


def test_a_green_verdict_does_not_preempt_the_active_pr(tmp_path):
    """The reconciliation point is an idle queue: parked PRs are inspected only
    when none is active, so recovery cannot displace the active PR or start a
    second executor."""
    parked = _ci_failure_park("ci_failed")
    store = _persisted_park(tmp_path, parked)
    with store.locked() as state:
        state["repositories"]["core-repo"]["active"] = {
            "pr": 2, "head": "d" * 40, "base": "b" * 40, "run": [12, 1],
            "wait_since": 900.0}
        store.save(state)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(run=11, attempt=None),
        ("inspect", "core-repo", 2): _in_progress()})

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)

    assert ("inspect", "core-repo", 1) not in gate.calls
    row = store.read()["repositories"]["core-repo"]
    assert row["active"]["pr"] == 2
    assert row["parked"]["1"] == parked


def test_a_recovered_pr_is_not_released_or_reviewed_twice(tmp_path):
    """Recovery deletes the park and leaves ordinary processing behind it: the
    next pass has no record to release and spends no refresh or review."""
    store = _persisted_park(tmp_path, _ci_failure_park("ci_failed"))
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(run=11, attempt=None)})
    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)

    gate.by_repository["core-repo"] = []  # merged in the first pass
    gate.calls.clear()
    again = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1100.0)[0]

    assert again["results"] == []
    assert not any(call[0] in {"refresh", "review", "merge"} for call in gate.calls)


def test_a_ci_failure_park_ends_on_ci_base_stale_success_but_not_on_pr_base_stale():
    """The park release names only `CI_BASE_STALE`; the reopen path's
    `_stale_base_success` is a separate, wider predicate and must not
    leak into it."""
    parked = _ci_failure_park("ci_failed")
    stale = _queue_inspect(ci="pending", attempt=None, conclusion="success",
                           blocking_reason="CI_BASE_STALE")

    assert merge_open_prs._parked_changed(parked, stale) is True
    assert merge_open_prs._parked_changed(
        parked, {**stale, "blocking_reason": "PR_BASE_STALE"}) is False
    assert merge_open_prs._stale_base_success({**stale, "blocking_reason": "PR_BASE_STALE"})


def test_a_new_head_admits_a_ci_failure_park_even_while_its_ci_is_red():
    """The green rule is scoped to the parked head. A different head is
    admitted by the older head rule on its own, red or not, so green must not
    become the only way back into the queue."""
    parked = _ci_failure_park("ci_failed")
    moved = {**_queue_inspect(ci="red", conclusion="failure", attempt=None, head="c"),
             "blocking_reason": "CI_RUN_FAILED"}

    assert merge_open_prs._parked_changed(parked, moved) is True
    assert merge_open_prs._parked_changed(parked, {**moved, "head_sha": "a" * 40}) is False


def test_a_red_renovate_refresh_has_its_own_repository_budget(tmp_path):
    repositories = [merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")]
    store = _queue_store(tmp_path)
    gate = FakeGateApplication(
        {"core-repo": [1], "docs-repo": [2]},
        head_refs={"core-repo": {1: "task/x"}, "docs-repo": {2: "renovate/x"}},
        labels={"core-repo": {1: []}, "docs-repo": {2: ["renovate"]}},
        responses={("inspect", "core-repo", 1): _stale_base(),
                   ("inspect", "docs-repo", 2): _stale_base(
                       "failure", ci_state="red", ci_run_id=11, ci_run_attempt=1)})

    merge_open_prs.run_automerge_pass(repositories, gate, queue_store=store)

    assert [call[1:3] for call in gate.calls if call[0] == "refresh"] == [
        ("core-repo", 1), ("docs-repo", 2)]
    row = store.read()["repositories"]["docs-repo"]
    assert row["active"]["pr"] == 2 and row["parked"] == {}
    assert row["active"]["refreshed_head"] == "c" * 40


@pytest.mark.parametrize("progress", [
    ["ci_run_queued", 1.0], ["CI_RUN_QUEUED"], ["CI_RUN_QUEUED", -1.0], "CI_RUN_QUEUED"])
def test_queue_state_validates_the_run_progress(tmp_path, progress):
    store = _queue_store(tmp_path)
    active = {"pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
              "wait_since": 1.0, "progress": ["CI_RUN_QUEUED", 2.0]}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": active, "parked": {}}
        store.save(state)
        state["repositories"]["core-repo"]["active"]["progress"] = progress
        with pytest.raises(merge_open_prs.QueueStateError):
            store.save(state)


def test_queue_state_validates_the_refreshed_head(tmp_path):
    store = _queue_store(tmp_path)
    active = {"pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
              "wait_since": 1.0, "refreshed_head": "c" * 40}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": active, "parked": {}}
        store.save(state)
        state["repositories"]["core-repo"]["active"]["refreshed_head"] = "c"
        with pytest.raises(merge_open_prs.QueueStateError):
            store.save(state)


# ------------------------------------------------------- batched merge train
#
# `merge_batch_policy: train-v1`. The gate builds, verifies and merges; these
# pin what the executor asks for, what it persists, and how it falls back to
# the serial queue. The gate side runs against real git in
# `test_merge_train_gate.py`.

_TRAIN_BASE = "b" * 40
_CANDIDATES = ("c" * 40, "d" * 40, "e" * 40)




def _rider(head, run):
    """A queued PR reviewed on an older base whose own run passed there.

    The gate reads that run on the recorded base, inside the accepted range,
    so its CI-evidence answer is accepted; train-v2 admits a rider
    on exactly that answer."""
    return _stale_base(head_sha=head * 40, ci_run_id=run, ci_run_attempt=1,
                       review_state="not_approved", latest_delegated_review_id=run,
                       latest_delegated_review_state="APPROVED", pr_state="open",
                       ci_review_accepted=True, ci_evidence_answer="accepted",
                       ci_evidence_reason="GREEN")


def _train_prefix_metadata(built):
    members = built["members"]
    return [
        {"position": position,
         "pr": built["train_pr"] if position == len(members) else built["train_pr"] + position,
         "branch": (built["train_branch"] if position == len(members)
                    else f"{built['train_branch']}-prefix-{position}"),
         "candidate": member["candidate"]}
        for position, member in enumerate(members, 1)
    ]


class FakeTrainApplication(FakeGateApplication):
    """The Gate application with the four merge-train routes."""

    def __init__(self, candidates, **kwargs):
        super().__init__(candidates, **kwargs)
        self.built = None

    def train_build(self, repository, members, expected_base):
        self.calls.append(("train_build", repository, tuple(members), expected_base))
        response = self.responses.get(("train_build", repository))
        if response is not None:
            self.built = response if response.get("ok") else self.built
            if response.get("ok"):
                self.built.setdefault("prefixes", _train_prefix_metadata(self.built))
            return response
        kept = [{"pr": pr, "head": head, "candidate": _CANDIDATES[index]}
                for index, (pr, head) in enumerate(members)]
        self.built = {"ok": True, "code": "TRAIN_BUILT", "train_pr": 900,
                      "train_branch": f"train/{expected_base[:12]}-{members[0][0]}",
                      "base_sha": expected_base, "candidate_sha": kept[-1]["candidate"],
                      "candidate_tree": "f" * 40, "members": kept, "dropped": []}
        self.built["prefixes"] = _train_prefix_metadata(self.built)
        return self.built

    def train_inspect(self, repository, pr):
        self.calls.append(("train_inspect", repository, pr))
        response = self.responses.get(("train_inspect", repository))
        if isinstance(response, list):
            response = response.pop(0)
        base = {"ok": True, "pr_state": "open", "head_sha": self.built["candidate_sha"],
                "base_sha": self.built["base_sha"], "members": self.built["members"],
                "candidate_tree": "f" * 40, "ci_state": "green", "ci_run_id": 70,
                "ci_run_attempt": 1, "ci_run_conclusion": "success"}
        if self.built.get("mode") == "full":
            base.update(mode="full", generation=self.built["generation"])
        observed = {**base, **(response or {})}
        if "prefixes" not in observed:
            observed["prefixes"] = [
                {**prefix, **{key: observed.get(key) for key in (
                    "ci_state", "blocking_reason", "ci_run_id", "ci_run_attempt", "ci_run_conclusion")}}
                for prefix in self.built.get("prefixes", [])
            ]
        return observed

    def train_member_merge(self, repository, train_pr, member_pr, head):
        self.calls.append(("train_member_merge", repository, member_pr, train_pr, head))
        response = self.responses.get(("train_member_merge", repository, member_pr))
        if isinstance(response, list):
            response = response.pop(0)
        return response or {"ok": True, "code": "TRAIN_MEMBER_MERGED", "ci_basis": "train",
                            "approval_basis": "exact"}

    def train_close(self, repository, train_pr, reason):
        self.calls.append(("train_close", repository, train_pr, reason))
        response = self.responses.get(("train_close", repository))
        if isinstance(response, list):
            response = response.pop(0)
        return response or {"ok": True, "code": "TRAIN_CLOSED", "branch_deleted": True}


def _train_gate(responses=None, numbers=(1, 2, 3)):
    return FakeTrainApplication({"core-repo": list(numbers)}, responses={
        **{("inspect", "core-repo", pr): _rider(head, 10 + pr)
           for pr, head in zip(numbers, "123456789")},
        **(responses or {})})


def _train_calls(gate, name):
    return [call for call in gate.calls if call[0] == name]


def _train_pass(tmp_path, gate, store, clock=None, policy="train-v1"):
    return merge_open_prs.run_automerge_pass(
        [_train_repository(tmp_path, policy)], gate, queue_store=store,
        now=(lambda: clock[0]) if clock else (lambda: 1000.0))[0]


@pytest.mark.parametrize("uncertain", [False, True])
def test_train_build_spends_only_its_repository_refresh_budget(tmp_path, uncertain):
    gate = _train_gate(numbers=(1, 2))
    gate.by_repository["docs-repo"] = [7]
    gate.responses[("inspect", "docs-repo", 7)] = _stale_base()
    original_build = gate.train_build

    def train_build(repository, members, expected_base):
        result = original_build(repository, members, expected_base)
        if uncertain:
            raise TimeoutError("the train build response was lost")
        return result

    gate.train_build = train_build
    repositories = [_train_repository(tmp_path), merge_open_prs.Repository(
        "docs-repo", tmp_path / "docs-repo", "forgejo", "acme/docs-repo")]
    results = merge_open_prs.run_automerge_pass(repositories, gate, queue_store=_queue_store(tmp_path))

    assert len(_train_calls(gate, "train_build")) == 1
    assert [call[1:3] for call in _train_calls(gate, "refresh")] == [("docs-repo", 7)]
    assert results[1]["results"] == [{"pr": 7, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert results[0]["train"]["outcome"] == ("build_refused" if uncertain else "built")


def test_without_the_batch_policy_the_serial_queue_refreshes_and_never_builds(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()

    summary = _train_pass(tmp_path, gate, store, policy="")

    assert summary["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]
    assert not any(call[0].startswith("train_") for call in gate.calls)
    assert "train" not in summary
    assert "train" not in store.read()["repositories"]["core-repo"]["active"]


def test_a_train_that_does_not_form_journals_why_each_candidate_was_left_out(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({
        ("inspect", "core-repo", 2): _rider("2", 12) | {"latest_delegated_review_state": None},
        ("inspect", "core-repo", 3): _stale_base("failure", head_sha="3" * 40),
        # Selection ranks PR 4 first; only the train scan's read fails.
        ("inspect", "core-repo", 4): [_rider("4", 14), {"ok": False, "code": "FETCH_FAILED"}],
        ("inspect", "core-repo", 5): _rider("5", 15) | {"mergeable": False},
        ("inspect", "core-repo", 6): _rider("6", 16) | {"pr_state": "draft"},
    }, numbers=(1, 2, 3, 4, 5, 6))

    summary = _train_pass(tmp_path, gate, store)

    assert summary["train"] == {
        "pr": None, "members": [1], "outcome": "not_formed", "reason": "TRAIN_TOO_SHORT",
        "inspected": [{"pr": 2, "why": "not_approved"}, {"pr": 3, "why": "ci_not_green"},
                      {"pr": 4, "why": "inspect_failed"}, {"pr": 5, "why": "not_mergeable"}]}
    assert not _train_calls(gate, "train_build")
    assert summary["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]


def _narrowed_rider(head, run):
    """An approved queued PR whose narrowed run on the current base the review accepts.

    The gate reads a gate-C1-class head that way, CI_PROFILE_INSUFFICIENT and not
    green, and `ci_review_accepted` is its answer."""
    return _rider(head, run) | {
        "ci_state": "unavailable", "blocking_reason": "CI_PROFILE_INSUFFICIENT",
        "ci_reason": "CI_PROFILE_INSUFFICIENT", "ci_review_accepted": True,
        "ci_evidence_reason": "NARROWED_RUN", "merge_candidate": False}


def test_a_train_admits_an_approved_narrowed_head_at_the_refresh_moment(tmp_path):
    """the gate's train build admits a head whose narrowed run the review
    accepts once it is approved, so the executor offers it as a member. The
    refresh-moment train keeps its three members and journals no cap."""
    gate = _train_gate({
        ("inspect", "core-repo", 2): _narrowed_rider("2", 12),
        ("inspect", "core-repo", 3): _narrowed_rider("3", 13),
    }, numbers=(1, 2, 3))

    summary = _train_pass(tmp_path, gate, _queue_store(tmp_path))

    assert [call[2] for call in _train_calls(gate, "train_build")] == [
        ((1, "1" * 40), (2, "2" * 40), (3, "3" * 40))]
    assert summary["train"]["outcome"] == "built"
    assert "member_cap" not in summary["train"]


# a train formed in the place of the candidate mark. Its leader is an
# approved head whose narrowed run the review accepts, and it is not marked yet.

class _MarkingTrainGate(FakeTrainApplication):
    """The train gate with the executor's mark route (`merge_candidate_policy: label-v1`)."""

    def mark_candidate(self, repository, pr, candidate):
        self.calls.append(("mark_candidate", repository, pr, candidate))
        return {"ok": True, "candidate": candidate, "label_changed": True}


def _election_gate(responses=None, numbers=(1, 2, 3)):
    return _MarkingTrainGate({"core-repo": list(numbers)}, responses={
        **{("inspect", "core-repo", pr): _narrowed_rider(head, 10 + pr)
           for pr, head in zip(numbers, "123456789")},
        **(responses or {})})


_V2_GENERATION = "a1" * 16


def _v2_election_gate(responses=None, numbers=(1, 2, 3)):
    """The existing executor fixture with the gate's real v2 response shape."""
    gate = _election_gate(responses, numbers)
    members = [{"pr": pr, "head": str(pr) * 40, "candidate": candidate}
               for pr, candidate in zip(numbers, _CANDIDATES)]
    branch = f"train/{_TRAIN_BASE[:12]}-{numbers[0]}-g{_V2_GENERATION}"
    gate.responses.setdefault(("train_build", "core-repo"), {
        "ok": True, "code": "TRAIN_BUILT", "mode": "full", "generation": _V2_GENERATION,
        "train_pr": 900, "train_branch": branch, "base_sha": _TRAIN_BASE,
        "candidate_sha": members[-1]["candidate"], "candidate_tree": "f" * 40,
        "members": members, "dropped": [],
        "prefixes": [{"position": position, "pr": 900, "branch": branch,
                      "candidate": member["candidate"]}
                     for position, member in enumerate(members, 1)],
    })
    return gate


def _election_pass(tmp_path, gate, store, policy="train-v1", clock=None):
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo",
        merge_batch_policy=policy, merge_candidate_policy="label-v1")
    return merge_open_prs.run_automerge_pass(
        [repository], gate, queue_store=store,
        now=(lambda: clock[0]) if clock else (lambda: 1000.0))[0]


_ELECTION_CAP = {"member_cap": 2, "cap_reason": "strong_ci_slots_unreadable"}


@pytest.mark.parametrize("numbers", [(1, 2), (1, 2, 3)])
def test_train_v2_forms_all_eligible_members_before_serial_mark(tmp_path, numbers):
    gate = _v2_election_gate(numbers=numbers)
    store = _queue_store(tmp_path)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert [call[2] for call in _train_calls(gate, "train_build")] == [
        tuple((pr, str(pr) * 40) for pr in numbers)]
    assert summary["train"]["outcome"] == "built"
    assert summary["train"]["members"] == list(numbers)
    assert "member_cap" not in summary["train"]
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")
    train = store.read()["repositories"]["core-repo"]["active"]["train"]
    assert [member["pr"] for member in train["members"]] == list(numbers)
    assert (train["mode"], train["generation"], train["phase"]) == (
        "full", _V2_GENERATION, "owner_wait")
    assert all(prefix["pr"] == train["pr"] and prefix["branch"] == train["branch"]
               for prefix in train["prefixes"])


def test_v2_lone_member_keeps_the_serial_path(tmp_path):
    """Fewer than two eligible members: the ordinary serial path, no build."""
    gate = _v2_election_gate(numbers=(1,))
    store = _queue_store(tmp_path)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert not _train_calls(gate, "train_build")
    assert summary["train"]["outcome"] == "not_formed"
    assert summary["train"]["reason"] == "TRAIN_NO_CANDIDATE"
    assert [(result["outcome"], result["reason"]) for result in summary["results"]] == [
        ("retained", "PR_CANDIDATE_MARKED")]


@pytest.mark.parametrize("numbers", [(1, 2), (1, 2, 3)])
def test_v2_native_owner_lookup_refusal_parks_without_starting_serial_ci(tmp_path, numbers):
    gate = _v2_election_gate({("train_build", "core-repo"): {
        "ok": False, "code": "TRAIN_BUILD_FAILED",
        "detail": {"park": True, "step": "owner_lookup_unavailable"},
    }}, numbers=numbers)
    store = _queue_store(tmp_path)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")
    assert not _train_calls(gate, "train_member_merge")
    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None
    assert set(row["parked"]) == {str(pr) for pr in numbers}
    assert all(p["reason"] == "evidence_unavailable" for p in row["parked"].values())
    assert row["agent_streak"] == 0
    assert summary["train"]["outcome"] == "parked"
    assert summary["train"]["step"] == "owner_lookup_unavailable"
    assert "train_pr" not in summary["train"]


def test_a_deploy_waiting_before_the_train_build_leaves_the_pass_through_the_hold(
        tmp_path, monkeypatch):
    """`train_build` runs behind the budget's deployment checkpoint, which raises
    before the gate is called. That yield is no uncertain dispatch: nothing was
    published, so no member is parked or marked and the pass leaves by the hold."""
    gate = _v2_election_gate(numbers=(1, 2))
    store = _queue_store(tmp_path)
    monkeypatch.setattr(merge_open_prs, "_deploy_in_progress", lambda: None)

    # The deployment takes the outer lock exactly when the pass reaches `train_build`, so the
    # yield is raised by the budget's checkpoint inside that call, before the gate runs.
    building: list[bool] = []
    resolve = merge_open_prs._ReviewBudget.__getattr__

    def reaching(budget, name):
        if name == "train_build":
            building.append(True)
        return resolve(budget, name)

    yielded: list[bool] = []

    def probe_deploy_lock():
        # Held for that one probe only: a swallowed yield would let the pass go on and journal
        # the build as refused, with no later checkpoint to yield again.
        if building and not yielded:
            yielded.append(True)
            return "held", "deployment holds the outer lock"
        return "free", ""

    monkeypatch.setattr(merge_open_prs._ReviewBudget, "__getattr__", reaching)
    monkeypatch.setattr(merge_open_prs, "probe_deploy_lock", probe_deploy_lock)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert building == [True]
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    # A swallowed yield would journal the build as refused and spend the refresh budget.
    assert "train" not in summary
    assert not _train_calls(gate, "train_build")
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")
    row = store.read()["repositories"]["core-repo"]
    assert "train" not in row["active"]
    assert row["parked"] == {}


def test_v2_native_red_history_returns_to_ordinary_serial_gates(tmp_path):
    gate = _v2_election_gate({("train_build", "core-repo"): {
        "ok": False, "code": "TRAIN_BUILD_FAILED",
        "detail": {"serial_only": True, "train_pr": 900, "step": "closed_batch"},
    }}, numbers=(1, 2))

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path), policy="train-v2")

    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]
    assert len(_train_calls(gate, "train_build")) == 1
    assert not _train_calls(gate, "train_member_merge")
    assert summary["train"]["serial_return"] is True
    assert (summary["train"]["step"], summary["train"]["train_pr"]) == ("closed_batch", 900)


def test_v2_gate_red_close_proof_refusal_parks_without_cleanup_authority(tmp_path):
    gate = _v2_election_gate({
        ("train_inspect", "core-repo"): {
            "ci_state": "red", "blocking_reason": "CI_RUN_FAILED", "ci_run_conclusion": "failure",
            "ci_terminal_red": True, "base_failure_match": False,
        },
        ("train_close", "core-repo"): {
            "ok": False, "code": "TRAIN_BUILD_FAILED",
            "detail": {"cleanup_started": False, "park": True},
        },
    })
    store = _queue_store(tmp_path)
    _election_pass(tmp_path, gate, store, policy="train-v2")

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    train = store.read()["repositories"]["core-repo"]["active"]["train"]
    assert train["phase"] == "owner_parked"
    assert "cleanup" not in train
    assert train["started"] == 1000.0
    assert summary["train"]["outcome"] == "parked"
    assert not summary["train"].get("serial_return")
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "train_member_merge")


def test_train_v2_uncertain_build_does_not_start_a_serial_candidate_run(tmp_path):
    gate = _v2_election_gate(numbers=(1, 2))
    original_build = gate.train_build

    def train_build(repository, members, expected_base):
        original_build(repository, members, expected_base)
        raise TimeoutError("the v2 train build response was lost")

    gate.train_build = train_build

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path), policy="train-v2")

    assert len(_train_calls(gate, "train_build")) == 1
    assert summary["train"]["outcome"] == "build_refused"
    assert summary["results"] == []
    assert not _train_calls(gate, "mark_candidate")


def test_v2_orphan_owner_is_adopted_instead_of_closed_after_a_lost_build(tmp_path):
    """A lost build response must not retire the one recoverable owner.

    The next pass discovers the owner PR with no saved train. Closing it as
    `TRAIN_ORPHANED` would leave the gate's closed-batch guard parking the same
    member-head set until a head or a base moved, so the executor re-reads the
    family and holds it in the active slot instead.
    """
    numbers = (1, 2)
    branch = f"train/{_TRAIN_BASE[:12]}-1-g{_V2_GENERATION}"
    members = [{"pr": pr, "head": str(pr) * 40, "candidate": candidate}
               for pr, candidate in zip(numbers, _CANDIDATES)]
    prefixes = [{"position": position, "pr": 900, "branch": branch,
                 "candidate": member["candidate"]}
                for position, member in enumerate(members, 1)]
    gate = _MarkingTrainGate(
        {"core-repo": [1, 2, 900]},
        head_refs={"core-repo": {900: branch}},
        responses={
            **{("inspect", "core-repo", pr): _narrowed_rider(head, 10 + pr)
               for pr, head in zip(numbers, "12")},
            ("train_inspect", "core-repo"): {
                "ok": True, "main_on_chain": True, "pr_state": "open",
                "train_branch": branch,
                "ci_state": "pending", "blocking_reason": "CI_RUN_IN_PROGRESS",
                "ci_run_id": 70, "ci_run_attempt": 1},
        })
    gate.built = {"ok": True, "code": "TRAIN_BUILT", "mode": "full",
                  "generation": _V2_GENERATION, "train_pr": 900, "train_branch": branch,
                  "base_sha": _TRAIN_BASE, "candidate_sha": members[-1]["candidate"],
                  "candidate_tree": "f" * 40, "members": members, "dropped": [],
                  "prefixes": prefixes}
    store = _queue_store(tmp_path)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert [call[1:4] for call in _train_calls(gate, "train_close")] == []
    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["train"]["pr"] == 900
    assert active["train"]["generation"] == _V2_GENERATION
    assert [member["pr"] for member in active["train"]["members"]] == list(numbers)
    assert (active["train"]["mode"], active["train"]["phase"]) == ("full", "owner_wait")
    assert summary["train"]["outcome"] == "waiting"
    assert not _train_calls(gate, "train_build")


def test_v2_owner_close_without_a_journal_entry_still_retires_an_invalid_family(tmp_path):
    """Only a valid, open, main-carried full owner is adopted; the rest is waste."""
    gate = _MarkingTrainGate(
        {"core-repo": [1, 2, 900]},
        head_refs={"core-repo": {900: f"train/{_TRAIN_BASE[:12]}-1-g{_V2_GENERATION}"}},
        responses={
            **{("inspect", "core-repo", pr): _narrowed_rider(head, 10 + pr)
               for pr, head in zip((1, 2), "12")},
            ("train_inspect", "core-repo"): {"ok": True, "mode": "legacy"},
        })
    # The fake reads its built shape before it merges the response.
    gate.built = {"mode": "legacy", "generation": _V2_GENERATION,
                  "candidate_sha": "c" * 40, "base_sha": _TRAIN_BASE,
                  "members": [], "prefixes": []}
    store = _queue_store(tmp_path)

    _election_pass(tmp_path, gate, store, policy="train-v2")

    assert [call[1:4] for call in _train_calls(gate, "train_close")] == [
        ("core-repo", 900, "TRAIN_ORPHANED")]


def test_v2_orphan_owner_never_replaces_a_persisted_train(tmp_path):
    """One slot, one train: an occupied slot defers the orphan, never replaces it."""
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo",
        merge_batch_policy="train-v2", merge_candidate_policy="label-v1")
    gate = _MarkingTrainGate({"core-repo": [1, 2, 901]},
                             head_refs={"core-repo": {901: "train/" + _TRAIN_BASE[:12] + "-1-g" + _V2_GENERATION}})
    row = {"active": {"pr": 900, "train": {"pr": 900}}, "parked": {}}
    summaries: dict = {"core-repo": {}}

    verdict = merge_open_prs._adopt_orphan_v2_owner(
        gate, repository, 901, row, summaries, lambda: 1000.0)

    assert verdict == "occupied"
    assert row["active"]["pr"] == 900
    assert not _train_calls(gate, "train_inspect")
    assert row["parked"] == {}


def test_v2_adopts_at_most_one_orphan_owner_per_pass(tmp_path):
    """Two recoverable owners cannot both take the single active slot."""
    branch = f"train/{_TRAIN_BASE[:12]}-1-g{_V2_GENERATION}"
    members = [{"pr": pr, "head": str(pr) * 40, "candidate": candidate}
               for pr, candidate in zip((1, 2), _CANDIDATES)]
    prefixes = [{"position": position, "pr": 900, "branch": branch,
                 "candidate": member["candidate"]}
                for position, member in enumerate(members, 1)]
    gate = _MarkingTrainGate(
        {"core-repo": [1, 2, 900, 901]},
        head_refs={"core-repo": {900: branch, 901: branch}},
        responses={
            **{("inspect", "core-repo", pr): _narrowed_rider(head, 10 + pr)
               for pr, head in zip((1, 2), "12")},
            ("train_inspect", "core-repo"): {
                "ok": True, "main_on_chain": True, "pr_state": "open",
                "train_branch": branch, "ci_state": "pending",
                "blocking_reason": "CI_RUN_IN_PROGRESS", "ci_run_id": 70,
                "ci_run_attempt": 1},
        })
    gate.built = {"ok": True, "code": "TRAIN_BUILT", "mode": "full",
                  "generation": _V2_GENERATION, "train_pr": 900, "train_branch": branch,
                  "base_sha": _TRAIN_BASE, "candidate_sha": members[-1]["candidate"],
                  "candidate_tree": "f" * 40, "members": members, "dropped": [],
                  "prefixes": prefixes}
    store = _queue_store(tmp_path)

    _election_pass(tmp_path, gate, store, policy="train-v2")

    inspected = [call[2] for call in _train_calls(gate, "train_inspect")]
    assert inspected[0] == 900
    assert 901 not in inspected
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900
    assert [call[1:4] for call in _train_calls(gate, "train_close")] == []


def test_v2_adoption_reconciles_a_prebuild_serial_locator(tmp_path):
    """A lost build response leaves the serial row; the matching owner is adopted."""
    branch = f"train/{_TRAIN_BASE[:12]}-1-g{_V2_GENERATION}"
    members = [{"pr": pr, "head": str(pr) * 40, "candidate": candidate}
               for pr, candidate in zip((1, 2), _CANDIDATES)]
    prefixes = [{"position": position, "pr": 900, "branch": branch,
                 "candidate": member["candidate"]}
                for position, member in enumerate(members, 1)]
    gate = _MarkingTrainGate(
        {"core-repo": [1, 2, 900]},
        head_refs={"core-repo": {900: branch}},
        responses={("train_inspect", "core-repo"): {
            "ok": True, "main_on_chain": True, "pr_state": "open",
            "train_branch": branch, "ci_state": "pending",
            "blocking_reason": "CI_RUN_IN_PROGRESS", "ci_run_id": 70,
            "ci_run_attempt": 1}})
    gate.built = {"ok": True, "code": "TRAIN_BUILT", "mode": "full",
                  "generation": _V2_GENERATION, "train_pr": 900, "train_branch": branch,
                  "base_sha": _TRAIN_BASE, "candidate_sha": members[-1]["candidate"],
                  "candidate_tree": "f" * 40, "members": members, "dropped": [],
                  "prefixes": prefixes}
    repository = merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo",
        merge_batch_policy="train-v2", merge_candidate_policy="label-v1")
    summaries: dict = {"core-repo": {}}
    row = {"active": {"pr": 1, "head": "1" * 40, "base": _TRAIN_BASE,
                      "run": [None, None], "wait_since": 1.0, "started": False},
           "parked": {}}

    assert merge_open_prs._adopt_orphan_v2_owner(
        gate, repository, 900, row, summaries, lambda: 1000.0) == "adopted"
    assert row["active"]["train"]["pr"] == 900
    assert row["active"]["train"]["generation"] == _V2_GENERATION

    # A locator whose own PR admission dropped: the authoritative owner still
    # wins, and the dropped PR stays queued for ordinary serial recovery.
    branch23 = f"train/{_TRAIN_BASE[:12]}-2-g{_V2_GENERATION}"
    members23 = [{"pr": pr, "head": str(pr) * 40, "candidate": candidate}
                 for pr, candidate in zip((2, 3), _CANDIDATES)]
    dropped = _MarkingTrainGate(
        {"core-repo": [1, 2, 3, 900]},
        head_refs={"core-repo": {900: branch23}},
        responses={("train_inspect", "core-repo"): {
            "ok": True, "main_on_chain": True, "pr_state": "open",
            "train_branch": branch23, "ci_state": "pending",
            "blocking_reason": "CI_RUN_IN_PROGRESS", "ci_run_id": 71,
            "ci_run_attempt": 1}})
    dropped.built = {"ok": True, "code": "TRAIN_BUILT", "mode": "full",
                     "generation": _V2_GENERATION, "train_pr": 900,
                     "train_branch": branch23, "base_sha": _TRAIN_BASE,
                     "candidate_sha": members23[-1]["candidate"], "candidate_tree": "f" * 40,
                     "members": members23, "dropped": [],
                     "prefixes": [{"position": position, "pr": 900, "branch": branch23,
                                   "candidate": member["candidate"]}
                                  for position, member in enumerate(members23, 1)]}
    other = {"active": {"pr": 1, "head": "1" * 40, "base": _TRAIN_BASE,
                        "run": [None, None], "wait_since": 1.0, "started": False},
             "parked": {}}

    assert merge_open_prs._adopt_orphan_v2_owner(
        dropped, repository, 900, other, summaries, lambda: 1000.0) == "adopted"
    assert other["active"]["train"]["pr"] == 900
    assert [member["pr"] for member in other["active"]["train"]["members"]] == [2, 3]


def test_v2_structured_open_pr_refusal_does_not_start_a_serial_run(tmp_path):
    """A lost create-PR response follows the owner ref: no serial run beside it."""
    gate = _v2_election_gate({("train_build", "core-repo"): {
        "ok": False, "code": "TRAIN_BUILD_FAILED", "detail": {"step": "open_pr"}}})
    store = _queue_store(tmp_path)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert summary["results"] == []
    assert summary["train"]["uncertain_publication"] is True
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")


def test_v2_unreadable_orphan_owner_defers_the_repository_without_closing(tmp_path):
    """Unreadable proof is not permission to retire a live owner or run beside it."""
    gate = _MarkingTrainGate(
        {"core-repo": [1, 2, 900]},
        head_refs={"core-repo": {900: f"train/{_TRAIN_BASE[:12]}-1-g{_V2_GENERATION}"}},
        responses={
            **{("inspect", "core-repo", pr): _narrowed_rider(head, 10 + pr)
               for pr, head in zip((1, 2), "12")},
            ("train_inspect", "core-repo"): {"ok": False, "code": "FETCH_FAILED"},
        })
    store = _queue_store(tmp_path)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert [call[1:4] for call in _train_calls(gate, "train_close")] == []
    assert summary["train_cleanup"] == {"pr": 900, "outcome": "deferred",
                                        "reason": "TRAIN_OWNER_INSPECT_UNAVAILABLE"}
    assert not _train_calls(gate, "train_build")
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")
    assert store.read()["repositories"].get("core-repo", {}).get("active") is None


def test_v2_uncertain_build_at_the_refresh_moment_starts_no_serial_refresh(tmp_path):
    """A lost build response at the refresh moment must not spend a second run.

    The gate may have pushed the owner before the response was lost, so the
    refresh this pass would otherwise perform can start a full run beside it.
    The next pass re-elects instead.
    """
    gate = _train_gate(numbers=(1, 2))
    original_build = gate.train_build

    def train_build(repository, members, expected_base):
        original_build(repository, members, expected_base)
        raise TimeoutError("the v2 train build response was lost")

    gate.train_build = train_build
    store = _queue_store(tmp_path)

    summary = _train_pass(tmp_path, gate, store, policy="train-v2")

    assert len(_train_calls(gate, "train_build")) == 1
    assert summary["train"]["outcome"] == "build_refused"
    assert summary["results"] == []
    assert [call[1:3] for call in _train_calls(gate, "refresh")] == []


def test_v2_riders_follow_readiness_inside_one_priority_group(tmp_path):
    """Readiness ranks riders inside the queue's existing priority order.

    Both riders are admitted, but the green head (readiness 0) rides ahead of
    the narrowed-accepted one (readiness 1) even though it is later in the
    queue.
    """
    gate = _v2_election_gate({
        ("inspect", "core-repo", 1): _rider("1", 11) | {"ci_state": "green"},
        ("inspect", "core-repo", 2): _narrowed_rider("2", 12),
        ("inspect", "core-repo", 3): _rider("3", 13) | {"ci_state": "green"},
    }, numbers=(1, 2, 3))

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path), policy="train-v2")

    assert [call[2] for call in _train_calls(gate, "train_build")] == [
        ((1, "1" * 40), (3, "3" * 40), (2, "2" * 40))]
    assert summary["train"]["outcome"] == "built"


@pytest.mark.parametrize("phase", ["owner_wait", "owner_parked", "merging", "cleanup"])
def test_v2_journal_round_trips_the_owner_phase_and_selected_attempt(tmp_path, phase):
    store = _queue_store(tmp_path)
    _election_pass(tmp_path, _v2_election_gate(), store, policy="train-v2")

    with store.locked() as state:
        active = state["repositories"]["core-repo"]["active"]
        active["run"] = [70, 2]
        active["train"]["phase"] = phase
        if phase == "cleanup":
            active["train"]["cleanup"] = {"reason": "TRAIN_PR_CLOSED", "outcome": "closed"}
        store.save(state)
    observed = store.read()["repositories"]["core-repo"]["active"]
    assert observed["run"] == [70, 2]
    assert observed["train"] == active["train"]


@pytest.mark.parametrize("field,value", [
    ("mode", "legacy"), ("mode", []), ("generation", "b2" * 16), ("generation", {}),
    ("phase", "prefix_round"), ("phase", []), ("phase", {}),
    ("branch", "train/bbbbbbbbbbbb-1"), ("branch", "train/bbbbbbbbbbbb-1-ga1"),
    ("owner_pr", 901), ("prefix_pr", 901), ("prefix_position", 2),
    ("prefix_branch", f"train/bbbbbbbbbbbb-1-g{_V2_GENERATION}-prefix-1"),
    ("prefix_candidate", "f" * 40),
])
def test_v2_journal_refuses_mixed_generations_and_legacy_prefix_inventory(tmp_path, field, value):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    _election_pass(tmp_path, _v2_election_gate(), store, policy="train-v2")

    with store.locked() as state:
        train = state["repositories"]["core-repo"]["active"]["train"]
        if field.startswith("prefix_"):
            train["prefixes"][0][field.removeprefix("prefix_")] = value
        else:
            train["pr" if field == "owner_pr" else field] = value
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


@pytest.mark.parametrize("ci_state,reason,conclusion,age,attempt,phase", [
    ("pending", "CI_RUN_IN_PROGRESS", None, 30, 1, "owner_wait"),
    ("unavailable", "CI_RUN_CANCELED", "cancelled", 30, 1, "owner_parked"),
    ("unavailable", "CI_RESPONSE_INVALID", "skipped", 30, 1, "owner_parked"),
    ("unavailable", "CI_API_UNAVAILABLE", None, 30, 1, "owner_parked"),
    ("unavailable", "CI_PROFILE_INSUFFICIENT", "failure", 30, 1, "owner_parked"),
    ("red", "CI_RUN_FAILED", "failure", 30, 1, "owner_parked"),
    ("pending", "CI_RUN_IN_PROGRESS", None, merge_open_prs.CI_WAIT_SECONDS + 1, 2,
     "owner_parked"),
    ("pending", "CI_RUN_IN_PROGRESS", None, merge_open_prs.CI_WAIT_SECONDS + 1, None,
     "owner_parked"),
])
def test_v2_owner_without_strict_full_red_parks_without_closing_or_renewing_the_deadline_before_the_lost_run_window(
        tmp_path, ci_state, reason, conclusion, age, attempt, phase):
    class AdvisoryTrap(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("the v2.0 executor consumed advisory classification")

        __getitem__ = get

    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_inspect", "core-repo"): {
        "ci_state": ci_state, "blocking_reason": reason, "ci_run_conclusion": conclusion,
        "ci_run_attempt": attempt, "ci_terminal_red": False,
        "failure_classification": AdvisoryTrap(kind="code", authority="advisory"),
    }})
    _election_pass(tmp_path, gate, store, policy="train-v2")

    summary = _train_pass(tmp_path, gate, store, clock=[1000.0 + age], policy="train-v2")

    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "train_member_merge")
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")
    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["train"]["started"] == 1000.0
    assert active["train"]["phase"] == phase
    assert active["run"] == [70, attempt]
    assert summary["train"]["outcome"] == ("waiting" if phase == "owner_wait" else "parked")


# a full-mode owner whose run ended cancelled or skipped, or never
# appeared, has no run left to wait for. The gate reports the three as closed
# reason codes (`ci.py`: CI_RUN_CANCELED and CI_RUN_SKIPPED are UNAVAILABLE
# without a retry hint; CI_RUN_MISSING carries one, so it reads `pending`;
# `_train_ci_projection` forwards them). The window is the 20 minute CI start
# window the gate documents, written out so a changed constant fails here.
_LOST_OWNER_WINDOW = 20 * 60
_OWNER_READINGS = {
    "CI_RUN_IN_PROGRESS": {"ci_state": "pending", "ci_run_conclusion": None},
    "CI_RUN_QUEUED": {"ci_state": "pending", "ci_run_conclusion": None},
    "CI_RUN_CANCELED": {"ci_state": "unavailable", "ci_run_conclusion": "cancelled"},
    "CI_RUN_SKIPPED": {"ci_state": "unavailable", "ci_run_conclusion": "skipped"},
    # a failed job without a strict full-C1 failure
    # (`ci.terminal_full_failure` false); the executor reads only the code.
    "CI_RUN_FAILED": {"ci_state": "red", "ci_run_conclusion": "failure"},
    "CI_RUN_MISSING": {"ci_state": "pending", "ci_run_conclusion": None,
                       "ci_run_id": None, "ci_run_attempt": None},
    "CI_API_UNAVAILABLE": {"ci_state": "unavailable", "ci_run_conclusion": None},
    # A skipped conclusion under another code is not the lost-run code.
    "CI_RESPONSE_INVALID": {"ci_state": "unavailable", "ci_run_conclusion": "skipped"},
    "CI_PROFILE_INSUFFICIENT": {"ci_state": "unavailable", "ci_run_conclusion": "failure"},
}


def _owner_pass(tmp_path, gate, store, clock, at, reading=None, attempt=1):
    """One train-v2 pass at time `at`, after the owner's CI reads as `reading`."""
    clock[0] = at
    if reading is not None:
        gate.responses[("train_inspect", "core-repo")] = {
            "blocking_reason": reading, "ci_terminal_red": False,
            "ci_run_attempt": attempt, **_OWNER_READINGS[reading]}
    return _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)


def _owner_active(store):
    return store.read()["repositories"]["core-repo"]["active"]


def _lost_owner_warnings(caplog, reading):
    return [record for record in caplog.records
            if record.levelno == logging.WARNING and reading in record.getMessage()]


@pytest.mark.parametrize("reading", ["CI_RUN_CANCELED", "CI_RUN_SKIPPED", "CI_RUN_FAILED"])
def test_v2_owner_run_that_ended_without_a_verdict_retires_the_family_a_window_after_first_seen(
        tmp_path, caplog, reading):
    """An owner run that was active for a long time and then ended
    cancelled with nothing in flight must not leave the family waiting for a
    manual kick. The loss is dated from the owner's first reading, not from
    the train: the owner may have run for a long time before it ended."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    _owner_pass(tmp_path, gate, store, clock, 1000.0, "CI_RUN_IN_PROGRESS")
    lost_at = 1000.0 + 3600

    with caplog.at_level(logging.WARNING, logger=merge_open_prs.LOGGER.name):
        first = _owner_pass(tmp_path, gate, store, clock, lost_at, reading)
        assert (first["train"]["outcome"], first["train"]["reason"]) == ("parked", reading)
        assert _owner_active(store)["progress"] == [reading, lost_at]
        inside = _owner_pass(tmp_path, gate, store, clock, lost_at + _LOST_OWNER_WINDOW - 1)
        assert inside["train"]["outcome"] == "parked"
        assert not _train_calls(gate, "train_close")
        assert not _lost_owner_warnings(caplog, reading)

        closed = _owner_pass(tmp_path, gate, store, clock, lost_at + _LOST_OWNER_WINDOW)

    reason = f"TRAIN_OWNER_CI_LOST: {reading}"
    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, reason)]
    assert (closed["train"]["outcome"], closed["train"]["reason"]) == ("closed", reason)
    # The members take the serial path in the same pass; the batch is not built again.
    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]
    assert len(_train_calls(gate, "train_build")) == 1
    assert not _train_calls(gate, "train_member_merge")
    active = _owner_active(store)
    assert active is None or "train" not in active
    warned = _lost_owner_warnings(caplog, reading)
    assert len(warned) == 1 and "#900" in warned[0].getMessage()


@pytest.mark.parametrize("reading", ["CI_RUN_CANCELED", "CI_RUN_FAILED"])
def test_v2_owner_closed_outside_the_executor_keeps_parking_past_the_lost_window(
        tmp_path, reading):
    """A closed owner whose main is on its chain but not its final tree proves
    nothing the executor may undo; the lost-run window does not close it. Main
    off the chain still closes it as a moved base."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    # The loss is first read while the owner is open, so its date is already
    # past the window when the closed owner is read.
    _owner_pass(tmp_path, gate, store, clock, 1000.0 + 60, reading)
    assert _owner_active(store)["progress"] == [reading, 1000.0 + 60]
    closed = {"pr_state": "closed", "main_on_chain": True, "main_is_final": False,
              "blocking_reason": reading, "ci_terminal_red": False, "ci_run_attempt": 1,
              **_OWNER_READINGS[reading]}
    gate.responses[("train_inspect", "core-repo")] = closed

    for at in (1000.0 + 60 + _LOST_OWNER_WINDOW, 1000.0 + 60 + 2 * _LOST_OWNER_WINDOW):
        clock[0] = at
        result = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
        assert (result["train"]["outcome"], result["train"]["reason"]) == (
            "parked", "TRAIN_CLOSED_PROOF_UNAVAILABLE")
    assert not _train_calls(gate, "train_close")
    assert "train" in _owner_active(store)

    gate.responses[("train_inspect", "core-repo")] = {**closed, "main_on_chain": False}
    clock[0] += 60
    moved = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    assert (moved["train"]["outcome"], moved["train"]["reason"]) == ("closed", "TRAIN_BASE_MOVED")
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_BASE_MOVED")]


def test_v2_owner_run_that_never_appeared_retires_the_family_a_window_after_the_build(tmp_path):
    """`_note_progress` dates a first missing reading from the wait start, which a
    fresh build sets, so a run that never starts closes at the CI start window."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    first = _owner_pass(tmp_path, gate, store, clock, 1000.0 + 30, "CI_RUN_MISSING")
    inside = _owner_pass(tmp_path, gate, store, clock, 1000.0 + _LOST_OWNER_WINDOW - 1)

    assert (first["train"]["outcome"], inside["train"]["outcome"]) == ("waiting", "waiting")
    assert not _train_calls(gate, "train_close")
    closed = _owner_pass(tmp_path, gate, store, clock, 1000.0 + _LOST_OWNER_WINDOW)
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_CI_LOST: CI_RUN_MISSING")]
    assert closed["train"]["outcome"] == "closed"
    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]


def test_v2_owner_back_in_flight_restarts_the_lost_run_window(tmp_path):
    """The lost-run sequence: the run ends cancelled, a job re-run puts it back in
    flight (attempt 2), and it ends cancelled again. The window runs from the
    second loss; the first one is no longer a reading of the owner."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    _owner_pass(tmp_path, gate, store, clock, 1000.0, "CI_RUN_IN_PROGRESS")
    _owner_pass(tmp_path, gate, store, clock, 2000.0, "CI_RUN_CANCELED")
    assert _owner_active(store)["progress"] == ["CI_RUN_CANCELED", 2000.0]
    _owner_pass(tmp_path, gate, store, clock, 2500.0, "CI_RUN_IN_PROGRESS", attempt=2)
    assert _owner_active(store)["progress"] == ["CI_RUN_IN_PROGRESS", 2500.0]
    _owner_pass(tmp_path, gate, store, clock, 3000.0, "CI_RUN_CANCELED", attempt=2)
    assert _owner_active(store)["progress"] == ["CI_RUN_CANCELED", 3000.0]

    # 1300 s after the first loss and 300 s after the second.
    _owner_pass(tmp_path, gate, store, clock, 3300.0)
    _owner_pass(tmp_path, gate, store, clock, 3000.0 + _LOST_OWNER_WINDOW - 1)
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, 3000.0 + _LOST_OWNER_WINDOW)
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_CI_LOST: CI_RUN_CANCELED")]


def _owner_pass_unread(tmp_path, monkeypatch, gate, store, clock, at, how):
    """One train-v2 pass at `at` in which the owner does not read as a lost run.

    `how` is a reading of `_OWNER_READINGS`, or a pass that fails to read the owner
    or finds it green."""
    if how in _OWNER_READINGS:
        return _owner_pass(tmp_path, gate, store, clock, at, how)

    def lost_on_the_wire(*_args):
        raise TimeoutError("the gate did not answer")

    with monkeypatch.context() as patch:
        if how == "inspection refused":
            gate.responses[("train_inspect", "core-repo")] = {"ok": False, "code": "FETCH_FAILED"}
        elif how == "inspection stopped":
            gate.responses[("train_inspect", "core-repo")] = {
                "ok": False, "code": "EXECUTOR_HOST_REQUIRED"}
        elif how == "inspection raised":
            patch.setattr(gate, "train_inspect", lost_on_the_wire)
        else:
            assert how == "owner green"
            # The gate's default reading is green. The first member's merge is lost on the
            # wire, so the pass merges nothing, parks nothing and leaves it to the next.
            gate.responses.pop(("train_inspect", "core-repo"))
            patch.setattr(gate, "train_member_merge", lost_on_the_wire)
        return _owner_pass(tmp_path, gate, store, clock, at)


@pytest.mark.parametrize("lost,between", [
    ("CI_RUN_CANCELED", "CI_API_UNAVAILABLE"),
    ("CI_RUN_CANCELED", "CI_RESPONSE_INVALID"),
    ("CI_RUN_CANCELED", "CI_PROFILE_INSUFFICIENT"),
    ("CI_RUN_CANCELED", "inspection refused"),
    # The raised inspection reports `CANDIDATE_ERROR:TimeoutError`, which the queue
    # store would refuse as a progress code: the pass must not persist the reading.
    ("CI_RUN_CANCELED", "inspection raised"),
    # The stop returns before the park every other pass ends in.
    ("CI_RUN_CANCELED", "inspection stopped"),
    ("CI_RUN_CANCELED", "owner green"),
    # A missing run is dated from the wait start. Once a pass has ended that interval, a
    # returning missing run is dated from its own pass, not from the wait start again.
    ("CI_RUN_MISSING", "CI_API_UNAVAILABLE"),
])
def test_v2_owner_lost_run_window_restarts_after_a_pass_that_did_not_read_it(
        tmp_path, monkeypatch, lost, between):
    """The owner read cancelled at 1000, unreadable at 1600 and cancelled
    at 2200, and the family closed at once: the unreadable interval counted toward the
    window. A return to a lost code opens a window of its own."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    _owner_pass(tmp_path, gate, store, clock, 1000.0, lost)
    assert _owner_active(store)["progress"] == [lost, 1000.0]

    _owner_pass_unread(tmp_path, monkeypatch, gate, store, clock, 1000.0 + 600, between)
    assert not _train_calls(gate, "train_close")

    # The first window would end at 2200. The return opens a new one at that moment.
    returned = 1000.0 + _LOST_OWNER_WINDOW
    _owner_pass(tmp_path, gate, store, clock, returned, lost)
    assert not _train_calls(gate, "train_close")
    assert _owner_active(store)["progress"] == [lost, returned]

    _owner_pass(tmp_path, gate, store, clock, returned + _LOST_OWNER_WINDOW - 1)
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, returned + _LOST_OWNER_WINDOW)
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, f"TRAIN_OWNER_CI_LOST: {lost}")]


def test_v2_owner_lost_run_window_restarts_when_the_lost_code_changes(tmp_path):
    """An owner that goes straight from cancelled to
    skipped, with no other reading between, is dated from the skipped reading. The
    cancelled deadline closes nothing; the family closes a full window after the
    skipped reading, under that code."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    _owner_pass(tmp_path, gate, store, clock, 1000.0, "CI_RUN_CANCELED")
    assert _owner_active(store)["progress"] == ["CI_RUN_CANCELED", 1000.0]

    skipped = 1000.0 + 600
    _owner_pass(tmp_path, gate, store, clock, skipped, "CI_RUN_SKIPPED")
    assert _owner_active(store)["progress"] == ["CI_RUN_SKIPPED", skipped]

    _owner_pass(tmp_path, gate, store, clock, 1000.0 + _LOST_OWNER_WINDOW, "CI_RUN_SKIPPED")
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, skipped + _LOST_OWNER_WINDOW - 1, "CI_RUN_SKIPPED")
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, skipped + _LOST_OWNER_WINDOW, "CI_RUN_SKIPPED")
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_CI_LOST: CI_RUN_SKIPPED")]


def test_v2_owner_lost_reading_while_a_member_is_unlisted_still_dates_a_later_missing_run(
        tmp_path):
    """A cancelled first reading while a member is
    briefly unlisted keeps the park, but it is still the owner's first reading. A
    missing run after the member returns is dated from itself, not from the build."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    gate.by_repository["core-repo"].remove(2)
    _owner_pass(tmp_path, gate, store, clock, 1030.0, "CI_RUN_CANCELED")
    assert _owner_active(store)["progress"] == ["CI_RUN_CANCELED", 1030.0]

    gate.by_repository["core-repo"].insert(1, 2)
    missing = 1000.0 + _LOST_OWNER_WINDOW + 60
    _owner_pass(tmp_path, gate, store, clock, missing, "CI_RUN_MISSING")
    assert _owner_active(store)["progress"] == ["CI_RUN_MISSING", missing]
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, missing + _LOST_OWNER_WINDOW - 1, "CI_RUN_MISSING")
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, missing + _LOST_OWNER_WINDOW, "CI_RUN_MISSING")
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_CI_LOST: CI_RUN_MISSING")]


@pytest.mark.parametrize("first", [
    "CI_API_UNAVAILABLE", "CI_RESPONSE_INVALID", "inspection refused", "inspection raised",
    "owner green"])
def test_v2_owner_first_read_without_a_lost_code_gives_a_later_missing_run_its_own_window(
        tmp_path, monkeypatch, first):
    """A fresh owner whose first pass read unreadable
    CI, failed to inspect or found it green kept no progress pair, so a missing run
    that followed was dated from the wait start. Once the build or the green pass was
    a window old, the family closed on that first missing reading. Only a missing run
    that is the owner's first reading is dated from the wait start;
    `test_v2_owner_run_that_never_appeared_retires_the_family_a_window_after_the_build`
    pins that case."""
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    # `_owner_pass_unread` drops the reading a green pass replaces; a fresh build has none.
    gate.responses[("train_inspect", "core-repo")] = {}
    _owner_pass_unread(tmp_path, monkeypatch, gate, store, clock, 1000.0 + 30, first)
    assert not _train_calls(gate, "train_close")

    # The wait start, 1000 or 1030, is more than a window old by now.
    missing_at = 1000.0 + _LOST_OWNER_WINDOW + 60
    _owner_pass(tmp_path, gate, store, clock, missing_at, "CI_RUN_MISSING")
    assert not _train_calls(gate, "train_close")
    assert _owner_active(store)["progress"] == ["CI_RUN_MISSING", missing_at]

    _owner_pass(tmp_path, gate, store, clock, missing_at + _LOST_OWNER_WINDOW - 1)
    assert not _train_calls(gate, "train_close")

    _owner_pass(tmp_path, gate, store, clock, missing_at + _LOST_OWNER_WINDOW)
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_CI_LOST: CI_RUN_MISSING")]


@pytest.mark.parametrize("lost,earlier", [
    ("CI_RUN_MISSING", None),
    ("CI_RUN_CANCELED", "CI_RUN_CANCELED"),
])
def test_v2_owner_red_close_refused_before_any_write_ends_the_lost_run_interval(
        tmp_path, lost, earlier):
    """The gate re-reads the red proof on close and can refuse before it writes; the
    executor then parks the family in `_close_train`, not in `park`. That pass read no
    lost code either: a fresh owner's later missing run is not dated from the wait
    start, and an earlier cancelled reading does not go on counting."""
    refused = {"ok": False, "code": "TRAIN_BUILD_FAILED",
               "detail": {"cleanup_started": False, "park": True}}
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate({("train_close", "core-repo"): [refused, None]})
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    if earlier is not None:
        _owner_pass(tmp_path, gate, store, clock, 1000.0, earlier)
    gate.responses[("train_inspect", "core-repo")] = {
        "ci_state": "red", "blocking_reason": "CI_RUN_FAILED", "ci_run_conclusion": "failure",
        "ci_terminal_red": True, "base_failure_match": False}
    _owner_pass(tmp_path, gate, store, clock, 1000.0 + 600)
    assert [call[3] for call in _train_calls(gate, "train_close")] == ["TRAIN_OWNER_RED"]
    assert _owner_active(store)["train"]["phase"] == "owner_parked"

    # The first window would end at 2200. The return opens a new one at that moment.
    returned = 1000.0 + _LOST_OWNER_WINDOW
    _owner_pass(tmp_path, gate, store, clock, returned, lost)
    assert [call[3] for call in _train_calls(gate, "train_close")] == ["TRAIN_OWNER_RED"]
    assert _owner_active(store)["progress"] == [lost, returned]

    _owner_pass(tmp_path, gate, store, clock, returned + _LOST_OWNER_WINDOW - 1)
    assert len(_train_calls(gate, "train_close")) == 1

    _owner_pass(tmp_path, gate, store, clock, returned + _LOST_OWNER_WINDOW)
    assert [call[3] for call in _train_calls(gate, "train_close")] == [
        "TRAIN_OWNER_RED", f"TRAIN_OWNER_CI_LOST: {lost}"]


def test_v2_owner_lost_after_a_member_merged_keeps_the_family_parked(tmp_path):
    gate = _v2_election_gate({("train_member_merge", "core-repo", 2): {
        "ok": False, "code": "PR_CI_NOT_GREEN", "detail": {"ci_verdict": "unavailable"}}})
    clock, store = [1000.0], _queue_store(tmp_path)
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    assert _owner_active(store)["train"]["merged"] == [1]

    _owner_pass(tmp_path, gate, store, clock, 1000.0, "CI_RUN_CANCELED")
    late = _owner_pass(tmp_path, gate, store, clock, 1000.0 + 5 * _LOST_OWNER_WINDOW)

    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "mark_candidate")
    assert late["train"]["outcome"] == "parked"
    assert _owner_active(store)["train"]["phase"] == "owner_parked"


def test_v2_owner_lost_after_a_member_left_the_queue_keeps_the_family_parked(tmp_path):
    """A native merge whose response was lost leaves the journal's `merged` empty,
    but the member is no longer listed: it counts as merged here."""
    gate = _v2_election_gate()
    clock, store = [1000.0], _queue_store(tmp_path)
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    gate.by_repository["core-repo"].remove(1)

    _owner_pass(tmp_path, gate, store, clock, 1000.0, "CI_RUN_CANCELED")
    late = _owner_pass(tmp_path, gate, store, clock, 1000.0 + 5 * _LOST_OWNER_WINDOW)

    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "mark_candidate")
    assert late["train"]["outcome"] == "parked"
    assert _owner_active(store)["train"].get("merged", []) == []


@pytest.mark.parametrize("reading", [
    "CI_RUN_QUEUED", "CI_RUN_IN_PROGRESS", "CI_API_UNAVAILABLE", "CI_RESPONSE_INVALID",
    "CI_PROFILE_INSUFFICIENT"])
def test_v2_owner_that_is_not_lost_is_never_retired_however_long_it_waits(tmp_path, reading):
    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate()
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    _owner_pass(tmp_path, gate, store, clock, 1000.0, reading)
    for at in (1000.0 + 4 * _LOST_OWNER_WINDOW, 1000.0 + 20 * _LOST_OWNER_WINDOW):
        summary = _owner_pass(tmp_path, gate, store, clock, at)
        assert summary["train"]["outcome"] in {"waiting", "parked"}

    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "mark_candidate")
    assert _owner_active(store)["train"]["phase"] == "owner_parked"


def test_v2_lost_owner_close_that_fails_keeps_a_storable_reason_and_warns_once(tmp_path, caplog):
    from git_gate.automerge_queue import TRAIN_CLOSE_REASON

    clock, store = [1000.0], _queue_store(tmp_path)
    gate = _v2_election_gate({("train_close", "core-repo"): [
        {"ok": False, "code": "TRAIN_CLOSE_FAILED"}, None]})
    reading = "CI_RUN_CANCELED"
    reason = f"TRAIN_OWNER_CI_LOST: {reading}"
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    _owner_pass(tmp_path, gate, store, clock, 1000.0, reading)

    with caplog.at_level(logging.WARNING, logger=merge_open_prs.LOGGER.name):
        refused = _owner_pass(tmp_path, gate, store, clock, 1000.0 + _LOST_OWNER_WINDOW)
        # The queue store validates what it persists: the reason must survive a reload.
        retained = _owner_active(store)["train"]["cleanup"]
        assert retained == {"reason": reason, "outcome": "closed"}
        assert TRAIN_CLOSE_REASON.fullmatch(reason)
        assert refused["train"]["close_failed"] == "TRAIN_CLOSE_FAILED"
        assert not _train_calls(gate, "mark_candidate")

        retried = _owner_pass(tmp_path, gate, store, clock, 1000.0 + _LOST_OWNER_WINDOW + 60)

    assert [call[3] for call in _train_calls(gate, "train_close")] == [reason, reason]
    assert retried["train"]["outcome"] == "closed"
    assert len(_lost_owner_warnings(caplog, reading)) == 1


@pytest.mark.parametrize("base_match", [True, None])
def test_v2_owner_red_with_matching_or_unreadable_base_proof_parks(tmp_path, base_match):
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_inspect", "core-repo"): {
        "ci_state": "red", "blocking_reason": "CI_RUN_FAILED", "ci_run_conclusion": "failure",
        "ci_terminal_red": True, "base_failure_match": base_match,
    }})
    _election_pass(tmp_path, gate, store, policy="train-v2")

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "train_member_merge")
    assert summary["train"]["outcome"] == "parked"
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["phase"] == "owner_parked"


@pytest.mark.parametrize("proof", ["missing", "pending", "red", "complete"])
def test_v2_closed_owner_resume_requires_native_recovery_proof(tmp_path, proof):
    response = {"pr_state": "closed", "ci_state": "pending", "ci_terminal_red": False,
                "main_is_final": False, "base_failure_match": False}
    if proof == "missing":
        response["mode"] = None
    elif proof == "red":
        response.update(ci_state="red", ci_terminal_red=True, ci_run_conclusion="failure")
    elif proof == "complete":
        response.update(ci_state="green", main_is_final=True)
    gate = _v2_election_gate({("train_inspect", "core-repo"): response})
    store = _queue_store(tmp_path)
    _election_pass(tmp_path, gate, store, policy="train-v2")
    if proof == "complete":
        for pr in (1, 2, 3):
            gate.responses[("inspect", "core-repo", pr)]["pr_state"] = "merged"

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    if proof in {"missing", "pending"}:
        assert not _train_calls(gate, "train_close")
        assert not _train_calls(gate, "mark_candidate")
        assert store.read()["repositories"]["core-repo"]["active"]["train"]["phase"] == "owner_parked"
    else:
        reason = "TRAIN_OWNER_RED" if proof == "red" else "TRAIN_MEMBERS_MERGED"
        assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, reason)]
        if proof == "red":
            assert summary["train"]["serial_return"] is True
        else:
            assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "train_member_merge")


def test_v2_owner_strict_red_returns_to_serial_without_another_train_or_prefix(tmp_path):
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_inspect", "core-repo"): {
        "ci_state": "red", "blocking_reason": "CI_RUN_FAILED", "ci_run_conclusion": "failure",
        "ci_terminal_red": True, "base_failure_match": False,
        "failure_classification": {"kind": "infrastructure", "authority": "advisory"},
    }})
    _election_pass(tmp_path, gate, store, policy="train-v2")

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_RED")]
    assert summary["train"]["serial_return"] is True
    assert len(_train_calls(gate, "train_build")) == 1
    assert not _train_calls(gate, "train_member_merge")
    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]


@pytest.mark.parametrize("reported,expected", [
    pytest.param({"owner_failed_jobs": ["lint"]}, ["lint"], id="one-leaf"),
    pytest.param({"owner_failed_jobs": ["leaf-02", "lint"]}, ["leaf-02", "lint"],
                 id="several-leaves"),
    pytest.param({"owner_failed_jobs": None}, None, id="unknown"),
    pytest.param({}, None, id="not-reported"),
])
def test_v2_owner_strict_red_journals_failed_jobs_for_attribution_only(tmp_path, reported, expected):
    """the gate's failed job names reach the journal verbatim and decide nothing."""
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_inspect", "core-repo"): {
        "ci_state": "red", "blocking_reason": "CI_RUN_FAILED", "ci_run_conclusion": "failure",
        "ci_terminal_red": True, "base_failure_match": False, **reported,
    }})
    _election_pass(tmp_path, gate, store, policy="train-v2")

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert summary["train"]["failed_jobs"] == expected
    # The decisions are those of the same red owner without the attribution.
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_RED")]
    assert summary["train"]["serial_return"] is True
    assert len(_train_calls(gate, "train_build")) == 1
    assert not _train_calls(gate, "train_member_merge")
    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]


def test_v2_owner_red_attribution_is_journaled_once_before_a_retained_cleanup(tmp_path):
    """the observing pass journals the names; a cleanup retry adds no null."""
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({
        ("train_inspect", "core-repo"): {
            "ci_state": "red", "blocking_reason": "CI_RUN_FAILED", "ci_run_conclusion": "failure",
            "ci_terminal_red": True, "base_failure_match": False, "owner_failed_jobs": ["lint"]},
        ("train_close", "core-repo"): [{"ok": False, "code": "TRAIN_CLOSE_FAILED"}, None],
    })
    _election_pass(tmp_path, gate, store, policy="train-v2")
    failed = _election_pass(tmp_path, gate, store, policy="train-v2")
    assert failed["train"]["close_failed"] == "TRAIN_CLOSE_FAILED"
    assert failed["train"]["failed_jobs"] == ["lint"]
    gate.calls.clear()

    # The next pass loads the persisted queue and retries the cleanup only.
    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert not _train_calls(gate, "train_inspect")
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_OWNER_RED")]
    assert summary["train"]["serial_return"] is True
    assert "failed_jobs" not in summary["train"]


def _v2_poll_clock(monkeypatch):
    clock, sleeps = [1000.0], []
    real_time = merge_open_prs.time

    class ExecutorTime:
        def __getattr__(self, name):
            return getattr(real_time, name)

        def monotonic(self):
            return clock[0]

        def sleep(self, seconds):
            sleeps.append(seconds)
            clock[0] += seconds

    # Do not replace attributes on the shared time module: subprocess waits
    # and review concurrency must retain their real clocks and sleeps.
    monkeypatch.setattr(merge_open_prs, "time", ExecutorTime())
    return clock, sleeps


@pytest.mark.parametrize("mergeable", [False, None])
def test_v2_owner_green_settles_in_pass_without_ci_between_members(tmp_path, monkeypatch, mergeable):
    clock, sleeps = _v2_poll_clock(monkeypatch)
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_member_merge", "core-repo", 1): [
        {"ok": False, "code": "PR_MERGE_FAILED", "detail": {"mergeable": mergeable}}, {}]})
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1, 1, 2, 3]
    assert sleeps == [2.0]
    assert summary["train"]["outcome"] == "merged"
    assert (summary["train"]["selected_run"], summary["train"]["selected_attempt"]) == (70, 1)
    assert not _train_calls(gate, "mark_candidate")
    assert not _train_calls(gate, "refresh")
    assert len(_train_calls(gate, "train_build")) == 1


def test_remote_intent_during_train_merge_settlement_stops_the_next_attempt(tmp_path, monkeypatch):
    clock, sleeps = _v2_poll_clock(monkeypatch)
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_member_merge", "core-repo", 1): {
        "ok": False, "code": "PR_MERGE_FAILED", "detail": {"mergeable": False}}})
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)
    marker = tmp_path / "deploy-intent.json"
    monkeypatch.setattr(merge_open_prs, "DEPLOY_MARKER_PATH", marker)
    sleep = merge_open_prs.time.sleep

    def announce(seconds):
        sleep(seconds)
        marker.write_text(json.dumps({
            "schema": "deploy-marker.v1", "state": "running", "origin": "remote-origin",
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }))

    monkeypatch.setattr(merge_open_prs.time, "sleep", announce)
    summary = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1]
    assert sleeps == [2.0]
    assert summary["hold"]["reason"] == "DEPLOY_WAITING"
    assert not _train_calls(gate, "train_close")
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900


def test_v2_owner_mergeability_settle_wait_stops_at_sixty_seconds(tmp_path, monkeypatch):
    clock, sleeps = _v2_poll_clock(monkeypatch)
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_member_merge", "core-repo", 1): {
        "ok": False, "code": "PR_MERGE_FAILED", "detail": {"mergeable": False}}})
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    summary = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    assert sum(sleeps) == merge_open_prs.MERGEABLE_SETTLE_SECONDS == 60
    assert len(_train_calls(gate, "train_member_merge")) == 31
    assert {call[2] for call in _train_calls(gate, "train_member_merge")} == {1}
    assert summary["train"]["reason"] == "PR_MERGE_FAILED"
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "PR_MERGE_FAILED")]


def test_v2_owner_does_not_retry_an_uncertain_native_merge_and_resumes_from_live_members(
        tmp_path, monkeypatch):
    clock, sleeps = _v2_poll_clock(monkeypatch)
    store = _queue_store(tmp_path)
    gate = _v2_election_gate()
    original_merge = gate.train_member_merge

    def merge(repository, owner, pr, head):
        result = original_merge(repository, owner, pr, head)
        if pr == 1:
            gate.by_repository[repository].remove(pr)  # Native merge succeeded; response was lost.
            raise TimeoutError("native merge response lost")
        return result

    gate.train_member_merge = merge
    _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    interrupted = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    assert interrupted["train"]["outcome"] == "merging"
    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1]
    assert not sleeps and not _train_calls(gate, "train_close")
    assert store.read()["repositories"]["core-repo"]["active"]["train"].get("merged", []) == []

    resumed = _election_pass(tmp_path, gate, store, policy="train-v2", clock=clock)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1, 2, 3]
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "merged #2, #3 - left #1")]
    assert resumed["train"]["outcome"] == "merged"
    assert not sleeps and not _train_calls(gate, "mark_candidate")
    assert len(_train_calls(gate, "train_build")) == 1


@pytest.mark.parametrize("refusal", ["CI_PROFILE_INSUFFICIENT", "PR_CI_NOT_GREEN"])
def test_v2_owner_proof_loss_after_a_partial_merge_parks_the_existing_family(
        tmp_path, refusal):
    store = _queue_store(tmp_path)
    gate = _v2_election_gate({("train_member_merge", "core-repo", 2): {
        "ok": False, "code": refusal, "detail": {"ci_verdict": "unavailable"}}})
    _election_pass(tmp_path, gate, store, policy="train-v2")

    summary = _election_pass(tmp_path, gate, store, policy="train-v2")

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1, 2]
    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "mark_candidate")
    assert summary["train"]["outcome"] == "parked"
    train = store.read()["repositories"]["core-repo"]["active"]["train"]
    assert train["merged"] == [1] and train["phase"] == "owner_parked"


def test_an_approved_narrowed_leader_forms_a_capped_train_instead_of_its_mark(tmp_path):
    store = _queue_store(tmp_path)
    gate = _election_gate()

    summary = _election_pass(tmp_path, gate, store)

    # The cap is two of the three narrowed heads: PR 3 is neither read for a seat
    # nor built in, and the mark that would start the leader's full run is not placed.
    assert [call[2] for call in _train_calls(gate, "train_build")] == [
        ((1, "1" * 40), (2, "2" * 40))]
    assert summary["train"]["outcome"] == "built"
    assert summary["train"]["members"] == [1, 2]
    assert {key: summary["train"][key] for key in _ELECTION_CAP} == _ELECTION_CAP
    assert not _train_calls(gate, "mark_candidate")
    train = store.read()["repositories"]["core-repo"]["active"]["train"]
    assert [member["pr"] for member in train["members"]] == [1, 2]


@pytest.mark.parametrize("numbers, rider, why", [
    pytest.param((1,), None, "TRAIN_NO_CANDIDATE", id="a_lone_leader"),
    pytest.param((1, 2), {"latest_delegated_review_state": None}, "TRAIN_TOO_SHORT",
                 id="the_only_rider_lacks_an_approval"),
])
def test_a_leader_without_a_rider_is_marked_in_the_same_pass(tmp_path, numbers, rider, why):
    """The attempt costs the serial path nothing: it falls through to the mark."""
    gate = _election_gate({
        ("inspect", "core-repo", 2): _narrowed_rider("2", 12) | (rider or {})}, numbers=numbers)

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path))

    assert summary["train"]["outcome"] == "not_formed" and summary["train"]["reason"] == why
    assert {key: summary["train"][key] for key in _ELECTION_CAP} == _ELECTION_CAP
    assert not _train_calls(gate, "train_build")
    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_CANDIDATE_MARKED"}]
    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]


def test_a_refused_election_time_build_falls_through_to_the_mark(tmp_path):
    """A refusal builds nothing, so it costs the serial path nothing either."""
    gate = _election_gate({
        ("train_build", "core-repo"): {"ok": False, "code": "TRAIN_BUILD_FAILED"}},
        numbers=(1, 2))

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path))

    assert summary["train"] == {
        "pr": None, "members": [1, 2], "candidate_sha": None, "candidate_tree": None,
        "outcome": "build_refused", "reason": "TRAIN_BUILD_FAILED", **_ELECTION_CAP}
    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "PR_CANDIDATE_MARKED"}]
    assert _train_calls(gate, "mark_candidate") == [("mark_candidate", "core-repo", 1, True)]


def test_an_uncertain_election_time_build_leaves_the_mark_to_the_next_pass(tmp_path):
    """The gate may have pushed the train branch before the response was lost. A mark
    in the same pass would start a second full run beside it."""
    gate = _election_gate(numbers=(1, 2))
    original_build = gate.train_build

    def train_build(repository, members, expected_base):
        original_build(repository, members, expected_base)
        raise TimeoutError("the train build response was lost")

    gate.train_build = train_build

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path))

    assert len(_train_calls(gate, "train_build")) == 1
    assert summary["train"]["outcome"] == "build_refused"
    assert {key: summary["train"][key] for key in _ELECTION_CAP} == _ELECTION_CAP
    assert not _train_calls(gate, "mark_candidate")
    assert summary["results"] == []


@pytest.mark.parametrize("leader, rider", [
    pytest.param({"merge_candidate": True}, {}, id="already_marked"),
    # An unapproved head leads only when no approved one is queued, so
    # its rider is unapproved too.
    pytest.param({"latest_delegated_review_state": None},
                 {"latest_delegated_review_state": None}, id="not_approved"),
    pytest.param({"mergeable": False}, {}, id="not_mergeable"),
    pytest.param({"ci_review_accepted": False}, {}, id="narrowed_run_refused_by_the_review"),
])
def test_no_train_forms_at_election_time_unless_the_serial_path_would_mark(
        tmp_path, leader, rider):
    """A marked leader already waits on its full run (no repeated rider scan per
    pass), and a head that cannot land next is no leader."""
    gate = _election_gate({
        ("inspect", "core-repo", 1): _narrowed_rider("1", 11) | leader,
        ("inspect", "core-repo", 2): _narrowed_rider("2", 12) | rider}, numbers=(1, 2))

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path))

    assert "train" not in summary
    assert not _train_calls(gate, "train_build")


def test_a_leader_green_on_the_full_profile_merges_without_a_train(tmp_path):
    """its green run is the evidence the merge needs, so nothing is marked."""
    gate = _election_gate({
        ("inspect", "core-repo", 1): _queue_inspect(
            head="1", merge_candidate=False, ci_accepted_profile="checks/c1")}, numbers=(1, 2))

    summary = _election_pass(tmp_path, gate, _queue_store(tmp_path))

    assert [call[:3] for call in gate.calls if call[0] == "merge"][:1] == [
        ("merge", "core-repo", 1)]
    assert not _train_calls(gate, "train_build")
    assert summary["results"][0] == {"pr": 1, "outcome": "merged"}


def test_a_lone_queued_pr_journals_no_candidate(tmp_path):
    summary = _train_pass(tmp_path, _train_gate(numbers=(1,)), _queue_store(tmp_path))

    assert summary["train"] == {"pr": None, "members": [1], "outcome": "not_formed",
                                "reason": "TRAIN_NO_CANDIDATE", "inspected": []}
    assert summary["results"] == [{"pr": 1, "outcome": "refreshed", "head_sha": "c" * 40}]


@pytest.mark.parametrize("failure, raised", [
    ("INTERNAL_ERROR", False), ("INTERNAL_ERROR", True), ("REVIEW_SUPERVISOR_INVALID", False)],
    ids=["integrity-returned", "integrity-raised", "authority-returned"])
def test_an_authority_or_integrity_failure_inspecting_a_later_train_member_stops_the_pass(
        monkeypatch, tmp_path, capsys, failure, raised):
    """PR 3 is inspected after PR 2 proved eligible, before any build."""
    gate = _train_gate(numbers=(1, 2, 3, 4))
    gate.by_repository["docs-repo"] = []  # So exit 2 can only be the stop.
    original_inspect = gate.inspect

    def inspect(repository, pr):
        if (repository, pr) != ("core-repo", 3):
            return original_inspect(repository, pr)
        gate.calls.append(("inspect", repository, pr))
        if raised:
            raise merge_open_prs.GateError(failure)
        return {"ok": False, "code": failure}

    gate.inspect = inspect
    repositories = {"core-repo": _train_repository(tmp_path),
                    "docs-repo": _docs_repo(tmp_path)}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: gate)

    code = merge_open_prs.main(["--automerge", "--json"])

    summary = json.loads(capsys.readouterr().out)["repositories"][0]
    assert code == 2
    assert summary["authority_integrity_failed"] is True
    assert summary["results"] == [{"pr": 3, "outcome": "retained", "reason": failure,
                                   "authority_integrity_failed": True}]
    assert "train" not in summary
    assert ("inspect", "core-repo", 4) not in gate.calls  # The scan ended at PR 3.
    assert not any(call[0].startswith("train_") or call[0] in _MUTATIONS for call in gate.calls)


def test_an_ordinary_failure_inspecting_a_later_train_member_still_builds_without_it(tmp_path):
    # Selection ranks PR 3 first; only the train scan's read fails.
    gate = _train_gate({("inspect", "core-repo", 3): [
        _rider("3", 13), {"ok": False, "code": "FETCH_FAILED"}]})

    summary = _train_pass(tmp_path, gate, _queue_store(tmp_path))

    assert [call[2] for call in _train_calls(gate, "train_build")] == [((1, "1" * 40), (2, "2" * 40))]
    assert summary["train"]["members"] == [1, 2]
    assert "authority_integrity_failed" not in summary or not summary["authority_integrity_failed"]
    assert summary["results"] == []


def test_three_members_ride_one_train_and_merge_under_the_train_basis(tmp_path):
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = _train_gate({("train_inspect", "core-repo"): [
        {"ci_state": "pending", "blocking_reason": "CI_RUN_IN_PROGRESS",
         "ci_run_conclusion": None}, {}]})

    built = _train_pass(tmp_path, gate, store, clock)

    assert _train_calls(gate, "train_build") == [
        ("train_build", "core-repo", ((1, "1" * 40), (2, "2" * 40), (3, "3" * 40)), _TRAIN_BASE)]
    assert _train_calls(gate, "refresh") == []  # The build is the pass's refresh.
    assert built["train"] == {"pr": 900, "members": [1, 2, 3], "candidate_sha": _CANDIDATES[2],
                              "candidate_tree": "f" * 40, "outcome": "built"}
    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["pr"] == 1
    assert active["train"] == {
        "pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": _TRAIN_BASE, "started": 1000.0,
        "members": [{"pr": pr, "head": head * 40, "candidate": candidate}
                    for pr, head, candidate in zip((1, 2, 3), "123", _CANDIDATES)],
        "prefixes": _train_prefix_metadata(gate.built)}
    assert gate.statuses[-1][1][1] == "queue=active reason=waiting_ci owner=queue pos=1"

    clock[0] += 60
    waiting = _train_pass(tmp_path, gate, store, clock)
    assert waiting["train"]["outcome"] == "waiting"
    assert waiting["results"] == []
    assert _train_calls(gate, "train_member_merge") == []

    clock[0] += 60
    merged = _train_pass(tmp_path, gate, store, clock)
    assert merged["results"] == [
        {"pr": pr, "outcome": "merged", "ci_basis": "train", "approval_basis": "exact",
         "train_pr": 900} for pr in (1, 2, 3)]
    assert [call[2:] for call in _train_calls(gate, "train_member_merge")] == [
        (pr, 900, head * 40) for pr, head in zip((1, 2, 3), "123")]
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "merged #1, #2, #3")]
    assert merged["train"]["outcome"] == "merged"
    assert store.read()["repositories"]["core-repo"]["active"] is None
    assert _train_calls(gate, "merge") == []  # AUTOMERGE_PR never carries a member.


def test_a_member_dropped_for_a_conflict_is_parked_and_the_rest_ride(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_build", "core-repo"): {
        "ok": True, "code": "TRAIN_BUILT", "train_pr": 900,
        "train_branch": "train/bbbbbbbbbbbb-1", "base_sha": _TRAIN_BASE,
        "candidate_sha": _CANDIDATES[1], "candidate_tree": "f" * 40,
        "members": [{"pr": 1, "head": "1" * 40, "candidate": _CANDIDATES[0]},
                    {"pr": 3, "head": "3" * 40, "candidate": _CANDIDATES[1]}],
        "dropped": [{"pr": 2, "reason": "PR_REFRESH_CONFLICT"}]}})

    summary = _train_pass(tmp_path, gate, store)

    row = store.read()["repositories"]["core-repo"]
    assert [member["pr"] for member in row["active"]["train"]["members"]] == [1, 3]
    assert row["parked"]["2"]["reason"] == "conflict"
    assert row["parked"]["2"]["head"] == "2" * 40
    assert {"pr": 2, "outcome": "retained", "reason": "PR_REFRESH_CONFLICT",
            "train_dropped": True} in summary["results"]


def test_a_refused_build_leaves_the_serial_refresh_to_the_same_pass(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_build", "core-repo"): {
        "ok": False, "code": "TRAIN_TOO_SHORT",
        "detail": {"dropped": [{"pr": 2, "reason": "PR_REFRESH_CONFLICT"}]}}}, numbers=(1, 2))

    summary = _train_pass(tmp_path, gate, store)

    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]
    row = store.read()["repositories"]["core-repo"]
    assert "train" not in row["active"]
    assert row["parked"]["2"]["reason"] == "conflict"
    assert summary["train"]["outcome"] == "build_refused"


def test_a_member_dropped_for_a_conflict_is_not_released_by_mergeable(tmp_path):
    """A train build found the conflict by merging, not from the forge flag,
    so the park carries no code and `mergeable: true` does not release it."""
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_build", "core-repo"): {
        "ok": False, "code": "TRAIN_TOO_SHORT",
        "detail": {"dropped": [{"pr": 2, "reason": "PR_REFRESH_CONFLICT"}]}}}, numbers=(1, 2))
    _train_pass(tmp_path, gate, store)
    parked = store.read()["repositories"]["core-repo"]["parked"]["2"]
    assert parked["reason"] == "conflict" and "code" not in parked

    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="c")
    gate.responses[("inspect", "core-repo", 2)] = _rider("2", 12) | {"mergeable": True}
    gate.calls.clear()
    summary = _train_pass(tmp_path, gate, store)

    assert summary["results"] == [{"pr": 1, "outcome": "merged"}]
    assert ("inspect", "core-repo", 2) in gate.calls
    parked = store.read()["repositories"]["core-repo"]["parked"]["2"]
    assert parked["reason"] == "conflict" and "code" not in parked
    assert not any(call[0] in {"refresh", "review", "merge"} and call[2] == 2
                   for call in gate.calls)


def test_a_member_whose_approval_does_not_reach_its_position_ends_the_train(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_member_merge", "core-repo", 2): {
        "ok": False, "code": "PR_REVIEW_NOT_APPROVED"}})
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()

    summary = _train_pass(tmp_path, gate, store)

    assert summary["results"][:2] == [
        {"pr": 1, "outcome": "merged", "ci_basis": "train", "approval_basis": "exact",
         "train_pr": 900},
        {"pr": 2, "outcome": "retained", "reason": "PR_REVIEW_NOT_APPROVED", "train_pr": 900}]
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "PR_REVIEW_NOT_APPROVED")]
    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1, 2]
    # The serial path takes the refused member at once, and no second train
    # is formed in the same pass.
    assert [call[2] for call in _train_calls(gate, "refresh")] == [2]
    assert _train_calls(gate, "train_build") == []
    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["pr"] == 2 and "train" not in active


@pytest.mark.parametrize("observed", [
    {"ci_state": "red", "ci_run_conclusion": "failure", "blocking_reason": "CI_RUN_FAILED"},
    {"ci_state": "pending", "ci_run_conclusion": "success", "blocking_reason": "CI_BASE_STALE"},
])
def test_a_red_or_rebased_train_run_closes_the_train_and_the_first_member_goes_serial(
        tmp_path, observed):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_inspect", "core-repo"): observed})
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()

    summary = _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, observed["blocking_reason"])]
    assert summary["train"]["outcome"] == "closed"
    assert _train_calls(gate, "train_member_merge") == []
    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]
    assert _train_calls(gate, "train_build") == []


def test_main_moving_before_a_member_merge_closes_the_train(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_member_merge", "core-repo", 1): {
        "ok": False, "code": "TRAIN_BASE_MOVED",
        "detail": {"pull_index": 1, "origin_main_sha": "9" * 40}}})
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()

    summary = _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_BASE_MOVED")]
    assert not summary["authority_integrity_failed"]
    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]


def test_a_tree_mismatch_after_a_member_merge_stops_automation_and_keeps_the_train(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_member_merge", "core-repo", 1): {
        "ok": False, "code": "TRAIN_TREE_MISMATCH", "detail": {"pull_index": 1}}})
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()
    published = len(gate.statuses)

    summary = _train_pass(tmp_path, gate, store)

    assert summary["authority_integrity_failed"] is True
    assert summary["results"] == [{"pr": 1, "outcome": "retained",
                                   "reason": "TRAIN_TREE_MISMATCH", "train_pr": 900,
                                   "authority_integrity_failed": True}]
    assert summary["train"]["outcome"] == "integrity_failed"
    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1]
    assert _train_calls(gate, "train_close") == []
    assert _train_calls(gate, "refresh") == []
    assert len(gate.statuses) == published
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900


@pytest.mark.parametrize("mergeable", [None, False])
def test_mergeability_recomputed_after_a_member_keeps_the_train_for_the_next_pass(
        tmp_path, mergeable):
    """Forgejo answers `false` while it rechecks a PR after main moved."""
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_member_merge", "core-repo", 2): [
        {"ok": False, "code": "PR_MERGE_FAILED",
         "detail": {"pull_index": 2, "mergeable": mergeable}},
        None]})
    _train_pass(tmp_path, gate, store)
    first = _train_pass(tmp_path, gate, store)
    assert first["train"]["outcome"] == "merging"
    assert _train_calls(gate, "train_close") == []
    gate.by_repository["core-repo"] = [2, 3]  # #1 landed.
    gate.calls.clear()

    second = _train_pass(tmp_path, gate, store)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [2, 3]
    assert [row["pr"] for row in second["results"] if row["outcome"] == "merged"] == [2, 3]
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "merged #1, #2, #3")]


def test_a_train_run_that_never_starts_stalls_like_a_candidate_run(tmp_path):
    store = _queue_store(tmp_path)
    clock = [1000.0]
    gate = _train_gate({("train_inspect", "core-repo"): {
        "ci_state": "pending", "blocking_reason": "CI_RUN_QUEUED", "ci_run_conclusion": None}})
    _train_pass(tmp_path, gate, store, clock)
    # The run's identity first appears here, which starts the wait, as it
    # does for a candidate's own run.
    clock[0] += 60
    assert _train_pass(tmp_path, gate, store, clock)["train"]["outcome"] == "waiting"
    clock[0] += merge_open_prs.CI_START_WAIT_SECONDS - 1
    assert _train_pass(tmp_path, gate, store, clock)["train"]["outcome"] == "waiting"
    clock[0] += 2

    _train_pass(tmp_path, gate, store, clock)

    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "CI_STALLED")]


def test_the_train_pr_itself_is_never_a_queue_candidate(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeTrainApplication(
        {"core-repo": [5, 900]}, head_refs={"core-repo": {5: "task/x", 900: "train/bbbbbbbbbbbb-5"}},
        responses={("inspect", "core-repo", 5): _queue_inspect(pr_state="open")})

    summary = _train_pass(tmp_path, gate, store)

    assert summary["results"] == [{"pr": 5, "outcome": "merged"}]
    assert not any(len(call) > 2 and call[2] == 900 and call[0] != "train_close" for call in gate.calls)
    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "TRAIN_ORPHANED")]
    assert all(900 not in published for _repo, published, _before in gate.statuses)


def test_renovate_hold_on_train_leader_closes_the_train(tmp_path):
    """The hold frees the slot; the train it led must not stay open with
    nothing left to drive or close it."""
    store = _queue_store(tmp_path)
    gate = _train_gate()
    gate.head_refs = {"core-repo": {1: "renovate/x", 2: "task/y", 3: "task/z"}}
    gate.labels = {"core-repo": {1: ["renovate"], 2: [], 3: []}}
    train = {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": _TRAIN_BASE, "started": 1.0,
             "members": [{"pr": 1, "head": "1" * 40, "candidate": _CANDIDATES[0]},
                         {"pr": 2, "head": "2" * 40, "candidate": _CANDIDATES[1]}]}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": {
            "pr": 1, "head": "1" * 40, "base": _TRAIN_BASE, "run": [None, None],
            "wait_since": 1000.0, "started": False, "train": train}, "parked": {}}
        store.save(state)
    gate.labels["core-repo"][1] = ["major", "renovate"]

    _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900,
                                                 "RENOVATE_HOLD")]
    assert _train_calls(gate, "train_inspect") == []
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "renovate_major"
    assert row["active"]["pr"] == 2  # The freed slot goes to the next PR.


def test_a_built_train_counts_its_riders_toward_the_renovate_slot(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()

    summary = _train_pass(tmp_path, gate, store)

    assert summary["train"]["outcome"] in {"built", "waiting", "merging", "merged"}
    assert len(_train_calls(gate, "train_build")) == 1
    # The leader's selection counts one, each of the two riders one more.
    assert store.read()["repositories"]["core-repo"]["agent_streak"] == 3


def test_main_off_chain_during_train_ci_releases_without_waiting(tmp_path):
    """A hand merge out of order moves main off every candidate tree: no
    member can merge, so the slot is released before the CI deadline."""
    store = _queue_store(tmp_path)
    waiting = {"ci_state": "pending", "blocking_reason": "CI_RUN_IN_PROGRESS",
               "ci_run_conclusion": None}
    gate = _train_gate({("train_inspect", "core-repo"): [
        {**waiting, "main_on_chain": True}, {**waiting, "main_on_chain": False}]})
    _train_pass(tmp_path, gate, store)
    assert _train_pass(tmp_path, gate, store)["train"]["outcome"] == "waiting"
    gate.calls.clear()

    summary = _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_BASE_MOVED")]
    assert summary["train"]["outcome"] == "closed"
    assert _train_calls(gate, "train_member_merge") == []
    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]


def test_train_journal_separates_executor_and_external_merges(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    gate.by_repository["core-repo"] = [2, 3]  # #1 merged by hand.

    summary = _train_pass(tmp_path, gate, store)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [2, 3]
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "merged #2, #3 - left #1")]
    assert summary["train"]["outcome"] == "merged"


def test_a_train_whose_members_all_left_is_closed_not_merged(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    gate.by_repository["core-repo"] = []  # Every member merged or closed by hand.

    summary = _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_member_merge") == []
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "TRAIN_MEMBERS_LEFT: left #1, #2, #3")]
    assert summary["train"]["outcome"] == "closed"
    assert store.read()["repositories"]["core-repo"]["active"] is None


@pytest.mark.parametrize("train", [
    {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40, "started": 1.0,
     "members": [{"pr": 1, "head": "1" * 40, "candidate": "c" * 40}]},
    {"pr": 900, "branch": "train/aaaaaaaaaaaa-1", "base": "b" * 40, "started": 1.0,
     "members": [{"pr": 1, "head": "1" * 40, "candidate": "c" * 40},
                 {"pr": 2, "head": "2" * 40, "candidate": "d" * 40}]},
    {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40, "started": 1.0,
     "members": [{"pr": 2, "head": "2" * 40, "candidate": "c" * 40},
                 {"pr": 1, "head": "1" * 40, "candidate": "d" * 40}]},
    {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40, "started": 1.0,
     "members": [{"pr": 1, "head": "1" * 40, "candidate": "c" * 40},
                 {"pr": 2, "head": "2" * 40}]},
    {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40, "started": 1.0,
     "members": [{"pr": 1, "head": "1" * 40, "candidate": "c" * 40},
                 {"pr": 2, "head": "2" * 40, "candidate": "d" * 40}], "extra": True},
])
def test_a_malformed_train_row_is_corrupt_state_and_stops_the_pass(tmp_path, train):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    row = {"active": {"pr": 1, "head": "1" * 40, "base": "b" * 40, "run": [None, None],
                      "wait_since": 1.0, "started": False, "train": train}, "parked": {}}
    with store.locked() as state:
        state["repositories"]["core-repo"] = row
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)
    store.path.write_text(json.dumps({"version": 1, "cursor": 0,
                                      "repositories": {"core-repo": row}}), encoding="utf-8")
    store.path.chmod(0o600)
    gate = _train_gate()

    with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
        _train_pass(tmp_path, gate, store)
    assert not any(call[0] in {"train_member_merge", "refresh", "merge", "train_build"}
                   for call in gate.calls)


def test_a_well_formed_train_row_round_trips(tmp_path):
    store = _queue_store(tmp_path)
    train = {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40, "started": 1.0,
             "members": [{"pr": 1, "head": "1" * 40, "candidate": "c" * 40},
                         {"pr": 2, "head": "2" * 40, "candidate": "d" * 40}]}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": {
            "pr": 1, "head": "1" * 40, "base": "b" * 40, "run": [None, None],
            "wait_since": 1.0, "started": False, "train": train}, "parked": {}}
        store.save(state)
    assert store.read()["repositories"]["core-repo"]["active"]["train"] == train


@pytest.mark.parametrize("malformed", [[3], [1, 1], "1", [[1]], [True]])
def test_a_train_row_records_the_members_the_executor_merged(tmp_path, malformed):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    train = {"pr": 900, "branch": "train/bbbbbbbbbbbb-1", "base": "b" * 40, "started": 1.0,
             "members": [{"pr": 1, "head": "1" * 40, "candidate": "c" * 40},
                         {"pr": 2, "head": "2" * 40, "candidate": "d" * 40}], "merged": [2]}
    active = {"pr": 1, "head": "1" * 40, "base": "b" * 40, "run": [None, None],
              "wait_since": 1.0, "started": False, "train": train}
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": active, "parked": {}}
        store.save(state)
        assert store.read()["repositories"]["core-repo"]["active"]["train"] == train
        train["merged"] = malformed
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


# ------------------------------------------- a train advances around paid reviews
#
# a pass polled the train once, at its repository's turn, then spent
# minutes on another repository's review. A prefix that turned green meanwhile
# waited for the next pass.

_PREFIX_PENDING = {"ci_state": "pending", "blocking_reason": "CI_RUN_IN_PROGRESS",
                   "ci_run_conclusion": None}


def _docs_repo(tmp_path):
    return merge_open_prs.Repository("docs-repo", tmp_path / "docs-repo", "forgejo",
                                     "acme/docs-repo")


def _train_beside_a_review(tmp_path, queued=("core-repo", "docs-repo")):
    """core-repo holds a two-member train whose prefixes are pending CI, and
    docs-repo PR 7 is green but unreviewed.

    The train is built by an core-repo-only pass, which leaves the cursor at
    0, so the next two-repository pass visits core-repo first: its own turn
    sees the prefix pending before docs-repo's review starts.
    """
    store = _queue_store(tmp_path, queued)
    gate = _train_gate({("train_inspect", "core-repo"): _PREFIX_PENDING}, numbers=(1, 2))
    _train_pass(tmp_path, gate, store)
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900
    assert store.read()["cursor"] == 0
    gate.by_repository["docs-repo"] = [7]
    gate.responses[("inspect", "docs-repo", 7)] = [
        _queue_inspect(review="missing"), _queue_inspect()]
    gate.calls.clear()
    return store, gate


def _train_review_pass(tmp_path, gate, store):
    return merge_open_prs.run_automerge_pass(
        [_train_repository(tmp_path), _docs_repo(tmp_path)], gate, queue_store=store,
        now=lambda: 1000.0)


def _call_index(gate, *call):
    return next(index for index, seen in enumerate(gate.calls) if seen[:len(call)] == call)


def test_a_prefix_green_during_another_repositorys_review_merges_after_it(tmp_path):
    store, gate = _train_beside_a_review(tmp_path)
    original_review = gate.review

    def review(repository, pr, head, base, executor_run, retry_of=None):
        gate.responses.pop(("train_inspect", "core-repo"))  # Both prefixes turn green.
        return original_review(repository, pr, head, base, executor_run, retry_of)

    gate.review = review

    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert trains["results"] == [
        {"pr": pr, "outcome": "merged", "ci_basis": "train", "approval_basis": "exact",
         "train_pr": 900} for pr in (1, 2)]
    assert reviewed["results"] == [{"pr": 7, "outcome": "merged"}]
    # The train's own turn saw the prefix pending and merged nothing; the
    # members merged after the review returned and before the reviewed PR.
    review_at = _call_index(gate, "review", "docs-repo", 7)
    first_member_at = _call_index(gate, "train_member_merge", "core-repo", 1)
    assert _call_index(gate, "train_inspect", "core-repo") < review_at < first_member_at
    assert first_member_at < _call_index(gate, "train_member_merge", "core-repo", 2)
    assert _call_index(gate, "train_member_merge", "core-repo", 2) < _call_index(
        gate, "merge", "docs-repo", 7)
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "merged #1, #2")]
    assert _train_calls(gate, "train_build") == []
    assert _train_calls(gate, "refresh") == []
    assert store.read()["repositories"]["core-repo"]["active"] is None


def test_a_pending_prefix_is_not_merged_around_a_review(tmp_path):
    store, gate = _train_beside_a_review(tmp_path)

    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert reviewed["results"] == [{"pr": 7, "outcome": "merged"}]
    assert trains["results"] == []
    assert trains["train"]["outcome"] == "waiting"
    # Its own turn, then once before and once after the one paid review.
    review_at = _call_index(gate, "review", "docs-repo", 7)
    polls = [index for index, call in enumerate(gate.calls) if call[0] == "train_inspect"]
    assert len(polls) == 3 and polls[1] < review_at < polls[2]
    assert _train_calls(gate, "train_member_merge") == []
    assert _train_calls(gate, "train_close") == []
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900


def test_no_train_is_advanced_around_a_review_when_none_is_active(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeTrainApplication({"core-repo": [1], "docs-repo": [7]}, responses={
        ("inspect", "core-repo", 1): [_queue_inspect(review="missing"), _queue_inspect()],
        ("inspect", "docs-repo", 7): [_queue_inspect(review="missing"), _queue_inspect()]})

    summaries = _train_review_pass(tmp_path, gate, store)

    assert [summary["results"] for summary in summaries] == [
        [{"pr": 1, "outcome": "merged"}], [{"pr": 7, "outcome": "merged"}]]
    assert [call[1:3] for call in gate.calls if call[0] == "review"] == [
        ("core-repo", 1), ("docs-repo", 7)]
    assert not any(call[0].startswith("train_") for call in gate.calls)


@pytest.mark.parametrize("green_at", ["inspect", "review"])
def test_a_train_integrity_stop_around_a_review_ends_every_later_write(tmp_path, green_at):
    """Turning green at the reviewed PR's inspection stops before its review
    is dispatched; turning green during the review stops before its merge."""
    store, gate = _train_beside_a_review(tmp_path)
    gate.responses[("train_member_merge", "core-repo", 1)] = {
        "ok": False, "code": "TRAIN_TREE_MISMATCH", "detail": {"pull_index": 1}}
    original = getattr(gate, green_at)

    def turn_green(repository, *args, **kwargs):
        if repository == "docs-repo":
            gate.responses.pop(("train_inspect", "core-repo"), None)
        return original(repository, *args, **kwargs)

    setattr(gate, green_at, turn_green)
    published = len(gate.statuses)

    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert trains["authority_integrity_failed"] is True
    assert trains["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "TRAIN_TREE_MISMATCH", "train_pr": 900,
                                  "authority_integrity_failed": True}]
    reviews = [call[1:3] for call in gate.calls if call[0] == "review"]
    assert reviews == ([] if green_at == "inspect" else [("docs-repo", 7)])
    assert not any(call[0] in {"merge", "refresh", "train_close"} for call in gate.calls)
    assert reviewed["results"] == []
    assert store.read()["repositories"]["docs-repo"]["active"]["pr"] == 7
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900
    # Only the review's own `reviewing` status may precede the stop.
    assert len(gate.statuses) == published + (green_at == "review")


@pytest.mark.parametrize("green_at", ["inspect", "review"])
def test_a_train_integrity_stop_around_a_review_ahead_ends_every_later_review(tmp_path, green_at):
    """The review-ahead stage has its own handler for the stop: the train
    turns green at the ahead candidate's inspection (before its review) or
    during its review, and the member merge fails its integrity check. The
    stop is recorded and no repository's later review ahead is dispatched."""
    store, gate = _train_beside_a_review(
        tmp_path, ("core-repo", "docs-repo", "third"))
    third = merge_open_prs.Repository("third", tmp_path / "third", "forgejo", "acme/third")
    gate.by_repository["docs-repo"] = [7, 8]
    gate.by_repository["third"] = [9, 10]
    gate.responses.update({
        ("inspect", "docs-repo", 7): _in_progress(),  # Active and waiting on CI,
        ("inspect", "docs-repo", 8): _queue_inspect(review="not_approved"),  # so 8 is ahead.
        ("inspect", "third", 9): _in_progress(),
        ("inspect", "third", 10): _queue_inspect(review="not_approved"),
    })
    gate.responses[("train_member_merge", "core-repo", 1)] = {
        "ok": False, "code": "TRAIN_TREE_MISMATCH", "detail": {"pull_index": 1}}
    original = getattr(gate, green_at)

    def turn_green(repository, pr, *args, **kwargs):
        if (repository, pr) == ("docs-repo", 8):  # Only the ahead candidate.
            gate.responses.pop(("train_inspect", "core-repo"), None)
        return original(repository, pr, *args, **kwargs)

    setattr(gate, green_at, turn_green)

    trains, reviewed, untouched = merge_open_prs.run_automerge_pass(
        [_train_repository(tmp_path), _docs_repo(tmp_path), third], gate,
        queue_store=store, now=lambda: 1000.0, max_reviews=3, max_reviews_per_repo=1)

    assert trains["authority_integrity_failed"] is True
    assert trains["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "TRAIN_TREE_MISMATCH", "train_pr": 900,
                                  "authority_integrity_failed": True}]
    # Turning green at the inspection stops before the ahead review is
    # dispatched; during it, before the next repository's review ahead.
    reviews = [call[1:3] for call in gate.calls if call[0] == "review"]
    assert reviews == ([] if green_at == "inspect" else [("docs-repo", 8)])
    assert ("review", "third", 10) not in gate.calls  # Selection reads it; the stop bars its review.
    assert not any(call[0] in {"merge", "refresh", "train_close"} for call in gate.calls)
    # The stop discards the interrupted review ahead's result.
    assert reviewed["results"] == [
        {"pr": 7, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert untouched["results"] == [
        {"pr": 9, "outcome": "retained", "reason": "CI_RUN_IN_PROGRESS"}]
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900


@pytest.mark.parametrize("when", ["turn", "inspect", "review", "cleanup"])
@pytest.mark.parametrize("code,raises,stops", [
    ("EXECUTOR_HOST_REQUIRED", False, True),
    ("INTERNAL_ERROR", False, True),
    ("INTERNAL_ERROR", True, True),
    ("GATE_LOCK_BUSY", False, False),
])
def test_a_train_close_refusal_stops_later_writes_but_busy_cleanup_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str,
    code: str, raises: bool, stops: bool,
) -> None:
    store, gate = _train_beside_a_review(tmp_path)
    before = store.read()["repositories"]["core-repo"]["active"]["train"]
    failed_ci = {"ci_state": "red", "ci_run_conclusion": "failure",
                 "blocking_reason": "CI_RUN_FAILED"}
    if when == "cleanup":
        with store.locked() as state:
            state["repositories"]["core-repo"]["active"]["train"]["cleanup"] = {
                "reason": "CI_RUN_FAILED", "outcome": "closed"}
            store.save(state)
    elif when == "turn":
        gate.responses[("train_inspect", "core-repo")] = failed_ci
    else:
        original = getattr(gate, when)

        def turn_red(repository: str, *args: object, **kwargs: object) -> dict:
            if repository == "docs-repo":
                gate.responses[("train_inspect", "core-repo")] = failed_ci
            return original(repository, *args, **kwargs)

        monkeypatch.setattr(gate, when, turn_red)
    close = gate.train_close
    attempts = []
    error = merge_open_prs.GateError(code) if raises else None

    def refuse_close(repository: str, train_pr: int, reason: str) -> dict:
        gate.calls.append(("train_close", repository, train_pr, reason))
        attempts.append((len(gate.calls), len(gate.statuses)))
        if error is not None:
            raise error
        return {"ok": False, "code": code}

    monkeypatch.setattr(gate, "train_close", refuse_close)
    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert len(attempts) == 1
    call_at, published = attempts[0]
    retained = store.read()["repositories"]["core-repo"]["active"]["train"]
    assert retained["members"] == before["members"]
    assert retained["prefixes"] == before["prefixes"]
    assert retained["cleanup"] == {"reason": "CI_RUN_FAILED", "outcome": "closed"}
    assert trains["train"]["close_failed"] == code
    assert trains["authority_integrity_failed"] is stops
    if stops:
        assert trains["results"][-1] == {
            "pr": 900, "outcome": "retained", "reason": code,
            "authority_integrity_failed": True}
        assert not any(call[0] in {"review", "merge", "refresh", "train_build",
                                  "train_member_merge", "train_close"}
                       for call in gate.calls[call_at:])
        assert reviewed["results"] == []
        assert len(gate.statuses) == published
    else:
        assert reviewed["results"] == [{"pr": 7, "outcome": "merged"}]
    # A fresh pass can finish the retained cleanup after the failure clears.
    monkeypatch.setattr(gate, "train_close", close)
    _train_pass(tmp_path, gate, store)
    assert "train" not in store.read()["repositories"]["core-repo"]["active"]


@pytest.mark.parametrize("when", ["turn", "inspect", "review"])
@pytest.mark.parametrize("code", ["INTERNAL_ERROR", "GATE_LOCK_DIR_UNSAFE"])
def test_a_train_member_exception_stops_later_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str, code: str,
) -> None:
    store, gate = _train_beside_a_review(tmp_path)
    before = store.read()["repositories"]["core-repo"]["active"]["train"]
    if when == "turn":
        gate.responses.pop(("train_inspect", "core-repo"))
    else:
        original = getattr(gate, when)

        def turn_green(repository: str, *args: object, **kwargs: object) -> dict:
            if repository == "docs-repo":
                gate.responses.pop(("train_inspect", "core-repo"), None)
            return original(repository, *args, **kwargs)

        monkeypatch.setattr(gate, when, turn_green)
    attempts = []
    error = merge_open_prs.GateError(code)

    def refuse_merge(repository: str, train_pr: int, member_pr: int, head: str) -> dict:
        gate.calls.append(("train_member_merge", repository, member_pr, train_pr, head))
        attempts.append((len(gate.calls), len(gate.statuses)))
        raise error

    monkeypatch.setattr(gate, "train_member_merge", refuse_merge)
    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert len(attempts) == 1
    call_at, published = attempts[0]
    assert trains["authority_integrity_failed"] is True
    assert trains["results"] == [{"pr": 1, "outcome": "retained", "reason": code,
                                   "train_pr": 900, "authority_integrity_failed": True}]
    assert trains["train"]["outcome"] == "integrity_failed"
    assert trains["train"]["reason"] == code
    assert not any(call[0] in {"review", "merge", "refresh", "train_build",
                              "train_member_merge", "train_close"}
                   for call in gate.calls[call_at:])
    assert reviewed["results"] == []
    assert len(gate.statuses) == published
    assert store.read()["repositories"]["core-repo"]["active"]["train"] == before


def test_a_retained_train_cleanup_is_not_retried_around_a_review(tmp_path):
    store, gate = _train_beside_a_review(tmp_path)
    gate.responses[("train_inspect", "core-repo")] = {
        "ci_state": "red", "ci_run_conclusion": "failure", "blocking_reason": "CI_RUN_FAILED"}
    gate.responses[("train_close", "core-repo")] = {"ok": False, "code": "TRAIN_CLOSE_FAILED"}
    _train_pass(tmp_path, gate, store)
    assert "cleanup" in store.read()["repositories"]["core-repo"]["active"]["train"]
    gate.calls.clear()

    _train_review_pass(tmp_path, gate, store)

    # The train's own turn retries the close once; the review adds none.
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "CI_RUN_FAILED")]
    assert ("review", "docs-repo", 7) in [call[:3] for call in gate.calls]


# ------------------------------------- a train advances while a paid review runs
#
# the hooks around a review left a prefix that turned green during it
# idle until the review ended, 4-5 minutes, while main moved on. The review now
# runs on a worker thread and the pass re-checks trains in every other
# repository once per `REVIEW_TRAIN_POLL_SECONDS`.

_APPROVED_REVIEW = {"ok": True, "dispatched": True, "verdict": "APPROVED"}


@pytest.fixture
def review_polls(monkeypatch):
    """Shrink the in-review slice and count the slices a review outlasted.

    `reached` opens once three slices ended with the review still running,
    so a review that waits on it has provably been polled around at least
    twice. Waiting on it replaces any sleep in the fakes.
    """
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
    yield polls
    leftover = [thread for thread in threading.enumerate()
                if thread.name.startswith("automerge-review")]
    assert leftover == [], "a review worker outlived its `review()` call"


class _BlockingReview:
    """A Gate application whose one review blocks until `release` opens."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.release = threading.Event()
        self.returned = threading.Event()
        self.threads = []

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        self.threads.append(threading.current_thread())
        try:
            if not self.release.wait(timeout=5):
                raise AssertionError("the review was never released")
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome
        finally:
            self.returned.set()


def _review_budget(application, **kwargs):
    return merge_open_prs._ReviewBudget(application, 3, 1, **kwargs)


def _budget_review(budget, repository="core-repo", pr=1):
    return budget.review(repository, pr, "a" * 40, "b" * 40, "run")


def test_a_prefix_green_during_another_repositorys_review_merges_while_it_runs(
        tmp_path, review_polls):
    store, gate = _train_beside_a_review(tmp_path)
    caller = threading.current_thread()
    threads = {}

    def on_thread(name, operation):
        def recorded(*args, **kwargs):
            threads.setdefault(name, set()).add(threading.current_thread())
            return operation(*args, **kwargs)
        return recorded

    for name in ("train_inspect", "train_member_merge"):
        setattr(gate, name, on_thread(name, getattr(gate, name)))
    store.save = on_thread("save", store.save)
    closed = threading.Event()
    original_close = gate.train_close

    def train_close(*args):
        threads.setdefault("train_close", set()).add(threading.current_thread())
        result = original_close(*args)
        closed.set()
        return result

    gate.train_close = train_close
    original_review = gate.review

    def review(repository, pr, head, base, executor_run, retry_of=None):
        threads.setdefault("review", set()).add(threading.current_thread())
        reviewed = original_review(repository, pr, head, base, executor_run, retry_of)
        gate.responses.pop(("train_inspect", "core-repo"))  # Both prefixes turn green.
        gate.calls.append(("review_returned", closed.wait(timeout=5)))
        return reviewed

    gate.review = review

    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert trains["results"] == [
        {"pr": pr, "outcome": "merged", "ci_basis": "train", "approval_basis": "exact",
         "train_pr": 900} for pr in (1, 2)]
    assert reviewed["results"] == [{"pr": 7, "outcome": "merged"}]
    # The whole train merged and closed while the review was still running.
    returned_at = _call_index(gate, "review_returned")
    assert gate.calls[returned_at] == ("review_returned", True)
    assert (_call_index(gate, "review", "docs-repo", 7)
            < _call_index(gate, "train_member_merge", "core-repo", 1)
            < _call_index(gate, "train_member_merge", "core-repo", 2)
            < _call_index(gate, "train_close", "core-repo")
            < returned_at < _call_index(gate, "merge", "docs-repo", 7))
    assert store.read()["repositories"]["core-repo"]["active"] is None
    # Only the pass's own thread advanced the train or wrote state.
    assert {name: seen for name, seen in threads.items() if name != "review"} == {
        name: {caller} for name in ("train_inspect", "train_member_merge", "train_close", "save")}
    (worker,) = threads["review"]
    assert worker is not caller


def test_a_train_waits_for_a_review_in_its_own_repository(tmp_path, review_polls):
    """A member merge would move the main the review is bound to, and the
    gate's fetches would share the review's checkout: that train waits."""
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_inspect", "core-repo"): _PREFIX_PENDING}, numbers=(1, 2))
    _train_pass(tmp_path, gate, store)
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900
    gate.by_repository["core-repo"] = [1, 2, 3]
    gate.responses[("inspect", "core-repo", 3)] = _queue_inspect(head="c", review="missing")
    gate.calls.clear()
    original_review = gate.review

    def review(repository, pr, head, base, executor_run, retry_of=None):
        reviewed = original_review(repository, pr, head, base, executor_run, retry_of)
        gate.responses.pop(("train_inspect", "core-repo"))  # Both prefixes turn green.
        gate.calls.append(("review_polled", review_polls.reached.wait(timeout=5)))
        return reviewed

    gate.review = review

    summary = merge_open_prs.run_automerge_pass(
        [_train_repository(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    review_at = _call_index(gate, "review", "core-repo", 3)
    returned_at = _call_index(gate, "review_polled")
    assert gate.calls[returned_at] == ("review_polled", True)
    assert not any(call[0].startswith("train_") for call in gate.calls[review_at:returned_at])
    # The hook after the review merges what turned green during it.
    assert returned_at < _call_index(gate, "train_member_merge", "core-repo", 1)
    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1, 2]
    assert {"pr": 3, "outcome": "reviewed_ahead"} in summary["results"]


def test_in_review_polling_without_a_train_has_no_observable_effect(tmp_path, review_polls):
    def run(directory, polled):
        directory.mkdir()
        store = _queue_store(directory)
        saves = []
        original_save = store.save

        def save(state):
            saves.append(len(saves))
            return original_save(state)

        store.save = save
        gate = FakeTrainApplication({"core-repo": [1], "docs-repo": [7]}, responses={
            ("inspect", "core-repo", 1): [_queue_inspect(review="missing"), _queue_inspect()],
            ("inspect", "docs-repo", 7): [_queue_inspect(review="missing"),
                                               _queue_inspect()]})
        waited = []
        if polled:
            original_review = gate.review

            def review(*args, **kwargs):
                review_polls.count = 0
                review_polls.reached.clear()
                reviewed = original_review(*args, **kwargs)
                waited.append(review_polls.reached.wait(timeout=5))
                return reviewed

            gate.review = review
        summaries = _train_review_pass(directory, gate, store)
        return (summaries, gate.calls, gate.statuses, len(saves),
                store.read()["repositories"]), waited

    unpolled, _ = run(tmp_path / "unpolled", polled=False)
    polled, waited = run(tmp_path / "polled", polled=True)

    assert waited == [True, True], "each review must outlast several poll slices"
    assert polled == unpolled
    assert not any(call[0].startswith("train_") for call in polled[1])


@pytest.mark.parametrize("stop", [
    lambda: merge_open_prs._TrainStop("core-repo"),
    lambda: merge_open_prs._DeployYield("DEPLOY_WAITING", "deployment holds the outer lock", []),
    lambda: RuntimeError("advance failed"),
], ids=["train-stop", "deploy-yield", "unexpected"])
@pytest.mark.parametrize(("outcome", "spent"), [
    (_APPROVED_REVIEW, 1),
    ({"ok": False, "code": "REVIEW_ATTEMPT_BUSY"}, 0),
    (OSError("review transport failed"), 1),
], ids=["dispatched", "not-dispatched", "review-raised"])
def test_a_stop_during_a_review_waits_for_it_and_spends_its_slot(
        review_polls, caplog, stop, outcome, spent):
    application = _BlockingReview(outcome)
    budget = _review_budget(application)
    raised = stop()
    advanced = []

    def advance_trains(reviewing=None):
        advanced.append(reviewing)
        if reviewing is not None:
            application.release.set()
            raise raised

    budget.advance_trains = advance_trains

    with caplog.at_level(logging.WARNING, logger=merge_open_prs.LOGGER.name):
        with pytest.raises(type(raised)) as excinfo:
            _budget_review(budget)

    assert excinfo.value is raised
    assert application.returned.is_set()
    assert budget.review_count == spent
    assert budget.repository_counts == ({"core-repo": 1} if spent else {})
    # Before the review and during it; nothing runs after a stop.
    assert advanced == [None, "core-repo"]
    assert "core-repo#1 review ended after a stop during it" in caplog.text


@pytest.mark.parametrize("intent", ["lock", "marker"])
def test_a_deploy_yield_met_during_a_review_waits_for_it_and_keeps_the_train(
        tmp_path, monkeypatch, review_polls, intent):
    """The real checkpoint, reached through the wrapped member merge."""
    store, gate = _train_beside_a_review(tmp_path)
    deploying = threading.Event()
    yielded = threading.Event()
    marker = tmp_path / "deploy-marker.json"
    monkeypatch.setattr(merge_open_prs, "DEPLOY_MARKER_PATH", marker)
    original_intent = merge_open_prs._deploy_in_progress

    def deploy_intent():
        observed = original_intent()
        if observed is not None:
            yielded.set()
        return observed

    def probe_deploy_lock():
        if not deploying.is_set() or intent == "marker":
            return "free", ""
        yielded.set()
        return "held", "deployment holds the outer lock"

    monkeypatch.setattr(merge_open_prs, "_deploy_in_progress", deploy_intent)
    monkeypatch.setattr(merge_open_prs, "probe_deploy_lock", probe_deploy_lock)
    original_review = gate.review
    returned = threading.Event()

    def review(repository, pr, head, base, executor_run, retry_of=None):
        reviewed = original_review(repository, pr, head, base, executor_run, retry_of)
        gate.responses.pop(("train_inspect", "core-repo"))  # Both prefixes turn green.
        if intent == "marker":
            marker.write_text(json.dumps({
                "schema": "deploy-marker.v1", "state": "running", "origin": "remote-origin",
                "plan_id": "mid-review", "claimed_at": datetime.now(timezone.utc).isoformat(),
            }))
        deploying.set()
        assert yielded.wait(timeout=5), "the pass must probe the deploy lock mid-review"
        returned.set()
        return reviewed

    gate.review = review

    summaries = _train_review_pass(tmp_path, gate, store)

    assert returned.is_set()
    assert all(summary["hold"]["reason"] == "DEPLOY_WAITING" for summary in summaries)
    assert ("review", "docs-repo", 7) in [call[:3] for call in gate.calls]
    assert not any(call[0] in {"train_member_merge", "train_close", "merge"} for call in gate.calls)
    rows = store.read()["repositories"]
    assert rows["core-repo"]["active"]["train"]["pr"] == 900
    assert "merged" not in rows["core-repo"]["active"]["train"]
    assert rows["docs-repo"]["active"]["pr"] == 7


def test_a_review_that_raises_while_polled_propagates_and_spends_its_slot(review_polls):
    failure = OSError("review transport failed")
    application = _BlockingReview(failure)
    budget = _review_budget(application)
    advanced = []
    checkpoints = []

    def advance_trains(reviewing=None):
        advanced.append(reviewing)
        if advanced.count("core-repo") == 2:
            application.release.set()

    budget.advance_trains = advance_trains
    budget.checkpoint = lambda: checkpoints.append(len(checkpoints))

    with pytest.raises(OSError) as excinfo:
        _budget_review(budget)

    assert excinfo.value is failure
    assert budget.review_count == 1 and budget.repository_counts == {"core-repo": 1}
    # Polled during the review; neither hook runs after a raised review.
    assert advanced[0] is None and len(advanced) >= 3
    assert set(advanced[1:]) == {"core-repo"}
    # One advance per slice that ended with the review still running.
    assert advanced.count("core-repo") == review_polls.count
    assert len(checkpoints) == 2
    (worker,) = application.threads
    assert worker is not threading.current_thread()


def test_in_review_polling_leaves_the_pass_deadline_to_the_review_start(review_polls):
    """The deadline gates only a review's start. Time the running review
    spends past the reserve neither cuts it short nor skips the hook after
    it; it stops the next review from starting."""
    clock = [0.0]
    application = _BlockingReview(_APPROVED_REVIEW)
    budget = merge_open_prs._ReviewBudget(application, 3, None, deadline=1450,
                                          now=lambda: clock[0])
    advanced = []

    def advance_trains(reviewing=None):
        advanced.append(reviewing)
        if reviewing is not None:
            clock[0] = 1400.0  # 50 s left: under one review's hard bound.
            application.release.set()

    budget.advance_trains = advance_trains

    assert _budget_review(budget) == _APPROVED_REVIEW
    assert budget.review_count == 1
    assert advanced[0] is None and advanced[-1] is None and "core-repo" in advanced
    assert _budget_review(budget, "docs-repo", 2) == {"ok": False, "code": "PASS_DEADLINE"}
    assert len(application.threads) == 1


# ------------------------------- lookahead reviews run beside a paid review
#
# with `--review-workers N` the pass reviews up to N-1 later queued
# PRs that lack only an approval while one review runs, so a train or the
# PR's own turn finds the approval ready. The pass never acts on those
# results, and it supervises every review it started until each one ends.

_PUBLISHED = {"ok": True, "code": "REVIEW_PUBLISHED", "dispatched": True,
              "verdict": "ACCEPT_STATIC_CHANGE"}


class _OverlappingReviews(FakeGateApplication):
    """Reviews that overlap with the first one the pass starts.

    That first review returns only once `expected` other reviews started
    while it ran and three in-review poll slices passed, so the pass had
    later chances to start more. Each review started meanwhile waits for it.
    """

    def __init__(self, candidates, *, polls, expected, **kwargs):
        super().__init__(candidates, **kwargs)
        self.polls = polls
        self.expected = expected
        self.changed = threading.Condition()
        self.primary = None
        self.primary_returned = threading.Event()
        self.started = []  # (repository, pr, whether it started beside the first review)
        self.running = 0
        self.peak = 0

    def lookaheads(self):
        return sorted((repository, pr) for repository, pr, beside in self.started if beside)

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        with self.changed:
            first = self.primary is None
            if first:
                self.primary = (repository, pr)
            beside = not first and not self.primary_returned.is_set()
            self.started.append((repository, pr, beside))
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.changed.notify_all()
        try:
            if first:
                with self.changed:
                    overlapped = self.changed.wait_for(
                        lambda: sum(entry[2] for entry in self.started) >= self.expected,
                        timeout=5)
                self.calls.append(("overlapped", overlapped))
                self.calls.append(("polled", self.polls.reached.wait(timeout=5)))
            elif beside and not self.primary_returned.wait(timeout=5):
                raise AssertionError("the first review never returned")
            return super().review(repository, pr, head, base, executor_run, retry_of)
        finally:
            with self.changed:
                self.running -= 1
                if first:
                    self.primary_returned.set()
                self.changed.notify_all()


def _overlapping_gate(polls, expected, numbers=(1, 2, 3), **kwargs):
    """core-repo PR 1 is green and unreviewed; every later PR lacks only its approval."""
    gate = _OverlappingReviews({"core-repo": list(numbers)}, polls=polls, expected=expected,
                               **kwargs)
    gate.responses.update({
        ("inspect", "core-repo", 1): [_queue_inspect(review="missing"), _queue_inspect()],
        **{("inspect", "core-repo", pr): _queue_inspect(head=str(pr), review="not_approved")
           for pr in numbers[1:]},
        **{("review", "core-repo", pr): _PUBLISHED for pr in numbers},
    })
    return gate


def test_review_workers_review_later_prs_of_the_same_repository_beside_the_active_one(
        tmp_path, review_polls, caplog):
    gate = _overlapping_gate(review_polls, expected=2)

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        summary = merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path),
            max_reviews=6, max_reviews_per_repo=3, review_workers=3, now=lambda: 1000.0)[0]

    assert ("overlapped", True) in gate.calls
    assert gate.peak == 3
    assert gate.lookaheads() == [("core-repo", 2), ("core-repo", 3)]
    reviews = {call[2]: call[3:] for call in gate.calls if call[0] == "review"}
    for pr in (2, 3):
        assert reviews[pr] == (str(pr) * 40, "b" * 40,
                               f"{merge_open_prs.DEFAULT_EXECUTOR_RUN_PREFIX}:core-repo:{pr}",
                               None)
        assert f"automerge: lookahead core-repo#{pr} REVIEW_PUBLISHED ACCEPT_STATIC_CHANGE" \
            in caplog.text
        # Each published its own `reviewing` status before it started.
        assert any("reason=reviewing" in published.get(pr, "")
                   for _repository, published, _before in gate.statuses)
    # The approvals merge nothing in this pass: the active PR merged alone.
    # Each lookahead is recorded as `_review_ahead` records its own.
    assert [call for call in gate.calls if call[0] in {"merge", "refresh"}] == [
        ("merge", "core-repo", 1, "a" * 40, "b" * 40)]
    assert sorted(summary["results"], key=lambda record: record["pr"]) == [
        {"pr": 1, "outcome": "merged"}, {"pr": 2, "outcome": "reviewed_ahead"},
        {"pr": 3, "outcome": "reviewed_ahead"}]
    final = gate.statuses[-1][1]
    assert final[2] == "queue=active reason=waiting_review owner=queue pos=1"
    assert final[3] == "queue=eligible reason=- owner=queue pos=2"


def test_an_active_review_that_answers_at_once_waits_for_no_lookahead(tmp_path):
    """A clean-refresh reuse dispatches nothing and returns at
    once; the merge must not wait for a paid lookahead review."""
    gate = FakeGateApplication({"core-repo": [1, 2, 3]}, responses={
        ("inspect", "core-repo", 1): [_queue_inspect(review="not_approved"), _queue_inspect()],
        **{("inspect", "core-repo", pr): _queue_inspect(head=str(pr), review="not_approved")
           for pr in (2, 3)},
        ("review", "core-repo", 1): {"ok": True, "code": "REVIEW_APPROVAL_REUSED",
                                     "verdict": "ACCEPT_STATIC_CHANGE"}})

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path),
        max_reviews=6, max_reviews_per_repo=3, review_workers=3, now=lambda: 1000.0)

    # PRs 2 and 3 are eligible lookaheads (see the test above); no review of
    # either started. Selection reads them to rank, which is no review.
    merged = _call_index(gate, "merge", "core-repo", 1)
    assert [call[:3] for call in gate.calls[:merged] if call[0] == "review"] == [
        ("review", "core-repo", 1)]
    assert [call[:3] for call in gate.calls[:merged]
            if call[0] in {"inspect", "review"} and call[2] == 1] == [
        ("inspect", "core-repo", 1), ("review", "core-repo", 1), ("inspect", "core-repo", 1)]


def test_one_review_worker_starts_no_review_beside_the_active_one(tmp_path, review_polls):
    gate = _overlapping_gate(review_polls, expected=0)
    store = _queue_store(tmp_path)

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store,
        max_reviews=6, max_reviews_per_repo=3, now=lambda: 1000.0)

    assert ("polled", True) in gate.calls
    assert gate.peak == 1
    assert gate.lookaheads() == []
    # Selection reads the later PRs to rank them; none is reviewed
    # while PR 1 is, nor before it merged.
    merged = _call_index(gate, "merge", "core-repo", 1)
    assert [call[2] for call in gate.calls[:merged] if call[0] == "review"] == [1]


def _seat_active(store, pr):
    """PR `pr` already holds the repository's active slot (a pass's selection is skipped)."""
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": {
            "pr": pr, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
            "wait_since": 1000.0, "started": True}, "parked": {}}
        store.save(state)


def _seed_parked(store, pr):
    with store.locked() as state:
        state["repositories"]["core-repo"] = {"active": None, "parked": {str(pr): {
            "reason": "ci_failed", "head": str(pr) * 40, "base": "b" * 40,
            "run": [11, 1], "review": [None, "none"]}}}
        store.save(state)


@pytest.mark.parametrize("excluded", [
    "renovate", "parked", "held", "changes_requested", "approved", "delegated_approval",
    "red_ci", "unmergeable", "queue_hold", "base_stale", "claim_error",
])
def test_a_lookahead_never_reviews_an_excluded_pr(tmp_path, review_polls, excluded):
    """PR 2 is excluded; PR 3 after it is eligible and proves the scan went past PR 2."""
    kwargs = {}
    if excluded == "renovate":
        kwargs["labels"] = {"core-repo": {1: [], 2: ["renovate"], 3: []}}
    elif excluded == "held":
        kwargs["labels"] = {"core-repo": {1: [], 2: [merge_open_prs.HOLD_LABEL], 3: []}}
    gate = _overlapping_gate(review_polls, expected=1, **kwargs)
    store = _queue_store(tmp_path)
    if excluded == "parked":
        _seed_parked(store, 2)
    if excluded in {"approved", "delegated_approval"}:
        _seat_active(store, 1)  # An approved PR 2 would outrank PR 1 in selection.
    inspected = {
        # Still red on the parked run: a green read would release the park.
        "parked": _queue_inspect(head="2", ci="red", review="not_approved",
                                 conclusion="failure", blocking_reason="CI_RUN_FAILED"),
        "changes_requested": _queue_inspect(head="2", review="changes_requested"),
        "approved": _queue_inspect(head="2", review="approved"),
        "delegated_approval": _queue_inspect(head="2", review="not_approved",
                                             latest_delegated_review_state="APPROVED"),
        "red_ci": _queue_inspect(head="2", ci="red", review="not_approved",
                                 conclusion="failure", blocking_reason="CI_RUN_FAILED"),
        "unmergeable": _queue_inspect(head="2", review="not_approved", mergeable=False),
        "queue_hold": _queue_inspect(head="2", review="not_approved", queue_hold=True),
        "base_stale": _queue_inspect(head="2", review="not_approved",
                                     blocking_reason="PR_BASE_STALE"),
        "claim_error": _queue_inspect(head="2", review="not_approved",
                                      claim_error=sorted(merge_open_prs.CLAIM_ERROR_CODES)[0]),
    }.get(excluded)
    if inspected is not None:
        gate.responses[("inspect", "core-repo", 2)] = inspected

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, review_workers=3,
        now=lambda: 1000.0)

    assert ("overlapped", True) in gate.calls and ("polled", True) in gate.calls
    assert gate.lookaheads() == [("core-repo", 3)]


def test_a_lookahead_reviews_a_green_run_on_a_moved_base_over_its_recorded_base(
        tmp_path, review_polls):
    gate = _overlapping_gate(review_polls, expected=1, numbers=(1, 2))
    gate.responses[("inspect", "core-repo", 2)] = _stale_base(
        head_sha="2" * 40, review_state="not_approved", recorded_base_sha="e" * 40)

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), review_workers=2,
        now=lambda: 1000.0)

    assert gate.lookaheads() == [("core-repo", 2)]
    assert ("review", "core-repo", 2, "2" * 40, "e" * 40) in [call[:5] for call in gate.calls]


def _narrowed_accepted(**overrides):
    """An unapproved gate-C1-class head on the current base.

    The gate reads it CI_PROFILE_INSUFFICIENT; `ci_review_accepted` says the
    snapshot review accepts its narrowed run all the same. The forge's
    fork point is the current tip, so the head contains it."""
    return _queue_inspect(
        head="2", ci="unavailable", review="not_approved",
        blocking_reason="CI_PROFILE_INSUFFICIENT", ci_reason="CI_PROFILE_INSUFFICIENT",
        merge_candidate=False,
        **{"ci_review_accepted": True, "recorded_base_sha": "b" * 40, **overrides})


def test_a_lookahead_reviews_a_narrowed_c1_class_head_the_gate_accepts(tmp_path, review_polls):
    """Its review needs no full run first, and goes over the current base."""
    gate = _overlapping_gate(review_polls, expected=1, numbers=(1, 2))
    gate.responses[("inspect", "core-repo", 2)] = _narrowed_accepted()

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), review_workers=2,
        now=lambda: 1000.0)

    assert gate.lookaheads() == [("core-repo", 2)]
    assert ("review", "core-repo", 2, "2" * 40, "b" * 40) in [call[:5] for call in gate.calls]


@pytest.mark.parametrize("refused", [{"ci_review_accepted": False}, {"ci_review_accepted": None}],
                         ids=["the_gate_refuses_the_run", "a_gate_that_sends_no_answer"])
def test_a_lookahead_skips_a_narrowed_c1_class_head_the_gate_does_not_accept(
        tmp_path, review_polls, refused):
    gate = _overlapping_gate(review_polls, expected=0, numbers=(1, 2))
    gate.responses[("inspect", "core-repo", 2)] = _narrowed_accepted(**refused)

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), review_workers=2,
        now=lambda: 1000.0)

    assert ("polled", True) in gate.calls
    assert gate.lookaheads() == []


def test_a_lookahead_reviews_a_narrowed_head_over_its_recorded_base(
        tmp_path, review_polls):
    """The fork-point guard holds: review the forge's fork point, not a current
    tip the head does not contain. The gate has accepted this pair's CI."""
    gate = _overlapping_gate(review_polls, expected=1, numbers=(1, 2))
    gate.responses[("inspect", "core-repo", 2)] = _narrowed_accepted(recorded_base_sha="d" * 40)

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), review_workers=2,
        now=lambda: 1000.0)

    assert gate.lookaheads() == [("core-repo", 2)]
    assert ("review", "core-repo", 2, "2" * 40, "d" * 40) in [call[:5] for call in gate.calls]


def test_a_repository_at_its_cap_lends_the_lookahead_to_the_next_repository(
        tmp_path, review_polls):
    gate = _overlapping_gate(review_polls, expected=1)
    gate.by_repository["docs-repo"] = [7]
    gate.responses[("inspect", "docs-repo", 7)] = _queue_inspect(
        head="7", review="not_approved")

    merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path), _docs_repo(tmp_path)], gate,
        queue_store=_queue_store(tmp_path), max_reviews=6, max_reviews_per_repo=2,
        review_workers=3, now=lambda: 1000.0)

    # core-repo: its active review and one lookahead fill its cap of two.
    assert gate.lookaheads() == [("core-repo", 2), ("docs-repo", 7)]


def test_a_lookahead_rotates_past_a_review_blocked_head_and_an_ineligible_window(
        tmp_path, review_polls):
    """four consistently ineligible PRs ahead of an eligible fifth must not
    hold every pass's inspection window on the same queue head."""
    class BlockedReviewRunning(FakeGateApplication):
        def review(self, repository, pr, head, base, executor_run, retry_of=None):
            if (repository, pr) != ("core-repo", 1):
                return super().review(repository, pr, head, base, executor_run, retry_of)
            self.calls.append(("review", repository, pr, head, base, executor_run, retry_of))
            # Outlast one poll slice so `_look_ahead` runs, then stay review-blocked.
            time.sleep(0.05)
            return {"ok": False, "code": "REVIEW_INCOMPLETE", "dispatched": True}

    store = _queue_store(tmp_path)
    gate = BlockedReviewRunning(
        {"core-repo": [1, 2, 3, 4, 5, 6]},
        responses={
            ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
            # Red, not rejected: selection parks a standing change request.
            **{("inspect", "core-repo", pr): _queue_inspect(
                head=str(pr), ci="red", review="not_approved", conclusion="failure",
                blocking_reason="CI_RUN_FAILED") for pr in (2, 3, 4, 5)},
            ("inspect", "core-repo", 6): _queue_inspect(head="6", review="not_approved"),
            ("review", "core-repo", 6): _PUBLISHED,
        })

    first = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, review_workers=2,
        max_reviews=6, now=lambda: 1000.0)[0]
    first_calls = list(gate.calls)
    assert store.read()["repositories"]["core-repo"]["review_ahead"] == 5
    # One window: the four ineligible PRs, never a fifth attempt in the same pass.
    # Selection read every PR to rank them before the review started.
    started = first_calls.index(next(call for call in first_calls if call[0] == "review"))
    assert [call[2] for call in first_calls[started:]
            if call[0] == "inspect" and call[2] != 1] == [2, 3, 4, 5]
    assert ("review", "core-repo", 6) not in [call[:3] for call in first_calls]
    assert {"pr": 1, "outcome": "retained", "reason": "REVIEW_INCOMPLETE"} in first["results"]

    second = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, review_workers=2,
        max_reviews=6, now=lambda: 1000.0)[0]
    second_calls = gate.calls[len(first_calls):]
    # The next pass resumes after PR 5 and offers the eligible PR before the old window.
    second_inspections = [call[2] for call in second_calls if call[0] == "inspect" and call[2] != 1]
    assert second_inspections[0] == 6
    assert len(second_inspections) <= merge_open_prs.TRAIN_SCAN_INSPECTIONS
    assert ("review", "core-repo", 6) in [call[:3] for call in second_calls]
    assert {"pr": 6, "outcome": "reviewed_ahead"} in second["results"]
    # The cursor names the last PR either scan inspected (`_review_ahead`
    # docstring), not the last one reviewed: while PR 1's review still runs,
    # the lookahead keeps scanning after PR 6 up to its window, so the cursor
    # ends on that scan's last inspection and the next pass resumes after it.
    assert store.read()["repositories"]["core-repo"]["review_ahead"] == second_inspections[-1]


def test_a_failed_lookahead_is_not_reviewed_again_in_the_same_pass(tmp_path, review_polls):
    """Its successor waits for the next pass, as after a failed active review."""
    gate = _overlapping_gate(review_polls, expected=1, numbers=(1, 2))
    gate.responses[("review", "core-repo", 2)] = {"ok": False, "code": "REVIEW_INCOMPLETE"}

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path), review_workers=2,
        now=lambda: 1000.0)[0]

    assert gate.lookaheads() == [("core-repo", 2)]
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1, 2]
    assert summary["results"] == [
        {"pr": 2, "outcome": "retained", "reason": "REVIEW_INCOMPLETE", "review_ahead": True},
        {"pr": 1, "outcome": "merged"},
        {"pr": 2, "outcome": "retained", "reason": "REVIEW_ATTEMPT_BUSY"}]


def test_a_train_waits_for_a_lookahead_review_in_its_own_repository(tmp_path, review_polls):
    """The train's prefixes turn green during docs-repo's review, once
    a lookahead reviews core-repo PR 3: no member merges until it ends."""
    store, gate = _train_beside_a_review(tmp_path)
    gate.by_repository["core-repo"] = [1, 2, 3]
    gate.responses[("inspect", "core-repo", 3)] = _queue_inspect(head="c", review="not_approved")
    lookahead_started = threading.Event()
    primary_returned = threading.Event()
    original_review = gate.review

    def review(repository, pr, head, base, executor_run, retry_of=None):
        reviewed = original_review(repository, pr, head, base, executor_run, retry_of)
        if (repository, pr) == ("docs-repo", 7):
            assert lookahead_started.wait(timeout=5)
            gate.responses.pop(("train_inspect", "core-repo"))  # Both prefixes turn green.
            review_polls.count = 0
            review_polls.reached.clear()
            gate.calls.append(("primary_polled", review_polls.reached.wait(timeout=5)))
            primary_returned.set()
        else:
            lookahead_started.set()
            assert primary_returned.wait(timeout=5)
            review_polls.count = 0
            review_polls.reached.clear()
            gate.calls.append(("lookahead_polled", review_polls.reached.wait(timeout=5)))
        return reviewed

    gate.review = review

    merge_open_prs.run_automerge_pass(
        [_train_repository(tmp_path), _docs_repo(tmp_path)], gate, queue_store=store,
        review_workers=2, now=lambda: 1000.0)

    assert ("review", "core-repo", 3) in [call[:3] for call in gate.calls]
    assert ("primary_polled", True) in gate.calls and ("lookahead_polled", True) in gate.calls
    lookahead_done = _call_index(gate, "lookahead_polled")
    # Polled while each review ran and after the first one ended; the train
    # merged only once the lookahead in its repository had ended too.
    assert not any(call[0] in {"train_member_merge", "train_close"}
                   for call in gate.calls[:lookahead_done])
    assert lookahead_done < _call_index(gate, "train_member_merge", "core-repo", 1)


_MUTATIONS = ("review", "refresh", "merge", "train_build", "train_member_merge", "train_close")


class _FailingLookahead(FakeGateApplication):
    """core-repo PR 1's review runs until a lookahead failure is observed.

    `failed` opens when the pass has seen the failure: at the failing
    inspection, or once a failing lookahead review is recorded. Then PR 1's
    review approves. `seen` keeps the call and status counts at that moment.

    With `primary_first`, PR 1's review instead approves once PR 2's
    lookahead has started, and PR 2's review answers only once the pass
    waits on it alone (`tail`): the pass meets the failure after the active
    review has ended and been seen to end.
    """

    def __init__(self, primary_first=False):
        super().__init__({"core-repo": [1, 2, 3], "docs-repo": []}, responses={
            ("inspect", "core-repo", 1): [_queue_inspect(review="missing"), _queue_inspect()],
            **{("inspect", "core-repo", pr): _queue_inspect(head=str(pr), review="not_approved")
               for pr in (2, 3)},
            **{("review", "core-repo", pr): _PUBLISHED for pr in (1, 2, 3)}})
        self.primary_first = primary_first
        self.failed = threading.Event()
        self.lookahead_started = threading.Event()
        self.primary_returned = threading.Event()
        self.tail = threading.Event()
        self.seen = None

    def observe(self):
        self.seen = (len(self.calls), len(self.statuses))
        self.failed.set()

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        if self.primary_first and (repository, pr) == ("core-repo", 2):
            self.lookahead_started.set()
            self.calls.append(("tail", self.tail.wait(timeout=5)))
        reviewed = super().review(repository, pr, head, base, executor_run, retry_of)
        if (repository, pr) == ("core-repo", 1):
            if self.primary_first:
                self.calls.append(("lookahead_started", self.lookahead_started.wait(timeout=5)))
                self.primary_returned.set()
            else:
                self.calls.append(("failure_observed", self.failed.wait(timeout=5)))
        return reviewed


def _run_failing_lookahead(monkeypatch, tmp_path, capsys, gate):
    repositories = {"core-repo": _core_repo(tmp_path), "docs-repo": _docs_repo(tmp_path)}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: gate)

    code = merge_open_prs.main(["--automerge", "--json", "--review-workers", "2"])

    payload = json.loads(capsys.readouterr().out)
    assert gate.seen is not None, "the pass never observed the failure"
    calls, statuses = gate.seen
    # Nothing is reviewed, refreshed, merged or published after the failure:
    # PR 1's approval lands after it and is discarded.
    assert [call for call in gate.calls[calls:] if call[0] in _MUTATIONS] == []
    assert len(gate.statuses) == statuses
    return code, payload["repositories"][0]


def test_an_integrity_failure_inspecting_a_lookahead_stops_the_pass(
        monkeypatch, tmp_path, capsys, review_polls):
    gate = _FailingLookahead()
    original_inspect = gate.inspect
    reads = []

    def inspect(repository, pr):
        if (repository, pr) != ("core-repo", 2):
            return original_inspect(repository, pr)
        reads.append(pr)
        if len(reads) == 1:  # Selection ranks PR 2; the lookahead's read fails.
            return original_inspect(repository, pr)
        gate.calls.append(("inspect", repository, pr))
        gate.observe()
        return {"ok": False, "code": "INTERNAL_ERROR"}

    gate.inspect = inspect

    code, summary = _run_failing_lookahead(monkeypatch, tmp_path, capsys, gate)

    assert ("failure_observed", True) in gate.calls
    assert code == 2
    assert summary["authority_integrity_failed"] is True
    assert summary["results"] == [
        {"pr": 2, "outcome": "retained", "reason": "INTERNAL_ERROR", "review_ahead": True,
         "authority_integrity_failed": True}]
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1]
    # Only selection read PR 3: the lookahead scan ended at PR 2.
    assert gate.calls.count(("inspect", "core-repo", 3)) == 1


@pytest.mark.parametrize("failure", ["REVIEW_SUPERVISOR_INVALID", "INTERNAL_ERROR"],
                         ids=["refused", "raised"])
@pytest.mark.parametrize("primary_first", [False, True],
                         ids=["during-active-review", "after-active-approval"])
def test_an_authority_failure_of_a_lookahead_review_stops_the_pass(
        monkeypatch, tmp_path, capsys, review_polls, failure, primary_first):
    gate = _FailingLookahead(primary_first)
    real_wait = merge_open_prs.wait

    def tail_wait(futures, timeout=None, **kwargs):
        # After PR 1 returned, only the tail loop waits on one future: PR 2's.
        if len(futures) == 1 and gate.primary_returned.is_set():
            gate.tail.set()
        return real_wait(futures, timeout=timeout, **kwargs)

    monkeypatch.setattr(merge_open_prs, "wait", tail_wait)
    if failure == "INTERNAL_ERROR":  # Raised as a gate refusal, not returned.
        original_review = gate.review

        def review(repository, pr, *args, **kwargs):
            reviewed = original_review(repository, pr, *args, **kwargs)
            if (repository, pr) == ("core-repo", 2):
                raise merge_open_prs.GateError(failure)
            return reviewed

        gate.review = review
    else:
        gate.responses[("review", "core-repo", 2)] = {
            "ok": False, "code": failure, "dispatched": True}
    original_record = merge_open_prs._record_automerge_result

    def record(result, repository, summaries):
        stop = original_record(result, repository, summaries)
        if result.get("pr") == 2:
            gate.observe()
        return stop

    monkeypatch.setattr(merge_open_prs, "_record_automerge_result", record)

    code, summary = _run_failing_lookahead(monkeypatch, tmp_path, capsys, gate)

    if primary_first:
        # PR 1's review approved before the failure: the pass discards it.
        assert ("lookahead_started", True) in gate.calls
        assert ("tail", True) in gate.calls
    else:
        assert ("failure_observed", True) in gate.calls
    assert code == 2
    assert summary["authority_integrity_failed"] is True
    assert summary["results"] == [
        {"pr": 2, "outcome": "retained", "reason": failure,
         "review_ahead": True, "authority_integrity_failed": True}]
    # No third review started in the freed worker, and PR 1 never merged.
    assert [call[2] for call in gate.calls if call[0] == "review"] == [1, 2]


# Budget-level: the supervising thread and the slots it holds.

class _HeldReviews:
    """A Gate application whose reviews block until released, one by one or all."""

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.changed = threading.Condition()
        self.released = set()
        self.everything = False
        self.started = []
        self.returned = []
        self.threads = set()

    def release(self, *keys):
        with self.changed:
            if keys:
                self.released.update(keys)
            else:
                self.everything = True
            self.changed.notify_all()

    def review(self, repository, pr, head, base, executor_run, retry_of=None):
        key = (repository, pr)
        with self.changed:
            self.started.append(key)
            self.threads.add(threading.current_thread())
            self.changed.notify_all()
            freed = self.changed.wait_for(
                lambda: self.everything or key in self.released, timeout=5)
        try:
            if not freed:
                raise AssertionError(f"review {key} was never released")
            outcome = self.outcomes.get(key, _APPROVED_REVIEW)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            with self.changed:
                self.returned.append(key)
                self.changed.notify_all()


def _offer(*keys):
    """A lookahead supplier offering `keys` once each, in order; it records each call."""
    offered = list(keys)
    calls = []

    def lookahead_candidate(reviewing, busy):
        calls.append((reviewing, frozenset(busy)))
        if not offered:
            return None
        repository, pr = offered.pop(0)
        return repository, pr, "c" * 40, "d" * 40, f"run:{repository}:{pr}"

    lookahead_candidate.calls = calls
    return lookahead_candidate


@pytest.mark.parametrize("stop", [
    lambda: merge_open_prs._TrainStop("core-repo"),
    lambda: merge_open_prs._DeployYield("DEPLOY_WAITING", "deployment holds the outer lock", []),
    lambda: RuntimeError("advance failed"),
], ids=["train-stop", "deploy-yield", "unexpected"])
@pytest.mark.parametrize("during", ["active-review", "lookahead-tail"])
def test_a_stop_during_lookahead_reviews_waits_for_every_review_and_spends_each_slot(
        review_polls, caplog, stop, during):
    application = _HeldReviews({("docs-repo", 8): {"ok": False, "code": "REVIEW_ATTEMPT_BUSY"},
                                ("docs-repo", 9): OSError("review transport failed")})
    budget = merge_open_prs._ReviewBudget(application, 6, 3, workers=4)
    budget.lookahead_candidate = _offer(("docs-repo", 7), ("docs-repo", 8),
                                        ("docs-repo", 9))
    raised = stop()
    advanced = []

    def advance_trains(**kwargs):
        advanced.append(kwargs)
        if "reviewing" in kwargs and "lookahead" not in kwargs:
            return  # The first slice: the lookaheads start after it.
        if "reviewing" in kwargs:
            with application.changed:
                assert application.changed.wait_for(lambda: len(application.started) == 4,
                                                    timeout=5)
            if during == "active-review":
                application.release()
                raise raised
            application.release(("core-repo", 1))
        elif "lookahead" in kwargs:
            application.release()
            raise raised

    budget.advance_trains = advance_trains

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        with pytest.raises(type(raised)) as excinfo:
            _budget_review(budget)

    assert excinfo.value is raised
    assert sorted(application.returned) == sorted(application.started) == [
        ("core-repo", 1), ("docs-repo", 7), ("docs-repo", 8), ("docs-repo", 9)]
    # The refused lookahead never started an attempt; the raised one may have.
    assert budget.review_count == 3
    assert budget.repository_counts == {"core-repo": 1, "docs-repo": 2}
    assert budget.lookahead_attempted == {("docs-repo", 7), ("docs-repo", 9)}
    assert not budget.exhausted("docs-repo"), "a settled review still holds its slot"
    assert "core-repo#1 review ended after a stop during it" in caplog.text
    assert "automerge: lookahead docs-repo#7 - APPROVED" in caplog.text
    assert "automerge: lookahead docs-repo#8 REVIEW_ATTEMPT_BUSY" in caplog.text
    assert "automerge: lookahead docs-repo#9 REVIEW_RAISED:OSError" in caplog.text
    assert threading.current_thread() not in application.threads


def test_trains_wait_for_every_repository_with_a_review_in_flight(review_polls):
    application = _HeldReviews()
    budget = merge_open_prs._ReviewBudget(application, None, workers=2)
    budget.lookahead_candidate = _offer(("docs-repo", 7))
    advanced = []

    def advance_trains(**kwargs):
        advanced.append(kwargs)
        if "reviewing" in kwargs and "lookahead" in kwargs:
            application.release(("core-repo", 1))
        elif "lookahead" in kwargs:
            application.release(("docs-repo", 7))

    budget.advance_trains = advance_trains

    assert _budget_review(budget) == _APPROVED_REVIEW

    alone = {"reviewing": "core-repo"}
    both = {**alone, "lookahead": frozenset({"docs-repo"})}
    tail = {"lookahead": frozenset({"docs-repo"})}
    # Before the reviews, its first slice alone, while both run, while only
    # the lookahead runs, after.
    assert advanced[0] == {} and advanced[1] == alone and advanced[-1] == {}
    assert advanced.index(both) < advanced.index(tail)
    assert all(kwargs in ({}, alone, both, tail) for kwargs in advanced)
    assert budget.review_count == 2


@pytest.mark.parametrize(("maximum", "per_repository", "offered", "started"), [
    (2, None, [("docs-repo", 7), ("docs-repo", 8)], [("docs-repo", 7)]),
    (None, 1, [("core-repo", 2), ("docs-repo", 7)], [("docs-repo", 7)]),
], ids=["pass-cap", "repository-cap"])
def test_a_lookahead_starts_only_within_both_caps_counting_reviews_in_flight(
        review_polls, maximum, per_repository, offered, started):
    application = _HeldReviews()
    budget = merge_open_prs._ReviewBudget(application, maximum, per_repository, workers=3)
    budget.lookahead_candidate = _offer(*offered)

    def advance_trains(**kwargs):
        if "lookahead" in kwargs:  # Every lookahead it may start has started.
            application.release()

    budget.advance_trains = advance_trains

    _budget_review(budget)

    assert sorted(application.started) == sorted([("core-repo", 1), *started])


def test_no_lookahead_starts_once_one_could_outlive_the_pass_deadline(review_polls):
    clock = [0.0]

    class Application(_HeldReviews):
        def review(self, repository, pr, *args, **kwargs):
            if repository == "docs-repo":
                clock[0] = 100.0  # 1350 s left: under one review's hard bound.
                self.release((repository, pr))
            return super().review(repository, pr, *args, **kwargs)

    application = Application()
    budget = merge_open_prs._ReviewBudget(application, None, deadline=1450,
                                          now=lambda: clock[0], workers=2)
    supplier = budget.lookahead_candidate = _offer(("docs-repo", 7), ("docs-repo", 8))

    def advance_trains(**kwargs):
        if "reviewing" in kwargs and ("docs-repo", 7) in application.returned \
                and review_polls.reached.is_set():
            application.release()

    budget.advance_trains = advance_trains

    assert _budget_review(budget) == _APPROVED_REVIEW
    # The free worker after the first lookahead was offered nothing new.
    assert sorted(application.started) == [("core-repo", 1), ("docs-repo", 7)]
    assert len(supplier.calls) == 1
    assert budget.review_count == 2


def test_review_workers_requires_automerge(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--review-workers", "2"])
    assert exc.value.code == 2
    assert "--review-workers requires --automerge" in capsys.readouterr().err


def test_review_workers_rejects_non_positive_values(capsys):
    with pytest.raises(SystemExit) as exc:
        merge_open_prs.main(["--automerge", "--review-workers", "0"])
    assert exc.value.code == 2


def test_review_workers_reaches_the_pass(monkeypatch, tmp_path):
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    seen = []

    def ran(selected, **kwargs):
        seen.append(kwargs)
        return [{"repository": repository.name, "results": []} for repository in selected]

    monkeypatch.setattr(merge_open_prs, "run_automerge_pass", ran)
    assert merge_open_prs.main(["--automerge", "--json", "--max-reviews", "6",
                                "--max-reviews-per-repo", "3", "--review-workers", "3"]) == 0
    assert seen == [{"max_reviews": 6, "max_reviews_per_repo": 3, "review_workers": 3}]


def test_tea_merge_refused_with_http_405_returns_nonzero(monkeypatch, tmp_path):
    # `tea api` exits 0 on an HTTP error status; `-i` puts the status on stderr.
    seen: list[list[str]] = []

    def run(argv, cwd):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout='{"message":"not mergeable"}',
                                           stderr="HTTP/1.1 405 Method Not Allowed\n")

    monkeypatch.setattr(merge_open_prs, "_run_completed", run)
    forge = merge_open_prs.ForgejoForge(merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"))
    code, output = forge.merge(_pull(7))
    assert code != 0
    assert "405 Method Not Allowed" in output
    assert "-i" in seen[0]


def test_tea_merge_unreadable_status_keeps_tea_returncode(monkeypatch, tmp_path):
    monkeypatch.setattr(merge_open_prs, "_run_completed",
                        lambda argv, cwd: subprocess.CompletedProcess(argv, 0, "{}", ""))
    forge = merge_open_prs.ForgejoForge(merge_open_prs.Repository(
        "core-repo", tmp_path, "forgejo", "acme/core-repo"))
    assert forge.merge(_pull(7)) == (0, "{}")


def _automerge_main(monkeypatch, tmp_path, merge_response):
    repositories = {name: merge_open_prs.Repository(
        name, tmp_path / name, "forgejo", f"acme/{name}")
        for name in ("core-repo", "docs-repo")}
    application = FakeGateApplication({"core-repo": [1], "docs-repo": []}, responses={
        ("inspect", "core-repo", 1): _queue_inspect(),
        ("merge", "core-repo", 1): merge_response,
    })
    monkeypatch.setattr(merge_open_prs, "load_repositories", lambda: repositories)
    monkeypatch.setattr(merge_open_prs, "_gate_application", lambda: application)
    return merge_open_prs.main(["--automerge", "--json"])


def test_automerge_merge_refusal_is_not_an_integrity_stop(monkeypatch, tmp_path, capsys, caplog):
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    code = _automerge_main(monkeypatch, tmp_path, {
        "ok": False, "code": "PR_MERGE_FAILED", "detail": {"http_status": 405}})
    payload = json.loads(capsys.readouterr().out)
    assert code != 2
    assert payload["repositories"][0]["authority_integrity_failed"] is False
    assert payload["repositories"][0]["results"][0]["reason"] == "PR_MERGE_FAILED"
    assert not any("integrity stop" in record.message for record in caplog.records)


def test_automerge_integrity_stop_logs_at_error(monkeypatch, tmp_path, capsys, caplog):
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    code = _automerge_main(monkeypatch, tmp_path, {
        "ok": False, "code": "PR_MERGE_POSTCONDITION_FAILED"})
    capsys.readouterr()
    assert code == 2
    errors = [record.message for record in caplog.records if record.levelno == logging.ERROR]
    assert errors == ["automerge integrity stop: core-repo#1 PR_MERGE_POSTCONDITION_FAILED"]


# ------------------------------------------------- executor downtime is not waiting


def _downtime_world(tmp_path, *, gap, waited=30, parked_for=200):
    """PR 1 active for `waited` seconds, then the executor is off for `gap`.

    The pass gap is measured on the host clock, so the seeded `last_pass` is
    `gap` seconds before the real now. The pass clock is the real now too, so
    the rebased `wait_since` reads `waited` seconds back, not `gap + waited`.
    """
    store = _queue_store(tmp_path)
    clock = [time.time()]
    with store.locked() as state:
        state["repositories"]["core-repo"] = {
            "active": {"pr": 1, "head": "a" * 40, "base": "b" * 40, "run": [11, 1],
                       "wait_since": clock[0] - gap - waited, "started": True},
            "parked": {"2": {"reason": "gate_blocked", "head": "c" * 40, "run": [12, 1],
                             "review": [None, "none"], "since": clock[0] - gap - parked_for}},
        }
        state["last_pass"] = clock[0] - gap
        store.save(state)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _in_progress()})
    return store, gate, clock


def _downtime_pass(tmp_path, gate, store, clock):
    return merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: clock[0])[0]


def test_executor_downtime_is_not_counted_as_waiting(tmp_path, caplog):
    """The first pass after a long stall must not log an inflated head_wait_s
    for a PR that had waited a second, nor park a PR waiting on CI as
    `ci_stalled` at once because the deadline counted the gap."""
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    gap = 12 * 3600
    store, gate, clock = _downtime_world(tmp_path, gap=gap)

    summary = _downtime_pass(tmp_path, gate, store, clock)

    assert summary["results"] == [{"pr": 1, "outcome": "retained",
                                   "reason": "CI_RUN_IN_PROGRESS"}]
    assert (summary["head_wait_s"], summary["park_wait_s"]) == (30, 200)
    row = store.read()["repositories"]["core-repo"]
    assert set(row["parked"]) == {"2"}
    assert row["active"]["wait_since"] == pytest.approx(clock[0] - 30, abs=1)
    rebased = [record.message for record in caplog.records if "pass gap" in record.message]
    assert len(rebased) == 1
    assert "re-based 2" in rebased[0] and str(gap) in rebased[0]


def test_a_deadline_still_trips_after_the_gap_has_been_rebased(tmp_path):
    store, gate, clock = _downtime_world(tmp_path, gap=12 * 3600)
    _downtime_pass(tmp_path, gate, store, clock)

    clock[0] += merge_open_prs.CI_WAIT_SECONDS  # Executor up: this wait counts.
    stalled = _downtime_pass(tmp_path, gate, store, clock)

    assert stalled["results"][0]["reason"] == "CI_STALLED"
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_stalled"


def test_a_normal_pass_cadence_leaves_the_wait_clocks_alone(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=merge_open_prs.__name__)
    store, gate, clock = _downtime_world(
        tmp_path, gap=merge_open_prs.PASS_GAP_SECONDS - 60, waited=100)

    summary = _downtime_pass(tmp_path, gate, store, clock)

    assert summary["head_wait_s"] == 100 + merge_open_prs.PASS_GAP_SECONDS - 60
    assert not [record for record in caplog.records if "pass gap" in record.message]


def test_the_first_pass_has_no_gap_to_measure(tmp_path):
    store, gate, clock = _downtime_world(tmp_path, gap=12 * 3600)
    with store.locked() as state:
        del state["last_pass"]
        store.save(state)

    summary = _downtime_pass(tmp_path, gate, store, clock)

    # Nothing to re-base against: the wait reads as it always did.
    assert summary["results"][0]["reason"] == "CI_STALLED"
    assert store.read()["last_pass"] >= clock[0] - 1


def test_rebase_shifts_every_persisted_wait_timestamp_but_never_past_now():
    state = {"repositories": {"core-repo": {
        "active": {"wait_since": 1000.0, "ci_missing": ["a" * 40, 1100.0],
                   "unmergeable": ["a" * 40, "b" * 40, 1200.0], "started": True},
        "parked": {"2": {"since": 900.0}, "3": {"reason": "no since here"}}}}}

    assert merge_open_prs._rebase_wait_clocks(state, 5000.0, 6000.0) == 4

    row = state["repositories"]["core-repo"]
    assert row["active"]["wait_since"] == 6000.0
    assert row["active"]["ci_missing"][1] == 6000.0
    assert row["active"]["unmergeable"][2] == 6000.0
    assert row["active"]["started"] is True
    assert row["parked"]["2"]["since"] == 5900.0
    assert row["parked"]["3"] == {"reason": "no since here"}


# Every wall-clock stamp the queue-state validator accepts (`time_valid` in
# the queue-state module) except `last_pass`, the reference the gap is
# measured from. Each is read as `now() - stamp` by a deadline or a metric.
REBASED_STAMPS = (
    ("active", "wait_since"),
    ("active", "ci_missing", 1),
    ("active", "unmergeable", 2),
    ("active", "progress", 1),
    ("active", "train", "started"),
    ("parked", "2", "since"),
)


def test_rebase_shifts_every_stamp_the_state_validator_knows():
    """downtime must not count against the stall budget (`progress`)
    or a train's own start, as it already does not for the other stamps."""
    import git_gate.automerge_queue as automerge_queue
    source = Path(automerge_queue.__file__).read_text()
    validated = re.findall(r"time_valid\(([^)]*)\)", source)
    assert sorted(validated) == sorted([
        "value: Any", 'train["started"]', 'state["last_pass"]', 'active["ci_missing"][1]',
        'active["progress"][1]', 'active["unmergeable"][2]', 'active["wait_since"]',
        'parked["since"]']), "a new validated stamp needs a REBASED_STAMPS entry"

    state = {"repositories": {"core-repo": {
        "active": {"wait_since": 1000.0, "ci_missing": ["a" * 40, 1000.0],
                   "unmergeable": ["a" * 40, "b" * 40, 1000.0],
                   "progress": ["CI_RUN_IN_PROGRESS", 1000.0],
                   "train": {"pr": 7, "started": 1000.0}, "started": True},
        "parked": {"2": {"since": 1000.0}}}}}

    assert merge_open_prs._rebase_wait_clocks(state, 500.0, 6000.0) == len(REBASED_STAMPS)

    row = state["repositories"]["core-repo"]
    for path in REBASED_STAMPS:
        value = row
        for key in path:
            value = value[key]
        assert value == 1500.0, path


@pytest.mark.parametrize("value", ["soon", -1, None, float("inf")])
def test_a_malformed_last_pass_is_corrupt_state(tmp_path, value):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    with store.locked() as state:
        state["last_pass"] = 1000.0
        store.save(state)
        assert store.read()["last_pass"] == 1000.0
        state["last_pass"] = value
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


# --------------------------------------------- a rejection parks in its own pass


@pytest.mark.parametrize("code", ["REVIEW_PUBLISHED", "REVIEW_REUSED", "REVIEW_RECONCILED"])
def test_a_rejecting_review_parks_in_the_pass_that_published_it(tmp_path, code):
    """The gate answers a rejection `ok` with the review's code and the
    verdict beside it. The executor journaled the code and held the PR active
    for one more pass."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        # The first read ranks PR 1 in selection, the second follows its review,
        # the third is the parked PR's release check in the next selection.
        ("inspect", "core-repo", 1): [_queue_inspect(review="not_approved"),
                                      _queue_inspect(review="changes_requested"),
                                      _queue_inspect(review="changes_requested")],
        # Not ready: PR 1 first. The reads: ranking, the next selection, its merge check.
        ("inspect", "core-repo", 2): [*_needs_review_then_approved(), _queue_inspect()],
        ("review", "core-repo", 1): {"ok": True, "code": code, "verdict": "CHANGES_REQUIRED"},
    })

    results = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]["results"]

    assert results == [{"pr": 1, "outcome": "retained", "reason": "CHANGES_REQUIRED"},
                       {"pr": 2, "outcome": "merged"}]
    row = store.read()["repositories"]["core-repo"]
    assert (row["active"], row["parked"]["1"]["reason"]) == (None, "changes_requested")


def test_a_review_ahead_rejection_is_journaled_as_the_verdict(tmp_path):
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 2): {"ok": True, "code": "REVIEW_PUBLISHED",
                                     "verdict": "CHANGES_REQUIRED"},
    })

    results = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path),
        max_reviews=1)[0]["results"]

    assert results[-1] == {"pr": 2, "outcome": "retained", "reason": "CHANGES_REQUIRED",
                           "review_ahead": True}


# ------------------------------------------- a busy repository lock is not stuck


def test_a_busy_gate_lock_never_parks_the_head_as_stuck(tmp_path):
    """`GATE_LOCK_BUSY` is raised before the operation body runs, so nothing
    was mutated; a holder that outlasts ten passes is a wait, which the
    time-based `PR_STALLED` deadline bounds, not a PR-owned fault."""
    store, gate, run = _stuck_world(tmp_path, {
        ("merge", "core-repo", 1): {"ok": False, "code": "GATE_LOCK_BUSY"}})
    for _ in range(merge_open_prs.STUCK_PASS_LIMIT + 3):
        assert run()["results"] == [
            {"pr": 1, "outcome": "retained", "reason": "GATE_LOCK_BUSY"}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"] == {}
    assert row["active"]["pr"] == 1 and "retained" not in row["active"]


def _prefix_observation(gate, states):
    """Independent per-prefix states at the Gate projection boundary."""
    return [
        {**prefix, "ci_state": state, "ci_run_id": 70 + position,
         "ci_run_attempt": 1,
         "ci_run_conclusion": "success" if state == "green" else "failure" if state == "red" else None,
         "blocking_reason": ("CI_RUN_FAILED" if state == "red"
                             else "CI_RUN_IN_PROGRESS" if state == "pending" else None)}
        for position, (prefix, state) in enumerate(zip(gate.built["prefixes"], states), 1)
    ]


def test_a_green_first_prefix_merges_while_the_later_prefixes_are_pending(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()
    gate.responses[("train_inspect", "core-repo")] = {
        "ci_state": "pending", "prefixes": _prefix_observation(gate, ["green", "pending", "pending"])}

    summary = _train_pass(tmp_path, gate, store)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1]
    assert summary["train"]["prefix_position"] == 2
    assert summary["train"]["outcome"] == "waiting"
    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["train"]["merged"] == [1]
    assert active["run"] == [72, 1]
    assert not _train_calls(gate, "train_close")
    assert gate.statuses[-1][1][2] == "queue=active reason=waiting_ci owner=queue pos=1"
    assert gate.statuses[-1][1][3].endswith("pos=2")


def test_a_red_first_prefix_blocks_merging_despite_a_green_final_candidate(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()
    gate.responses[("train_inspect", "core-repo")] = {
        "ci_state": "green", "prefixes": _prefix_observation(gate, ["red", "green", "green"])}

    summary = _train_pass(tmp_path, gate, store)

    assert not _train_calls(gate, "train_member_merge")
    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "CI_RUN_FAILED")]
    assert summary["train"]["outcome"] == "closed"
    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]


def test_a_later_red_prefix_keeps_the_landed_member_and_releases_the_rest_to_serial(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    gate.calls.clear()
    gate.responses[("train_inspect", "core-repo")] = {
        "prefixes": _prefix_observation(gate, ["green", "red", "green"])}

    summary = _train_pass(tmp_path, gate, store)

    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1]
    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "CI_RUN_FAILED")]
    assert [call[2] for call in _train_calls(gate, "refresh")] == [2]
    assert summary["results"][0]["outcome"] == "merged"


def test_a_failed_branch_delete_retains_cleanup_until_a_later_pass(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate({("train_close", "core-repo"): [
        {"ok": False, "code": "TRAIN_CLOSE_FAILED"}, None]})
    _train_pass(tmp_path, gate, store)
    gate.responses[("train_inspect", "core-repo")] = {
        "ci_state": "red", "ci_run_conclusion": "failure", "blocking_reason": "CI_RUN_FAILED"}

    refused = _train_pass(tmp_path, gate, store)

    active = store.read()["repositories"]["core-repo"]["active"]
    assert active["train"]["cleanup"] == {"reason": "CI_RUN_FAILED", "outcome": "closed"}
    assert refused["train"]["close_failed"] == "TRAIN_CLOSE_FAILED"
    assert not _train_calls(gate, "refresh")
    gate.calls.clear()
    # A closed final PR need not be inspectable for cleanup to retry.
    gate.responses[("train_inspect", "core-repo")] = {"ok": False, "code": "TRAIN_INVALID"}

    retried = _train_pass(tmp_path, gate, store)

    assert not _train_calls(gate, "train_inspect")
    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "CI_RUN_FAILED")]
    assert retried["train"]["outcome"] == "closed"
    assert "train" not in store.read()["repositories"]["core-repo"]["active"]


def test_a_legacy_final_only_train_is_closed_before_any_member_merge(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    with store.locked() as state:
        state["repositories"]["core-repo"]["active"]["train"].pop("prefixes")
        store.save(state)
    gate.calls.clear()

    summary = _train_pass(tmp_path, gate, store)

    assert not _train_calls(gate, "train_member_merge")
    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "TRAIN_INVALID")]
    assert summary["train"]["outcome"] == "closed"
    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]


def test_prefix_siblings_of_the_active_train_are_protected_from_orphan_cleanup(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    gate.by_repository["core-repo"] = [1, 2, 3, 900, 901, 902]
    gate.head_refs = {"core-repo": {
        900: "train/bbbbbbbbbbbb-1", 901: "train/bbbbbbbbbbbb-1-prefix-1",
        902: "train/bbbbbbbbbbbb-1-prefix-2"}}
    gate.responses[("train_inspect", "core-repo")] = {"ci_state": "pending"}
    gate.calls.clear()

    _train_pass(tmp_path, gate, store)

    assert not _train_calls(gate, "train_close")
    assert not _train_calls(gate, "train_member_merge")
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900


@pytest.mark.parametrize("mutation", ["duplicate_pr", "wrong_candidate", "wrong_branch", "short", "bool_position"])
def test_a_corrupt_prefix_projection_is_rejected_by_the_private_queue(tmp_path, mutation):
    from git_gate.automerge_queue import QueueStateError

    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    with store.locked() as state:
        prefixes = state["repositories"]["core-repo"]["active"]["train"]["prefixes"]
        if mutation == "duplicate_pr":
            prefixes[0]["pr"] = prefixes[-1]["pr"]
        elif mutation == "wrong_candidate":
            prefixes[0]["candidate"] = "f" * 40
        elif mutation == "wrong_branch":
            prefixes[0]["branch"] = "train/bbbbbbbbbbbb-1-prefix-2"
        elif mutation == "short":
            prefixes.pop()
        else:
            prefixes[0]["position"] = True
        with pytest.raises(QueueStateError, match="AUTOMERGE_STATE_INVALID"):
            store.save(state)


def test_a_partial_build_cleanup_failure_keeps_its_created_owner_in_the_active_slot(tmp_path):
    store = _queue_store(tmp_path)
    members = [{"pr": pr, "head": head * 40, "candidate": candidate}
               for pr, head, candidate in zip((1, 2, 3), "123", _CANDIDATES)]
    gate = _train_gate({("train_build", "core-repo"): {
        "ok": False, "code": "TRAIN_BUILD_FAILED", "detail": {
            "train_pr": 900, "train_branch": "train/bbbbbbbbbbbb-1", "base_sha": _TRAIN_BASE,
            "members": members, "cleanup_failed": "TRAIN_CLOSE_FAILED", "step": "push"}}})

    refused = _train_pass(tmp_path, gate, store)

    train = store.read()["repositories"]["core-repo"]["active"]["train"]
    assert train["cleanup"] == {"reason": "TRAIN_BUILD_FAILED", "outcome": "closed"}
    assert train["pr"] == 900
    assert refused["train"]["close_failed"] == "TRAIN_CLOSE_FAILED"
    assert (refused["train"]["step"], refused["train"]["train_pr"]) == ("push", 900)
    assert not _train_calls(gate, "refresh")
    gate.calls.clear()

    _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_close") == [("train_close", "core-repo", 900, "TRAIN_BUILD_FAILED")]
    assert not _train_calls(gate, "train_inspect")
    assert [call[2] for call in _train_calls(gate, "refresh")] == [1]


def test_a_train_with_an_externally_closed_leader_displays_the_next_queued_member(tmp_path):
    store = _queue_store(tmp_path)
    gate = _train_gate()
    _train_pass(tmp_path, gate, store)
    row = store.read()["repositories"]["core-repo"]

    descriptions = merge_open_prs._queue_descriptions(row, [2, 3, 4], "waiting_ci")

    assert descriptions[2][0] == "queue=active reason=waiting_ci owner=queue pos=1"
    assert descriptions[3][0].endswith("pos=2")
    assert descriptions[4][0].endswith("pos=3")


@pytest.mark.parametrize(("first", "next_result", "paid_calls", "parked"), [
    ({"code": "REVIEW_EXECUTION_UNCERTAIN", "recovery_action": "retry", "cleanup_proven": True},
     {"ok": True, "code": "REVIEW_PUBLISHED", "verdict": "ACCEPT_STATIC_CHANGE"}, 2, False),
    ({"code": "REVIEW_EXECUTION_UNCERTAIN", "recovery_action": "operator", "cleanup_proven": True},
     None, 1, True),
    ({"code": "REVIEW_EXECUTION_UNCERTAIN", "recovery_action": "operator", "cleanup_proven": False},
     None, 1, True),
    ({"code": "REVIEW_ATTEMPT_BUSY", "recovery_action": "wait", "cleanup_proven": False},
     None, 1, False),
    ({"code": "REVIEW_INCOMPLETE", "recovery_action": "retry", "cleanup_proven": True},
     None, 1, False),
    ({"code": "REVIEW_INCOMPLETE", "recovery_action": "operator", "cleanup_proven": True},
     None, 1, True),
])
def test_review_ahead_uses_the_same_recovery_and_operator_parking(
        tmp_path, first, next_result, paid_calls, parked):
    marker = "gate-review-" + "d" * 24
    replies = [{"ok": False, "run_marker": marker, **first}]
    if next_result is not None:
        replies.append(next_result)
    gate = FakeGateApplication({"core-repo": [1, 2]}, responses={
        ("inspect", "core-repo", 1): _in_progress(),
        ("inspect", "core-repo", 2): _queue_inspect(review="not_approved"),
        ("review", "core-repo", 2): replies,
    })
    store = _queue_store(tmp_path)
    result = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1, max_reviews_per_repo=1)[0]
    calls = [call for call in gate.calls if call[0] == "review"]
    assert len(calls) == paid_calls
    if paid_calls == 2:
        assert calls[-1][6] == marker
        assert result["results"][-1]["outcome"] == "reviewed_ahead"
    row = store.read()["repositories"]["core-repo"]
    assert ("2" in row["parked"]) is parked
    if parked:
        assert row["parked"]["2"]["reason"] == "review_execution_failed"
        merge_open_prs.run_automerge_pass(
            [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1, max_reviews_per_repo=1)
        assert len([call for call in gate.calls if call[0] == "review"]) == paid_calls
    assert not any(call[0] in {"merge", "refresh"} for call in gate.calls)


@pytest.mark.parametrize("code", ["REVIEW_NATIVE_REUSED", "REVIEW_REUSED", "REVIEW_RECONCILED"])
def test_native_or_saved_review_reuse_does_not_spend_a_paid_slot(code):
    assert not merge_open_prs._review_attempt_started({"ok": True, "code": code})
    assert not merge_open_prs._review_attempt_started({
        "ok": True, "code": "REVIEW_PUBLISHED", "dispatched": False})


# ------------------------------------------------ author queue hold
#
# An author holds a published PR out of the queue with the `gate/hold` label
# (the hold command). The label is a stop signal: the executor parks the PR for
# its author before any refresh, review, train membership or merge, and an
# unreadable label list stops it too, because failing closed only delays.

HOLD_LABEL = "gate/hold"


def _hold_gate(labels, numbers=(1,), **kwargs):
    return FakeGateApplication(
        {"core-repo": list(numbers)},
        head_refs={"core-repo": {number: "task/x" for number in numbers}},
        labels={"core-repo": labels}, **kwargs)


def test_a_held_pr_parks_for_its_author_and_is_never_reviewed_or_merged(tmp_path):
    store = _queue_store(tmp_path)
    gate = _hold_gate({1: [HOLD_LABEL], 2: []}, numbers=(1, 2), responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved"),
        ("inspect", "core-repo", 2): _in_progress()})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "author_hold"
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained", "reason": "AUTHOR_HOLD"} in summary["results"]
    assert gate.statuses[-1][1][1] == "queue=parked reason=author_hold owner=author pos=-"
    # The author chose to wait: no operator alert for it.
    assert summary["park_wait_s"] == 0
    # The next PR still has its turn.
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


def test_a_held_park_outlasts_a_head_change_and_a_green_run(tmp_path):
    store = _queue_store(tmp_path)
    gate = _hold_gate({1: [HOLD_LABEL]})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    gate.responses[("inspect", "core-repo", 1)] = _queue_inspect(head="d")
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 90000.0)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "author_hold"
    assert _reviewed_or_refreshed(gate, 1) == []
    # The forge write keeps it parked on the new head, not released.
    assert gate.parked_heads[-1] == {}


def test_a_released_pr_is_admitted_again(tmp_path):
    store = _queue_store(tmp_path)
    gate = _hold_gate({1: [HOLD_LABEL]})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert _reviewed_or_refreshed(gate, 1) == []
    gate.labels["core-repo"][1] = []
    gate.calls.clear()

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_a_pr_without_the_label_is_not_held(tmp_path):
    gate = _hold_gate({1: ["bug", "harness/alpha"]})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_an_active_pr_given_a_hold_gives_up_its_slot(tmp_path):
    store = _queue_store(tmp_path)
    gate = _hold_gate({1: []}, responses={("inspect", "core-repo", 1): _in_progress()})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    gate.labels["core-repo"][1] = [HOLD_LABEL]

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None
    assert row["parked"]["1"]["reason"] == "author_hold"


@pytest.mark.parametrize("labels", [
    {1: None},  # the gate could not read them
    {1: 7},
    {1: [7]},
    {1: HOLD_LABEL},  # a bare string is no list of names
    {},  # the listing carries labels, but none for this PR
])
def test_unread_labels_stop_a_pr_that_is_not_renovates(tmp_path, labels):
    """An unread label list is not an empty one: a hold may sit behind it."""
    store = _queue_store(tmp_path)
    gate = _hold_gate(labels, responses={
        ("inspect", "core-repo", 1): _queue_inspect(review="not_approved")})

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, max_reviews=1,
        now=lambda: 1000.0)[0]

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "hold_labels_unknown")
    assert _reviewed_or_refreshed(gate, 1) == []
    assert {"pr": 1, "outcome": "retained", "reason": "HOLD_LABELS_UNKNOWN"} in summary["results"]
    assert gate.statuses[-1][1][1] == (
        "queue=parked reason=hold_labels_unknown owner=operator pos=-")


def test_an_unread_labels_park_alerts_and_ends_when_they_read(tmp_path):
    store = _queue_store(tmp_path)
    gate = _hold_gate({1: None})
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store,
                                       now=lambda: 1000.0)

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 12000.0)[0]
    assert summary["park_wait_s"] == 11000

    gate.labels["core-repo"][1] = []
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert "1" not in store.read()["repositories"]["core-repo"]["parked"]
    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_a_listing_without_label_data_holds_nothing(tmp_path):
    """No `labels` field at all is an application that never reports them."""
    gate = FakeGateApplication({"core-repo": [1]})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                       queue_store=_queue_store(tmp_path))

    assert [call[0] for call in _reviewed_or_refreshed(gate, 1)] == ["merge"]


def test_a_hold_outranks_a_renovate_hold_label_and_is_not_a_consent(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate()
    gate.labels["core-repo"][1] = ["major", "renovate", HOLD_LABEL]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "author_hold"

    # The operator clears `major` while the author's hold stands: no consent.
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = ["renovate", HOLD_LABEL]
    gate.calls.clear()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "author_hold"
    assert "consented" not in row
    assert _reviewed_or_refreshed(gate, 1) == []

    # Released: the update the label history shows needed consent is still held.
    gate.labels["core-repo"][1] = ["renovate"]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert "merge" not in [call[0] for call in _reviewed_or_refreshed(gate, 1)]


def test_a_hold_label_that_returns_a_released_renovate_pr_to_its_own_hold(tmp_path):
    store = _queue_store(tmp_path)
    gate = _major_gate()
    gate.labels["core-repo"][1] = ["major", "renovate", HOLD_LABEL]
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "author_hold"
    gate.by_repository["core-repo"] = [1]
    gate.labels["core-repo"][1] = ["major", "renovate"]

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "renovate_major"


@pytest.mark.parametrize("sequence", ["first", "before_merge"])
def test_a_hold_that_lands_during_a_pass_stops_the_merge(tmp_path, sequence):
    """The listing is read at the start of the pass; a paid review can run for
    minutes. The hold is read again with each inspection of the candidate."""
    store = _queue_store(tmp_path)
    held = _queue_inspect(queue_hold=True)
    responses = ({("inspect", "core-repo", 1): held} if sequence == "first" else {
        ("inspect", "core-repo", 1): [_queue_inspect(review="not_approved"), held]})
    gate = _hold_gate({1: []}, responses=responses)

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store, now=lambda: 1000.0)[0]

    assert not any(call[0] == "merge" for call in gate.calls)
    assert {"pr": 1, "outcome": "retained", "reason": "PR_QUEUE_HOLD"} in summary["results"]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "author_hold"
    assert row["active"] is None


def test_a_held_pr_is_not_admitted_to_a_new_train(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeTrainApplication(
        {"core-repo": [1, 2, 3]}, labels={"core-repo": {1: [], 2: [HOLD_LABEL], 3: []}},
        responses={("inspect", "core-repo", pr): _rider(head, 10 + pr)
                   for pr, head in zip((1, 2, 3), "123")})

    _train_pass(tmp_path, gate, store)

    assert [call[2] for call in _train_calls(gate, "train_build")] == [
        ((1, "1" * 40), (3, "3" * 40))]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["2"]["reason"] == "author_hold"
    assert [member["pr"] for member in row["active"]["train"]["members"]] == [1, 3]


def test_a_train_is_not_torn_down_for_a_hold_it_stops_before_the_held_member(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeTrainApplication(
        {"core-repo": [1, 2, 3]}, labels={"core-repo": {1: [], 2: [], 3: []}},
        responses={("inspect", "core-repo", pr): _rider(head, 10 + pr)
                   for pr, head in zip((1, 2, 3), "123")})
    _train_pass(tmp_path, gate, store)
    assert store.read()["repositories"]["core-repo"]["active"]["train"]["pr"] == 900
    gate.labels["core-repo"][2] = [HOLD_LABEL]
    gate.calls.clear()

    summary = _train_pass(tmp_path, gate, store)

    # The prefix before the held member still merges under the train's CI.
    assert [call[2] for call in _train_calls(gate, "train_member_merge")] == [1]
    assert _train_calls(gate, "train_close") == [
        ("train_close", "core-repo", 900, "merged #1 - left #2, #3")]
    assert summary["train"]["outcome"] == "merged"
    row = store.read()["repositories"]["core-repo"]
    # The train is gone; the rest return to the serial queue in the same pass.
    assert row["active"]["pr"] == 3 and "train" not in row["active"]
    assert row["parked"]["2"]["reason"] == "author_hold"
    assert "3" not in row["parked"]
    assert _train_calls(gate, "merge") == []  # The held PR is never merged directly either.


def test_a_held_train_leader_is_not_merged_and_its_train_closes(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeTrainApplication(
        {"core-repo": [1, 2, 3]}, labels={"core-repo": {1: [], 2: [], 3: []}},
        responses={("inspect", "core-repo", pr): _rider(head, 10 + pr)
                   for pr, head in zip((1, 2, 3), "123")})
    _train_pass(tmp_path, gate, store)
    gate.labels["core-repo"][1] = [HOLD_LABEL]
    gate.calls.clear()

    _train_pass(tmp_path, gate, store)

    assert _train_calls(gate, "train_member_merge") == []
    assert [call[3] for call in _train_calls(gate, "train_close")] == [
        "TRAIN_MEMBERS_LEFT: left #1, #2, #3"]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "author_hold"


@pytest.mark.parametrize("failure", [False, True])
def test_serial_merge_has_no_premerge_delivery_and_retry_failure_is_nonblocking(tmp_path, failure):
    gate = FakeGateApplication({"core-repo": [7]}, responses={
        ("inspect", "core-repo", 7): _queue_inspect(review_state="approved"),
    })
    retries = []

    def retry():
        retries.append(True)
        if failure:
            raise TimeoutError("board response unavailable")
        return {"ok": False, "code": "REVIEW_FOLLOWUPS_UNAVAILABLE"}

    gate.review_followups = lambda *_args: pytest.fail("serial merge attempted premerge ticket delivery")
    gate.retry_review_followups = retry
    result = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path))[0]
    assert result["results"] == [{"pr": 7, "outcome": "merged"}]
    assert retries == [True]


def test_train_member_merge_has_no_premerge_ticket_delivery(tmp_path):
    gate = _train_gate(numbers=(1, 2))
    gate.review_followups = lambda *_args: pytest.fail("train member merge attempted premerge ticket delivery")
    gate.retry_review_followups = lambda: {"ok": False, "code": "REVIEW_FOLLOWUPS_TIMEOUT"}
    store = _queue_store(tmp_path)
    _train_pass(tmp_path, gate, store)
    result = _train_pass(tmp_path, gate, store)
    assert [entry["pr"] for entry in result["results"] if entry["outcome"] == "merged"] == [1, 2]


def test_followup_retry_runs_when_every_pr_already_left_queue(tmp_path):
    gate = FakeGateApplication({"core-repo": []})
    retries = []
    gate.retry_review_followups = lambda: retries.append(True) or {"ok": True}
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=_queue_store(tmp_path))
    assert retries == [True]


# ------------------------------------------------ comment re-review

_REVIEW_BASE = "d" * 40


def _comment_due(**extra):
    """A standing delegated rejection the gate row says agent comments answer."""
    return _queue_inspect(**{"review": "not_approved", "latest_delegated_review_id": 31,
                             "latest_delegated_review_state": "REQUEST_CHANGES",
                             "agent_comments_pending": True, "agent_comment_set": "e" * 64,
                             "agent_comment_review_base": _REVIEW_BASE, **extra})


def _comment_rereview_pass(tmp_path, review_response, *, store=None, **pass_kwargs):
    store = store or _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _comment_due(),
        ("review", "core-repo", 1): review_response,
    })
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                                queue_store=store, **pass_kwargs)[0]
    return gate, store, summary


def _mutations(gate):
    return [call for call in gate.calls if call[0] in {"refresh", "merge"}]


def test_comment_rereview_due_needs_the_flag_and_a_review_base():
    assert merge_open_prs._comment_rereview_due(_comment_due()) is True
    assert merge_open_prs._comment_rereview_due(_comment_due(agent_comments_pending=False)) is False
    assert merge_open_prs._comment_rereview_due(_comment_due(agent_comments_pending="true")) is False
    for base in (None, "d" * 39, "D" * 40, 7):
        assert merge_open_prs._comment_rereview_due(
            _comment_due(agent_comment_review_base=base)) is False
    assert merge_open_prs._comment_rereview_due(_queue_inspect()) is False


def test_a_released_park_is_re_reviewed_at_the_standing_review_base(tmp_path):
    """The park of a plain rejection holds; a due row releases it and the
    review runs on the same head at `agent_comment_review_base`, not main."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _comment_due(agent_comments_pending=False),
        ("review", "core-repo", 1): {"ok": True, "code": "REVIEW_PUBLISHED",
                                     "verdict": "CHANGES_REQUIRED"},
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "changes_requested"
    assert not [call for call in gate.calls if call[0] == "review"]

    gate.responses[("inspect", "core-repo", 1)] = _comment_due()
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    reviews = [call for call in gate.calls if call[0] == "review"]
    assert reviews == [("review", "core-repo", 1, "a" * 40, _REVIEW_BASE,
                        f"{merge_open_prs.DEFAULT_EXECUTOR_RUN_PREFIX}:core-repo:1", None)]
    assert _REVIEW_BASE != _comment_due()["base_sha"]
    assert _mutations(gate) == []


def test_a_comment_rereview_approval_ranks_the_pr_in_the_same_selection(tmp_path):
    """the re-review runs at selection; the read after it is ranked as
    any other, and the ordinary path merges the approved head."""
    class ApprovingGate(FakeGateApplication):
        def review(self, repository, pr, head, base, executor_run, retry_of=None):
            super().review(repository, pr, head, base, executor_run, retry_of)
            self.responses[("inspect", repository, pr)] = _queue_inspect()
            return {"ok": True, "code": "REVIEW_PUBLISHED", "verdict": "ACCEPT_STATIC_CHANGE"}

    store = _queue_store(tmp_path)
    gate = ApprovingGate({"core-repo": [1]},
                         responses={("inspect", "core-repo", 1): _comment_due()})
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert summary["results"] == [
        {"pr": 1, "outcome": "retained", "reason": "COMMENT_REREVIEW_APPROVED",
         "comment_rereview": True},
        {"pr": 1, "outcome": "merged"}]
    assert len([call for call in gate.calls if call[0] == "review"]) == 1
    assert _mutations(gate) == [("merge", "core-repo", 1, "a" * 40, "b" * 40)]


def test_a_comment_rereview_approval_under_a_standing_request_is_parked(tmp_path):
    """The approval did not end the request (another reviewer's, say): never elected."""
    gate, store, summary = _comment_rereview_pass(
        tmp_path, {"ok": True, "code": "REVIEW_PUBLISHED", "verdict": "ACCEPT_STATIC_CHANGE"})
    assert summary["results"] == [{"pr": 1, "outcome": "retained",
                                   "reason": "COMMENT_REREVIEW_APPROVED", "comment_rereview": True}]
    row = store.read()["repositories"]["core-repo"]
    assert row["active"] is None and row["parked"]["1"]["reason"] == "changes_requested"
    assert _mutations(gate) == []


def test_a_comment_rereview_rejection_parks_with_the_new_review_identity(tmp_path):
    class RejectingGate(FakeGateApplication):
        def review(self, repository, pr, head, base, executor_run, retry_of=None):
            super().review(repository, pr, head, base, executor_run, retry_of)
            # The new review is now the standing one; its window is empty.
            self.responses[("inspect", repository, pr)] = _queue_inspect(
                review="not_approved", latest_delegated_review_id=32,
                latest_delegated_review_state="REQUEST_CHANGES")
            return {"ok": True, "code": "REVIEW_PUBLISHED", "verdict": "CHANGES_REQUIRED"}

    store = _queue_store(tmp_path)
    gate = RejectingGate({"core-repo": [1]},
                         responses={("inspect", "core-repo", 1): _comment_due()})
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert summary["results"] == [{"pr": 1, "outcome": "retained", "reason": "CHANGES_REQUIRED",
                                   "comment_rereview": True}]
    # Round-tripped through the queue validator: two-element identity, no new field.
    parked = store.read()["repositories"]["core-repo"]["parked"]["1"]
    assert parked["reason"] == "changes_requested"
    assert parked["review"] == [32, "REQUEST_CHANGES"]
    assert set(parked) <= {"reason", "head", "base", "run", "review", "claim_sha", "since"}
    assert _mutations(gate) == []
    # The new standing review has no comments after it: the park holds.
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len([call for call in gate.calls if call[0] == "review"]) == 1


@pytest.mark.parametrize(("response", "park", "copied"), [
    ({"ok": False, "code": "REVIEW_CLEANUP_UNKNOWN", "recovery_action": "operator"},
     "review_execution_failed", {"recovery_action": "operator"}),
    ({"ok": False, "code": "PR_CLAIM_UNAVAILABLE", "claim_error": "PR_CLAIM_MALFORMED",
      "dispatched": False}, "claim_unavailable", {"claim_error": "PR_CLAIM_MALFORMED"}),
    ({"ok": False, "code": "PR_COMMENTS_UNAVAILABLE", "dispatched": False},
     "changes_requested", {}),
    ({"ok": False, "code": "REVIEW_STATE_CHANGED", "dispatched": False},
     "changes_requested", {}),
    # A fresh incomplete run is retryable and `_park_reason` leaves it unparked;
    # the standing request still binds, so the PR parks instead of keeping the slot.
    ({"ok": False, "code": "REVIEW_INCOMPLETE", "recovery_action": "retry"},
     "changes_requested", {"recovery_action": "retry"}),
    ({"ok": False, "code": "REVIEW_INCOMPLETE", "recovery_action": "operator"},
     "review_execution_failed", {"recovery_action": "operator"}),
], ids=["execution-failure", "claim", "comments-unreadable", "state-changed",
        "incomplete-retryable", "incomplete-after-retry"])
def test_a_failed_comment_rereview_parks_by_its_code(tmp_path, response, park, copied):
    gate, store, summary = _comment_rereview_pass(tmp_path, response)
    (record,) = summary["results"]
    assert record["reason"] == response["code"] and record["comment_rereview"] is True
    assert {key: record[key] for key in copied} == copied
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == park
    assert store.read()["repositories"]["core-repo"]["active"] is None
    assert _mutations(gate) == []


def test_a_refused_comment_rereview_on_a_red_head_parks_as_ci_failed(tmp_path):
    """`_park_reason` ranks a terminal CI failure above the standing request, so
    the pre-dispatch `PR_CI_NOT_GREEN` refusal parks as `ci_failed`. A comment
    does not release that park: the pending set is not retried on every pass."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _comment_due(ci="red", conclusion="failure"),
        ("review", "core-repo", 1): {"ok": False, "code": "PR_CI_NOT_GREEN", "dispatched": False},
    })
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    (record,) = summary["results"]
    assert record["reason"] == "PR_CI_NOT_GREEN" and record["comment_rereview"] is True
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "ci_failed"
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len([call for call in gate.calls if call[0] == "review"]) == 1
    assert _mutations(gate) == []


def test_an_exhausted_review_budget_costs_nothing_and_leaves_the_park(tmp_path):
    gate, store, summary = _comment_rereview_pass(
        tmp_path, {"ok": True, "verdict": "ACCEPT_STATIC_CHANGE"}, max_reviews=0)
    assert summary["results"] == [{"pr": 1, "outcome": "retained",
                                   "reason": "REVIEW_BUDGET_EXHAUSTED", "comment_rereview": True}]
    assert not [call for call in gate.calls if call[0] == "review"]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "changes_requested"


def test_a_comment_rereview_exception_is_a_retained_record(tmp_path):
    class FailingGate(FakeGateApplication):
        def review(self, *_args, **_kwargs):
            raise RuntimeError("transport lost")

    store = _queue_store(tmp_path)
    gate = FailingGate({"core-repo": [1]},
                       responses={("inspect", "core-repo", 1): _comment_due()})
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert summary["results"] == [{"pr": 1, "outcome": "retained",
                                   "reason": "CANDIDATE_ERROR:RuntimeError", "comment_rereview": True}]
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "changes_requested"


def test_a_retryable_incomplete_comment_rereview_is_retried_on_the_next_pass(tmp_path):
    """The park holds the slot free; the due row releases it, and the review
    owner's receipt (not this pass) bounds the retry to one linked successor."""
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, responses={
        ("inspect", "core-repo", 1): _comment_due(),
        ("review", "core-repo", 1): {"ok": False, "code": "REVIEW_INCOMPLETE",
                                     "recovery_action": "retry"},
    })
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "changes_requested"
    gate.responses[("review", "core-repo", 1)] = {"ok": False, "code": "REVIEW_INCOMPLETE",
                                                  "recovery_action": "operator"}
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len([call for call in gate.calls if call[0] == "review"]) == 2
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "review_execution_failed")
    # An operator-owned park is not released by the comments that are still pending.
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len([call for call in gate.calls if call[0] == "review"]) == 2
    assert _mutations(gate) == []


@pytest.mark.parametrize("green_at", ["inspect", "review"])
def test_a_train_integrity_stop_around_a_comment_rereview_ends_every_later_write(
        tmp_path, green_at):
    """The branch handles the stop like the ordinary review path: the train's
    record survives and nothing is written for the PR. Selection runs the
    re-review, so the PR is neither active nor parked."""
    store, gate = _train_beside_a_review(tmp_path)
    gate.responses[("inspect", "docs-repo", 7)] = _comment_due()
    gate.responses[("train_member_merge", "core-repo", 1)] = {
        "ok": False, "code": "TRAIN_TREE_MISMATCH", "detail": {"pull_index": 1}}
    original = getattr(gate, green_at)

    def turn_green(repository, *args, **kwargs):
        if repository == "docs-repo":
            gate.responses.pop(("train_inspect", "core-repo"), None)
        return original(repository, *args, **kwargs)

    setattr(gate, green_at, turn_green)

    trains, reviewed = _train_review_pass(tmp_path, gate, store)

    assert trains["authority_integrity_failed"] is True
    assert trains["results"] == [{"pr": 1, "outcome": "retained",
                                  "reason": "TRAIN_TREE_MISMATCH", "train_pr": 900,
                                  "authority_integrity_failed": True}]
    reviews = [call[1:3] for call in gate.calls if call[0] == "review"]
    assert reviews == ([] if green_at == "inspect" else [("docs-repo", 7)])
    assert not any(call[0] in {"merge", "refresh", "train_close"} for call in gate.calls)
    assert reviewed["results"] == []
    rows = store.read()["repositories"]
    assert rows["docs-repo"]["active"] is None and rows["docs-repo"]["parked"] == {}
    assert rows["core-repo"]["active"]["train"]["pr"] == 900


@pytest.mark.parametrize("code", ["REVIEW_NATIVE_REUSED", "REVIEW_REUSED", "REVIEW_RECONCILED"])
def test_a_comment_rereview_without_a_new_review_is_visible(tmp_path, monkeypatch, code):
    warnings = []
    monkeypatch.setattr(merge_open_prs.LOGGER, "warning", lambda *args: warnings.append(args))
    gate, store, summary = _comment_rereview_pass(
        tmp_path, {"ok": True, "code": code, "verdict": "CHANGES_REQUIRED"})
    (record,) = summary["results"]
    assert record["comment_rereview_no_new_review"] is True and record["comment_rereview"] is True
    assert record["reason"] == "CHANGES_REQUIRED"
    assert any(code in args for args in warnings)
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == "changes_requested"


def test_a_renovate_pr_with_a_due_row_keeps_its_own_handling(tmp_path):
    store = _queue_store(tmp_path)
    gate = FakeGateApplication({"core-repo": [1]}, head_refs={"core-repo": {1: "renovate/a"}},
                               responses={("inspect", "core-repo", 1): _comment_due()})
    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert not [call for call in gate.calls if call[0] == "review"]
    assert all("comment_rereview" not in record for record in summary["results"])
    assert store.read()["repositories"]["core-repo"]["parked"]["1"]["reason"] == (
        "renovate_changes_requested")


def _parked_record(reason="changes_requested", head="a" * 40):
    return {"reason": reason, "head": head, "base": "b" * 40, "run": [11, 1],
            "review": [31, "REQUEST_CHANGES"], "claim_sha": None, "since": 1.0}


def test_parked_changed_releases_only_a_change_request_on_the_same_head_with_a_due_row():
    due, plain = _comment_due(), _comment_due(agent_comments_pending=False)
    # Same identities: only the due row releases it.
    assert merge_open_prs._parked_changed(_parked_record(), plain) is False
    assert merge_open_prs._parked_changed(_parked_record(), due) is True
    for reason in ("renovate_changes_requested", merge_open_prs.AUTHOR_HOLD,
                   "review_execution_failed", "claim_unavailable", "stuck"):
        assert merge_open_prs._parked_changed(_parked_record(reason), due) is False, reason
    # A park on another head is released by the head change, not by comments.
    assert merge_open_prs._parked_changed(_parked_record(head="c" * 40), plain) is True


def test_a_conflict_park_is_head_bound_so_a_moved_head_readmits_the_renovate_pr():
    """The fair-slot selection needs the parked-head premise.

    A non-hold park whose recorded head differs from the live head is released
    here, and the pass then selects that Renovate PR, which resets
    `agent_streak` (`test_a_due_renovate_slot_selects_the_renovate_pr_and_resets_the_streak`).
    """
    inspected = _queue_inspect(head="a", review="changes_requested")
    parked = {"reason": "conflict", "head": inspected["head_sha"], "base": inspected["base_sha"],
              "run": [inspected["ci_run_id"], inspected["ci_run_attempt"]],
              "review": merge_open_prs._review_identity(inspected), "claim_sha": None,
              "since": 1.0}

    assert merge_open_prs._parked_changed(parked, inspected) is False
    assert merge_open_prs._parked_changed(
        parked, _queue_inspect(head="c", review="changes_requested")) is True


_EXHAUSTED_DETAIL = "2 comment-triggered attempts of this head exist (limit 2)"


def test_an_exhausted_comment_rereview_parks_as_a_review_execution_failure(tmp_path):
    """the review owner refuses a third comment-triggered attempt of a head."""
    response = {"ok": False, "code": "REVIEW_COMMENT_REREVIEWS_EXHAUSTED",
                "review_error_detail": _EXHAUSTED_DETAIL, "dispatched": False}
    gate, store, summary = _comment_rereview_pass(tmp_path, response)
    assert summary["results"] == [{
        "pr": 1, "outcome": "retained", "reason": "REVIEW_COMMENT_REREVIEWS_EXHAUSTED",
        "review_error_detail": _EXHAUSTED_DETAIL, "comment_rereview": True}]
    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "review_execution_failed" and row["active"] is None
    assert _mutations(gate) == []
    # The gate row still says comments are pending: no further comment reviews the head again.
    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)
    assert len([call for call in gate.calls if call[0] == "review"]) == 1


def test_a_park_after_an_exhausted_comment_rereview_ends_only_with_a_new_head():
    assert "REVIEW_COMMENT_REREVIEWS_EXHAUSTED" in merge_open_prs.REVIEW_EXECUTION_FAILURE_CODES
    parked = _parked_record("review_execution_failed")
    assert merge_open_prs._parked_changed(parked, _comment_due()) is False
    assert merge_open_prs._parked_changed(parked, _comment_due(head_sha="c" * 40)) is True


def test_the_review_identity_stays_two_elements_on_a_due_row():
    assert merge_open_prs._review_identity(_comment_due()) == [31, "REQUEST_CHANGES"]


# ------------------------------------------------ readiness order


def _readiness_gate(reads, **kwargs):
    return FakeGateApplication({"core-repo": sorted(reads)}, responses={
        ("inspect", "core-repo", pr): read for pr, read in reads.items()}, **kwargs)


def _first_write(gate):
    return next(call[:3] for call in gate.calls if call[0] in {"review", "refresh", "merge"})


def test_an_approved_green_pr_is_elected_ahead_of_an_earlier_unreviewed_one(tmp_path):
    gate = _readiness_gate({1: _queue_inspect(review="not_approved"), 2: _queue_inspect()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                      queue_store=_queue_store(tmp_path))

    assert _first_write(gate) == ("merge", "core-repo", 2)


def test_an_approved_pr_waiting_on_ci_is_elected_ahead_of_an_unreviewed_green_one(tmp_path):
    store = _queue_store(tmp_path)
    gate = _readiness_gate({1: _queue_inspect(review="not_approved"), 2: _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


def test_a_delegated_approval_on_an_older_base_ranks_as_approved(tmp_path):
    store = _queue_store(tmp_path)
    gate = _readiness_gate({
        1: _queue_inspect(review="not_approved"),
        2: _queue_inspect(review="not_approved", latest_delegated_review_state="APPROVED",
                          ci="pending", blocking_reason="CI_BASE_STALE"),
    })

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert _first_write(gate) == ("refresh", "core-repo", 2)
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


def test_a_pr_with_a_standing_change_request_is_never_elected(tmp_path):
    store = _queue_store(tmp_path)
    gate = _readiness_gate({1: _queue_inspect(review="changes_requested"), 2: _in_progress()})

    summary = merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate,
                                                queue_store=store)[0]

    row = store.read()["repositories"]["core-repo"]
    assert row["parked"]["1"]["reason"] == "changes_requested"
    # Only PR 2 was elected: an election of PR 1 would have counted too.
    assert (row["active"]["pr"], row["agent_streak"]) == (2, 1)
    assert {"pr": 1, "outcome": "retained",
            "reason": "PR_REVIEW_CHANGES_REQUESTED"} in summary["results"]
    assert not [call for call in gate.calls if call[0] in {"review", "refresh", "merge"}]


def test_readiness_outranks_a_due_renovate_slot(tmp_path):
    store = _queue_store(tmp_path)
    _seed_agent_streak(store, merge_open_prs.RENOVATE_SLOT_EVERY)
    gate = FakeGateApplication(
        {"core-repo": [1, 2]}, head_refs={"core-repo": {1: "renovate/a", 2: "task/x"}},
        responses={("inspect", "core-repo", 1): _in_progress(review="not_approved"),
                   ("inspect", "core-repo", 2): _in_progress()})

    merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 2


@pytest.mark.parametrize("reads,elected", [
    # Tier 0 (approved, green) merges first; the unreviewed PR 1 (tier 2) follows.
    ({1: _queue_inspect(review="not_approved"), 2: _queue_inspect()},
     [{"pr": 2, "tier": 0}, {"pr": 1, "tier": 2}]),
    # Tier 1: approved, the full run still going.
    ({1: _queue_inspect(review="not_approved"), 2: _in_progress()}, [{"pr": 2, "tier": 1}]),
    # Tier 2: nothing is approved; the first PR in queue order is elected.
    ({1: _in_progress(review="not_approved")}, [{"pr": 1, "tier": 2}]),
], ids=["tier-0-then-2", "tier-1", "tier-2"])
def test_the_summary_records_the_tier_of_every_election(tmp_path, reads, elected):
    """The per-repository summary lists each election of the pass with its tier
   : the log evidence that readiness, not queue position, picks the PR."""
    store = _queue_store(tmp_path)
    gate = _readiness_gate(reads)

    summary = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert summary["elected"] == elected


def test_a_pass_that_elects_nothing_records_no_election(tmp_path):
    """An active PR that keeps waiting is not elected again: the summary has no
    `elected` key, so a count of the key counts elections and nothing else."""
    store = _queue_store(tmp_path)
    gate = _readiness_gate({1: _in_progress()})
    first = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]
    assert first["elected"] == [{"pr": 1, "tier": 1}]

    second = merge_open_prs.run_automerge_pass(
        [_core_repo(tmp_path)], gate, queue_store=store)[0]

    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    assert "elected" not in second


def test_selection_inspects_at_most_its_cap(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(merge_open_prs, "SELECTION_INSPECTIONS", 1)
    store = _queue_store(tmp_path)
    gate = _readiness_gate({1: _in_progress(review="not_approved"), 2: _in_progress()})

    with caplog.at_level(logging.INFO, logger=merge_open_prs.LOGGER.name):
        merge_open_prs.run_automerge_pass([_core_repo(tmp_path)], gate, queue_store=store)

    # PR 2 is approved: ranked, it would have been elected.
    assert store.read()["repositories"]["core-repo"]["active"]["pr"] == 1
    assert "1 later PRs wait" in caplog.text
