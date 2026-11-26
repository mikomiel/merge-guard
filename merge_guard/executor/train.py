"""Batched merge train: build, per-member merge, and close (``train-v1``, ``train-v2``).

The automerge executor asks; this module decides. A train is up to three
queued PRs merged in queue order onto canonical main as a chain of candidate
commits ``candidate_1..candidate_n`` on a ``train/<main12>-<first PR>``
branch. Each candidate has an immutable draft PR whose trusted
``pull_request_target`` run judges that prefix on the original base. Every operation re-derives the train from git and
the forge under the repository lock and trusts nothing the executor
persisted: the chain is read back from the train branch's own commits, CI is
the member prefix PR's trusted run bound to its candidate and the train base,
and each member's approval reaches its position only through the
whitespace-exact range digest ``_range_diff_digest`` already owns.

A ``train-v2`` owner is named by its batch: ``train/<main12>-<first PR>-g<G>``
where ``G`` (``_train_generation``) hashes the full base and the sorted member
heads. The name is therefore computable, and a build finds every earlier owner
of the same member-head set by reading the batch's own variant names (one
exact-head PR lookup and one ``git ls-remote`` of those names, under the
repository lock), never the repository's PR history.

A member lands through its own PR with the ordinary expected-head forge
merge, and only while canonical main carries exactly the tree of the
candidate before it; afterwards main must carry exactly the tree of the
member's own candidate, or the executor stops (``TRAIN_TREE_MISMATCH``).

``merge_pr`` and ``automerge_pr_by_index`` (the gate's merge operations) never
read any of this: train evidence is accepted by ``train_member_merge`` alone.

Candidates are built with ``git merge-tree --write-tree`` and ``git
commit-tree`` in the managed checkout's object store (``_auto_merge``, the
resolution recompute, is the shared merge): the same two-parent merge ``git
merge --no-ff`` records, without touching the working checkout's index,
worktree, or local refs.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import replace
from typing import NoReturn

from .. import catalog, ci
from ..contracts import (
    GIT_PATH,
    MAIN_BRANCH,
    PAGE_SIZE,
    SHA_RE,
    TRAIN_BUILD,
    TRAIN_CLOSE,
    TRAIN_MEMBER_MERGE,
    TRAIN_V1_POLICY,
    TRAIN_V2_POLICY,
    GateError,
    fail,
    fail_with,
)
from ..forge import _http_status, _response_is_2xx
from ..pr_labels import TRAIN_LABEL
from ..repository import PullRequest, WorktreeContext, _lock_phase
from .landing import _recorded_base

TRAIN_POLICY = TRAIN_V1_POLICY
TRAIN_BRANCH_PREFIX = "train/"
TRAIN_MAX_MEMBERS = 3
# A closed owner whose selected run reads one of these will never get a
# verdict: the same member set returns to serial instead of parking.
_TRAIN_LOST_OWNER_REASONS = frozenset({"CI_RUN_CANCELED", "CI_RUN_SKIPPED", "CI_RUN_MISSING"})
# CI_RUN_FAILED without a strict full-C1 failure
# (`ci.terminal_full_failure`) is lost too, as the executor retires it.
_TRAIN_NONSTRICT_RED_REASON = "CI_RUN_FAILED"
# An exact-name row is discarded as another repository's only when it names one.
_TRAIN_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Forgejo has no draft flag; the WIP title prefix is its draft.
TRAIN_TITLE_PREFIX = "WIP: merge-train"
# The two branch contracts never match each other: a v1 name carries no
# generation and a v2 name always does. Named groups keep the chain walker
# independent of the mode.
_TRAIN_BRANCH_RES = {
    TRAIN_POLICY: re.compile(
        r"train/(?P<base>[0-9a-f]{12})-(?P<pr>[1-9][0-9]{0,8})(?:-prefix-(?P<prefix>[12]))?\Z"
    ),
    TRAIN_V2_POLICY: re.compile(
        r"train/(?P<base>[0-9a-f]{12})-(?P<pr>[1-9][0-9]{0,8})-g(?P<generation>[0-9a-f]{32})"
        r"(?:-prefix-(?P<prefix>[12]))?\Z"
    ),
}
_TRAIN_MERGE_SUBJECT_RE = re.compile(r"Merge PR #([1-9][0-9]{0,8}) into train\Z")
# The frozen claim and review provenance identities a v2 train link records.
_TRAIN_LINK_CLAIM_RE = re.compile(r"^Train-Claim: ([0-9a-f]{64})$", re.MULTILINE)
_TRAIN_LINK_REVIEW_RE = re.compile(r"^Train-Review: ([0-9a-f]{64})$", re.MULTILINE)
_CLOSE_REASON_RE = re.compile(r"[A-Za-z0-9_#:,. -]{1,200}\Z")
# the reason the executor closes a v2 owner with once its CI run is
# lost (`merge_open_prs._TRAIN_OWNER_LOST`); the code names which loss it read.
_TRAIN_OWNER_LOST_CLOSE_RE = re.compile(
    r"TRAIN_OWNER_CI_LOST: CI_RUN_(?:CANCELED|SKIPPED|MISSING|FAILED)\Z")
# A fixed identity: the managed checkout carries no committer of its own.
_TRAIN_IDENT = ["-c", "user.name=merge-train", "-c", "user.email=merge-train@example.invalid"]


def _train_branch_parts(branch: str) -> tuple[str, dict[str, str | None]] | None:
    """The owning mode and named parts of a recognized train branch, or None."""
    for mode, expression in _TRAIN_BRANCH_RES.items():
        found = expression.fullmatch(branch)
        if found is not None:
            return mode, found.groupdict()
    return None


def _train_link_claim(body: str) -> str | None:
    """The one frozen claim identity a v2 train link records, or None.

    Exactly one trailer is allowed. A missing, duplicated or malformed line
    refuses in the chain walker, so a v2 link that did not freeze a claim
    cannot pass for a generation that did.
    """
    claims = _TRAIN_LINK_CLAIM_RE.findall(body)
    return claims[0].lower() if len(claims) == 1 else None


def _train_link_review(body: str) -> str | None:
    """The one frozen review-provenance identity a v2 train link records."""
    found = _TRAIN_LINK_REVIEW_RE.findall(body)
    return found[0].lower() if len(found) == 1 else None


def _train_branch_mode(branch: str) -> str | None:
    parts = _train_branch_parts(branch)
    return None if parts is None else parts[0]


def _train_stem(branch: str) -> str:
    """The logical family stem of a recognized train branch."""
    parts = _train_branch_parts(branch)
    if parts is None:
        fail("TRAIN_INVALID")
    _mode, named = parts
    return f"{TRAIN_BRANCH_PREFIX}{named['base']}-{named['pr']}"


def _train_family_of(branch: str) -> str:
    """The physical owner branch of *branch*: its family with any prefix stripped."""
    parts = _train_branch_parts(branch)
    if parts is None:
        fail("TRAIN_INVALID")
    mode, named = parts
    stem = _train_stem(branch)
    return f"{stem}-g{named['generation']}" if mode == TRAIN_V2_POLICY else stem


def _train_generation(base: str, heads: Iterable[str]) -> str:
    """The deterministic generation ``G`` of one batch on one base.

    ``G`` is the first 32 hexadecimal characters of the SHA-256 digest of
    this byte string, where every SHA is its 40 lowercase hexadecimal ASCII
    characters (the text, never the raw 20 bytes) and ``\\0`` is the NUL byte::

        b"train-v2\\0" + <full base SHA> + b"\\0" + b"\\n".join(<head SHAs, sorted as text>)

    The heads are sorted, so ``G`` does not depend on member order; it changes
    with the base and with any one member head. A build passes the heads of
    the members that survive admission (``kept``); the reader of an existing
    owner passes the heads of that owner's own validated chain, and the name
    is deterministic only when the result equals the name's suffix.
    """
    payload = (b"train-v2\0" + base.encode("ascii") + b"\0"
               + "\n".join(sorted(heads)).encode("ascii"))
    return hashlib.sha256(payload).hexdigest()[:32]


def _train_variants(base: str, kept: list[dict[str, object]]) -> list[str]:
    """The owner names this batch can carry: one per kept member, the batch's own first.

    A variant takes one kept member's PR as the first PR. Every order of the
    same member-head set on the same base shares ``G``, so a reordered request
    finds an earlier owner of the set through the variant that names the
    earlier first PR. At most ``TRAIN_MAX_MEMBERS`` names.
    """
    generation = _train_generation(base, (str(member["head"]) for member in kept))
    return [f"{TRAIN_BRANCH_PREFIX}{base[:12]}-{member['pr']}-g{generation}"
            for member in kept]


def _train_chain_is(
    chain: list[dict[str, object]], kept: list[dict[str, object]],
) -> bool:
    """Whether a generation's own chain is exactly this batch: order, claims and reviews.

    The candidate SHAs of a rebuild differ from the ref's own (the commit
    identity and message are fixed, but not the committer time), so equality
    ignores them: an adoption returns the ref's chain, because the executor's
    run and the reported SHA must name one commit.
    """
    return [(member["pr"], member["head"], member.get("claim"), member.get("review"))
            for member in chain] == [
            (member["pr"], member["head"], member.get("claim"), member.get("review"))
            for member in kept]


def _train_mode_of(policy: object) -> str:
    mode = catalog.merge_train_mode(policy)
    if not mode:
        fail("TRAIN_POLICY_DISABLED")
    return mode


def _request_pr(request: dict[str, object], key: str) -> int:
    value = request.get(key)
    if type(value) is not int or value <= 0:
        fail("USAGE")
    return value


def _request_sha(request: dict[str, object], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or len(value) != 40 or not SHA_RE.fullmatch(value):
        fail("USAGE")
    return value.lower()


def _train_request(request: dict[str, object]) -> tuple[str, list[tuple[int, str]]]:
    """The expected base and ordered ``(pr, head)`` members of a build request."""
    expected_base = _request_sha(request, "expected_base")
    raw_members = request.get("members")
    if (not isinstance(raw_members, list)
            or not 2 <= len(raw_members) <= TRAIN_MAX_MEMBERS
            or not all(isinstance(item, dict) for item in raw_members)):
        fail("USAGE")
    members = [(_request_pr(item, "pr"), _request_sha(item, "head")) for item in raw_members]
    if len({pr for pr, _head in members}) != len(members):
        fail("USAGE")
    return expected_base, members


def _backend_json(result: subprocess.CompletedProcess[str]) -> object:
    """The decoded JSON body of a successful backend read, else None."""
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


class TrainOperationsMixin:
    """Merge-train build, member merge, close and read (mixed into the gate class)."""

    # ------------------------------------------------------------ build

    def train_build(
        self, context: WorktreeContext, request: dict[str, object],
    ) -> dict[str, object]:
        """Build, push and open one train from the named members, in order.

        The repository's policy selects one of two evidence contracts. v1
        publishes an immutable draft PR for every prefix up front and admits a
        member on trusted successful CI on its own head: green on any base, or
        a narrowed ``checks/c2`` run on the current base that the snapshot
        review accepts (``ci_review_accepted``); the train's own run is the
        merge evidence. v2 publishes only the full owner and admits members on
        trusted narrowed runs. Both re-inspect each member through
        ``inspect_pr_by_index`` (open, not a draft, based on main, head
        unchanged); its approval must reach its candidate position by digest.
        A member that fails, or whose merge onto the candidate so far
        conflicts, is dropped and named in ``dropped``; fewer than two
        survivors is ``TRAIN_TOO_SHORT`` before anything leaves the host.
        """
        mode = _train_mode_of(context.policy)
        expected_base, members = _train_request(request)
        _lock_phase("train-base")
        base = self._train_expected_base(context, expected_base)
        if mode == TRAIN_V2_POLICY:
            return self._train_build_v2(context, base, members)
        return self._train_build_v1(context, base, members)

    def _train_build_v1(
        self, context: WorktreeContext, base: str, members: list[tuple[int, str]]
    ) -> dict[str, object]:
        """The v1 contract: one immutable draft PR per prefix, built up front."""
        _lock_phase("train-members")
        kept: list[dict[str, object]] = []
        dropped: list[dict[str, object]] = []
        parent = base
        for pr, head in members:
            reason = self._train_member_refusal(context, pr, head)
            candidate = None if reason is not None else self._train_link(context, parent, pr, head)
            if reason is None and candidate is None:
                reason = "PR_REFRESH_CONFLICT"
            if candidate is not None and self._train_approval(
                    context, pr, head, parent, candidate) is None:
                reason = "PR_REVIEW_NOT_APPROVED"
            if reason is not None or candidate is None:
                dropped.append({"pr": pr, "reason": reason})
                continue
            kept.append({"pr": pr, "head": head, "candidate": candidate})
            parent = candidate
        if len(kept) < 2:
            fail_with("TRAIN_TOO_SHORT", dropped=dropped)

        branch = f"{TRAIN_BRANCH_PREFIX}{base[:12]}-{kept[0]['pr']}"
        self._refresh_forge_auth(context.cwd, context.policy)
        # A failed earlier build may have published a branch before it had a
        # PR or before the executor could save its result. Reconcile exactly
        # this family under the repository lock before creating new refs.
        if self._train_remote_refs(context, branch):
            self._train_cleanup_family(context, branch, "TRAIN_BUILD_RECOVERY")
        prefixes: list[dict[str, object]] = []
        descriptions_written = True
        pull = None
        try:
            # Publish the final owner first. A crash while creating siblings
            # then leaves an open family PR that ordinary orphan cleanup sees.
            for position in [len(kept), *range(1, len(kept))]:
                prefix_branch = self._train_prefix_branch(branch, position, len(kept))
                prefix = kept[:position]
                prefix_pull, described = self._train_publish_prefix(
                    context, prefix_branch, base, prefix
                )
                descriptions_written = descriptions_written and described
                if position == len(kept):
                    pull = prefix_pull
                prefixes.append({"position": position, "pr": prefix_pull.index,
                                 "branch": prefix_branch, "candidate": prefix[-1]["candidate"]})
        except GateError as exc:
            try:
                self._train_cleanup_family(context, branch, "TRAIN_BUILD_FAILED")
            except GateError as cleanup:
                recovery = ({"train_pr": pull.index, "train_branch": branch,
                             "base_sha": base, "members": kept}
                            if pull is not None else {})
                fail_with("TRAIN_BUILD_FAILED", **exc.context, **recovery,
                          cleanup_failed=cleanup.code, cleanup_detail=cleanup.context)
            raise
        assert pull is not None
        prefixes.sort(key=lambda row: int(row["position"]))
        success = self._success(replace(context, branch=branch), TRAIN_BUILD, pull.index)
        success.update({
            "code": "TRAIN_BUILT",
            "train_pr": pull.index,
            "train_branch": branch,
            "base_sha": base,
            "candidate_sha": parent,
            "candidate_tree": self._train_tree(context, parent),
            "members": kept,
            "prefixes": prefixes,
            "dropped": dropped,
            "description_written": descriptions_written,
        })
        return success

    def _train_build_v2(
        self, context: WorktreeContext, base: str, members: list[tuple[int, str]],
    ) -> dict[str, object]:
        """The v2 contract: one immutable full owner, no prefixes before red.

        Admission accepts a trusted narrowed run in place of a member's own
        full C1. A member whose changed paths intersect an earlier selected
        member's (``PR_TRAIN_OVERLAP``), or that changes the trusted workflow
        file (``PR_TRAIN_WORKFLOW``), is dropped and keeps the serial path;
        conflict, review and digest checks are unchanged. Each admitted
        member's review claim identity is frozen into its link commit,
        so a later claim change cannot ride this generation's approval. Only
        the full owner candidate is published, under the batch's deterministic
        generation (``_train_generation``), and only when no earlier owner of
        the same member-head set on this base has a PR or a ref.
        """
        _lock_phase("train-members")
        kept: list[dict[str, object]] = []
        dropped: list[dict[str, object]] = []
        parent = base
        selected_paths: set[str] = set()
        for pr, head in members:
            reason = self._train_member_refusal(context, pr, head, mode=TRAIN_V2_POLICY)
            member_paths = (None if reason is not None
                            else self._train_changed_paths(context, base, head))
            if reason is None and member_paths is None:
                fail_with("TRAIN_BUILD_FAILED", step="changed_paths", pull_index=pr)
            if reason is None and ci.FORGEJO_LAUNCHER_PATH in member_paths:
                reason = "PR_TRAIN_WORKFLOW"
            if reason is None and member_paths & selected_paths:
                reason = "PR_TRAIN_OVERLAP"
            claim = (None if reason is not None
                     else self._train_member_claim(context, pr, head))
            if reason is None and claim is None:
                reason = "PR_CLAIM_UNAVAILABLE"
            merged_tree: str | None = None
            if reason is None:
                try:
                    merged_tree, conflicting = self._auto_merge(context.cwd, parent, head)
                except GateError:
                    fail_with("TRAIN_BUILD_FAILED", step="merge", pull_index=pr)
                if conflicting:
                    reason = "PR_REFRESH_CONFLICT"
            approval = None
            if reason is None and merged_tree is not None:
                approval = self._train_approval(context, pr, head, parent, merged_tree)
                if approval is None:
                    reason = "PR_REVIEW_NOT_APPROVED"
            candidate = None
            if reason is None and approval is not None:
                candidate = self._train_link(
                    context, parent, pr, head, tree=merged_tree, claim=claim,
                    review=approval[1])
            if reason is not None or candidate is None or approval is None:
                dropped.append({"pr": pr, "reason": reason})
                continue
            kept.append({"pr": pr, "head": head, "candidate": candidate,
                         "claim": claim, "review": approval[1]})
            selected_paths |= member_paths
            parent = candidate
        if len(kept) < 2:
            fail_with("TRAIN_TOO_SHORT", dropped=dropped)

        own_pull, own_tip = self._train_closed_batch_guard(context, base, kept)
        stem = f"{TRAIN_BRANCH_PREFIX}{base[:12]}-{kept[0]['pr']}"
        self._refresh_forge_auth(context.cwd, context.policy)
        try:
            branch, pull, described, members = self._train_open_v2(
                context, base, kept, own_pull=own_pull, own_tip=own_tip)
        except (GateError, OSError, subprocess.SubprocessError) as exc:
            self._train_reconcile_refusal(exc)
        candidate = str(members[-1]["candidate"])
        success = self._success(replace(context, branch=branch), TRAIN_BUILD, pull.index)
        success.update({
            "code": "TRAIN_BUILT",
            "mode": "full",
            "generation": branch.removeprefix(f"{stem}-g"),
            "train_pr": pull.index,
            "train_branch": branch,
            "base_sha": base,
            "candidate_sha": candidate,
            "candidate_tree": self._train_tree(context, candidate),
            "members": members,
            # Every member's admission proof is the one owner run, so each
            # position projects the same owner PR and its own candidate tree.
            "prefixes": [
                {"position": position, "pr": pull.index, "branch": branch,
                 "candidate": member["candidate"]}
                for position, member in enumerate(members, 1)
            ],
            "dropped": dropped,
            "description_written": described,
        })
        return success

    def _train_closed_batch_guard(
        self, context: WorktreeContext, base: str, kept: list[dict[str, object]],
    ) -> tuple[int | None, str | None]:
        """Never form a second owner for a member-head set that has one on this base.

        Discovery reads the batch's own variant names (``_train_variants``)
        and nothing else: one exact-head PR lookup for each and one ``git
        ls-remote`` of the same names, under the repository lock. The journal
        is unnecessary: a PR that records the name locates its immutable Git
        chain. Every hit is proven natively (the PR, ``refs/pull/<n>/head``,
        the chain, the base and the member-head set), so a name that a new
        branch reused fails ``REMOTE_HEAD_MISMATCH`` and parks.

        An open PR on the batch's own variant with no ref parks
        (``generation_hidden``). Another variant parks
        (``closed_batch_active``) when it has an open PR, or a ref that no PR
        records: the first is a live owner and the second has no native state
        to prove. A variant whose PR is closed is a closed owner, whether or
        not its branch was deleted: a branch that remains must still name the
        commit at ``refs/pull/<n>/head`` (the forge reports the live tip as
        the PR's head), or the proof fails ``REMOTE_HEAD_MISMATCH`` and parks.
        A closed owner decides as before: fresh strict owner red with
        nonmatching base failure, and a lost owner run (cancelled, skipped or
        missing), permit serial return, because neither can turn
        into a verdict and parking would only repeat this refusal until main
        or a head moves; everything else parks. What remains (no owner PR, or
        the batch's own open owner with its ref) returns the guard's one
        record of the own name: the number of the own open owner it proved,
        or None, and the tip of the own ref it listed, or None.
        ``_train_open_v2`` holds every later read to that record. An open own
        owner is proven like a closed one: its listed tip, its live tip, its
        API head and ``refs/pull/<n>/head`` name one commit.
        """
        _lock_phase("train-history")
        variants = _train_variants(base, kept)
        pulls = self._train_owner_pulls(context, variants)
        refs = self._train_variant_refs(context, variants)
        own = variants[0]
        if own in pulls and pulls[own][1] == "open" and own not in refs:
            # Forgejo keeps this PR open with its head at `refs/pull/<n>/head`,
            # so no chain can be proven: the open and close paths own it.
            fail_with("TRAIN_BUILD_FAILED", step="generation_hidden", park=True,
                      train_pr=pulls[own][0])
        for name in variants[1:]:
            recorded = pulls[name][1] if name in pulls else None
            if recorded == "open" or (recorded is None and name in refs):
                fail_with("TRAIN_BUILD_FAILED", step="closed_batch_active", park=True,
                          branch=name)
        wanted = {str(member["head"]) for member in kept}
        closed: list[tuple[PullRequest, str]] = []
        for name, (index, state) in pulls.items():
            try:
                pull = self._train_pull(context, index)
                if pull.state == "open":
                    opened, actual = self._train_head(context, index)
                    if actual is None:
                        fail("REMOTE_HEAD_MISMATCH")
                    # The live tip agrees with the API head; the hidden head
                    # must name that same commit before the owner is adopted.
                    _label, hidden, old_base, chain = self._train_hidden_head(
                        context, opened, label=name, state="open")
                    if hidden != actual or actual != refs.get(name):
                        fail("REMOTE_HEAD_MISMATCH")
                else:
                    actual, old_base, chain = self._train_closed_head(context, pull)
                if (pull.head != name or pull.state != state or old_base != base
                        or len(chain) != len(kept)
                        or {str(member["head"]) for member in chain} != wanted):
                    fail("REMOTE_HEAD_MISMATCH")
            except (GateError, OSError, subprocess.SubprocessError) as exc:
                fail_with("TRAIN_BUILD_FAILED", step="owner_proof_failed", park=True,
                          train_pr=index,
                          cause=exc.code if isinstance(exc, GateError) else type(exc).__name__)
            if pull.state != "open":
                closed.append((pull, actual))
        if not closed:
            return (pulls[own][0] if own in pulls else None), refs.get(own)
        if len(closed) != 1:
            fail_with("TRAIN_BUILD_FAILED", step="closed_batch_ambiguous", park=True)
        pull, candidate = closed[0]
        try:
            for opened in self.list_open_pulls(context.policy, context.cwd):
                parts = _train_branch_parts(opened.head)
                if parts and parts[0] == TRAIN_V2_POLICY and parts[1]["base"] == base[:12]:
                    fail_with("TRAIN_BUILD_FAILED", step="closed_batch_active", park=True)
            status = self._train_ci_status(context, pull, candidate, base, full=True)
            evidence = status.get("ci_evidence")
            red = (status.get("verdict") == "red"
                   and ci.terminal_full_failure(evidence if isinstance(evidence, dict) else None))
            reason = evidence.get("reason_code") if isinstance(evidence, dict) else None
            lost = (reason in _TRAIN_LOST_OWNER_REASONS
                    or (reason == _TRAIN_NONSTRICT_RED_REASON and not red))
            baseline = (self._train_base_failure_match(context, pull, candidate, base, evidence)
                        if red and isinstance(evidence, dict) else None)
        except (GateError, OSError, subprocess.SubprocessError):
            fail_with("TRAIN_BUILD_FAILED", step="closed_batch_proof", park=True, train_pr=pull.index)
        if lost or (red and baseline is False):
            fail_with("TRAIN_BUILD_FAILED", step="closed_batch", serial_only=True, train_pr=pull.index)
        fail_with("TRAIN_BUILD_FAILED", step="closed_batch", park=True, train_pr=pull.index)

    @staticmethod
    def _train_reconcile_refusal(exc: BaseException) -> NoReturn:
        """Re-raise a refusal from after the guard as a park, keeping its meaning.

        Once the guard has run, the stem may hold a live or unproven owner, so
        a refusal is no permission to start member CI: every one parks. Three
        answers keep their own meaning: a park, ``serial_only`` (a spent
        generation) and ``open_pr``, the uncertain publication the executor
        already answers without a serial mark. Any other refusal, a
        ``TRAIN_INVALID`` and a runner exception included, parks with its step
        (``reconcile_failed`` when it has none) and its code as the ``cause``.
        """
        context = exc.context if isinstance(exc, GateError) else {}
        if (context.get("park") is True or context.get("serial_only") is True
                or context.get("step") == "open_pr"):
            raise exc
        step = context.get("step")
        fail_with("TRAIN_BUILD_FAILED", park=True,
                  step=step if isinstance(step, str) else "reconcile_failed",
                  cause=exc.code if isinstance(exc, GateError) else type(exc).__name__,
                  **{key: context[key] for key in ("branch", "train_pr", "refs") if key in context})

    def _train_open_v2(
        self, context: WorktreeContext, base: str, kept: list[dict[str, object]],
        *, own_pull: int | None, own_tip: str | None,
    ) -> tuple[str, PullRequest, bool, list[dict[str, object]]]:
        """Adopt or create the one owner this batch can have.

        The guard has already refused every state that a PR decides. What is
        left is the batch's own variant name and the other generations under
        its stem. A ref at the own name is adopted when its chain is exactly
        this batch and is otherwise spent (``serial_only``): it is never
        retired and its name is never pushed again. The other generations are
        then handled by class (``_train_retire_others``), and the push of a
        new owner ref is the last write.

        Every read of the own name is held to the guard's record
        (``own_pull``, ``own_tip``), and every such read comes before the
        first retirement. The stem listing must show the own ref at the
        guard's tip: a ref that is gone parks (``generation_vanished``),
        because the absent-ref creation lease cannot tell a deleted name from
        a new one, and a ref that appeared or moved parks
        (``generation_changed``). An adopted ref's chain is read at that tip,
        and its PR record is read again by exact name (``_train_own_owner``).
        What no read can close is the interval between the last read and the
        PR creation or the push; the repository lock excludes the gate's own
        writers there, not an outside one.
        """
        _lock_phase("train-reconcile")
        own = _train_variants(base, kept)[0]
        generations = self._train_generation_refs(context, _train_stem(own))
        mine = generations.pop(own, None)
        if mine is not None and set(mine) != {own}:
            # A prefix ref here has no proof of its own (no fallback path
            # created it). Validating siblings belongs with that path.
            fail_with("TRAIN_INVALID", step="generation_inventory", refs=sorted(mine))
        listed = None if mine is None else mine[own]
        if listed != own_tip:
            fail_with("TRAIN_BUILD_FAILED", park=True, branch=own,
                      step="generation_vanished" if listed is None else "generation_changed")
        adopted = pull = None
        if listed is not None:
            chain_base, chain = self._train_generation_chain(context, own, listed=listed)
            if chain_base != base or not _train_chain_is(chain, kept):
                fail_with("TRAIN_BUILD_FAILED", step="generation_spent", serial_only=True,
                          branch=own)
            adopted = chain
            pull = self._train_own_owner(context, own, kept, known=own_pull, tip=listed)
        self._train_retire_others(context, generations)
        if adopted is None:
            pull, described = self._train_publish_prefix(context, own, base, kept, full=True)
            return own, pull, described, kept
        if pull is None:
            # A crash before the owner PR existed: complete it on the adopted ref.
            pull, described = self._train_open_owner_pr(context, own, base, kept, full=True)
            return own, pull, described, adopted
        return own, pull, True, adopted

    def _train_owner_pulls(
        self, context: WorktreeContext, names: list[str],
    ) -> dict[str, tuple[int, str]]:
        """The one same-repository PR at each exact head name: ``{name: (number, state)}``.

        One lookup for each name, never a read of the PR history. On
        Forgejo, ``head=<branch>`` matches that exact head name only (a
        prefix or ``*`` matches nothing) and finds a closed PR whose branch
        was deleted; ``tea api -i`` puts the HTTP
        status and ``X-Total-Count`` on stderr and the JSON on stdout. A row
        whose ``head.repo`` is another well-formed ``owner/name`` is ignored;
        one whose ``full_name`` is not such an identity is malformed, never
        foreign, because discarding it could hide a retired owner. A failed read,
        a status other than 200, a missing or repeated total, a total above
        one page, a malformed row, or two same-repository PRs on one name
        parks the build before it reads or writes a ref.
        """
        reader = getattr(self._backend_for(context.policy), "list_pulls_by_head", None)
        found: dict[str, tuple[int, str]] = {}
        for name in names:
            if not callable(reader):
                fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_unavailable", park=True)
            try:
                result = reader(self._runner, context.cwd,
                                context.policy.forge_repository, name)
            except (GateError, OSError, subprocess.SubprocessError):
                fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_unavailable", park=True)
            totals = re.findall(r"^X-Total-Count:\s*([0-9]{1,12})\s*$", result.stderr or "",
                                flags=re.MULTILINE | re.IGNORECASE)
            if result.returncode != 0 or _http_status(result) != 200 or len(totals) != 1:
                fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_unavailable", park=True)
            try:
                rows = json.loads(result.stdout)
            except (TypeError, json.JSONDecodeError):
                fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_malformed", park=True)
            if (not isinstance(rows, list) or int(totals[0]) > PAGE_SIZE
                    or len(rows) != int(totals[0])):
                fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_malformed", park=True)
            here: list[tuple[int, str]] = []
            for row in rows:
                head = row.get("head") if isinstance(row, dict) else None
                repo = head.get("repo") if isinstance(head, dict) else None
                if (not isinstance(row, dict) or type(row.get("number")) is not int
                        or row["number"] <= 0 or row.get("state") not in ("open", "closed")
                        or not isinstance(head, dict) or not isinstance(repo, dict)
                        or not isinstance(repo.get("full_name"), str)
                        or not _TRAIN_REPOSITORY_RE.fullmatch(repo["full_name"])
                        or any(not isinstance(head.get(key), str) for key in ("ref", "label"))):
                    fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_malformed", park=True)
                if repo["full_name"] != context.policy.forge_repository:
                    continue
                if name not in (head["ref"], head["label"]):
                    fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_malformed", park=True)
                here.append((row["number"], row["state"]))
            if len(here) > 1:
                fail_with("TRAIN_BUILD_FAILED", step="owner_lookup_ambiguous", park=True,
                          branch=name)
            if here:
                found[name] = here[0]
        return found

    def _train_variant_refs(
        self, context: WorktreeContext, names: list[str],
    ) -> dict[str, str]:
        """The exact *names* that exist on origin, with their SHAs: one ``git ls-remote``."""
        try:
            listed = self._runner(
                [GIT_PATH, "ls-remote", "--heads", "origin",
                 *(f"refs/heads/{name}" for name in names)], context.cwd)
        except (GateError, OSError, subprocess.SubprocessError):
            fail_with("TRAIN_BUILD_FAILED", step="owner_refs_unavailable", park=True)
        if listed.returncode != 0:
            fail_with("TRAIN_BUILD_FAILED", step="owner_refs_unavailable", park=True)
        refs: dict[str, str] = {}
        for line in listed.stdout.splitlines():
            fields = line.split()
            name = fields[1].removeprefix("refs/heads/") if len(fields) == 2 else ""
            if (len(fields) != 2 or not SHA_RE.fullmatch(fields[0])
                    or name not in names or name in refs):
                fail_with("TRAIN_BUILD_FAILED", step="owner_refs_unavailable", park=True)
            refs[name] = fields[0].lower()
        return refs

    def _train_generation_chain(
        self, context: WorktreeContext, branch: str, *, listed: str | None = None,
    ) -> tuple[str, list[dict[str, object]]]:
        """The base and proven chain of an owner ref; one that cannot be read parks.

        A name whose chain the gate cannot read or validate is never treated
        as a random-generation name, so a failed read retires nothing. With
        ``listed``, the tip the stem listing showed, a ref that moved before
        this read parks (``generation_changed``): the chain returned is then
        always the chain of the tip the caller decided on.
        """
        try:
            head = self._train_fetch_ref(context, branch)
            base, chain = self._train_cleanup_chain(context, branch, head)
        except GateError as exc:
            fail_with("TRAIN_BUILD_FAILED", step="generation_unreadable", park=True,
                      branch=branch, cause=exc.code)
        if listed is not None and head != listed:
            fail_with("TRAIN_BUILD_FAILED", step="generation_changed", park=True, branch=branch)
        return base, chain

    def _train_retire_others(
        self, context: WorktreeContext, generations: dict[str, dict[str, str]],
    ) -> None:
        """Retire the other generations under the stem by class; keep every tombstone.

        A random-generation owner (``G`` recomputed from its own chain differs
        from its suffix) and a deterministic owner that has a PR are retired
        through the existing cleanup: the PR keeps recording the name. A
        deterministic owner without a PR is a tombstone and is never deleted,
        because a later build on the same base would otherwise push its name
        again. Every read (each chain, and one exact-name lookup for each
        deterministic owner) comes before the first deletion.

        A retirement is bound to the tip that was classified. The chain is
        read at the tip the stem listing showed, and the cleanup refuses unless
        the family's refs still equal the listed ones, so the lease of each
        deletion names that tip. A ref that an outside writer replaced in
        between parks the build (``generation_changed``) and is not deleted:
        a name that carried another batch's chain when it was classified as
        random-generation is never deleted once it carries its own chain,
        which makes it a tombstone. A refusal at a chain read comes before
        every deletion; one at a cleanup can follow the retirement of an
        earlier generation, which stays retired. Every cleanup refusal parks:
        one that is not already a park (a ref that moved after the listing, a
        failed lease or PR close) parks as ``generation_retire_failed`` with
        its code as the ``cause``.
        """
        retire: list[str] = []
        for family, refs in sorted(generations.items()):
            if set(refs) != {family}:
                fail_with("TRAIN_INVALID", step="generation_inventory", refs=sorted(refs))
            chain_base, chain = self._train_generation_chain(context, family, listed=refs[family])
            deterministic = (_train_generation(chain_base, (str(member["head"]) for member in chain))
                             == family.rpartition("-g")[2])
            if not deterministic or self._train_owner_pulls(context, [family]):
                retire.append(family)
        for family in retire:
            try:
                self._train_cleanup_family(context, family, "TRAIN_BUILD_RECOVERY",
                                           expected=generations[family])
            except (GateError, OSError, subprocess.SubprocessError) as exc:
                if isinstance(exc, GateError) and exc.context.get("park") is True:
                    raise
                # A retirement that stopped part-way is no verdict on this
                # batch: the stem may still hold a live generation, so the
                # members must not start serial CI beside it.
                fail_with("TRAIN_BUILD_FAILED", step="generation_retire_failed", park=True,
                          branch=family,
                          cause=exc.code if isinstance(exc, GateError) else type(exc).__name__)

    def _train_generation_refs(
        self, context: WorktreeContext, stem: str
    ) -> dict[str, dict[str, str]]:
        """The logical family's v2 refs, grouped by physical generation."""
        listed = self._runner(
            [GIT_PATH, "ls-remote", "--heads", "origin", f"refs/heads/{stem}-g*"],
            context.cwd,
        )
        if listed.returncode != 0:
            fail_with("TRAIN_BUILD_FAILED", step="list_refs")
        generations: dict[str, dict[str, str]] = {}
        for line in listed.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not SHA_RE.fullmatch(fields[0]):
                fail("TRAIN_INVALID")
            branch = fields[1].removeprefix("refs/heads/")
            parts = _train_branch_parts(branch)
            if parts is None or parts[0] != TRAIN_V2_POLICY or _train_stem(branch) != stem:
                fail_with("TRAIN_INVALID", step="generation_ref", branch=branch)
            generations.setdefault(_train_family_of(branch), {})[branch] = fields[0].lower()
        return generations

    def _train_fetch_ref(self, context: WorktreeContext, branch: str) -> str:
        remote_ref = f"refs/remotes/origin/{branch}"
        fetched = self._runner(
            [GIT_PATH, "fetch", "origin", f"+refs/heads/{branch}:{remote_ref}"], context.cwd
        )
        if fetched.returncode != 0:
            fail_with("TRAIN_BUILD_FAILED", step="fetch", branch=branch)
        return self._run(
            [GIT_PATH, "rev-parse", remote_ref], context.cwd, "TRAIN_INVALID"
        ).strip().lower()

    def _train_own_owner(
        self, context: WorktreeContext, family: str, kept: list[dict[str, object]],
        *, known: int | None, tip: str,
    ) -> PullRequest | None:
        """Read the adopted own ref's PR record again; write nothing.

        ``known`` is the open owner PR the guard proved on *family*, or None
        when the guard found no PR there. The record is read by exact name,
        never from the open-PR listing, which cannot show a PR closed in
        between. Anything other than the guard's answer (the known PR no
        longer open, no longer at *tip*, or a PR the guard did not see) parks
        (``owner_proof_failed``). Returns the known PR, or None when the name
        has no PR and the caller creates one.
        """
        numbers = ", ".join(f"#{member['pr']}" for member in kept)
        found = self._train_owner_pulls(context, [family]).get(family)
        if found != (None if known is None else (known, "open")):
            fail_with("TRAIN_BUILD_FAILED", step="owner_proof_failed", park=True,
                      train_pr=known if found is None else found[0], cause="PR_STATE_CHANGED")
        if known is None:
            return None
        try:
            pull, actual = self._train_head(context, known)
        except (GateError, OSError, subprocess.SubprocessError) as exc:
            fail_with("TRAIN_BUILD_FAILED", step="owner_proof_failed", park=True, train_pr=known,
                      cause=exc.code if isinstance(exc, GateError) else type(exc).__name__)
        if pull.state != "open" or pull.head != family or actual != tip:
            fail_with("TRAIN_BUILD_FAILED", step="owner_proof_failed", park=True,
                      train_pr=known, cause="PR_STATE_CHANGED")
        if not isinstance(pull.title, str) or pull.title != f"{TRAIN_TITLE_PREFIX} {numbers}":
            fail_with("TRAIN_BUILD_FAILED", step="owner_proof_failed", park=True,
                      train_pr=known, cause="TRAIN_INVALID")
        return pull

    def _train_changed_paths(
        self, context: WorktreeContext, base: str, head: str
    ) -> set[str] | None:
        """The files *head* changes relative to its merge base with *base*.

        Three-dot, so a member that merged main contributes only its own
        files. ``--no-renames`` keeps a rename as the deletion and the
        addition it is, which is the overlap the admission rule cares about.
        None means the comparison could not be made and must refuse.
        """
        result = self._runner(
            [GIT_PATH, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
             f"{base}...{head}"],
            context.cwd,
        )
        if result.returncode != 0 or not isinstance(result.stdout, str):
            return None
        return {item for item in result.stdout.split("\0") if item}

    def _train_publish_prefix(
        self, context: WorktreeContext, branch: str, base: str,
        members: list[dict[str, object]], *, full: bool = False,
    ) -> tuple[PullRequest, bool]:
        """Publish one new immutable ref and its draft technical PR."""
        self._train_publish_ref(context, branch, str(members[-1]["candidate"]))
        return self._train_open_owner_pr(context, branch, base, members, full=full)

    def _train_publish_ref(
        self, context: WorktreeContext, branch: str, candidate: str
    ) -> None:
        _lock_phase("train-publish")
        self._ensure_classification_labels(context, {TRAIN_LABEL})
        # An empty expected ref admits creation only. It cannot overwrite a
        # ref another actor created between discovery and this push.
        pushed = self._runner(
            [GIT_PATH, "push", f"--force-with-lease=refs/heads/{branch}:",
             "origin", f"{candidate}:refs/heads/{branch}"], context.cwd
        )
        if pushed.returncode != 0:
            fail_with("TRAIN_BUILD_FAILED", step="push", branch=branch)

    def _train_open_owner_pr(
        self, context: WorktreeContext, branch: str, base: str,
        members: list[dict[str, object]], *, full: bool = False,
    ) -> tuple[PullRequest, bool]:
        backend = self._backend_for(context.policy)
        numbers = ", ".join(f"#{member['pr']}" for member in members)
        created = backend.create_pr(
            self._runner, context.cwd, context.policy.forge_repository,
            f"{TRAIN_TITLE_PREFIX} {numbers}", branch, MAIN_BRANCH,
            TRAIN_LABEL,
        )
        pull = None
        if created.returncode == 0:
            try:
                opened = self.list_open_pulls(context.policy, context.cwd)
            except (GateError, OSError, subprocess.SubprocessError):
                # The create call returned, so the owner PR may exist even
                # though the listing failed. Report it as the same uncertain
                # publication the executor must not dispatch serially beside.
                fail_with("TRAIN_BUILD_FAILED", step="open_pr", branch=branch)
            pull = self.same_branch_pull(opened, replace(context, branch=branch))
        if pull is None:
            fail_with("TRAIN_BUILD_FAILED", step="open_pr", branch=branch)
        if full:
            body = "\n".join([
                f"Merge train owner of {numbers} on main {base}.",
                "",
                "This draft PR tests the whole batch once before any member merges.",
                "Each member merges through its own PR after this run passes CI.",
                "Shorter-prefix PRs are created only after a terminal red result.",
                "",
                *(f"- #{member['pr']} at {member['head']}" for member in members),
            ])
        else:
            body = "\n".join([
                f"Merge train prefix of {numbers} on main {base}.",
                "",
                "This draft PR tests this prefix before any member merges.",
                "Each member merges through its own PR after its matching prefix passes CI.",
                "This technical PR is closed, never merged.",
                "",
                *(f"- #{member['pr']} at {member['head']}" for member in members),
            ])
        described = backend.write_description(
            self._runner, context.cwd, context.policy.forge_repository, pull.index, body
        )
        return pull, described.returncode == 0

    def _train_member_refusal(
        self, context: WorktreeContext, pr: int, head: str,
        *, mode: str = TRAIN_POLICY,
    ) -> str | None:
        """Why *pr* at *head* cannot ride a train now, or None when it can.

        v2 admits exactly the members the record's CI-evidence answer accepts
        (``ci_evidence_answer``), the answer every other inspection
        path gives: green CI, or where the repository narrows non-candidate
        heads a trusted narrowed success whose judged base lies between the
        member's recorded base and canonical main. An unknown answer refuses
        with its own reason, never as a CI failure. v1 keeps its own looser
        rule: trusted successful CI on the member's own head, green on any
        base, or a run the snapshot review accepts; it is
        exempt from the one-answer rule. Either way the run is admission
        evidence only: the merge still requires the owner's full C1.
        """
        native: dict[str, object] | None = None
        try:
            if mode == TRAIN_V2_POLICY:
                native = self._read_pr_state(context, pr)
                recorded_base = _recorded_base(native)
                inspected = (
                    self.inspect_pr_by_index(context, pr, review_base=recorded_base)
                    if recorded_base is not None else self.inspect_pr_by_index(context, pr)
                )
            else:
                inspected = self.inspect_pr_by_index(context, pr)
        except GateError as exc:
            return exc.code
        pull = inspected["_pull"]
        assert isinstance(pull, PullRequest)
        if pull.head.startswith(TRAIN_BRANCH_PREFIX):
            return "TRAIN_INVALID"
        if inspected["head_sha"] != head:
            return "EXPECTED_REFS_CHANGED"
        blocker = inspected["blocking_reason"]
        if blocker == "PR_REVIEW_CHANGES_REQUESTED":
            return str(blocker)
        if mode == TRAIN_V2_POLICY:
            if native is None or self._queue_hold(native.get("labels")) is not False:
                return "PR_QUEUE_HOLD"
            # The record's own answer, the one the review reads: reading it
            # imports no other operations module's rule.
            answer = inspected.get("ci_evidence_answer")
            if answer == "accepted":
                return None
            if answer == "unknown":
                return str(inspected["ci_evidence_reason"])
            return str(blocker or "PR_CI_NOT_GREEN")
        # The train run is the merge evidence; the member's own run need only
        # have passed on its exact head: on any base, or narrowed to `checks/c2`
        # where the snapshot review accepts it.
        # ``ci_review_accepted`` is that review's own answer, read from the
        # record: an operations module imports no other.
        if inspected["ci_state"] != "green" and not (
                inspected.get("ci_review_accepted") is True
                or (blocker == "CI_BASE_STALE" and inspected["ci_run_conclusion"] == "success")):
            return str(blocker or "PR_CI_NOT_GREEN")
        return None

    def _train_full_mode(self, context: WorktreeContext, family: str) -> bool:
        """True when this generation has no shorter prefixes (v2 full mode)."""
        refs = self._train_remote_refs(context, family)
        if family not in refs:
            fail("TRAIN_INVALID")
        return set(refs) == {family}

    def _train_member_claim(
        self, context: WorktreeContext, pr: int, head: str
    ) -> str | None:
        """The member's current review claim identity, or None.

        The identity is the reviewer's input, not the description text: a body
        edit that keeps the claim fields hashes the same, and a native
        Renovate claim keeps its fixed marker. The generation freezes this
        value, so a claim change cannot ride the batch's old approval.
        """
        native = self._read_pr_state(context, pr)
        pull = self.pull_by_index(context, pr)
        return self._claim_identity(
            native.get("body"), head,
            native_claim=self._native_claim_allowed(pull, native),
        )

    def _train_member_ready(
        self, context: WorktreeContext, pr: int, head: str
    ) -> tuple[PullRequest, dict[str, object]]:
        """Validate one member's native lifecycle; return its pull and state.

        Shared by the per-member merge path and the v2 whole-batch preflight,
        so both apply the same open, main-base, non-draft and exact-head
        rules. A closed or draft later member must refuse before the first
        native merge, not only when its own merge is attempted.
        """
        pull = self.pull_by_index(context, pr)
        if (pull.repository != context.policy.forge_repository
                or pull.base != MAIN_BRANCH
                or pull.state != "open"
                or pull.head.startswith(TRAIN_BRANCH_PREFIX)):
            fail("PR_SELECTION_MISMATCH")
        if pull.title is None:
            fail("PR_DRAFT_STATUS_UNAVAILABLE")
        if pull.is_draft:
            fail("PR_DRAFT")
        native = self._read_pr_state(context, pr)
        if native.get("state") != "open":
            fail("PR_SELECTION_MISMATCH")
        raw_head = native.get("head")
        raw_sha = raw_head.get("sha") if isinstance(raw_head, dict) else None
        if not isinstance(raw_sha, str) or raw_sha.lower() != head:
            fail("EXPECTED_REFS_CHANGED")
        return pull, native

    def _train_validate_batch(
        self, context: WorktreeContext, base: str, chain: list[dict[str, object]]
    ) -> None:
        """Revalidate every frozen binding before the generation's first merge.

        A change to any later member's head, claim or review provenance must
        refuse the batch *before* the first native merge (which would leave
        main interim); the per-member checks cover later positions.
        """
        before = base
        for member in chain:
            pr = int(member["pr"])
            head = str(member["head"])
            _pull, native = self._train_member_ready(context, pr, head)
            if self._queue_hold(native.get("labels")) is not False:
                fail_with("TRAIN_INVALID", step="member_held", pull_index=pr)
            if self._train_member_claim(context, pr, head) != member.get("claim"):
                fail_with("TRAIN_INVALID", step="member_claim_changed", pull_index=pr)
            approval = self._train_approval(
                context, pr, head, before, str(member["candidate"]))
            if approval is None:
                fail_with("PR_REVIEW_NOT_APPROVED", pull_index=pr)
            if approval[1] != member.get("review"):
                fail_with("TRAIN_INVALID", step="member_review_changed", pull_index=pr)
            before = str(member["candidate"])

    def _train_link(
        self, context: WorktreeContext, parent: str, pr: int, head: str,
        *, tree: str | None = None, claim: str | None = None,
        review: str | None = None,
    ) -> str | None:
        """Commit *head* merged onto *parent* as one train link, or None on conflict.

        A v2 link records the admitted member's frozen claim and review
        provenance identities in its body. The commit is immutable, so the
        generation carries the bindings adoption and merge revalidate; v1
        links carry no body claims.
        """
        if tree is None:
            try:
                tree, conflicting = self._auto_merge(context.cwd, parent, head)
            except GateError:
                fail_with("TRAIN_BUILD_FAILED", step="merge", pull_index=pr)
            if conflicting:
                return None
        message = f"Merge PR #{pr} into train"
        if (claim is None) != (review is None):
            fail_with("TRAIN_BUILD_FAILED", step="link_identities", pull_index=pr)
        if claim is not None and review is not None:
            message += f"\n\nTrain-Claim: {claim}\nTrain-Review: {review}"
        candidate = self._run(
            [GIT_PATH, *_TRAIN_IDENT, "commit-tree", tree, "-p", parent, "-p", head,
             "-m", message],
            context.cwd, "TRAIN_BUILD_FAILED",
        ).strip().lower()
        if not SHA_RE.fullmatch(candidate):
            fail_with("TRAIN_BUILD_FAILED", step="commit", pull_index=pr)
        return candidate

    # ------------------------------------------------------ member merge

    def train_member_merge(
        self, context: WorktreeContext, request: dict[str, object]
    ) -> dict[str, object]:
        """Merge one train member under the train's CI evidence (``ci_basis: train``).

        Refuses before any forge write unless: the train PR is open on a
        ``train/`` branch of this repository's own policy whose chain walks
        back to an ancestor of main; the member sits in that chain at its
        expected head; main carries exactly the tree of the candidate before
        the member (``TRAIN_BASE_MOVED`` otherwise); the member's evidence has
        trusted green full CI on the train base; the member PR is open and
        mergeable; and the member's approval reaches its position by digest.
        After the merge main must carry the member's candidate tree and the
        member head (``TRAIN_TREE_MISMATCH`` otherwise).

        v1 reads the member's own immutable prefix run. v2 reads the one owner
        run for the whole batch, because no prefix was published on green; a
        v2 family carrying a prefix ref is refused in this version. Every member's merge commit carries
        ``Train-Candidate`` and ``Train-Base`` trailers; a v2 full-mode merge
        adds ``Train-Final`` and ``Train-Owner`` so interim main is derivable
        from git. The main push may reuse the corresponding run only after
        the C1 reuse verifier confirms its tree, workflow, base and full
        C1 jobs.
        """
        mode = _train_mode_of(context.policy)
        train_index = _request_pr(request, "train_pr")
        member_index = _request_pr(request, "member_pr")
        member_head = _request_sha(request, "member_head")
        main_before = self._train_fetch_main(context)
        train_pull, candidate = self._train_head(context, train_index)
        if candidate is None:
            fail("TRAIN_INVALID")
        if _train_branch_mode(train_pull.head) != mode:
            fail("TRAIN_INVALID")
        if self._train_family(train_pull.head) != train_pull.head:
            fail("TRAIN_INVALID")
        base, chain = self._train_chain(context, train_pull.head, candidate)
        full_mode = mode == TRAIN_V2_POLICY
        if full_mode and not self._train_full_mode(context, train_pull.head):
            fail_with("TRAIN_INVALID", step="generation_inventory", branch=train_pull.head)
        position = next(
            (k for k, member in enumerate(chain) if member["pr"] == member_index), None
        )
        if position is None:
            fail("TRAIN_INVALID")
        if chain[position]["head"] != member_head:
            fail("EXPECTED_REFS_CHANGED")
        before = base if position == 0 else str(chain[position - 1]["candidate"])
        after = str(chain[position]["candidate"])
        final = str(chain[-1]["candidate"])
        if mode == TRAIN_V2_POLICY:
            # The generation froze the member's claim when it was admitted.
            # A changed claim (with a fresh approval of its own) ends this
            # generation's authority over that member, even at the same head.
            frozen_claim = chain[position].get("claim")
            live_claim = self._train_member_claim(context, member_index, member_head)
            if not isinstance(frozen_claim, str) or live_claim != frozen_claim:
                fail_with("TRAIN_INVALID", step="member_claim_changed",
                          pull_index=member_index)
        if self._train_tree(context, main_before) != self._train_tree(context, before):
            fail_with("TRAIN_BASE_MOVED", pull_index=member_index,
                      origin_main_sha=main_before, expected_candidate_sha=before)
        if mode == TRAIN_V2_POLICY and position == 0:
            # The generation's first native merge revalidates every frozen
            # binding, so a changed later member cannot leave main interim.
            self._train_validate_batch(context, base, chain)

        if full_mode:
            # One owner run judges the whole chain; every member merges on it.
            evidence_pull = train_pull
            ci_candidate = final
        else:
            evidence_pull = self._train_prefix_pull(
                context, train_pull, base, chain, position + 1,
                self.list_open_pulls(context.policy, context.cwd),
            )
            ci_candidate = after
        ci_status = self._train_ci_status(context, evidence_pull, ci_candidate, base, full=full_mode)
        evidence = ci_status.get("ci_evidence")
        if (ci_status.get("verdict") != "green"
                or not isinstance(evidence, dict)
                or evidence.get("result") != "PASS"
                or evidence.get("head_sha") != ci_candidate
                or evidence.get("base_sha") != base
                or evidence.get("pull_request") != evidence_pull.index):
            fail_with("PR_CI_NOT_GREEN", ci_verdict=ci_status.get("verdict"),
                      train_pr=train_index)
        # The narrowing rule: a `train/` head runs full C1; require that it did,
        # as every other merge does.
        self._require_merge_profile(context, evidence, train_pr=train_index)

        member_pull, native = self._train_member_ready(
            context, member_index, member_head)
        if native.get("mergeable") is not True:
            # Forgejo answers false (or nothing) while it rechecks a PR after
            # the previous member landed. The tree check above already rules
            # out a real conflict, so the executor retries on this detail.
            fail_with("PR_MERGE_FAILED", pull_index=member_index,
                      mergeable=native.get("mergeable"))
        if mode == TRAIN_V2_POLICY and self._queue_hold(native.get("labels")) is not False:
            fail_with("TRAIN_INVALID", step="member_held", pull_index=member_index)
        approval = self._train_approval(context, member_index, member_head, before, after)
        if approval is None:
            fail("PR_REVIEW_NOT_APPROVED")
        approval_basis, review_identity = approval
        if (mode == TRAIN_V2_POLICY
                and review_identity != chain[position].get("review")):
            fail_with("TRAIN_INVALID", step="member_review_changed",
                      pull_index=member_index)

        # The trailers name the run. Reuse still verifies every trust
        # predicate and the landed tree independently.
        if full_mode:
            merge_message = (
                f"Train-Candidate: {after}\nTrain-Base: {base}\n"
                f"Train-Final: {final}\nTrain-Owner: {train_index}"
            )
        else:
            merge_message = f"Train-Candidate: {after}\nTrain-Base: {base}"
        self._refresh_forge_auth(context.cwd, context.policy)
        backend = self._backend_for(context.policy)
        merged = backend.merge_pr(
            self._runner, context.cwd, context.policy.forge_repository,
            member_index, member_head, merge_message=merge_message,
        )
        status = _http_status(merged)
        if merged.returncode != 0 or (status is not None and not 200 <= status < 300):
            fail_with("PR_MERGE_FAILED", pull_index=member_index, http_status=status)
        try:
            self._fetch_main(context.cwd, "PR_MERGE_POSTCONDITION_FETCH_FAILED")
        except GateError as exc:
            # Integrity stop either way; see `_rewrite_evidence`.
            rewrite = ({"main_history_rewritten": True, **exc.context}
                       if exc.code == "MAIN_HISTORY_REWRITTEN" else {})
            fail_with("PR_MERGE_POSTCONDITION_FETCH_FAILED", pull_index=member_index,
                      pre_merge_origin_main_sha=main_before, reviewed_head_sha=member_head,
                      **rewrite)
        main_after = self._origin_main_head(context.cwd)
        candidate_tree = self._train_tree(context, after)
        main_tree = self._train_tree(context, main_after)
        if (main_tree != candidate_tree
                or not self._train_is_ancestor(context, member_head, main_after)
                or not self._train_is_ancestor(context, main_before, main_after)):
            fail_with("TRAIN_TREE_MISMATCH", pull_index=member_index, train_pr=train_index,
                      origin_main_sha=main_after, origin_main_tree=main_tree,
                      candidate_sha=after, candidate_tree=candidate_tree)
        self._record_lifecycle(
            context.policy, TRAIN_MEMBER_MERGE, 'PR_MERGED', branch=member_pull.head,
            revision=member_head, pull_request=member_index,
        )
        success = self._success(
            replace(context, branch=member_pull.head), TRAIN_MEMBER_MERGE, member_index
        )
        success.update({
            "code": "TRAIN_MEMBER_MERGED",
            "ci_basis": "train",
            "approval_basis": approval_basis,
            "train_pr": train_index,
            "prefix_pr": evidence_pull.index,
            "candidate_sha": after,
            "candidate_tree": candidate_tree,
            "pre_merge_origin_main_sha": main_before,
            "reviewed_head_sha": member_head,
            "origin_main_sha": main_after,
        })
        if mode == TRAIN_V2_POLICY:
            success.update({
                "mode": "full",
                "final_candidate": final,
                "owner_pr": train_index,
                "interim": full_mode and after != final,
            })
        # Evidence only: a forge that dropped the message field leaves the
        # push on the full-C1 path, so this never fails the merge.
        landed = self._runner(
            [GIT_PATH, "show", "-s", "--format=%B", main_after], context.cwd
        )
        success["merge_trailer_landed"] = (
            landed.returncode == 0 and merge_message in landed.stdout
        )
        return success

    # ------------------------------------------------------------- close

    def train_close(
        self, context: WorktreeContext, request: dict[str, object]
    ) -> dict[str, object]:
        """Delete proven family refs, then close its draft PRs.

        Never merge a technical PR. A closed owner still retries remaining
        sibling refs, and a missing ref makes this operation idempotent.
        """
        train_index = _request_pr(request, "train_pr")
        reason = request.get("reason")
        if not isinstance(reason, str) or not _CLOSE_REASON_RE.fullmatch(reason):
            fail("USAGE")
        try:
            pull = self._train_pull(context, train_index)
        except GateError:
            pull = self._open_hidden_owner(context, train_index, reason)
        family = self._train_family(pull.head)
        if reason == "TRAIN_OWNER_RED" and _train_branch_mode(pull.head) == TRAIN_V2_POLICY:
            qualified = False
            try:
                if pull.state == "closed":
                    candidate, base, _chain = self._train_closed_head(context, pull)
                    refs = self._train_remote_refs(context, family)
                    full = set(refs) <= {family}
                else:
                    try:
                        pull, candidate = self._train_head(context, train_index)
                        if candidate is None:
                            fail("TRAIN_INVALID")
                        base, _chain = self._train_cleanup_chain(context, family, candidate)
                        full = self._train_full_mode(context, family)
                    except GateError:
                        # The qualified red close deletes the ref first; the
                        # explicit PR close can then fail while the forge has not
                        # closed it yet. Prove the same owner through the hidden
                        # head and frozen chain, and treat the absence of every
                        # family ref as full mode, so cleanup can be retried.
                        label = pull.head
                        _label, candidate, base, _chain = self._train_hidden_head(
                            context, pull, label=label, state="open")
                        full = not self._train_remote_refs(context, family)
                if full and self._train_fetch_main(context) == base:
                    status = self._train_ci_status(context, pull, candidate, base, full=True)
                    evidence = status.get("ci_evidence")
                    qualified = (status.get("verdict") == "red" and isinstance(evidence, dict)
                                 and ci.terminal_full_failure(evidence)
                                 and self._train_base_failure_match(
                                     context, pull, candidate, base, evidence) is False)
            except (GateError, OSError, subprocess.SubprocessError):
                qualified = False
            if not qualified:
                fail_with("TRAIN_BUILD_FAILED", step="owner_red_proof", park=True,
                          cleanup_started=False, train_pr=train_index)
        self._refresh_forge_auth(context.cwd, context.policy)
        cleaned = self._train_cleanup_family(context, family, reason, anchor=pull)
        success = self._success(replace(context, branch=pull.head), TRAIN_CLOSE, train_index)
        success.update({
            "code": "TRAIN_CLOSED",
            "train_pr": train_index,
            "train_branch": family,
            "branch_deleted": True,
            "closed_prs": cleaned["closed_prs"],
            "deleted_branches": cleaned["deleted_branches"],
        })
        return success

    def _open_hidden_owner(
        self, context: WorktreeContext, train_index: int, reason: str,
    ) -> PullRequest:
        """A v2 owner-close retry whose open owner already lost its ref.

        Forgejo can report the hidden pull ref while the PR is still open and
        keep the generation in ``head.label``. Only the qualified red close and
        the lost-run close may normalise that representation to the
        label; both delete the ref before the explicit PR close, which can then
        fail. Every other reason keeps the strict closed-owner rule in
        ``_train_pull``. The red close proves the owner again in
        ``train_close``; the lost-run close reaches no such proof there, so the
        native identity and frozen chain are proven here, before any write.
        """
        lost = _TRAIN_OWNER_LOST_CLOSE_RE.fullmatch(reason) is not None
        raise_on_refusal = reason != "TRAIN_OWNER_RED" and not lost
        pull = self.pull_by_index(context, train_index)
        native = self._read_pr_state(context, train_index)
        head = native.get("head")
        label = head.get("label") if isinstance(head, dict) else None
        if (raise_on_refusal
                or pull.repository != context.policy.forge_repository
                or pull.base != MAIN_BRANCH
                or pull.state != "open"
                or pull.head != f"refs/pull/{train_index}/head"
                or not isinstance(label, str)
                or _train_branch_parts(label) is None
                or _train_branch_mode(label) != TRAIN_V2_POLICY):
            raise
        pull = replace(pull, head=label)
        if lost:
            # As for a closed owner in `_train_pull`: the chain base must be an
            # ancestor of the main this checkout holds now.
            self._train_fetch_main(context)
            self._train_hidden_head(context, pull, label=label, state="open")
        return pull

    def _train_cleanup_family(
        self, context: WorktreeContext, family: str, reason: str,
        *, anchor: PullRequest | None = None, expected: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Delete and close one proven family, including refs without a PR.

        Validate all discovered refs before the first write. Forgejo closes
        branch PRs after ref deletion, so keep the owner ref until all prefix
        deletions succeed. Its open PR still exposes an orphan after failure.
        Closed PRs need no forge listing: their remaining refs are discovered
        directly, and an explicit anchor covers a closed final owner.

        A build that retires a generation passes ``expected``, the refs it
        classified. The cleanup then refuses before anything else, with the
        park ``generation_changed``, unless the family's refs are exactly those
        names at exactly those tips. The deletion leases below then name the
        classified tips, whatever an outside writer did after the
        classification. Every other caller passes nothing.
        """
        refs = self._train_remote_refs(context, family)
        if expected is not None and refs != expected:
            fail_with("TRAIN_BUILD_FAILED", step="generation_changed", park=True, branch=family)
        chains: dict[str, tuple[str, list[dict[str, object]]]] = {}
        for branch, head in refs.items():
            remote_ref = f"refs/remotes/origin/{branch}"
            fetched = self._runner(
                [GIT_PATH, "fetch", "origin", f"+refs/heads/{branch}:{remote_ref}"], context.cwd
            )
            if fetched.returncode != 0:
                fail_with("TRAIN_CLOSE_FAILED", step="fetch", branch=branch)
            fetched_head = self._run(
                [GIT_PATH, "rev-parse", remote_ref], context.cwd, "TRAIN_INVALID"
            ).strip().lower()
            if fetched_head != head:
                fail_with("TRAIN_CLOSE_FAILED", step="ref_changed", branch=branch)
            chains[branch] = self._train_cleanup_chain(context, branch, head)
        if chains:
            base, longest = max(chains.values(), key=lambda item: len(item[1]))
            if any(other_base != base or chain != longest[:len(chain)]
                   for other_base, chain in chains.values()):
                fail("TRAIN_INVALID")
        if family in chains:
            self._train_tombstone_guard(context, family, *chains[family], anchor=anchor)
        pulls = {pull.index: pull for pull in self.list_open_pulls(context.policy, context.cwd)
                 if _train_branch_parts(pull.head)
                 and self._train_family(pull.head) == family}
        if anchor is not None:
            pulls[anchor.index] = anchor
        if any(pull.base != MAIN_BRANCH or pull.repository != context.policy.forge_repository
               or not isinstance(pull.title, str) or not pull.title.startswith(TRAIN_TITLE_PREFIX)
               for pull in pulls.values()):
            fail("TRAIN_INVALID")
        deleted_branches: list[str] = []
        failed_branches: list[str] = []
        # Native deletion can close that branch's PR. Delete the canonical
        # owner last and stop on failure so an orphan remains discoverable.
        for branch, head in sorted(refs.items(), key=lambda item: (item[0] == family, item[0])):
            deleted = self._runner(
                [GIT_PATH, "push", f"--force-with-lease=refs/heads/{branch}:{head}",
                 "origin", f":refs/heads/{branch}"], context.cwd
            )
            if deleted.returncode != 0:
                failed_branches.append(branch)
                break
            deleted_branches.append(branch)
        if not failed_branches:
            failed_branches = sorted(self._train_remote_refs(context, family))
        if failed_branches:
            fail_with("TRAIN_CLOSE_FAILED", train_branch=family, failed_prs=[],
                      failed_branches=failed_branches, closed_prs=[],
                      deleted_branches=deleted_branches)

        backend = self._backend_for(context.policy)
        closed_prs: list[int] = []
        failed_prs: list[int] = []
        for pull in pulls.values():
            if pull.state != "open":
                continue
            backend.add_pr_comment(
                self._runner, context.cwd, context.policy.forge_repository, pull.index,
                f"Merge train closed: {reason}",
            )
            closed = backend.close_pr(
                self._runner, context.cwd, context.policy.forge_repository, pull.index
            )
            if closed.returncode != 0 or not _response_is_2xx(closed):
                failed_prs.append(pull.index)
            else:
                closed_prs.append(pull.index)
        if failed_prs:
            fail_with("TRAIN_CLOSE_FAILED", train_branch=family, failed_prs=failed_prs,
                      failed_branches=[], closed_prs=closed_prs,
                      deleted_branches=deleted_branches)
        return {"closed_prs": closed_prs, "deleted_branches": deleted_branches}

    def _train_tombstone_guard(
        self, context: WorktreeContext, family: str, base: str,
        chain: list[dict[str, object]], *, anchor: PullRequest | None,
    ) -> None:
        """Refuse to delete a deterministic owner that no same-repository PR records.

        Every deletion of an owner ref passes here, so build recovery,
        ``train_close`` and orphan cleanup keep the tombstone rule alike. A
        name whose ``G`` its own chain reproduces is deleted only when the
        exact-name lookup finds a PR from this repository on it, and, for an
        anchored close, only when that PR is the anchor. The anchor alone
        proves nothing: a PR from a fork can carry the same branch name.
        """
        if _train_generation(base, (str(member["head"]) for member in chain)) != (
                family.rpartition("-g")[2]):
            return
        found = self._train_owner_pulls(context, [family]).get(family)
        if found is None or (anchor is not None and anchor.index != found[0]):
            fail_with("TRAIN_INVALID", step="generation_tombstone", branch=family)

    def _train_remote_refs(self, context: WorktreeContext, family: str) -> dict[str, str]:
        """List only this family's canonical ref and its two prefix names."""
        listed = self._runner(
            [GIT_PATH, "ls-remote", "--heads", "origin", f"refs/heads/{family}",
             f"refs/heads/{family}-prefix-1", f"refs/heads/{family}-prefix-2"], context.cwd
        )
        if listed.returncode != 0:
            fail_with("TRAIN_CLOSE_FAILED", step="list_refs", train_branch=family)
        refs: dict[str, str] = {}
        for line in listed.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2 or not SHA_RE.fullmatch(parts[0]):
                fail("TRAIN_INVALID")
            branch = parts[1].removeprefix("refs/heads/")
            if (not _train_branch_parts(branch)
                    or self._train_family(branch) != family or branch in refs):
                fail("TRAIN_INVALID")
            refs[branch] = parts[0].lower()
        return refs

    # -------------------------------------------------------------- read

    def inspect_train_by_index(
        self, context: WorktreeContext, train_index: int
    ) -> dict[str, object]:
        """Project one train PR's chain and trusted CI for the executor.

        Grants no authority: ``train_member_merge`` re-derives every fact it
        acts on. It fetches the train branch and main into the managed
        checkout's remote-tracking refs, as ``inspect_pr_by_index`` does.

        ``main_on_chain`` is True while main carries the tree of the train
        base or of any candidate in the chain, so an in-order hand merge of
        a member keeps it True. False means main left the chain and no
        member can merge under this train; the executor only closes the
        train on it. Under v2, ``mode`` names ``full`` (one owner run for the
        whole chain) or ``legacy`` (a v1 family read only for cleanup); a
        v2 family with a prefix ref refuses (this stage has no fallback).
        """
        mode = _train_mode_of(context.policy)
        main = self._train_fetch_main(context)
        pull, candidate = self._train_head(context, train_index)
        record: dict[str, object] = {
            "repository": context.policy.name,
            "pull_request": train_index,
            "train_branch": pull.head,
        }
        closed_owner = candidate is None
        family_mode = _train_branch_mode(pull.head)
        if closed_owner and not (mode == TRAIN_V2_POLICY and family_mode == TRAIN_V2_POLICY):
            native = self._read_pr_state(context, train_index)
            record["pr_state"] = "merged" if native.get("merged") is True else "closed"
            return record
        if self._train_family(pull.head) != pull.head:
            fail("TRAIN_INVALID")
        if closed_owner:
            candidate, base, chain = self._train_closed_head(context, pull)
        else:
            base, chain = self._train_chain(context, pull.head, candidate)
        if mode != TRAIN_V2_POLICY:
            prefixes = self._train_prefixes(context, pull, base, chain)
        elif family_mode != TRAIN_V2_POLICY:
            record["mode"] = "legacy"
            prefixes = self._train_prefixes(context, pull, base, chain)
        elif (set(self._train_remote_refs(context, pull.head)) <= {pull.head}
              if closed_owner else self._train_full_mode(context, pull.head)):
            record.update(mode="full", generation=pull.head.rsplit("-g", 1)[1])
            prefixes = self._train_prefixes_full(context, pull, base, chain)
        else:
            # This version has no fallback qualification. A v2 family
            # with a prefix ref is unproven inventory, not a second mode.
            fail_with("TRAIN_INVALID", step="generation_inventory", branch=pull.head)
        final = prefixes[-1]
        chain_trees = {self._train_tree(context, base)} | {
            self._train_tree(context, str(member["candidate"])) for member in chain
        }
        record.update({
            "pr_state": "closed" if closed_owner else "open",
            "head_sha": candidate,
            "base_sha": base,
            "candidate_tree": self._train_tree(context, candidate),
            "members": chain,
            "prefixes": prefixes,
            "main_on_chain": self._train_tree(context, main) in chain_trees,
            "main_is_final": (self._train_tree(context, main) == self._train_tree(context, candidate)
                              and all(self._runner(
                                  [GIT_PATH, "merge-base", "--is-ancestor", str(m["head"]), main],
                                  context.cwd).returncode == 0 for m in chain)),
            **{key: final.get(key) for key in (
                "ci_state", "blocking_reason", "ci_run_conclusion", "ci_run_id", "ci_run_attempt")},
        })
        if record.get("mode") == "full":
            record["ci_terminal_red"] = final.get("ci_terminal_red") is True
            record["base_failure_match"] = final.get("base_failure_match")
            record["owner_failed_jobs"] = final.get("owner_failed_jobs")
        return record

    def _train_prefixes_full(
        self, context: WorktreeContext, owner: PullRequest, base: str,
        chain: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Project the one owner run at every member position (v2 full mode).

        No shorter prefix exists on green, so each position's evidence is the
        owner's full-candidate run. The records keep the v1 shape so the
        executor's per-member loop reads them unchanged; none of them grants
        merge authority, which ``train_member_merge`` re-derives.
        """
        candidate = str(chain[-1]["candidate"])
        try:
            status = self._train_ci_status(context, owner, candidate, base, full=True)
        except GateError as exc:
            shared = {"ci_state": "unavailable", "blocking_reason": exc.code}
        else:
            shared = self._train_ci_projection(status)
            evidence = status.get("ci_evidence")
            shared["ci_terminal_red"] = (
                status.get("verdict") == "red"
                and ci.terminal_full_failure(evidence if isinstance(evidence, dict) else None))
            if shared["ci_terminal_red"] is True and isinstance(evidence, dict):
                try:
                    shared["base_failure_match"] = self._train_base_failure_match(
                        context, owner, candidate, base, evidence)
                except GateError:
                    shared["base_failure_match"] = None
                # attribution only. Its own guard, so a failed read
                # can never turn the base comparison above into unknown.
                try:
                    shared["owner_failed_jobs"] = self._train_owner_failed_jobs(context, evidence)
                except GateError:
                    shared["owner_failed_jobs"] = None
        return [
            {"position": position, "pr": owner.index, "branch": owner.head,
             "candidate": member["candidate"], **shared}
            for position, member in enumerate(chain, 1)
        ]

    def _train_base_failure_match(
        self, context: WorktreeContext, owner: PullRequest, candidate: str, base: str,
        owner_evidence: dict[str, object],
    ) -> bool | None:
        """Compare native failed jobs with B's trusted push; None is unknown.

        Only terminal full-owner red reaches this read. No advisory proof,
        job log, retry or new run is requested. The existing CI owner judges
        the full push and the complete native job inventory.
        """
        backend = self._backend_for(context.policy)
        read_runs = getattr(backend, "actions_runs_for_workflow", None)
        read_jobs = getattr(backend, "actions_run_jobs", None)
        if not callable(read_runs) or not callable(read_jobs):
            return None

        runs = _backend_json(read_runs(
            self._runner, context.cwd, context.policy.forge_repository,
            ci.FORGEJO_LAUNCHER_PATH, base, PAGE_SIZE))
        rows = runs.get("workflow_runs") if isinstance(runs, dict) else None
        if (not isinstance(rows, list) or not isinstance(runs, dict)
                or runs.get("awaiting_task_rows", False) is not False):
            return None
        selected = ci.select_newest_run(rows, base, event="push", path=ci.FORGEJO_LAUNCHER_PATH)
        if selected is None or ci.newer_run_in_flight(
                rows, selected, base, event="push", path=ci.FORGEJO_LAUNCHER_PATH) is not None:
            return None
        base_run = selected.get("id")
        owner_run = owner_evidence.get("workflow_run_id")
        if type(base_run) is not int or type(owner_run) is not int:
            return None
        base_jobs = _backend_json(read_jobs(
            self._runner, context.cwd, context.policy.forge_repository, base_run, PAGE_SIZE))
        launcher = self._runner(
            [GIT_PATH, "cat-file", "-e", f"{base}:{ci.FORGEJO_LAUNCHER_PATH}"], context.cwd)
        base_failures = ci.base_push_failed_jobs(
            base_sha=base, run=selected,
            jobs=base_jobs if isinstance(base_jobs, dict) else None,
            launcher_file_exists=launcher.returncode == 0)
        if base_failures is None:
            return None
        if not base_failures:
            return False
        owner_jobs = _backend_json(read_jobs(
            self._runner, context.cwd, context.policy.forge_repository, owner_run, PAGE_SIZE))
        failures = ci.complete_failed_job_names(
            owner_jobs if isinstance(owner_jobs, dict) else None, owner_run)
        fresh = self._train_ci_status(context, owner, candidate, base, full=True)
        evidence = fresh.get("ci_evidence")
        if (fresh.get("verdict") != "red" or not isinstance(evidence, dict)
                or not ci.terminal_full_failure(evidence)
                or any(evidence.get(key) != owner_evidence.get(key) for key in (
                    "workflow_run_id", "workflow_run_attempt", "job_ids"))):
            return None
        if not failures:
            return None
        if base_failures - ci.IDENTITY_JOB_NAMES:
            # The base's own inventory names concrete leaves: one shared leaf
            # proves the batch reproduced that base failure, and disjoint
            # leaves are positively different. A batch may fail more than the
            # base did.
            return bool((failures & base_failures) - ci.IDENTITY_JOB_NAMES)
        # The base failed only its aggregate identity job, so which leaf broke
        # is unknown: a shared `c1` is no proof of a reproduced failure.
        return None

    def _train_owner_failed_jobs(
        self, context: WorktreeContext, owner_evidence: dict[str, object],
    ) -> list[str] | None:
        """Sorted failed job names of the red owner run; None is unknown.

        Advisory attribution: it tells a flake from a member defect
        and grants nothing. No admission, park, close, serial-return or merge
        decision reads it. One native jobs read: no job log, retry or new run.
        An aggregate identity job is named only when no concrete leaf failed.
        A red run whose inventory names no failed job is inconsistent, so it
        reads as unknown, never as an empty list.
        """
        read_jobs = getattr(self._backend_for(context.policy), "actions_run_jobs", None)
        run_id = owner_evidence.get("workflow_run_id")
        if not callable(read_jobs) or type(run_id) is not int:
            return None
        jobs = _backend_json(read_jobs(
            self._runner, context.cwd, context.policy.forge_repository, run_id, PAGE_SIZE))
        failures = ci.complete_failed_job_names(jobs if isinstance(jobs, dict) else None, run_id)
        if not failures:
            return None
        return sorted((failures - ci.IDENTITY_JOB_NAMES) or failures)

    @staticmethod
    def _train_ci_projection(status: dict[str, object]) -> dict[str, object]:
        """The bounded CI fields a prefix record carries, from one status read."""
        evidence = status.get("ci_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        reason = evidence.get("reason_code")
        conclusion = evidence.get("workflow_run_conclusion")
        run_id = evidence.get("workflow_run_id")
        attempt = evidence.get("workflow_run_attempt")
        return {
            "ci_state": str(status.get("verdict", "unavailable")),
            "blocking_reason": (reason if isinstance(reason, str)
                                and reason in ci.CI_REASON_CODES else None),
            "ci_run_conclusion": (conclusion if isinstance(conclusion, str)
                                  and ci._CONCLUSION_RE.fullmatch(conclusion) else None),
            "ci_run_id": run_id if type(run_id) is int and run_id > 0 else None,
            "ci_run_attempt": attempt if type(attempt) is int and attempt > 0 else None,
        }

    # ----------------------------------------------------------- helpers

    def _train_fetch_main(self, context: WorktreeContext) -> str:
        self._fetch_main(context.cwd, "FETCH_FAILED")
        return self._origin_main_head(context.cwd)

    def _train_expected_base(self, context: WorktreeContext, expected_base: str) -> str:
        """Fetch canonical main and refuse a build whose snapshot base moved."""
        base = self._train_fetch_main(context)
        if base != expected_base:
            fail_with("EXPECTED_REFS_CHANGED", expected_base_sha=expected_base,
                      observed_base_sha=base)
        return base

    def _train_tree(self, context: WorktreeContext, commit: str) -> str:
        return self._run(
            [GIT_PATH, "rev-parse", f"{commit}^{{tree}}"], context.cwd, "TRAIN_INVALID"
        ).strip().lower()

    def _train_is_ancestor(self, context: WorktreeContext, ancestor: str, descendant: str) -> bool:
        return self._runner(
            [GIT_PATH, "merge-base", "--is-ancestor", ancestor, descendant], context.cwd
        ).returncode == 0

    def _train_pull(self, context: WorktreeContext, train_index: int) -> PullRequest:
        pull = self.pull_by_index(context, train_index)
        if pull.repository != context.policy.forge_repository or pull.base != MAIN_BRANCH:
            fail("TRAIN_INVALID")
        if _train_branch_parts(pull.head):
            return pull
        # Forgejo retains the original branch in head.label after deletion,
        # and exposes its commit only through this PR's hidden head ref.
        if (context.policy.forge != "forgejo" or pull.state != "closed"
                or pull.head != f"refs/pull/{train_index}/head"):
            fail("TRAIN_INVALID")
        if (not isinstance(pull.title, str) or not pull.title.startswith(TRAIN_TITLE_PREFIX)):
            fail("TRAIN_INVALID")
        native = self._read_pr_state(context, train_index)
        native_head = native.get("head")
        label = native_head.get("label") if isinstance(native_head, dict) else None
        if not isinstance(label, str) or _train_branch_parts(label) is None:
            fail("TRAIN_INVALID")
        if not isinstance(native.get("base"), dict) or native["base"].get("sha") != pull.base_commit:
            fail("TRAIN_INVALID")
        self._train_fetch_main(context)
        label, candidate, base, chain = self._train_hidden_head(
            context, pull, label=label, state="closed")
        # The immutable proof is keyed by the content it proved, so the close,
        # inspection and no-re-form callers reuse it instead of fetching again.
        proofs = getattr(self, "_train_proofs", None)
        if proofs is None:
            proofs = self._train_proofs = {}
        proofs[(pull.index, label)] = (candidate, base, chain)
        return replace(pull, head=label)

    def _train_hidden_head(
        self, context: WorktreeContext, pull: PullRequest, *,
        label: str, state: str,
    ) -> tuple[str, str, str, list[dict[str, object]]]:
        """The one native-read, hidden-head fetch and frozen-chain proof.

        Forgejo keeps a deleted branch's commit only in the PR's hidden head
        ref and keeps the original name in ``head.label``. A retired owner and
        an open owner whose explicit close failed after the ref deletion prove
        identity here, so no caller re-implements the check and the journal is
        never authority. ``label`` is the generation branch the caller believes
        this owner carries; ``state`` is the native state it accepts.
        """
        native = self._read_pr_state(context, pull.index)
        head, base = native.get("head"), native.get("base")
        if (pull.state != state or _train_branch_parts(label) is None
                or type(native.get("number")) is not int or native["number"] != pull.index
                or native.get("state") != state or native.get("merged") is not False
                or native.get("title") != pull.title or not isinstance(head, dict)
                or not isinstance(base, dict) or base.get("ref") != MAIN_BRANCH
                or head.get("ref") not in (label, f"refs/pull/{pull.index}/head")
                or head.get("label") != label):
            fail("TRAIN_INVALID")
        for endpoint in (head, base):
            repo = endpoint.get("repo")
            if not isinstance(repo, dict) or repo.get("full_name") != context.policy.forge_repository:
                fail("TRAIN_INVALID")
        sha = head.get("sha")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            fail("TRAIN_INVALID")
        fetched = self._runner(
            [GIT_PATH, "fetch", "origin", f"refs/pull/{pull.index}/head"], context.cwd)
        if fetched.returncode != 0:
            fail("FETCH_FAILED")
        actual = self._run(
            [GIT_PATH, "rev-parse", "FETCH_HEAD"], context.cwd, "TRAIN_INVALID").strip().lower()
        if actual != sha.lower():
            fail("REMOTE_HEAD_MISMATCH")
        chain_base, chain = self._train_cleanup_chain(context, label, actual)
        numbers = ", ".join(f"#{member['pr']}" for member in chain)
        if pull.title != f"{TRAIN_TITLE_PREFIX} {numbers}":
            fail("TRAIN_INVALID")
        return label, actual, chain_base, chain

    def _train_closed_head(
        self, context: WorktreeContext, pull: PullRequest,
    ) -> tuple[str, str, list[dict[str, object]]]:
        """A closed owner's native identity and independently fetched Git chain.

        A closed state alone is no recovery verdict. This supplies the same
        immutable candidate proof to inspection, no-re-form and red closure.
        """
        if self._train_family(pull.head) != pull.head:
            fail("TRAIN_INVALID")
        proofs = getattr(self, "_train_proofs", None)
        reused = None if proofs is None else proofs.pop((pull.index, pull.head), None)
        if reused is not None:
            return reused
        _label, candidate, base, chain = self._train_hidden_head(
            context, pull, label=pull.head, state="closed")
        return candidate, base, chain

    def _train_cleanup_chain(
        self, context: WorktreeContext, branch: str, head: str,
    ) -> tuple[str, list[dict[str, object]]]:
        base, chain = self._train_chain(context, branch, head)
        before = base
        for member in chain:
            candidate = str(member["candidate"])
            ident = self._run(
                [GIT_PATH, "show", "-s", "--format=%an%n%ae%n%cn%n%ce", candidate],
                context.cwd, "TRAIN_INVALID",
            ).strip().splitlines()
            if ident != ["merge-train", "merge-train@example.invalid"] * 2:
                fail("TRAIN_INVALID")
            tree, conflicts = self._auto_merge(context.cwd, before, str(member["head"]))
            if conflicts or self._train_tree(context, candidate) != tree:
                fail("TRAIN_INVALID")
            before = candidate
        return base, chain

    def _train_head(
        self, context: WorktreeContext, train_index: int
    ) -> tuple[PullRequest, str | None]:
        """The train PR and its fetched head, or None for a closed train.

        The one place a draft is admitted, and only for a ``train/`` head:
        ``inspect_pr_by_index`` keeps refusing drafts everywhere else.
        """
        pull = self._train_pull(context, train_index)
        if pull.state != "open":
            return pull, None
        if pull.title is None:
            fail("PR_DRAFT_STATUS_UNAVAILABLE")
        if not pull.is_draft or not pull.title.startswith(TRAIN_TITLE_PREFIX):
            fail("TRAIN_INVALID")
        native = self._read_pr_state(context, train_index)
        native_head = native.get("head")
        raw_head = native_head.get("sha") if isinstance(native_head, dict) else None
        if (not isinstance(raw_head, str) or not SHA_RE.fullmatch(raw_head)
                or not isinstance(native_head, dict) or native_head.get("ref") != pull.head
                or native.get("state") != "open"):
            fail("TRAIN_INVALID")
        remote_ref = f"refs/remotes/origin/{pull.head}"
        fetched = self._runner(
            # `+`, as for a PR head: the SHA check below is the authority.
            [GIT_PATH, "fetch", "origin", f"+refs/heads/{pull.head}:{remote_ref}"], context.cwd
        )
        if fetched.returncode != 0:
            fail("FETCH_FAILED")
        fetched_head = self._run(
            [GIT_PATH, "rev-parse", remote_ref], context.cwd, "REMOTE_HEAD_INVALID"
        ).strip().lower()
        if fetched_head != raw_head.lower():
            fail("REMOTE_HEAD_MISMATCH")
        return pull, fetched_head

    def _train_chain(
        self, context: WorktreeContext, branch: str, candidate: str
    ) -> tuple[str, list[dict[str, object]]]:
        """Walk *candidate*'s first parents back to the train base.

        A link is a two-parent commit with the train's own merge subject;
        main's tip is itself a merge commit, so parent count alone cannot
        find the base. The base must carry the branch name's ``main12``
        prefix and be an ancestor of canonical main, the first member must
        be the branch name's PR, and at most three links are admitted. A v2
        link must also carry exactly one ``Train-Claim`` trailer; that is the
        frozen claim identity adoption and merge compare against live state.
        """
        parts = _train_branch_parts(branch)
        if parts is None:
            fail("TRAIN_INVALID")
        mode, named = parts
        chain: list[dict[str, object]] = []
        commit = candidate
        while True:
            shown = self._run(
                [GIT_PATH, "log", "-1", "--format=%P%x00%s%x00%B", commit],
                context.cwd, "TRAIN_INVALID",
            )
            fields = shown.split("\0", 2)
            if len(fields) != 3:
                fail("TRAIN_INVALID")
            parents = fields[0].split()
            subject = fields[1].strip()
            link = _TRAIN_MERGE_SUBJECT_RE.fullmatch(subject)
            if link is None or len(parents) != 2:
                break
            if len(chain) == TRAIN_MAX_MEMBERS:
                fail("TRAIN_INVALID")
            member: dict[str, object] = {
                "pr": int(link.group(1)), "head": parents[1].lower(), "candidate": commit,
            }
            if mode == TRAIN_V2_POLICY:
                claim = _train_link_claim(fields[2])
                if claim is None:
                    fail_with("TRAIN_INVALID", step="member_claim_missing",
                              pull_index=int(link.group(1)))
                review = _train_link_review(fields[2])
                if review is None:
                    fail_with("TRAIN_INVALID", step="member_review_missing",
                              pull_index=int(link.group(1)))
                member["claim"] = claim
                member["review"] = review
            chain.append(member)
            commit = parents[0].lower()
        chain.reverse()
        numbers = [member["pr"] for member in chain]
        prefix_position = int(named["prefix"]) if named["prefix"] else None
        if (len(chain) < (1 if prefix_position is not None else 2)
                or prefix_position is not None and len(chain) != prefix_position
                or len(set(numbers)) != len(numbers)
                or not commit.startswith(str(named["base"]))
                or numbers[0] != int(str(named["pr"]))
                or not self._train_is_ancestor(context, commit, self._origin_main_head(context.cwd))):
            fail("TRAIN_INVALID")
        return commit, chain

    @staticmethod
    def _train_family(branch: str) -> str:
        return _train_family_of(branch)

    @staticmethod
    def _train_prefix_branch(family: str, position: int, count: int) -> str:
        return family if position == count else f"{family}-prefix-{position}"

    def _train_prefix_pull(
        self, context: WorktreeContext, owner: PullRequest, base: str,
        chain: list[dict[str, object]], position: int, pulls: list[PullRequest],
    ) -> PullRequest:
        """Re-derive the PR for exactly this member's immutable prefix."""
        branch = self._train_prefix_branch(owner.head, position, len(chain))
        if position == len(chain):
            return owner
        matching = [pull for pull in pulls if pull.head == branch]
        if len(matching) != 1:
            fail_with("TRAIN_INVALID", step="prefix_missing", prefix_position=position)
        pull, head = self._train_head(context, matching[0].index)
        if head is None or head != chain[position - 1]["candidate"]:
            fail_with("TRAIN_INVALID", step="prefix_head", prefix_position=position)
        prefix_base, prefix_chain = self._train_chain(context, branch, head)
        if prefix_base != base or prefix_chain != chain[:position]:
            fail_with("TRAIN_INVALID", step="prefix_chain", prefix_position=position)
        return pull

    def _train_prefixes(
        self, context: WorktreeContext, owner: PullRequest, base: str,
        chain: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Project separate CI state per prefix; grant no merge authority."""
        pulls = self.list_open_pulls(context.policy, context.cwd)
        prefixes: list[dict[str, object]] = []
        for position, member in enumerate(chain, 1):
            record: dict[str, object] = {
                "position": position, "pr": None,
                "branch": self._train_prefix_branch(owner.head, position, len(chain)),
                "candidate": member["candidate"],
            }
            try:
                pull = self._train_prefix_pull(
                    context, owner, base, chain, position, pulls
                )
                record["pr"] = pull.index
                status = self._train_ci_status(context, pull, str(member["candidate"]), base)
            except GateError as exc:
                record.update(ci_state="unavailable", blocking_reason=exc.code)
            else:
                record.update(self._train_ci_projection(status))
            prefixes.append(record)
        return prefixes

    def _train_ci_status(
        self, context: WorktreeContext, pull: PullRequest, candidate: str, base: str,
        *, full: bool = False,
    ) -> dict[str, object]:
        """Trusted CI bound to the immutable ``(base, candidate)`` pair.

        Native PR bases follow main as members land; this binding does not.
        A v2 full owner requires the strongest profile, independently of the
        changed-path class. Legacy prefix callers keep their existing path.
        """
        bound = replace(pull, base_commit=base)
        if full:
            return self._automerge_ci_status(
                replace(context, branch=pull.head), bound, candidate, merge_candidate=True)
        return self._automerge_ci_status(replace(context, branch=pull.head), bound, candidate)

    def _train_approval(
        self, context: WorktreeContext, member_index: int, member_head: str,
        before: str, after: str,
    ) -> tuple[str, str] | None:
        """The ``(basis, provenance identity)`` covering ``before..after``, or None.

        The delegated reviewer's latest live review must be an approval with
        a valid provenance marker for ``(reviewed_base, reviewed_head)``, and
        no other reviewer may have a live change request. ``exact``: the
        reviewer saw the member's current head. ``clean_refresh``: the head
        is a clean refresh of the reviewed one (``_clean_refresh_preserves_
        review`` against the member's fork point with the train), which now
        requires the reviewed files to be unchanged as well as the digest to
        match. Either way the reviewed diff must be digest-identical to the
        member's diff at its train position; a binary range has no digest and
        refuses. The
        identity digests the accepted provenance, so a v2 link can freeze it
        and a later substitution is detectable; ``after`` may be the merged
        tree at build time (the digest is tree-based) or the link commit at
        merge time.
        """
        reviews = self._list_pull_reviews(context, member_index)
        entry = self._latest_delegated_review(reviews)
        if entry is None or entry.get("dismissed") is not False:
            return None
        state = entry.get("state")
        reviewed_head = entry.get("commit_id")
        if (not isinstance(state, str) or state.upper() != "APPROVED"
                or not isinstance(reviewed_head, str) or not SHA_RE.fullmatch(reviewed_head)):
            return None
        provenance = self._delegated_review_provenance(entry, base_sha=None)
        if provenance is None:
            return None
        native = self._read_pr_state(context, member_index)
        claim_sha = self._claim_identity(
            native.get("body"), member_head,
            native_claim=self._native_claim_allowed(
                self.pull_by_index(context, member_index), native
            ),
        )
        if claim_sha is None or provenance["claim_sha"] != claim_sha:
            return None
        reviewed_head = reviewed_head.lower()
        reviewed_base = provenance["base_sha"].lower()
        if self._review_decision_for_head(
            reviews, member_head, reviewed_base, claim_sha
        ) == "changes_requested":
            return None
        if reviewed_head == member_head:
            basis = "exact"
        else:
            if not catalog.review_reuse_enabled(context.policy):
                return None
            fork = self._runner([GIT_PATH, "merge-base", member_head, before], context.cwd)
            fork_point = fork.stdout.strip().lower() if isinstance(fork.stdout, str) else ""
            if fork.returncode != 0 or not SHA_RE.fullmatch(fork_point):
                return None
            if not self._clean_refresh_preserves_review(
                    context, reviewed_head=reviewed_head, reviewed_base=reviewed_base,
                    head_sha=member_head, base_sha=fork_point):
                return None
            basis = "clean_refresh"
        reviewed_digest = self._range_diff_digest(context, reviewed_base, reviewed_head)
        position_digest = self._range_diff_digest(context, before, after)
        if reviewed_digest is None or reviewed_digest != position_digest:
            return None
        if _train_mode_of(context.policy) == TRAIN_V2_POLICY:
            # Bind the actual position's whole reviewed files, not just the
            # normalized hunk digest or a clean-refresh check of member_head.
            if not self._reviewed_files_unchanged(
                    context, reviewed_base=reviewed_base,
                    reviewed_head=reviewed_head, head_sha=after):
                return None
            # Reversing the same reviewed range selects its preimage paths.
            if not self._reviewed_files_unchanged(
                    context, reviewed_base=reviewed_head,
                    reviewed_head=reviewed_base, head_sha=before):
                return None
        identity = hashlib.sha256("\0".join((
            provenance["run_id"], provenance["base_sha"], provenance["worker"],
            provenance["harness"], provenance["model"], provenance["claim_sha"],
            reviewed_head,
        )).encode("utf-8")).hexdigest()
        return basis, identity
