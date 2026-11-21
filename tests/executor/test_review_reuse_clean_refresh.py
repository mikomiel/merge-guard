"""What `git` actually says about a lane refresh.

The check in `merge_guard/executor/clean_refresh.py` decides whether a
delegated-reviewer approval issued for `(H1, B1)` still carries merge
authority at a later `(H2, B2)`. Its whole claim is about commit-graph shape
and whitespace-exact diff identity, so the repositories here are built rather
than mocked: a `FakeRunner` answering `rev-list` and `diff` could only restate
the expectation this file exists to check. Every git command the code issues
is a real subprocess against a real repository, through the production
`run_command` runner.

The fixtures deliberately include the near misses, because the failure this
guard is exposed to is approving a refresh that quietly changed content:

* a conflict resolved by hand produces a merge commit with the same shape and
  the same subject as a clean one;
* `main` touching a line three away from the lane's own hunk merges without a
  conflict while changing the diff the reviewer read;
* and — the reason this test does not use `git patch-id` — a merge commit can
  carry a tree that differs from the reviewed content in indentation alone.
  git does not verify a merge commit's tree against its parents, so that
  commit is structurally perfect, and `patch-id` strips whitespace before
  hashing and calls it identical. `test_a_whitespace_tampered_merge_tree_...`
  builds exactly that commit and asserts both halves of the trap.

Every False case asserts its own premise first, so a fixture that stopped
reproducing its near miss fails loudly instead of passing for the wrong
reason.
"""

from __future__ import annotations

import importlib.util as _iu
import subprocess
import sys
from pathlib import Path

import pytest
from gitfixtures import git, git_config_deterministic, rev_parse

SCRIPTS = Path(__file__).resolve().parents[3] / "tools-config" / "scripts"
_spec = _iu.spec_from_file_location(
    "gate_review_reuse_clean_refresh", str(SCRIPTS / "git_maintainer_gate.py")
)
gate = _iu.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


# Twelve numbered lines, so a change can be placed at a chosen distance from
# another one and the merge outcome follows from that distance alone.
_LINES = [f"a{index}\n" for index in range(1, 13)]


def _write_lines(repo: Path, **replacements: str) -> None:
    """Rewrite ``f.txt`` with the named lines replaced (``a7="MAIN"``)."""
    body = "".join(
        replacements.get(line.strip(), line.strip()) + "\n" for line in _LINES
    )
    (repo / "f.txt").write_text(body)


def _commit(repo: Path, message: str) -> str:
    git(["commit", "-qam", message], repo)
    return rev_parse(repo)


def _merge(repo: Path, ref: str) -> subprocess.CompletedProcess[str]:
    """Merge *ref* into the current branch, tolerating a conflict exit."""
    return git(["merge", "--no-edit", ref], repo, check=False)


@pytest.fixture
def lane(tmp_path):
    """A repository on branch ``lane``, one reviewed lane commit ahead of ``main``.

    Returns ``(repo, B1, H1)``: the base the review was taken against and the
    head it was taken at. Each test advances ``main`` its own way and refreshes
    the lane, which is exactly the sequence the gate sees in production.
    """
    repo = tmp_path / "lane-repo"
    repo.mkdir()
    git(["init", "-q", "-b", "main", "."], repo)
    git_config_deterministic(repo)
    _write_lines(repo)
    (repo / "g.txt").write_text("other\n")
    git(["add", "-A"], repo)
    reviewed_base = _commit(repo, "base")

    git(["checkout", "-q", "-b", "lane"], repo)
    _write_lines(repo, a10="LANE")
    reviewed_head = _commit(repo, "lane change")
    return repo, reviewed_base, reviewed_head


def _application(repo: Path, runner=gate.run_command):
    """The production mixin bound to *repo*, and the context it reads.

    The policy on the context is synthetic and unread: the helpers answer from
    the commit graph alone. Policy gating lives one level up and is covered by
    the merge-review tests.
    """
    policy = gate.RepositoryPolicy(
        name="test/clean-refresh",
        remote_url=str(repo),
        managed_checkout=str(repo),
        task_root=str(repo.parent),
        forge="forgejo",
        forge_repository="test/clean-refresh",
    )
    context = gate.WorktreeContext(
        cwd=repo,
        top_level=repo,
        branch="lane",
        origin=str(repo),
        policy=policy,
        registered_worktrees=frozenset({repo}),
    )
    application = gate.operations.GitMaintainerGate(runner=runner, cwd=repo)
    return application, context


def _probe(repo: Path, runner=gate.run_command):
    """The clean-refresh verdict, bound to a real runner on *repo*."""
    application, context = _application(repo, runner)

    def call(*, reviewed_head: str, reviewed_base: str, head_sha: str, base_sha: str) -> bool:
        return application._clean_refresh_preserves_review(
            context,
            reviewed_head=reviewed_head,
            reviewed_base=reviewed_base,
            head_sha=head_sha,
            base_sha=base_sha,
        )

    return call


def _lane_content(repo: Path, base: str, head: str) -> str:
    """The range's diff with only hunk headers and blob ids normalized away.

    The fixture's own statement of what "the same lane content" means, written
    out here rather than borrowed from `_range_diff_digest`: a premise
    assertion derived from the implementation it is checking would follow that
    implementation into any mistake it makes. Returned as text and not hashed,
    so an unexpected difference is readable in the failure output.
    """
    diff = git(["diff", "--no-ext-diff", "--no-textconv", base, head], repo)
    return "\n".join(
        "@@" if line.startswith("@@ ") else line
        for line in diff.stdout.split("\n")
        if not line.startswith("index ")
    )


def _patch_id(repo: Path, base: str, head: str) -> str:
    """What the *rejected* implementation would have answered.

    `git patch-id` was the first content test here and is kept only as a
    contrary control: the whitespace-tamper case below asserts that patch-id
    calls the two ranges identical while the shipped comparison refuses them.
    Without this the test could not show that the change bought anything.
    """
    diff = git(["diff", "--no-ext-diff", "--no-textconv", base, head], repo)
    computed = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=str(repo), input=diff.stdout, capture_output=True, text=True, check=True,
    )
    return computed.stdout.split()[0] if computed.stdout.split() else ""


def test_clean_merge_of_advanced_main_preserves_the_review(lane):
    """The case the whole feature exists for: `main` moved, the lane's own
    diff did not, and `git merge` produced one two-parent commit.

    `main` changes `g.txt`, which the reviewed range never touched, so the
    reviewer's files are identical at both heads. The reviewed-files condition
    has to leave this refresh admitted: the narrowing must not become a
    blanket refusal of every advanced base.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("other-changed\n")
    base_sha = _commit(repo, "unrelated main change")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: this is a genuine refresh, not a no-op, and the lane's diff
    # really is unchanged.
    assert head_sha not in {reviewed_head, base_sha}
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is True


def test_two_consecutive_clean_refreshes_still_preserve_the_review(lane):
    """One approval survives a chain: the first refresh's merge commit is a
    permitted first parent for the second's, so a lane that waited through two
    other merges is not re-reviewed."""
    repo, reviewed_base, reviewed_head = lane

    for index, content in enumerate(("main-one\n", "main-two\n")):
        git(["checkout", "-q", "main"], repo)
        (repo / "g.txt").write_text(content)
        base_sha = _commit(repo, f"main change {index}")
        git(["checkout", "-q", "lane"], repo)
        assert _merge(repo, "main").returncode == 0
        head_sha = rev_parse(repo)

    # Premise: two merge commits were added, not one.
    added = git(["rev-list", head_sha, f"^{reviewed_head}", f"^{base_sha}"], repo)
    assert len(added.stdout.split()) == 2

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is True


def test_the_refresh_chain_is_bounded_at_its_declared_ceiling(lane):
    """`_CLEAN_REFRESH_MAX_MERGES` read from both sides.

    The bound is a cost ceiling — each admitted commit costs two extra git
    probes — so the contract is that the last permitted chain still reuses and
    the first over-long one does not. Both are asserted here, and the patch
    ids are asserted equal throughout, so the refusal at the ceiling cannot be
    mistaken for a content refusal.
    """
    from git_gate import forge

    ceiling = forge._CLEAN_REFRESH_MAX_MERGES
    repo, reviewed_base, reviewed_head = lane
    probe = _probe(repo)

    def _refresh(index):
        git(["checkout", "-q", "main"], repo)
        (repo / "g.txt").write_text(f"main-{index}\n")
        base = _commit(repo, f"main change {index}")
        git(["checkout", "-q", "lane"], repo)
        assert _merge(repo, "main").returncode == 0
        return base, rev_parse(repo)

    for index in range(ceiling):
        base_sha, head_sha = _refresh(index)
    assert len(
        git(["rev-list", head_sha, f"^{reviewed_head}", f"^{base_sha}"], repo).stdout.split()
    ) == ceiling
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )
    assert probe(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is True

    base_sha, head_sha = _refresh(ceiling)

    # Premise: the lane's own diff is still byte-identical, so only the length
    # of the chain can be what refuses this one.
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )
    assert probe(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_lane_commit_after_the_refresh_ends_the_reuse(lane):
    """New lane work is new content, whatever the patch id of the merge was.
    A one-parent commit in the added set fails the structural test outright."""
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("other-changed\n")
    base_sha = _commit(repo, "unrelated main change")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    _write_lines(repo, a10="LANE", a2="LATER")
    head_sha = _commit(repo, "more lane work")

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_hand_resolved_conflict_ends_the_reuse(lane):
    """The near miss the structural test alone cannot catch.

    A publish that commits a hand-resolved merge has the same two
    parents and the same subject as a clean one. Only the content separates
    them, and the premise below asserts that this fixture really did produce
    the identical shape.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    _write_lines(repo, a10="MAIN-TOOK-THE-SAME-LINE")
    base_sha = _commit(repo, "main edits the lane's own line")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode != 0
    _write_lines(repo, a10="LANE-AND-MAIN-RECONCILED")
    git(["add", "-A"], repo)
    git(["commit", "-q", "--no-edit"], repo)
    head_sha = rev_parse(repo)

    # Premise: structurally indistinguishable from the clean case — one added
    # commit, two parents, the reviewed head first — so only content decides.
    added = git(["rev-list", head_sha, f"^{reviewed_head}", f"^{base_sha}"], repo).stdout.split()
    assert added == [head_sha]
    assert git(["rev-list", "--parents", "-n", "1", head_sha], repo).stdout.split() == [
        head_sha, reviewed_head, base_sha,
    ]
    assert _lane_content(repo, reviewed_base, reviewed_head) != _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_fast_forward_refresh_is_not_a_reusable_head(lane):
    """A lane fast-forwarded onto a base that already contains it has no diff
    of its own left. `head_sha == base_sha` is refused before any probe runs,
    rather than being asked to compare an empty range."""
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    assert _merge(repo, "lane").returncode == 0
    base_sha = rev_parse(repo)
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: it really was a fast-forward, and the reviewed head is still
    # contained, so only the equality rule can be what refuses this.
    assert head_sha == base_sha
    assert git(
        ["merge-base", "--is-ancestor", reviewed_head, head_sha], repo, check=False
    ).returncode == 0

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_clean_merge_that_moved_the_diff_context_ends_the_reuse(lane):
    """The conservative boundary, documented by a test rather than by prose.

    `main` changes a line three away from the lane's own hunk: close enough to
    fall inside the diff's three-line context, far enough that `git merge`
    reports no conflict. The lane's net effect on the file is unchanged, and
    the reuse is refused anyway, because `patch-id` hashes context lines. A
    false negative costs one review round; a false positive merges a diff
    nobody read.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    _write_lines(repo, a7="MAIN")
    base_sha = _commit(repo, "main changes a nearby line")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: a clean merge with no conflict markers left behind, whose
    # patch ids nonetheless differ. If git's merge or context width ever
    # changed enough to make this a conflict, this assertion says so instead
    # of the test quietly becoming a duplicate of the conflict case above.
    assert "<<<<<<<" not in (repo / "f.txt").read_text()
    assert git(["status", "--porcelain"], repo).stdout == ""
    assert _lane_content(repo, reviewed_base, reviewed_head) != _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_reviewed_base_outside_the_current_base_ends_the_reuse(lane):
    """The recovered B1 must be contained in the live base.

    A marker naming a base the current base does not contain is not a base
    this lane moved forward from — it is a different history, or a tampered
    marker. Either way the graph cannot vouch for it.
    """
    repo, _reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("other-changed\n")
    base_sha = _commit(repo, "unrelated main change")
    git(["checkout", "-q", "-b", "sideline", base_sha], repo)
    (repo / "h.txt").write_text("sideline\n")
    git(["add", "-A"], repo)
    foreign_base = _commit(repo, "commit on an unrelated line of history")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: the structural and content halves would otherwise both pass —
    # this is the clean-refresh case with one field changed.
    assert git(
        ["merge-base", "--is-ancestor", foreign_base, base_sha], repo, check=False
    ).returncode != 0

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=foreign_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_whitespace_tampered_merge_tree_ends_the_reuse(lane):
    """The case that replaced `git patch-id`, built as a real commit.

    git does not verify a merge commit's tree against its parents, so
    `git commit-tree <tree> -p H1 -p B2` produces a commit that passes every
    structural test this guard applies while carrying whatever content its
    author chose. Here that content is the clean merge's tree with one line
    inside the lane's own hunk re-indented — nothing else. In Python, YAML or
    a Makefile that is a semantic change the reviewer never saw.

    Two premises make the point, and the test is worthless without both:
    `patch-id` calls the two ranges identical, which is what the rejected
    implementation would have merged on; and the tampered tree differs from
    the clean merge tree in whitespace alone, which is what makes this the
    whitespace case rather than an ordinary content change.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("other-changed\n")
    base_sha = _commit(repo, "unrelated main change")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    clean_head = rev_parse(repo)

    # The tampered tree, built on a scratch branch so the lane is untouched.
    git(["checkout", "-q", "-b", "tamper", clean_head], repo)
    _write_lines(repo, a10="    LANE")
    tampered = _commit(repo, "re-indent the lane's own line")
    tampered_tree = git(["rev-parse", f"{tampered}^{{tree}}"], repo).stdout.strip()
    git(["checkout", "-q", "lane"], repo)
    head_sha = git(
        ["commit-tree", tampered_tree, "-p", reviewed_head, "-p", base_sha,
         "-m", "Merge branch 'main' into lane"],
        repo,
    ).stdout.strip()

    # Premise one: the commit is structurally a textbook clean refresh.
    added = git(["rev-list", head_sha, f"^{reviewed_head}", f"^{base_sha}"], repo).stdout.split()
    assert added == [head_sha]
    assert git(["rev-list", "--parents", "-n", "1", head_sha], repo).stdout.split() == [
        head_sha, reviewed_head, base_sha,
    ]

    # Premise two: `git patch-id` — the rejected content test — sees no
    # difference at all, so a check built on it would authorize this merge.
    assert _patch_id(repo, reviewed_base, reviewed_head) == _patch_id(
        repo, base_sha, head_sha
    )

    # Premise three: the tree really does differ from the clean merge in
    # whitespace alone, so this is the whitespace case and not a content
    # change that happens to collide.
    assert git(["diff", "--quiet", clean_head, head_sha], repo, check=False).returncode != 0
    assert git(
        ["diff", "-w", "--quiet", clean_head, head_sha], repo, check=False
    ).returncode == 0

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_refresh_that_only_moved_the_lane_hunk_requires_a_fresh_review(lane):
    """A position-only shift still changes the file the reviewer read.

    `main` prepends lines above the lane's hunk. The lane's own change is
    untouched, but every `@@` header shifts and the preimage blob id changes,
    which is exactly what the digest normalizes away; the reviewed range and
    the refreshed range are therefore content-identical. The reviewed-file
    test does not normalize it: the file at the refreshed head is not the file
    the reviewer saw, and no diff-level check can tell an inert shift from a
    semantic one. This refusal is the deliberate cost of the narrowing.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / "f.txt").write_text(
        "prelude-1\nprelude-2\n" + (repo / "f.txt").read_text()
    )
    base_sha = _commit(repo, "main prepends lines above the lane hunk")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: the raw diffs differ, yet the range content is identical after
    # the permitted normalization. So the refusal below comes only from the
    # reviewed-file test, not from the digest.
    reviewed_raw = git(
        ["diff", "--no-ext-diff", "--no-textconv", reviewed_base, reviewed_head], repo
    ).stdout
    current_raw = git(
        ["diff", "--no-ext-diff", "--no-textconv", base_sha, head_sha], repo
    ).stdout
    assert reviewed_raw != current_raw
    assert "@@ -7," in reviewed_raw and "@@ -9," in current_raw
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


# A blob git reports as binary. The NUL byte is what makes git say so, and
# the payload is shaped like a PNG header only to look like the real thing.
_BINARY_REVIEWED = b"\x89PNG\x00\x01"
_BINARY_TAMPERED = b"\x89PNG\x00\x03"


@pytest.fixture
def binary_lane(tmp_path):
    """A lane whose reviewed change is to a binary path.

    Returns ``(repo, B1, H1)`` like `lane`, but the lane commit rewrites
    ``img.bin`` rather than a text file.
    """
    repo = tmp_path / "binary-lane-repo"
    repo.mkdir()
    git(["init", "-q", "-b", "main", "."], repo)
    git_config_deterministic(repo)
    (repo / "img.bin").write_bytes(_BINARY_REVIEWED)
    (repo / "g.txt").write_text("other\n")
    git(["add", "-A"], repo)
    reviewed_base = _commit(repo, "base")

    git(["checkout", "-q", "-b", "lane"], repo)
    (repo / "img.bin").write_bytes(b"\x89PNG\x00\x02")
    reviewed_head = _commit(repo, "lane changes the binary asset")
    return repo, reviewed_base, reviewed_head


def test_a_lane_touching_a_binary_path_never_reuses_its_review(binary_lane):
    """A structurally perfect, genuinely clean refresh is still refused when
    the range touches a binary path.

    `git diff` without `--binary` prints one `Binary files ... differ` line
    and no bytes, so the digest cannot see what changed. It must not vouch
    for content it did not read, and the cost is one fresh review for any
    lane that touches a binary asset.
    """
    repo, reviewed_base, reviewed_head = binary_lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("other-changed\n")
    base_sha = _commit(repo, "unrelated main change")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: git really does report this range as binary, and the refresh
    # really is otherwise clean -- the same fixture with a text file returns
    # True in `test_clean_merge_of_advanced_main_preserves_the_review`.
    reviewed_diff = git(
        ["diff", "--no-ext-diff", "--no-textconv", reviewed_base, reviewed_head], repo
    ).stdout
    assert "Binary files " in reviewed_diff
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_merge_tree_that_changed_only_binary_bytes_ends_the_reuse(binary_lane):
    """The attack the binary refusal exists to stop.

    Same `git commit-tree` construction as the whitespace case: a merge
    commit with the reviewed head and the new base as parents, carrying a
    tree whose only difference from the clean merge is the bytes of a binary
    blob. The premise below shows the two normalized diffs are byte-identical
    — the `index` line that distinguished them is exactly what the digest
    drops, so nothing but refusing binary paths outright can catch this.
    """
    repo, reviewed_base, reviewed_head = binary_lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("other-changed\n")
    base_sha = _commit(repo, "unrelated main change")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    clean_head = rev_parse(repo)

    git(["checkout", "-q", "-b", "tamper", clean_head], repo)
    (repo / "img.bin").write_bytes(_BINARY_TAMPERED)
    tampered = _commit(repo, "swap the binary payload")
    tampered_tree = git(["rev-parse", f"{tampered}^{{tree}}"], repo).stdout.strip()
    git(["checkout", "-q", "lane"], repo)
    head_sha = git(
        ["commit-tree", tampered_tree, "-p", reviewed_head, "-p", base_sha,
         "-m", "Merge branch 'main' into lane"],
        repo,
    ).stdout.strip()

    # Premise one: structurally a textbook clean refresh.
    assert git(
        ["rev-list", head_sha, f"^{reviewed_head}", f"^{base_sha}"], repo
    ).stdout.split() == [head_sha]
    assert git(["rev-list", "--parents", "-n", "1", head_sha], repo).stdout.split() == [
        head_sha, reviewed_head, base_sha,
    ]

    # Premise two: the merged tree really does carry different bytes.
    # Compared by blob id rather than by content, because `git cat-file blob`
    # through the text-mode fixture runner cannot even decode them -- which
    # is the point in miniature: those bytes never reach the digest either.
    assert git(["rev-parse", f"{clean_head}:img.bin"], repo).stdout.strip() != git(
        ["rev-parse", f"{head_sha}:img.bin"], repo
    ).stdout.strip()

    # Premise three: and yet the normalized diffs are byte-identical. The
    # `index` line that told them apart is exactly what the digest drops, so
    # nothing short of refusing binary paths outright can catch this.
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_gitattributes_nodiff_path_is_treated_as_binary(lane):
    """`.gitattributes` can make git report a *text* path the same way.

    A `-diff` attribute produces the identical `Binary files ... differ`
    output, so the same refusal has to cover it -- otherwise a repository
    could opt a path out of the content test by configuration alone.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / ".gitattributes").write_text("f.txt -diff\n")
    (repo / "g.txt").write_text("other-changed\n")
    git(["add", "-A"], repo)
    base_sha = _commit(repo, "main marks the lane's file as non-diffable")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: the attribute is in force for the current range.
    assert "Binary files " in git(
        ["diff", "--no-ext-diff", "--no-textconv", base_sha, head_sha], repo
    ).stdout

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_base_change_elsewhere_in_a_reviewed_file_ends_the_reuse(lane):
    """The digest's blind spot: `main` changes a far line of a reviewed file.

    The lane's hunk is at `a10` and the change is at `a1`, so it never enters
    the diff's three-line context. The range digest is byte-identical and the
    merge is clean, which is exactly why the reviewed-file comparison has to
    exist: without it this refresh reuses an approval for a file whose content
    the reviewer did not see.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    _write_lines(repo, a1="MAIN")
    base_sha = _commit(repo, "main changes a far line")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    # Premise: a clean merge whose ranges are content-identical, so the digest
    # alone would admit this refresh.
    assert "<<<<<<<" not in (repo / "f.txt").read_text()
    assert git(["status", "--porcelain"], repo).stdout == ""
    assert _lane_content(repo, reviewed_base, reviewed_head) == _lane_content(
        repo, base_sha, head_sha
    )

    assert _probe(repo)(
        reviewed_head=reviewed_head, reviewed_base=reviewed_base,
        head_sha=head_sha, base_sha=base_sha,
    ) is False


def test_a_rename_contributes_both_names(lane):
    """`-M` exists because `main` may change the name the reviewer saw.

    The reviewed range renames `f.txt` to `renamed.txt`, so rename detection
    pairs them. Both names have to be in the set: the old one is what the
    reviewer saw and what `main` could have changed, the new one is what the
    refreshed head carries. A list with only the new name, or with a quoted
    name, would miss the old one.
    """
    repo, reviewed_base, _ = lane

    git(["checkout", "-q", "lane"], repo)
    (repo / "f.txt").rename(repo / "renamed.txt")
    git(["add", "-A"], repo)
    head = _commit(repo, "rename the lane file")

    application, context = _application(repo)

    paths = application._reviewed_paths(context, reviewed_base, head)

    assert paths is not None
    assert {"f.txt", "renamed.txt"} <= paths


def test_a_path_containing_a_newline_survives_the_file_list(lane):
    """`-z` exists because a path can contain a newline.

    Without NUL separation git quotes the name and escapes the newline, so the
    set would hold a different string and the path would never match the
    refreshed head. The name is asserted byte for byte.
    """
    repo, reviewed_base, _ = lane

    git(["checkout", "-q", "lane"], repo)
    name = "line\nbreak.txt"
    (repo / name).write_text("content\n")
    git(["add", "-A"], repo)
    head = _commit(repo, "add a path with a newline")

    application, context = _application(repo)

    paths = application._reviewed_paths(context, reviewed_base, head)

    assert paths is not None
    assert name in paths


def test_an_unreadable_file_list_refuses_the_reuse(lane):
    """The "cannot be compared" case: a failed read never admits.

    A probe that could not run must degrade to "no reuse", not to a grant, so
    the runner below fails only the `--name-status` call and the verdict is
    still False.
    """
    repo, reviewed_base, reviewed_head = lane

    git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("main changed this\n")
    _commit(repo, "main changes an unrelated file")
    git(["checkout", "-q", "lane"], repo)
    assert _merge(repo, "main").returncode == 0
    head_sha = rev_parse(repo)

    def failing(argv, cwd, **_kwargs):
        if "--name-status" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        return gate.run_command(argv, cwd)

    application, context = _application(repo, failing)

    assert application._reviewed_files_unchanged(
        context,
        reviewed_base=reviewed_base,
        reviewed_head=reviewed_head,
        head_sha=head_sha,
    ) is False
