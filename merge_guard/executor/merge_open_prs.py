#!/usr/bin/env python3
"""Merge every open pull request in the managed repositories, worst-ordered last.

This is the merge command's implementation.  It exists because merging a batch
by hand is error-prone in two ways, and neither is visible until after the
damage:

  * **Order.**  Two PRs that touch a common file merge cleanly in isolation and
    conflict the moment one of them lands.  `mergeable` is measured by the
    forge against the base branch, never against a sibling PR, so every PR in a
    batch can read MERGEABLE and half of them still fail.  This orders by
    *collision degree* -- PRs whose changed-file sets are disjoint from every
    sibling go first, and the colliding cluster goes last, where a failure can
    no longer block anything independent.  `--dry-run` goes further and
    *simulates* that order with `git merge-tree`, so the question above is
    answered before anything is attempted rather than mitigated by scheduling.
    Degree is a proxy and over-reports: two PRs editing different sections of
    one README share a path and merge cleanly.
  * **Silence.**  `gh pr merge` on a conflicted PR prints a one-line refusal
    naming no file, so a batch run ends with "some failed" and no way to act.
    Every skip here carries a reason, and a conflict carries the *paths*,
    computed locally with `git merge-tree` rather than guessed.

Which forge holds each repository is read from the repository catalog, the
owner of every repository's path and forge -- never inferred from a
repository's name. There are no GitHub or Gitea forge classes, because no
current record selects one; `FORGES` holds the one tea backend, Forgejo.
Naming no repository acts on the default repository list, in catalog order;
every other repository is opt-in.  `--automerge` additionally visits every
repository the configuration marks for automerge, in catalog order, and
reviews, refreshes and merges its pull requests exactly as it does the
default list.  It never deploys one.

Renovate's pull requests are held back by default.  They are not ordinary
changes -- each rewrites a dependency pin, and a Renovate PR body may carry
notes the operator must read *before* it lands (for example that a
floating-tag digest bump may cross a major version with nothing in the diff
to say so).  A batch merge that swept them in would land those unread.

Author is *not* the signal.  Renovate may run under the same
account as ordinary changes, in which case every pull request in the repository shares one author and an
author filter would match all of them or none.  The head branch prefix and the
`renovate` label are the signals, and *either alone* is enough: both are
Renovate configuration (`branchPrefix`, `labels` in `renovate.json`) and either
could be changed without the other, so requiring both would fail open on a
config edit.  `--include-renovate` merges them anyway.  The `--automerge`
executor is different: it queues Renovate PRs behind every other PR, reviews
and merges a minor, patch or digest update like any PR, and parks one
labelled `major` or `premerge` for the operator (`_park_renovate_holds`).
Removing the label is the operator's consent to one head. A Renovate PR whose
forge label history shows such a label but which has no consent record (the
label went before any pass parked it) is parked as `renovate_consent_missing`
instead of merged (`_consent_missing`).

Merging is deliberately the whole job.  Deploying what was merged and
sweeping landed branches are separate concerns left to the caller, not
reimplemented here.

In `--automerge` mode every candidate goes through the shared gate API:
inspect, review when required, inspect again, and merge with explicit expected
head and base. The merge owner re-reads those refs under its repository lock.

Nothing here judges *whether* a change should land.  A routing change and a
typo fix are both MERGEABLE.  Use `--dry-run` first when the batch contains
infrastructure, credentials, firewall or routing changes; it now reports, per
PR, whether it will actually merge once the ones ahead of it have landed, and
names the colliding files when it will not.  The simulation is read-only --
`merge-tree` and `commit-tree` write unreferenced loose objects and nothing
else -- and it never gates a real merge, so a wrong prediction costs a line of
output rather than a withheld PR.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

import yaml

# Imported, never re-declared: each backend module's docstring states it is the
# sole place its binary path (and the login) is written down.  A second
# literal "/usr/bin/tea" here is how one of them silently stops tracking the
# other.  The backends are ordinary submodules of the gate package, so the
# gate needs no by-path loader for its forge boundary; the package resolves as
# a normal package once this file's directory is on sys.path.
# The merge mutation is delegated to the backend's own `merge_pr`, so the tea
# merge argv is also written down exactly once.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_chain.catalog import load_repos_yaml  # noqa: E402
from deploy_chain.host import HostDeployError  # noqa: E402
from deploy_chain.host import changed_paths as _diff_paths  # noqa: E402
from deploy_chain.state import (  # noqa: E402
    DEPLOY_MARKER_PATH,
    DEPLOY_MARKER_TTL_SECONDS,
    probe_deploy_lock,
)
from deploy_chain.transport import LocalRunner  # noqa: E402
from gate_server_estate import (  # noqa: E402
    PRIVATE_REVIEW_SUBPROCESS_TIMEOUT_SECONDS,
    private_reopen_review,
)
from git_gate.automerge_queue import (  # noqa: E402
    HOLD_ID,
    PARK_OWNERS,
    QUEUE_CODE,
    QueueStateError,
    QueueStore,
    parked_details,
)
from git_gate.ci import STRONGEST_PROFILE, TERMINAL_FAILURE_CONCLUSIONS  # noqa: E402
from git_gate.contracts import (  # noqa: E402
    HOLD_LABEL,
    QUEUE_BACKPRESSURE_THRESHOLD,
    QUEUE_HIGH_LABEL,
    QUEUE_LOW_LABEL,
    QUEUE_NORMAL_LABEL,
    GateError,
)
from git_gate.forge import (  # noqa: E402
    CLAIM_ERROR_CODES,
    QUEUE_DONE,
    QUEUE_HELD,
    _http_status,
    claim_validation_succeeded,
)
from git_gate.gate_backend_forgejo import FORGEJO_LOGIN, FORGEJO_OPERATOR_LOGIN  # noqa: E402
from git_gate.gate_backend_tea import TEA_PATH, merge_pr  # noqa: E402

# Resolved from this file rather than from a hardcoded install path, so a run out
# of a checkout reads that checkout's own catalog.  Testing a change to
# the repository table against another copy would prove nothing.
REPOS_YAML = Path(__file__).resolve().parents[2] / "repos.yaml"

# The merge command with no repository named: the default repository list, in
# the order the other commands visit it.  Other repositories are deliberately
# absent: sweeping them by default would merge unrelated work as a side effect
# of shipping the main product.
DEFAULT_REPOSITORIES = ("core-repo", "docs-repo")
AUTOMERGE_INTEGRITY_CODES = frozenset({
    "GATE_LOCK_DIR_UNSAFE",
    "INTERNAL_ERROR",
    "PR_MERGE_POSTCONDITION_FAILED",
    "PR_MERGE_POSTCONDITION_FETCH_FAILED",
    "TRAIN_TREE_MISMATCH",
})
AUTOMERGE_AUTHORITY_CODES = frozenset({
    "EXECUTOR_HOST_REQUIRED",
    "EXECUTOR_SERVICE_REQUIRED",
    "AUTOMERGE_CGROUP_UNAVAILABLE",
    "REPOS_YAML_UNREADABLE",
    "REVIEW_SUPERVISOR_INVALID",
    "REVIEW_SUPERVISOR_UNAVAILABLE",
})
# Reasons that mean the review executor itself could not reach a verdict --
# never a reviewer's CHANGES_REQUIRED verdict, a CI-not-green wait, or a busy
# lane.  Retained candidates carrying one of these are worth an operator's
# attention, so `--automerge` logs them at journal priority `err`; the other
# retained reasons are a verdict or ordinary waiting and stay at `info`.
# `_park_reason` parks each one except `REVIEW_INCOMPLETE`, which the next
# pass's receipt reread decides.  A review refusal that is not a stable code
# is retained as `REVIEW_EXECUTION_FAILED` (`_automerge_candidate`).
REVIEW_EXECUTION_FAILURE_CODES = frozenset({
    "REVIEW_INCOMPLETE",
    "REVIEW_EXECUTION_UNCERTAIN",
    "REVIEW_CLEANUP_UNKNOWN",
    "REVIEW_PUBLICATION_UNCERTAIN",
    "REVIEW_SUPERVISOR_UNAVAILABLE",
    "REVIEW_SUPERVISOR_INVALID",
    "REVIEW_RESULT_INVALID",
    "DELEGATED_REVIEW_READBACK_FAILED",
    "REVIEW_EXECUTION_FAILED",
    "REVIEW_SNAPSHOT_UNSUPPORTED",
    "REVIEW_NATIVE_IDENTITY_UNVERIFIED",
    "REVIEW_RECEIPT_INVALID",
    "REVIEW_RECEIPT_WRITE_FAILED",
    "CURRENT_REVIEW_FINDINGS_TOO_LARGE",
    "REVIEW_DIFF_TOO_LARGE",
    "REVIEW_COMMENT_REREVIEWS_EXHAUSTED",
    # the base's path-filter rules are unreadable; the operator fixes them.
    "REVIEW_PRIVATE_FILTER_UNAVAILABLE",
})
_REFUSAL_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]*")
# Review refusals decided without the no-progress deadline: parked or alerted
# by code, a pass-wide stop, or the pass's own budget, which is no stall.
_REVIEW_REFUSALS_NOT_WAITED = (
    REVIEW_EXECUTION_FAILURE_CODES | AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES
    | {"PR_CLAIM_UNAVAILABLE", "REVIEW_BUDGET_EXHAUSTED", "PASS_DEADLINE"}
    # Self-resolving: each is owned by its own timer or the next pass's re-inspection.
    | {"REVIEW_ATTEMPT_BUSY", "REVIEW_STATE_CHANGED", "PR_CI_NOT_GREEN", "REVIEW_CANCELLED"})

# Typing the full name always works; these are the shorthands the merge command takes.
ALIASES = {"core": "core-repo", "docs": "docs-repo"}

MERGE_METHOD = "--merge"
# `gh pr view` reports UNKNOWN while the forge recomputes mergeability after a
# merge lands.  It is a "come back later", not an answer.
UNKNOWN_RETRY_SECONDS = 5

# Both are Renovate configuration, and matching either is deliberate -- see the
# module docstring. `branchPrefix` is expected at Renovate's default, and the
# `renovate` label on every Renovate PR (a global `labels` setting plus the
# major rule).
RENOVATE_LABEL = "renovate"
RENOVATE_BRANCH_PREFIX = "renovate/"
# The Renovate configuration is expected to add `major` to every major update and `premerge` to every
# update whose note asks for a step before merge that CI and the automated
# reviewer cannot take. The executor parks such a Renovate PR for the operator
# instead of reviewing and merging it, under the park reason named here; a PR
# carrying both is parked as major.
RENOVATE_HOLD_LABELS = {"major": "renovate_major", "premerge": "renovate_premerge"}
RENOVATE_LABEL_HOLD_REASONS = frozenset(RENOVATE_HOLD_LABELS.values())
# A Renovate PR whose listed labels could not be read: the executor cannot
# tell a held PR from a free one, so it parks it until the listing reads.
RENOVATE_LABELS_UNKNOWN = "renovate_labels_unknown"
# A Renovate PR whose label history (the gate's `label_history`, read from the
# forge timeline) shows a hold label, but which has no consent record: the
# label went before any pass parked the PR (during a deploy hold, or while
# its labels were unread), so no consent ever named a head. It stays parked
# until the operator adds the label back and removes it again.
RENOVATE_CONSENT_MISSING = "renovate_consent_missing"
RENOVATE_HOLD_REASONS = RENOVATE_LABEL_HOLD_REASONS | {
    RENOVATE_LABELS_UNKNOWN, RENOVATE_CONSENT_MISSING}

# the `gate/hold` label is an author's stop signal for
# any PR, Renovate's or not. It parks the PR for its author before any refresh,
# review, train membership or merge, and ends only when the label is gone: there
# is no consent to bind to a head. Labels the listing could not read hold a PR
# too, for the operator, because a hold may sit behind them. A Renovate PR keeps
# `renovate_labels_unknown`, which also carries its consent rules.
AUTHOR_HOLD = "author_hold"
HOLD_LABELS_UNKNOWN = "hold_labels_unknown"
LABEL_HOLD_REASONS = frozenset({AUTHOR_HOLD, HOLD_LABELS_UNKNOWN})
# What `_park_held` keeps through every head, CI or review change.
HOLD_PARK_REASONS = RENOVATE_HOLD_REASONS | LABEL_HOLD_REASONS
# The gate's inspect projection reports a hold it read from the PR itself.
QUEUE_HOLD_RESULT = "PR_QUEUE_HOLD"

SHA_RE = re.compile(r"[0-9a-f]{40}")
# A v2 owner branch names its 128-bit generation; only that shape is adoptable.
GENERATION_RE = re.compile(r"[0-9a-f]{32}")
MAX_REASON_CHARS = 300
LOGGER = logging.getLogger(__name__)


def _listed_labels(listed: dict[str, Any], number: int) -> set[str]:
    labels = listed.get("labels")
    names = labels.get(str(number)) if isinstance(labels, dict) else None
    if not isinstance(names, list):
        return set()
    return {name for name in names if isinstance(name, str)}


def _renovate_branches(listed: dict[str, Any], numbers: list[int]) -> set[int]:
    """The listed PRs whose head branch is Renovate's: the gate's own test
    for a refresh of a red Renovate PR (`_refresh_eligible`)."""
    heads = listed.get("head_refs")
    heads = heads if isinstance(heads, dict) else {}
    return {number for number in numbers
            if isinstance(head := heads.get(str(number)), str)
            and head.startswith(RENOVATE_BRANCH_PREFIX)}


def _renovate_numbers(listed: dict[str, Any], numbers: list[int]) -> set[int]:
    """The listed PRs that the head branch or the label marks as Renovate's.

    Either signal alone is enough, as in the module docstring. An absent or
    malformed `head_refs` or `labels` marks none: queue order and park
    ownership then fail safe to plain ascending order and an author-owned park.
    """
    return _renovate_branches(listed, numbers) | {
        number for number in numbers if RENOVATE_LABEL in _listed_labels(listed, number)}


def _renovate_holds(listed: dict[str, Any], renovate: set[int]) -> dict[int, str]:
    """The Renovate PRs a hold label parks, each with its park reason.

    The operator takes each one: removing the label, once its note is acted
    on, is the consent that lets the queue review and merge the head the PR
    has then (`_park_renovate_holds`). Renovate stops syncing a PR's labels
    once another account changes them, so its next scan does not re-add the
    label. Labels the listing could not read are `_renovate_labels_unknown`'s.
    """
    holds: dict[int, str] = {}
    for number in renovate:
        labels = _listed_labels(listed, number)
        for label, reason in RENOVATE_HOLD_LABELS.items():
            if label in labels:
                holds[number] = reason
                break
    return holds


def _author_holds(listed: dict[str, Any], numbers: list[int]) -> dict[int, str]:
    """The listed PRs an author's hold label or an unread label list stops.

    A hold label always wins, a Renovate PR's included. A PR whose labels the
    listing could not read is stopped as well, except a Renovate PR, which
    `_renovate_labels_unknown` holds under its own consent rules. A listing
    with no `labels` field at all carries no label data (see
    `_renovate_labels_unknown`) and stops nothing.
    """
    holds: dict[int, str] = {}
    unread = _renovate_labels_unknown(listed, set(numbers))
    renovate = _renovate_numbers(listed, numbers)
    for number in numbers:
        if HOLD_LABEL in _listed_labels(listed, number):
            holds[number] = AUTHOR_HOLD
        elif number in unread and number not in renovate:
            holds[number] = HOLD_LABELS_UNKNOWN
    return holds


# queue priority ranks; `_slot_order` serves the lower rank first.
PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW = 0, 1, 2
_PRIORITY_RANKS = {QUEUE_HIGH_LABEL: PRIORITY_HIGH, QUEUE_NORMAL_LABEL: PRIORITY_NORMAL,
                   QUEUE_LOW_LABEL: PRIORITY_LOW}


def _queue_priorities(
    listed: dict[str, Any], numbers: list[int]
) -> tuple[dict[int, int], list[int], list[int]]:
    """Each listed PR's priority rank, the `queue/high` PRs left unverified,
    and those whose label another account added.

    A PR without a `queue/*` label is normal; one with several takes the
    lowest priority among them, so a conflict never jumps the queue. A login
    with forge write access can add a label, so `queue/high` counts only when
    its newest timeline event (the gate's `queue_label_events`) is an add by
    the operator's own forge account. Another actor, the shared gate account
    included, leaves the PR normal and untrusted. A timeline the gate could
    not read, or a newest event that is no add, leaves it normal and
    unverified: the privilege fails closed, and the pass goes on.
    """
    events = listed.get("queue_label_events")
    events = events if isinstance(events, dict) else {}
    ranks: dict[int, int] = {}
    unverified: list[int] = []
    untrusted: list[int] = []
    for number in numbers:
        rank = max((_PRIORITY_RANKS[name] for name in _listed_labels(listed, number)
                    if name in _PRIORITY_RANKS), default=PRIORITY_NORMAL)
        if rank == PRIORITY_HIGH:
            newest = events.get(str(number))
            event = newest.get(QUEUE_HIGH_LABEL) if isinstance(newest, dict) else None
            if (not isinstance(event, dict) or not isinstance(event.get("actor"), str)
                    or event.get("added") is not True):
                unverified.append(number)
                rank = PRIORITY_NORMAL
            elif event["actor"].lower() != FORGEJO_OPERATOR_LOGIN.lower():
                untrusted.append(number)
                rank = PRIORITY_NORMAL
        ranks[number] = rank
    return ranks, unverified, untrusted


def _consent_missing(inspected: dict[str, Any]) -> bool:
    """True when a Renovate PR with no consent record still needs one.

    The gate reports `label_history` for every PR it marks as Renovate's:
    each label the forge timeline shows added or removed. A hold label in it
    means the update needed the operator's consent, whether or not the label
    is still there. A history the gate did not report, or reported in any
    other shape, cannot be told from one that holds a hold label.
    """
    history = inspected.get("label_history")
    if not isinstance(history, list) or not all(isinstance(name, str) for name in history):
        return True
    return not RENOVATE_HOLD_LABELS.keys().isdisjoint(history)


def _renovate_labels_unknown(listed: dict[str, Any], renovate: set[int]) -> set[int]:
    """The Renovate PRs whose listed labels could not be read.

    A listing with no `labels` field carries no label data at all, and queue
    order already treats it so. Once the field is there, a Renovate PR whose
    entry is missing, None or anything but a list of names cannot be told
    from one labelled `major` or `premerge`, so it is held too: fail closed,
    never as a PR without a hold label.
    """
    if "labels" not in listed:
        return set()
    labels = listed["labels"]
    if not isinstance(labels, dict):
        return set(renovate)
    return {number for number in renovate
            if not isinstance(names := labels.get(str(number)), list)
            or not all(isinstance(name, str) for name in names)}


# Run identities may come from a timer or a pipeline: a caller names one
# activation identity (the timer, or a pipeline run),
# never a free-form string.
EXECUTOR_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
DEFAULT_EXECUTOR_RUN_PREFIX = "merge-guard-automerge"
# A bounded pass (`--max-reviews`) also starts at most this many reviews per
# repository unless `--max-reviews-per-repo` says otherwise, so one burst of
# green PRs in one repository cannot take every slot. Without `--max-reviews`,
# no per-repository cap applies unless its flag is given.
DEFAULT_MAX_REVIEWS_PER_REPO = 1
# With `--pass-deadline-seconds`, a review starts only while at least one
# review's hard bound (`PRIVATE_REVIEW_SUBPROCESS_TIMEOUT_SECONDS`) plus this
# margin is left: the margin covers the queue-status write before the review
# and the gate reads around it. Without the option the pass has no deadline.
PASS_DEADLINE_MARGIN_SECONDS = 60
# while a paid review runs, the pass re-checks active trains in the
# other repositories once per this many seconds, so a prefix that turns green
# mid-review merges within one interval instead of after the review.
REVIEW_TRAIN_POLL_SECONDS = 30
# The option's default under `--automerge`. The service unit sets it in
# its environment rather than its argv, so an older deployment ignores it
# instead of failing on an unknown option. An explicit option wins; a
# malformed value is ignored with a warning, leaving the pass without a
# deadline.
PASS_DEADLINE_ENV = "GATE_AUTOMERGE_PASS_DEADLINE_SECONDS"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _executor_run_prefix(value: str) -> str:
    if EXECUTOR_RUN_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must match ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
        )
    return value

# journald parses a leading `<N>` syslog priority off each stderr line by
# default; without one every line -- including LOGGER.error -- lands at the
# default priority 6 (info), so a failed review would otherwise read as
# routine in the journal.  systemd sets JOURNAL_STREAM only when stderr is the journal,
# so the prefix is applied only there; a terminal or file redirect keeps the
# plain `%(levelname)s %(message)s` form.
_SYSLOG_PRIORITY = {
    logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4,
    logging.INFO: 6, logging.DEBUG: 7,
}


class _JournalPriorityFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        priority = _SYSLOG_PRIORITY.get(record.levelno, 6)
        return f"<{priority}>{super().format(record)}"

# Gitea (and its Forgejo fork) has no draft flag on a pull request -- the API
# object carries no such field, checked against a real one. A draft there is
# the title prefix the tea family's own web UI writes and reads.
TEA_DRAFT_PREFIXES = ("WIP:", "[WIP]")


def _run(argv: list[str], cwd: str | None = None) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output)."""
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _bounded_first_line(output: str, fallback: str) -> str:
    """Return one printable, bounded diagnostic line."""
    line = output.splitlines()[0].strip() if output else fallback
    return line[:MAX_REASON_CHARS]


def _run_completed(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command, returning the CompletedProcess the gate's forge
    backends expect from their runner.  ``_run`` returns a tuple, so the
    backends cannot take it directly; this is the adapter between the two
    contracts, with identical invocation and capture semantics."""
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False
    )


def _fresh_remote_main(cwd: str) -> tuple[str | None, str | None]:
    """Fetch canonical main and return its exact SHA or a bounded reason."""
    code, output = _run(
        ["git", "fetch", "origin", "main:refs/remotes/origin/main"], cwd=cwd
    )
    if code != 0:
        reason = _bounded_first_line(output, "git fetch failed")
        return None, f"cannot fetch canonical main: {reason}"
    code, output = _run(["git", "rev-parse", "origin/main"], cwd=cwd)
    sha = output.strip().lower()
    if code != 0 or not SHA_RE.fullmatch(sha):
        return None, "fresh canonical main did not resolve to one commit"
    return sha, None


def _is_ancestor(cwd: str, ancestor: str, descendant: str) -> bool:
    code, _output = _run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd
    )
    return code == 0


# --------------------------------------------------------------- repositories


@dataclass(frozen=True)
class Repository:
    """One mergeable repository, as the repository catalog describes it."""

    name: str
    path: Path
    forge: str
    forge_repository: str
    # The catalog's own key, absent on every record but a product one.
    # Defaulted so the four positional fields above stay the constructor
    # every existing caller already uses.
    gate_profile: str | None = None
    # The catalog's `merge_batch_policy`; empty keeps the serial queue. The
    # gate validates the value and refuses every train route without it.
    merge_batch_policy: str = ""
    # The catalog's `merge_candidate_policy` (the narrowing rule); `label-v1` makes the queue
    # mark the PR it is about to land with `gate/candidate`. The gate
    # validates the value and refuses the mark without it.
    merge_candidate_policy: str = ""


def forge_slug(remote: str) -> str:
    """`owner/name` from a Git remote URL, for `tea --repo`.

    Both remote spellings in use reduce the same way.  An scp-style
    `git@forge.example.org:acme/shop-repo.git` keeps everything after the host
    separator; a URL-style `https://host/owner/name.git` keeps its last two
    path segments. Taking the last two segments of the tail handles both
    without branching on which one this is.
    """
    tail = remote.rsplit(":", 1)[-1].removesuffix(".git").strip("/")
    return "/".join(tail.split("/")[-2:])


def load_repositories(repos_yaml: Path = REPOS_YAML) -> dict[str, Repository]:
    """Every repository in the catalog that has somewhere to merge *to*.

    Both sections of the catalog are read; the difference between them
    matters to the gate, not to a merge, and a caller that names one
    gets the same treatment either way.  A record missing a deploy path, a
    forge or a remote is skipped rather than half-built -- it names
    nothing this can act on, and `resolve_selection` reports it as unknown when
    someone asks for it.
    """
    document = load_repos_yaml(repos_yaml)
    records: dict[str, Repository] = {}
    for section in ("canonical_repos", "standalone_repos"):
        for entry in document.get(section) or []:
            name = entry.get("name")
            deploy_path = entry.get("deploy_path")
            forge = entry.get("writable_forge")
            remote = entry.get("remote")
            if not (name and deploy_path and forge and remote):
                continue
            records[name] = Repository(
                name=name,
                path=Path(deploy_path),
                forge=forge,
                forge_repository=forge_slug(remote),
                gate_profile=entry.get("gate_profile"),
                merge_batch_policy=entry.get("merge_batch_policy") or "",
                merge_candidate_policy=entry.get("merge_candidate_policy") or "",
            )
    return records


def automerge_selection(records: dict[str, "Repository"]) -> list[str]:
    """Repository names `--automerge` visits, in the order it visits them.

    `DEFAULT_REPOSITORIES` first, then every record the catalog marks for
    automerge, in catalog order, all merged through the
    same queue.  Derived from the records rather than restated as a constant,
    so a catalog that marks none simply yields the default list.

    The two defaults are named unconditionally, exactly as the empty
    selection named them before: a catalog missing one of them is a
    `RuntimeError` from `resolve_selection`, not a silently shorter pass.
    """
    product = [
        name for name, record in records.items()
        if record.gate_profile == "product-pr" and name not in DEFAULT_REPOSITORIES
    ]
    return list(DEFAULT_REPOSITORIES) + product


def resolve_selection(
    requested: list[str], records: dict[str, Repository]
) -> list[Repository]:
    """The repositories to act on, in the order they were asked for."""
    names = [ALIASES.get(name, name) for name in (requested or DEFAULT_REPOSITORIES)]
    selected: list[Repository] = []
    for name in names:
        if name not in records:
            known = ", ".join(sorted(records) + sorted(ALIASES))
            raise RuntimeError(
                f"{name!r} is not in the repository catalog with a deploy path and a "
                f"forge. Known: {known}"
            )
        if records[name] not in selected:
            selected.append(records[name])
    return selected


# ---------------------------------------------------------------------- pulls


@dataclass(frozen=True)
class Pull:
    """One open pull request, in the facts a batch merge needs.

    `head_sha` comes from the forge rather than from a local `FETCH_HEAD`
    read, and that is load-bearing.  `FETCH_HEAD` holds only the most recent
    fetch, so fetching the base branch after the pull head silently replaces
    one with the other -- and a three-dot diff of the base against itself is
    empty, which reads as "this pull request changes nothing", gives every PR
    a collision degree of zero, and orders a colliding batch as though it were
    independent.  The forge
    already reports the head commit in the listing this record is built from,
    so nothing here has to depend on the order two fetches ran in.
    """

    number: int
    title: str
    head_ref: str
    head_sha: str
    base_ref: str
    labels: tuple[str, ...]
    is_draft: bool

    @property
    def is_renovate(self) -> bool:
        return (
            self.head_ref.startswith(RENOVATE_BRANCH_PREFIX)
            or RENOVATE_LABEL in self.labels
        )


class ForgejoForge:
    """`tea`-backed forge operations for the Forgejo instance.

    Two `tea` output shapes are in play and they do not agree, which is the
    whole reason this reads the way it does.  `tea pulls` (the list form)
    stringifies every value -- `"index": "210"`, `"mergeable": "false"`, and
    `labels` collapsed to a single string -- while `tea pulls <index>` (the
    detail form) returns native JSON types and a real `labels` list, ignoring
    `--fields` entirely.  So the list form is used only to enumerate indexes,
    where a string is unambiguous, and every field a decision rests on is read
    from the detail form.  Splitting that collapsed label string on a separator
    nobody has confirmed is exactly the kind of guess that fails open.

    Forgejo is a Gitea fork with a compatible ``/api/v1``, so the tea
    mechanics apply unchanged. The
    list/detail JSON asymmetry is a property of the tea API family, described
    in exactly one place: here.
    """

    # Which configured `tea` login this forge authenticates as, and therefore
    # which instance it reaches. The Forgejo account: no default may point at
    # another forge instance.
    LOGIN = FORGEJO_LOGIN

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.cwd = str(repository.path)
        self._base = [
            "--login", self.LOGIN, "--repo", repository.forge_repository,
            "--output", "json",
        ]

    def _json(self, args: list[str]) -> Any:
        code, out = _run([TEA_PATH, *args], cwd=self.cwd)
        if code != 0:
            raise RuntimeError(f"tea {' '.join(args)} failed: {out}")
        return json.loads(out) if out else None

    def _detail(self, index: int) -> dict[str, Any]:
        return self._json(["pulls", str(index), *self._base])

    def open_pulls(self) -> list[Pull]:
        rows = self._json(
            ["pulls", *self._base, "--state", "open", "--limit", "100",
             "--fields", "index"]
        ) or []
        pulls: list[Pull] = []
        for row in rows:
            detail = self._detail(int(row["index"]))
            index = int(detail["index"])
            title = detail.get("title") or ""
            head_sha = str(detail.get("headSha") or "").lower()
            if not SHA_RE.fullmatch(head_sha):
                raise RuntimeError(
                    f"#{index}: forge reported a malformed pull-request head"
                )
            pulls.append(
                Pull(
                    number=index,
                    title=title,
                    head_ref=detail.get("head") or "",
                    head_sha=head_sha,
                    base_ref=detail.get("base") or "main",
                    labels=tuple(_tea_labels(detail)),
                    is_draft=title.startswith(TEA_DRAFT_PREFIXES),
                )
            )
        return pulls

    def mergeable(self, pull: Pull) -> str:
        """The tea API answers a boolean where GitHub's answered a tri-state.

        There is no UNKNOWN to wait out, so `false` is reported as CONFLICTING
        and the caller resolves what actually collides against the local tree.
        The API also returns `false` for a PR blocked on required approvals or
        a WIP title, so `merge_one` says "no conflict found locally" rather
        than naming files when `git merge-tree` comes back clean.
        """
        detail = self._detail(pull.number)
        current_head = str(detail.get("headSha") or "").lower()
        if not SHA_RE.fullmatch(current_head):
            return "HEAD_INVALID"
        if current_head != pull.head_sha:
            return "HEAD_CHANGED"
        return "MERGEABLE" if detail.get("mergeable") else "CONFLICTING"

    def not_green_reason(self, pull: Pull) -> str | None:
        """Always a refusal when `--require-green` is passed.

        The tea API's pull-request detail object carries no status field to
        read, so "all checks green" cannot be established from what the forge
        reports here (Forgejo does run CI; this tool just has no read for it).
        Reporting "green" would be a check that cannot fail, so
        `--require-green` skips every pull request rather than waving them
        through.
        """
        return "no CI status readable on this forge; --require-green cannot be satisfied"

    def merge(self, pull: Pull) -> tuple[int, str]:
        # The tea merge argv is written down once, in the packaged
        # `merge_pr` (the shared tea engine); this call
        # preserves this module's (returncode, combined-stripped-output)
        # return contract.
        proc = merge_pr(
            _run_completed, Path(self.cwd), self.repository.forge_repository,
            pull.number, pull.head_sha, login=self.LOGIN,
        )
        status = _http_status(proc)
        returncode = proc.returncode or int(status is not None and not 200 <= status < 300)
        return returncode, (proc.stdout + proc.stderr).strip()


def _tea_labels(detail: dict[str, Any]) -> list[str]:
    """Label names from a detail object, whose entries may be dicts or strings."""
    names: list[str] = []
    for label in detail.get("labels") or []:
        names.append(label.get("name", "") if isinstance(label, dict) else str(label))
    return [name for name in names if name]


Forge = ForgejoForge

# Annotated rather than inferred: a bare dict literal holding one class object
# is inferred as `dict[str, type[ForgejoForge]]` and every lookup out of it
# then types correctly, but the annotation documents that the table is the
# closed forge vocabulary.
FORGES: dict[str, type[ForgejoForge]] = {
    "forgejo": ForgejoForge,
}


def forge_for(repository: Repository) -> Forge:
    if repository.forge not in FORGES:
        raise RuntimeError(
            f"{repository.name}: the repository catalog declares forge "
            f"{repository.forge!r}, which nothing here can merge on"
        )
    return FORGES[repository.forge](repository)


# ------------------------------------------------------------ local git facts


def ensure_head_local(pull: Pull, cwd: str) -> bool:
    """Make this PR's head and its base branch available to local git.

    Both forges publish `refs/pull/<n>/head`, so one fetch answers for either,
    and the head may well not be local yet -- a PR from a lane this checkout
    never fetched.  Fetch order does not matter here and must not: the commit
    to compare against is `pull.head_sha`, taken from the forge, never from
    whichever fetch happened to write `FETCH_HEAD` last.

    Nothing is written to the working tree and no ref is created, so a managed
    checkout stays clean and the gate keeps accepting it.

    The base moves its remote-tracking ref only by fast-forward, as the gate's
    own `fetch_main` does. A bare `git fetch origin main` would apply
    the checkout's configured `+refs/heads/*` refspec and force
    `origin/main` onto a rewritten remote main, so the gate's next fetch could
    no longer name the rewrite. `--refmap=` drops that configured refspec and
    the explicit destination carries no `+`. A refused base fetch raises:
    `changed_paths` and the preflight read `origin/<base>`, and a rewritten
    main is the operator's to accept.
    """
    _run(["git", "fetch", "-q", "origin", f"refs/pull/{pull.number}/head"], cwd=cwd)
    code, output = _run(
        ["git", "fetch", "-q", "--refmap=", "origin",
         f"refs/heads/{pull.base_ref}:refs/remotes/origin/{pull.base_ref}"], cwd=cwd)
    if code != 0:
        raise RuntimeError(
            f"#{pull.number}: cannot fast-forward origin/{pull.base_ref}: "
            f"{_bounded_first_line(output, 'git fetch failed')}")
    code, _ = _run(["git", "cat-file", "-e", f"{pull.head_sha}^{{commit}}"], cwd=cwd)
    return code == 0


def changed_paths(pull: Pull, cwd: str) -> set[str]:
    """The files this PR changes, from the merge base rather than from the tip.

    Three-dot diff, so a base branch that moved under the PR does not make every
    file someone else touched look like this PR's work -- which would inflate
    its collision degree and push an independent PR to the back of the queue for
    no reason.  Computed locally because Gitea's CLI has no changed-files call
    at all, and doing it the same way on both forges means one behaviour to
    reason about instead of two.

    An empty set here is indistinguishable from "this PR changes nothing", and
    both make a PR look independent, so an unreachable head raises rather than
    returning one.  `ensure_head_local` is the guard; this asserts it ran.
    """
    if not pull.head_sha:
        raise RuntimeError(f"#{pull.number}: the forge reported no head commit")
    try:
        return _diff_paths(
            LocalRunner(), cwd, f"origin/{pull.base_ref}", pull.head_sha,
            merge_base=True,
        )
    except HostDeployError as exc:
        raise RuntimeError(f"#{pull.number}: cannot diff against its base: {exc}") from exc


def collision_order(
    pulls: list[Pull], paths: dict[int, set[str]]
) -> list[tuple[Pull, int]]:
    """Order PRs by how many siblings share a changed path with them.

    Ascending by degree, then by number so a run is reproducible.  Degree 0
    means nothing else in this batch touches any file this PR touches, so
    nothing this run does can turn it conflicted.
    """
    degree = {
        pull.number: sum(
            1
            for other in pulls
            if other.number != pull.number
            and paths[pull.number] & paths[other.number]
        )
        for pull in pulls
    }
    return sorted(
        ((pull, degree[pull.number]) for pull in pulls),
        key=lambda item: (item[1], item[0].number),
    )


def conflicting_paths(pull: Pull, cwd: str) -> list[str]:
    """The files that actually collide between this PR's head and its base.

    `git merge-tree --write-tree` performs the merge in memory: exit 1 means
    conflicts, the first output line is the resulting tree OID, and the lines
    after it up to the first blank line are the conflicted paths.  Everything
    past that blank line is human-readable prose in the caller's locale, so it
    is never parsed.
    """
    if not pull.head_sha:
        return []
    code, out = _run(
        ["git", "merge-tree", "--write-tree", "--name-only",
         f"origin/{pull.base_ref}", pull.head_sha],
        cwd=cwd,
    )
    if code != 1:
        return []
    return _conflicted_names(out)


def _conflicted_names(out: str) -> list[str]:
    """The conflicted paths from a `--name-only` merge-tree body."""
    paths: list[str] = []
    for line in out.splitlines()[1:]:
        if not line.strip():
            break
        paths.append(line)
    return paths


# ------------------------------------------------------------------- preflight
#
# `collision_order` answers "which PRs *might* interfere" from shared changed
# paths, and orders the batch so a failure lands where it blocks least.  That is
# a proxy, and it over-reports: two PRs editing different sections of one README
# share a path and merge perfectly cleanly.  What it cannot do is answer the
# question the module docstring opens with -- whether a PR that reads MERGEABLE
# now will *still* merge after its siblings land -- because the forge measures
# `mergeable` against the base as it stands, never against a sibling.
#
# This simulates the plan instead of proxying it.  Walk the planned order,
# merging each head into a base that already carries everything ahead of it, and
# report what actually collides.  Nothing here touches the working tree, creates
# a ref, or contacts the forge: `merge-tree` and `commit-tree` write loose
# objects into the object store and nothing points at them, so they are ordinary
# garbage and `git gc` reaps them.  That is why this is safe to run against the
# working checkout while other work is in flight.
#
# Deliberately dry-run only.  Predicting a conflict is useful; *acting* on the
# prediction would put this simulation in the path of every merge, and a wrong
# simulation would then withhold a PR that would have landed.  Let the operator
# read it and decide.

_SIM_IDENT = [
    "-c", "user.name=merge-preflight",
    "-c", "user.email=merge-preflight@example.invalid",
]


_OID_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


class MergeVerdict(StrEnum):
    """The closed `verdict`/`status` vocabulary this preflight can report.

    `_merge_tree` itself only ever returns `CLEAN`, `CONFLICT` or `ERROR`;
    `UNKNOWN` completes the set `preflight_batch` actually persists onto
    `record["verdict"]` for a PR with no head commit to simulate. A `StrEnum`
    so the record stays a plain string wherever it is printed or compared,
    while a typo in a comparison (`MergeVerdict.CONFLICT` misspelled) is an
    `missing-attribute` ty catches instead of a silently-false string.
    """

    CLEAN = "clean"
    CONFLICT = "conflict"
    ERROR = "error"
    UNKNOWN = "unknown"


def _merge_tree(base: str, head: str, cwd: str) -> tuple[MergeVerdict, str, list[str]]:
    """Merge *head* into *base* in memory.

    Returns ``(status, tree_oid, conflicted_paths)`` where status is ``clean``,
    ``conflict`` or ``error``.

    **The exit code is not the discriminator, and assuming it was is a live
    trap.**  `git merge-tree` exits 0 for a clean merge and 1 for conflicts --
    but it also exits 1 for "not something we can merge", which is what an
    unreachable commit or a typo'd ref produces.  A conflict and an input it
    could not resolve are therefore indistinguishable by status alone, and
    treating every 1 as a conflict quietly converts "I could not check" into a
    definite answer.

    What separates them is the first output line: a real merge, conflicted or
    not, begins with the written tree's OID, and a failed one begins with an
    error message.  So the OID shape is the test.  Never the message -- it is
    localised, and under a non-English locale it arrives translated.

    Accepts both OID lengths so this keeps working on a sha256 repository.
    """
    code, out = _run(
        ["git", "merge-tree", "--write-tree", "--name-only", base, head], cwd=cwd
    )
    lines = out.splitlines()
    tree = lines[0].strip() if lines else ""
    if not _OID_RE.match(tree):
        return MergeVerdict.ERROR, "", []
    if code == 0:
        return MergeVerdict.CLEAN, tree, []
    if code == 1:
        return MergeVerdict.CONFLICT, tree, _conflicted_names(out)
    return MergeVerdict.ERROR, "", []


def _project(base: str, head: str, tree: str, cwd: str) -> str:
    """A commit standing for "base with head merged in", for the next round.

    Returns the new commit OID, or "" if git would not build one -- in which
    case the caller stops simulating rather than silently continuing against a
    base that no longer reflects what has landed.
    """
    code, out = _run(
        ["git", *_SIM_IDENT, "commit-tree", tree, "-p", base, "-p", head,
         "-m", "merge preflight (simulated, unreferenced)"],
        cwd=cwd,
    )
    return out.strip() if code == 0 else ""


def preflight_batch(
    ordered: list[tuple[Pull, int]], cwd: str
) -> list[dict[str, Any]]:
    """Predict each PR's fate in the planned order, before anything merges.

    One record per PR, in plan order, with ``verdict`` one of ``clean``,
    ``conflict``, ``error`` or ``unknown``.  A conflicting PR does not advance
    the projected base, because a PR that cannot merge does not land -- so the
    PRs behind it are judged against the base they would really meet.

    ``after`` names the siblings already folded into the base when this PR was
    tested, which is the whole point: a PR that is clean against `main` today
    and conflicts here is one the forge would have called MERGEABLE right up
    until the moment it failed.
    """
    records: list[dict[str, Any]] = []
    # Per base_ref: a batch spanning two base branches is two independent
    # sequences, and simulating them as one would merge unrelated histories.
    projected: dict[str, str] = {}
    landed: dict[str, list[int]] = {}

    for pull, degree in ordered:
        base_ref = pull.base_ref
        base = projected.setdefault(base_ref, f"origin/{base_ref}")
        ahead = landed.setdefault(base_ref, [])
        record: dict[str, Any] = {
            "pr": pull.number,
            "degree": degree,
            "after": list(ahead),
        }

        if not pull.head_sha:
            record["verdict"] = MergeVerdict.UNKNOWN
            record["detail"] = "the forge reported no head commit"
            records.append(record)
            continue

        status, tree, paths = _merge_tree(base, pull.head_sha, cwd)
        record["verdict"] = status
        if status == MergeVerdict.CONFLICT:
            record["conflicting_paths"] = paths
            records.append(record)
            continue
        if status == MergeVerdict.ERROR:
            record["detail"] = f"git merge-tree could not merge onto {base}"
            records.append(record)
            continue

        nxt = _project(base, pull.head_sha, tree, cwd)
        if not nxt:
            # Simulation cannot continue truthfully past here.  Say so on this
            # record rather than testing the rest against a stale base.
            record["detail"] = "simulated further merges unavailable"
            projected[base_ref] = base
        else:
            projected[base_ref] = nxt
            ahead.append(pull.number)
        records.append(record)

    return records


def _render_preflight(record: dict[str, Any]) -> str:
    """One plan line: the PR, why it is placed here, and what will happen."""
    degree = record["degree"]
    placement = (
        "independent" if degree == 0 else f"shares files with {degree} sibling(s)"
    )
    verdict = record["verdict"]
    if verdict == MergeVerdict.CLEAN:
        outcome = "merges clean"
    elif verdict == MergeVerdict.CONFLICT:
        after = record.get("after") or []
        when = (
            " once " + ", ".join(f"#{n}" for n in after) + " land"
            if after else " against its base"
        )
        paths = ", ".join(record.get("conflicting_paths") or []) or "unnamed paths"
        outcome = f"CONFLICTS{when} — in: {paths}"
    else:
        outcome = f"{verdict}: {record.get('detail', 'no detail')}"
    return f"    #{record['pr']:<6} {placement:<30} {outcome}"


# --------------------------------------------------------------------- merging


def merge_one(
    pull: Pull,
    forge: Forge,
    require_green: bool,
) -> dict[str, Any]:
    """Attempt one merge, returning a result record with a reason on failure."""
    cwd = str(forge.repository.path)
    if not SHA_RE.fullmatch(pull.head_sha):
        return {
            "pr": pull.number,
            "outcome": "failed",
            "reason": "selected pull-request head is malformed",
        }
    state = forge.mergeable(pull)
    if state == "UNKNOWN":
        time.sleep(UNKNOWN_RETRY_SECONDS)
        state = forge.mergeable(pull)

    if state in {"HEAD_CHANGED", "HEAD_INVALID"}:
        reason = (
            "pull-request head changed after batch selection"
            if state == "HEAD_CHANGED"
            else "fresh pull-request head is malformed"
        )
        return {"pr": pull.number, "outcome": "failed", "reason": reason}

    if state != "MERGEABLE":
        record: dict[str, Any] = {
            "pr": pull.number, "outcome": "skipped", "reason": state.lower(),
        }
        if state == "CONFLICTING":
            paths = conflicting_paths(pull, cwd)
            record["conflicting_paths"] = paths
            record["reason"] = (
                "conflicts with " + pull.base_ref + " in: " + ", ".join(paths)
                if paths
                else f"forge reports not mergeable, but no conflict with "
                     f"{pull.base_ref} was found locally"
            )
        return record

    if require_green:
        not_green_reason = forge.not_green_reason(pull)
        if not_green_reason:
            return {
                "pr": pull.number,
                "outcome": "skipped",
                "reason": not_green_reason,
            }

    if pull.base_ref != "main":
        return {
            "pr": pull.number,
            "outcome": "skipped",
            "reason": f"base {pull.base_ref!r} is not canonical main",
        }

    origin_main_before, fetch_reason = _fresh_remote_main(cwd)
    if origin_main_before is None:
        return {
            "pr": pull.number,
            "outcome": "failed",
            "reason": fetch_reason,
        }

    code, out = forge.merge(pull)
    if code != 0:
        return {
            "pr": pull.number, "outcome": "failed",
            "reason": _bounded_first_line(out, "merge command failed"),
        }
    origin_main_after, fetch_reason = _fresh_remote_main(cwd)
    if origin_main_after is None:
        return {
            "pr": pull.number,
            "outcome": "failed",
            "reason": fetch_reason,
            "authority_integrity_failed": True,
            "pre_merge_origin_main_sha": origin_main_before,
            "selected_head_sha": pull.head_sha,
        }

    missing: list[str] = []
    for label, candidate in (
        ("pre-merge main", origin_main_before),
        ("selected head", pull.head_sha),
    ):
        if not _is_ancestor(cwd, candidate, origin_main_after):
            missing.append(f"{label} {candidate}")
    if missing:
        return {
            "pr": pull.number,
            "outcome": "failed",
            "reason": (
                "fresh canonical main "
                f"{origin_main_after} omits " + ", ".join(missing)
            ),
            "pre_merge_origin_main_sha": origin_main_before,
            "selected_head_sha": pull.head_sha,
            "origin_main_sha": origin_main_after,
            "authority_integrity_failed": True,
        }
    return {
        "pr": pull.number,
        "outcome": "merged",
        "pre_merge_origin_main_sha": origin_main_before,
        "selected_head_sha": pull.head_sha,
        "origin_main_sha": origin_main_after,
    }


def _render(record: dict[str, Any]) -> str:
    """One line per pull request, reason included when it is not a merge."""
    if record["outcome"] == "merged":
        return f"#{record['pr']:<6} MERGED"
    return f"#{record['pr']:<6} {record['outcome'].upper()}: {record['reason']}"


def _held_back(
    pulls: list[Pull], include_drafts: bool, include_renovate: bool
) -> tuple[list[Pull], list[str]]:
    """Split the batch into what will be merged and why the rest will not.

    Drafts were already excluded before Renovate was, but silently, so a run
    could report "no open pull requests" while some existed.  Both exclusions
    are announced now; the notes are printed, never
    counted as failures, so holding everything back still exits 0.

    A draft Renovate PR is reported once, as a draft, rather than counted under
    both headings and making the two notes sum to more than the batch.
    """
    drafts = [] if include_drafts else [pull for pull in pulls if pull.is_draft]
    held = {pull.number for pull in drafts}
    renovate = (
        [] if include_renovate
        else [
            pull for pull in pulls
            if pull.is_renovate and pull.number not in held
        ]
    )
    notes: list[str] = []
    if drafts:
        numbers = ", ".join(f"#{pull.number}" for pull in drafts)
        notes.append(f"holding back {len(drafts)} draft: {numbers}")
    if renovate:
        numbers = ", ".join(f"#{pull.number}" for pull in renovate)
        notes.append(
            f"holding back {len(renovate)} renovate: {numbers} "
            f"(--include-renovate to merge them)"
        )
    excluded = held | {pull.number for pull in renovate}
    return [pull for pull in pulls if pull.number not in excluded], notes


def run_one(repository: Repository, args: argparse.Namespace) -> dict[str, Any]:
    """Merge one repository's batch, returning a JSON-shaped summary."""
    forge = forge_for(repository)
    cwd = str(repository.path)
    pulls, notes = _held_back(
        forge.open_pulls(), args.include_drafts, args.include_renovate
    )
    for note in notes:
        if not args.json:
            print(f"  {note}", flush=True)

    if not pulls:
        if not args.json:
            print("  no open pull requests to merge", flush=True)
        return {"repository": repository.name, "notes": notes, "results": []}

    unreachable = [pull for pull in pulls if not ensure_head_local(pull, cwd)]
    if unreachable:
        numbers = ", ".join(f"#{pull.number}" for pull in unreachable)
        raise RuntimeError(
            f"could not fetch the head commit of {numbers}; refusing to order a "
            f"batch on incomplete history"
        )
    paths = {pull.number: changed_paths(pull, cwd) for pull in pulls}
    ordered = collision_order(pulls, paths)

    if args.dry_run:
        # Simulated, not proxied: every line below is the outcome of an actual
        # in-memory merge against the base that PR would really meet.  Reported
        # under `preflight` rather than folded into `results`, which a --json
        # consumer is entitled to read as "what this run merged" -- and a
        # dry-run merged nothing.
        preflight = preflight_batch(ordered, cwd)
        if not args.json:
            print(f"  plan for {len(ordered)} open PR(s), independent first:")
            for record in preflight:
                print(_render_preflight(record))
            blocked = [r for r in preflight if r["verdict"] == MergeVerdict.CONFLICT]
            unknown = [
                r
                for r in preflight
                if r["verdict"] in (MergeVerdict.ERROR, MergeVerdict.UNKNOWN)
            ]
            if blocked:
                numbers = ", ".join(f"#{r['pr']}" for r in blocked)
                print(
                    f"  --- {len(blocked)} of {len(preflight)} will conflict in "
                    f"this order: {numbers}"
                )
            if unknown:
                numbers = ", ".join(f"#{r['pr']}" for r in unknown)
                print(
                    f"  --- {len(unknown)} could not be simulated, so their "
                    f"outcome is unproven, not clean: {numbers}"
                )
            if not blocked and not unknown:
                print(f"  --- all {len(preflight)} merge clean in this order")
        return {
            "repository": repository.name,
            "notes": notes,
            "results": [],
            "preflight": preflight,
        }

    # Each merge is a round trip to the forge and takes seconds, so a run of a
    # dozen is long enough that a silent terminal reads as a hang. Print the
    # plan before starting and each outcome the moment it is known, flushing
    # every line: without the flush this is block-buffered the instant stdout
    # is a pipe (the command piped to a file or a log), and the whole point is lost
    # exactly where nobody is watching a TTY to notice.
    if not args.json:
        print(f"  merging {len(ordered)} open PR(s), independent first:", flush=True)

    results: list[dict[str, Any]] = []
    for pull, _ in ordered:
        record = merge_one(pull, forge, args.require_green)
        results.append(record)
        if not args.json:
            print(f"  {_render(record)}", flush=True)
        if record.get("authority_integrity_failed"):
            if not args.json:
                print(
                    "  stopping batch: canonical history preservation is unproven",
                    flush=True,
                )
            break

    if not args.json:
        merged = sum(1 for record in results if record["outcome"] == "merged")
        print(f"  --- {merged}/{len(results)} merged", flush=True)

    return {
        "repository": repository.name,
        "notes": notes,
        "results": results,
        "authority_integrity_failed": any(
            bool(record.get("authority_integrity_failed")) for record in results
        ),
    }


def _gate_application() -> Any:
    """Load the gate API and its fixed operations adapter."""
    from gate_server.application import GateApplication
    from gate_server_estate import EstateGateOperations

    return GateApplication(EstateGateOperations())


# A fresh failed run returns REVIEW_INCOMPLETE and is left for the next pass;
# that pass's reread returns REVIEW_EXECUTION_UNCERTAIN with cleanup proven,
# and only then is the successor started. Retrying REVIEW_INCOMPLETE inline
# would start a second up-to-REVIEW_TIMEOUT execution in the same pass --
# doubling its length, risking the pipeline's own timeout (a kill there
# leaves `cleanup_proven` false, which is never retried), and running straight
# back into whatever transient provider problem just failed. Waiting for the
# next pass's stale-receipt reread (the review module's `_UNCERTAIN` branch) is what
# spaces the retry out and matches the documented manual recovery
# (a manual review retry), which already requires exactly this evidence
# before retrying by hand. This is the automated form of that same check.
_RETRYABLE_UNCERTAIN_CODES = frozenset({"REVIEW_EXECUTION_UNCERTAIN"})

# Successful results that nevertheless launched nothing. `ok` alone cannot
# tell these apart from a published review, so they are named.
# `REVIEW_APPROVAL_REUSED` dispatches no model and writes no
# receipt: the executor only asked git whether the refresh was clean, using
# objects it had already fetched.
_REVIEW_NON_ATTEMPT_OK_CODES = frozenset({
    "REVIEW_APPROVAL_REUSED", "REVIEW_NATIVE_REUSED", "REVIEW_REUSED", "REVIEW_RECONCILED",
})


def _review_attempt_started(reviewed: dict[str, Any]) -> bool:
    """True when a `review()` call itself launched or finished an execution.

    Only a proved receipt reread, a busy attempt lock, a clean approval
    reuse, or a refusal the review owner marks `dispatched: false` leaves the
    budget untouched. A failed post-dispatch CI/ref check or ambiguous
    transport may already have consumed a paid execution.

    A reused clean-refresh approval is `ok` and is still not an attempt.
    Counting it would let one reuse displace a candidate that genuinely needs
    a review slot of the pass, which is the opposite of what the reuse
    buys.
    """
    if reviewed.get("code") in _REVIEW_NON_ATTEMPT_OK_CODES:
        return False
    # The review owner refused before it wrote a dispatch intent.
    if reviewed.get("dispatched") is False:
        return False
    if reviewed.get("code") == "REVIEW_ATTEMPT_BUSY":
        return False
    if reviewed.get("code") == "REVIEW_EXECUTION_UNCERTAIN":
        # A named predecessor is a receipt reread. Without one, dispatch may
        # have started before the transport failed: spend the paid slot.
        return not (bool(reviewed.get("run_marker"))
                    and reviewed.get("cleanup_proven") is True)
    # An unrecognized failure after dispatch may have consumed a model call.
    # The review owner alone can prove a call never started.
    return True


def _log_primary_not_started(repository: str, pr: int, head: str, reviewed: Any) -> None:
    """Log one alertable line when this attempt's primary reviewer never started.

    The review owner names the outage in `primary_not_started`; the gate then
    went straight to the fallback reviewer. The owner marks a fresh dispatch
    with `dispatched: true`; a receipt reread or a resumed publication never
    does, so one outage is one line, however often its receipt is read again.
    This is separate from `_review_attempt_started`, which counts the review
    budget. The line carries no detail.
    """
    outage = reviewed.get("primary_not_started") if isinstance(reviewed, dict) else None
    if isinstance(outage, dict) and reviewed.get("dispatched") is True:
        LOGGER.error("automerge: review_primary_not_started %s#%s head=%s route=%s err=%s",
                     repository, pr, head, outage.get("worker"), outage.get("error"))


def _retryable_uncertain(reviewed: dict[str, Any]) -> bool:
    """Retry a receipt reread only when its owner explicitly authorizes it.

    A fresh REVIEW_INCOMPLETE spends this pass's slot and waits for a later
    pass. No guessed state or cleanup flag can authorize another paid call.
    """
    return (not reviewed.get("ok")
            and reviewed.get("code") in _RETRYABLE_UNCERTAIN_CODES
            and reviewed.get("recovery_action") == "retry"
            and isinstance(reviewed.get("run_marker"), str)
            and bool(reviewed["run_marker"]))


def _review_with_recovery(application: Any, repository: str, pr: int,
                          head: str, base: str, executor_run: str) -> dict[str, Any]:
    """Share one call and at most one linked retry between active and ahead."""
    reviewed = application.review(repository, pr, head, base, executor_run)
    if not _retryable_uncertain(reviewed):
        return reviewed
    retried = application.review(repository, pr, head, base, executor_run,
                                 retry_of=str(reviewed["run_marker"]))
    if retried.get("ok") or not str(retried.get("code") or "").startswith("REVIEW_RETRY_"):
        return retried
    return {**reviewed, "recovery_action": "operator", "retry_refused": str(retried["code"])}


class _ReviewBudget:
    """Proxies the gate API, counting only its *started* `review` calls.

    `--max-reviews` bounds a pass by finished or launched review attempts,
    not by candidates visited outright -- a candidate needing no review (CI
    not green, changes requested, already approved) must not count against
    the budget, and wrapping only `review` is what keeps that true without
    threading a second return value through `_automerge_candidate`. Within a
    `review()` call, an instant refusal (a busy attempt lock, an uncertain
    receipt reread with no fresh run) likewise leaves the slot unspent --
    `_review_attempt_started` is the shared rule for both.

    `--max-reviews-per-repo` adds a second cap on the same count, kept per
    repository: one repository's burst of green PRs cannot take every slot
    of the pass, and a repository at its cap stops only itself.

    `--pass-deadline-seconds` adds a clock: no `review()` call starts once
    the time left before `deadline` is less than one review's hard bound
    plus `PASS_DEADLINE_MARGIN_SECONDS`. The pass cannot tell a paid
    dispatch from a cheap receipt reread before it asks, so the deadline
    defers both; an already-approved PR never calls `review()` and still
    merges.

    `--review-workers N` lets one `review()` call start up to N-1
    lookahead reviews of later queued PRs once its own review has run for
    one `REVIEW_TRAIN_POLL_SECONDS` slice. The
    pass chooses each one (`lookahead_candidate`). A review in flight holds
    its slot in both caps from its start, so concurrent reviews cannot
    overshoot either cap, and the deadline check runs before each start, so
    no review can outlive the deadline. The pass records each lookahead
    outcome as `_review_ahead` records its own and never merges on one: the
    review owner publishes it, and the PR's own turn reads it from the
    forge. An authority or integrity failure the pass meets while choosing
    or settling a lookahead ends the call with `_TrainStop` once every
    review it started has ended.
    """

    def __init__(self, application: Any, maximum: int | None,
                 per_repository: int | None = None, *,
                 deadline: float | None = None,
                 now: Callable[[], float] = time.time,
                 workers: int = 1) -> None:
        self._application = application
        self.maximum = maximum
        self.per_repository = per_repository
        self.deadline = deadline
        self._now = now
        self.workers = workers
        self.review_count = 0
        self.repository_counts: dict[str, int] = {}
        # Reviews started and not yet settled, per repository.
        self._in_flight: dict[str, int] = {}
        # Every (repository, pr) whose lookahead attempt started in this pass.
        # Its successor waits for the next pass, as after a failed active
        # review (`_RETRYABLE_UNCERTAIN_CODES`).
        self.lookahead_attempted: set[tuple[str, int]] = set()
        self.before_review: Callable[[str, int, str], None] | None = None
        self.checkpoint: Callable[[], None] | None = None
        # A paid review can hold the pass for minutes; a train prefix that
        # turns green meanwhile is merged before, during and after it, not on
        # the next pass. The call during the review passes `reviewing` and,
        # with lookaheads in flight, `lookahead`: the trains of those
        # repositories wait for their reviews to end.
        self.advance_trains: Callable[..., None] | None = None
        # Set by the pass: `lookahead_candidate(reviewing, busy)` returns the
        # next (repository, pr, head, base, executor_run) to review ahead, or
        # None, and raises `_TrainStop` on an authority or integrity failure;
        # `after_lookahead(repository, pr, result)` records each outcome and
        # returns True on such a failure rather than raising, so every review
        # still in flight is settled and recorded.
        self.lookahead_candidate: Callable[
            [str, set[tuple[str, int]]], tuple[str, int, str, str, str] | None] | None = None
        self.after_lookahead: Callable[[str, int, dict[str, Any]], bool] | None = None

    def __getattr__(self, name: str) -> Any:
        operation = getattr(self._application, name)
        if name not in {"refresh", "merge", "mark_candidate", "rerun_lost", "train_build",
                        "train_member_merge", "train_close"}:
            return operation

        def checked(*args: Any, **kwargs: Any) -> Any:
            if self.checkpoint is not None:
                self.checkpoint()
            return operation(*args, **kwargs)

        return checked

    def spent(self) -> bool:
        """True once the pass-wide cap is reached, counting reviews in flight."""
        return self.maximum is not None and (
            self.review_count + sum(self._in_flight.values()) >= self.maximum)

    def exhausted(self, repository: str) -> bool:
        """True when either cap refuses another review in `repository`."""
        return self.spent() or (
            self.per_repository is not None
            and self.repository_counts.get(repository, 0) + self._in_flight.get(repository, 0)
            >= self.per_repository)

    def out_of_time(self) -> bool:
        """True once one more review could outlast the pass deadline."""
        return self.deadline is not None and self.deadline - self._now() < (
            PRIVATE_REVIEW_SUBPROCESS_TIMEOUT_SECONDS + PASS_DEADLINE_MARGIN_SECONDS)

    def _count(self, repository: str) -> None:
        self.review_count += 1
        self.repository_counts[repository] = self.repository_counts.get(repository, 0) + 1

    def _review_logged(self, repository: str, pr: int, head: str, *args: Any,
                       **kwargs: Any) -> Any:
        """One `review` call, then its primary-outage line, on the worker thread."""
        reviewed = self._application.review(repository, pr, head, *args, **kwargs)
        _log_primary_not_started(repository, pr, head, reviewed)
        return reviewed

    def _start(self, pool: ThreadPoolExecutor, repository: str, *args: Any,
               **kwargs: Any) -> Future[Any]:
        """Start one review on `pool`, holding its slot until `_settle`."""
        running = pool.submit(self._review_logged, repository, *args, **kwargs)
        self._in_flight[repository] = self._in_flight.get(repository, 0) + 1
        return running

    def _settle(self, repository: str, running: Future[Any]) -> Any:
        """Return a finished review's result and spend its slot, as one call would."""
        self._in_flight[repository] -= 1
        try:
            result = running.result()
        except Exception:
            self._count(repository)
            raise
        if _review_attempt_started(result):
            self._count(repository)
        return result

    def _lookahead_ended(self, repository: str, pr: int, reviewed: dict[str, Any]) -> bool:
        """Log and record one lookahead outcome; True on an authority or integrity stop."""
        verdict = reviewed.get("verdict")
        LOGGER.log(logging.INFO if reviewed.get("ok") else logging.WARNING,
                   "automerge: lookahead %s#%s %s%s", repository, pr,
                   reviewed.get("code") or "-", f" {verdict}" if verdict else "")
        return self.after_lookahead is not None and self.after_lookahead(repository, pr, reviewed)

    def _settle_lookaheads(self, ahead: dict[Future[Any], tuple[str, int]],
                           finished: set[Future[Any]]) -> bool:
        """Settle each finished lookahead in start order; True if one stops the pass.

        A raised review keeps a gate refusal's code, as `_candidate_exception`
        does, so an authority or integrity refusal still stops the pass.
        """
        stop = False
        for running in [running for running in ahead if running in finished]:
            repository, pr = ahead.pop(running)
            try:
                reviewed = self._settle(repository, running)
            except Exception as exc:
                reviewed = {"ok": False, "code": exc.code if isinstance(exc, GateError)
                            else f"REVIEW_RAISED:{type(exc).__name__}"}
                self.lookahead_attempted.add((repository, pr))
            else:
                if _review_attempt_started(reviewed):
                    self.lookahead_attempted.add((repository, pr))
            stop = self._lookahead_ended(repository, pr, reviewed) or stop
        return stop

    def _look_ahead(self, pool: ThreadPoolExecutor, ahead: dict[Future[Any], tuple[str, int]],
                    repository: str, pr: int) -> None:
        """Start lookahead reviews in the free workers while `repository#pr` is reviewed.

        Each start runs the same checks and status publication as `review()`
        itself. A candidate in a repository at its cap is skipped.
        """
        while 1 + len(ahead) < self.workers and self.lookahead_candidate is not None:
            if self.checkpoint is not None:
                self.checkpoint()
            if self.spent() or self.out_of_time():
                return
            candidate = self.lookahead_candidate(repository, {(repository, pr), *ahead.values()})
            if candidate is None:
                return
            name, number, head, base, executor_run = candidate
            if self.exhausted(name):
                continue
            if self.before_review is not None:
                self.before_review(name, number, head)
            if self.checkpoint is not None:
                self.checkpoint()
            if self.out_of_time():
                self._lookahead_ended(name, number, {"ok": False, "code": "PASS_DEADLINE"})
                return
            # One call, never a linked retry: that stays with the PR's own turn.
            ahead[self._start(pool, name, number, head, base, executor_run)] = (name, number)

    def review(self, repository: str, pr: int, head: str, base: str,
               executor_run: str, retry_of: str | None = None) -> Any:
        if self.checkpoint is not None:
            self.checkpoint()
        if self.exhausted(repository):
            return {"ok": False, "code": "REVIEW_BUDGET_EXHAUSTED"}
        if (repository, pr) in self.lookahead_attempted:
            # A lookahead already started an attempt of this PR in this pass.
            return {"ok": False, "code": "REVIEW_ATTEMPT_BUSY", "dispatched": False}
        # Before `before_review`, which publishes `reviewing` to the forge.
        if self.out_of_time():
            return {"ok": False, "code": "PASS_DEADLINE"}
        if self.advance_trains is not None:
            self.advance_trains()
        if self.before_review is not None:
            self.before_review(repository, pr, head)
        # Status publication can block on the forge while deploy announces intent.
        if self.checkpoint is not None:
            self.checkpoint()
        if self.out_of_time():
            return {"ok": False, "code": "PASS_DEADLINE"}
        # every review runs on a worker thread and does nothing else.
        # This thread alone chooses lookaheads, advances trains, probes the
        # deploy lock and saves state. It never returns before every review
        # it started does, so no review subprocess is ever left without its
        # supervisor, and the pass never moves a main that one is bound to.
        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix="automerge-review") as worker:
            running = self._start(worker, repository, pr, head, base, executor_run,
                                  **({"retry_of": retry_of} if retry_of is not None else {}))
            ahead: dict[Future[Any], tuple[str, int]] = {}
            try:
                # Lookaheads start only once this review outlasts one slice: a
                # reuse or a refusal answers at once and must not wait for them.
                # A lookahead failure that stops the pass starts nothing more
                # and discards this review's result (`_TrainStop`).
                while True:
                    finished = wait((running, *ahead), timeout=REVIEW_TRAIN_POLL_SECONDS,
                                    return_when=FIRST_COMPLETED).done
                    if self._settle_lookaheads(ahead, finished):
                        raise _TrainStop(repository)
                    if running in finished:
                        break
                    if self.advance_trains is not None:
                        self.advance_trains(reviewing=repository, **_lookahead_kwargs(ahead))
                    self._look_ahead(worker, ahead, repository, pr)
                while ahead:
                    if self._settle_lookaheads(ahead, wait(
                            tuple(ahead), timeout=REVIEW_TRAIN_POLL_SECONDS,
                            return_when=FIRST_COMPLETED).done):
                        raise _TrainStop(repository)
                    if ahead and self.advance_trains is not None:
                        self.advance_trains(**_lookahead_kwargs(ahead))
            except BaseException:
                # A stop or deploy yield met during the reviews waits for each
                # (`_settle` blocks) and spends its slot as usual. The review
                # owner publishes before it returns; this pass discards it and
                # records each lookahead outcome.
                try:
                    code = self._settle(repository, running).get("code")
                except Exception as exc:
                    code = f"REVIEW_RAISED:{type(exc).__name__}"
                LOGGER.warning("automerge: %s#%s review ended after a stop during it: %s",
                               repository, pr, code)
                self._settle_lookaheads(ahead, set(ahead))
                raise
        result = self._settle(repository, running)
        # The review owner has completed native publication before returning.
        if self.checkpoint is not None:
            self.checkpoint()
        if self.advance_trains is not None and _review_attempt_started(result):
            self.advance_trains()
        return result


def _lookahead_kwargs(ahead: dict[Future[Any], tuple[str, int]]) -> dict[str, frozenset[str]]:
    """`advance_trains`' `lookahead` argument: no key at all while none runs."""
    return {"lookahead": frozenset(name for name, _pr in ahead.values())} if ahead else {}


class _RefreshBudget:
    """At most one refresh or successful/uncertain train build per repository per pass.

    A fixed limit, not a separate scheduler setting.
    `run_automerge_pass` creates a fresh budget for each repository. Any attempted
    `refresh()` call spends it, whatever the result, so a conflict or a
    changed state is left for a later tick and never retried in this pass.
    A refresh is not a review: it never touches `_ReviewBudget` and never
    stops the pass.
    """

    def __init__(self) -> None:
        self.spent = False


def _refresh_eligible(repository: Repository, inspected: dict[str, Any], *,
                      red_renovate: bool = False) -> bool:
    """True for stale green CI or a caller-admitted red Renovate retry.

    `CI_BASE_STALE` means the trusted run judged an older base; its own
    conclusion says whether pulling it forward is worth a CI run at all. A
    failed, cancelled or unfinished run is not, except under
    `red_renovate`: the executor alone keeps a Renovate branch current
    (`renovate.json` `rebaseWhen: conflicted`). The caller refreshes a red
    Renovate head once, then parks a red result. A changed head, new trusted
    run or attempt, or explicit unpark permits another bounded retry,
    including for an executor-created head. Main movement alone cannot
    invalidate that park; there is no lifetime head/base retry ledger.
    The gate admits that refresh only for a `renovate/` head branch.
    """
    conclusion = inspected.get("ci_run_conclusion")
    return (inspected.get("blocking_reason") in {"CI_BASE_STALE", "PR_BASE_STALE"}
            and (conclusion == "success"
                 or red_renovate and conclusion in TERMINAL_FAILURE_CONCLUSIONS))


def _perform_refresh(
    application: Any,
    repository: Repository,
    pr: int,
    inspected: dict[str, Any],
    refresh_budget: _RefreshBudget,
) -> dict[str, Any]:
    """Spend `refresh_budget` and issue the one refresh call for `pr`.

    The scheduler calls this only for its persisted active candidate.
    """
    refresh_budget.spent = True
    refreshed = application.refresh(
        repository.name, pr, str(inspected["head_sha"]), str(inspected["base_sha"]),
    )
    if refreshed.get("ok"):
        record: dict[str, Any] = {"pr": pr, "outcome": "refreshed"}
        if refreshed.get("head_sha"):
            record["head_sha"] = str(refreshed["head_sha"])
        return record
    return {"pr": pr, "outcome": "retained",
            "reason": str(refreshed.get("code") or "PR_REFRESH_FAILED")}


def _automerge_candidate(
    application: Any,
    repository: Repository,
    pr: int,
    executor_run_prefix: str = DEFAULT_EXECUTOR_RUN_PREFIX,
    *,
    refresh_budget: _RefreshBudget | None = None,
    inspected: dict[str, Any] | None = None,
    red_renovate: bool = False,
    consented_head: str | None = None,
) -> dict[str, Any]:
    """Run one finite inspect/review/reinspect/merge sequence.

    With a `refresh_budget` that is unspent, a stale-base candidate whose
    trusted run succeeded, or a `red_renovate` one whose run failed, is
    refreshed instead of only retained (`_refresh_eligible`).
    The gate re-reads the PR under the repository lock and refuses unless
    head, base and blocker are unchanged.
    """
    inspected = inspected if inspected is not None else application.inspect(repository.name, pr)
    if not inspected.get("ok"):
        return {"pr": pr, "outcome": "retained",
                "reason": str(inspected.get("code") or "INSPECT_FAILED")}
    if inspected.get("queue_hold"):
        # A hold that landed after the pass read its listing.
        return {"pr": pr, "outcome": "retained", "reason": QUEUE_HOLD_RESULT}
    if consented_head is not None and inspected.get("head_sha") != consented_head:
        return {"pr": pr, "outcome": "retained", "reason": "RENOVATE_CONSENT_STALE"}
    if inspected.get("blocking_reason") == "PR_REVIEW_CHANGES_REQUESTED":
        return {"pr": pr, "outcome": "retained",
                "reason": "PR_REVIEW_CHANGES_REQUESTED"}
    if _refresh_eligible(repository, inspected, red_renovate=red_renovate):
        if refresh_budget is not None and not refresh_budget.spent:
            return _perform_refresh(application, repository, pr, inspected, refresh_budget)
        return {"pr": pr, "outcome": "retained",
                "reason": str(inspected["blocking_reason"])}
    if inspected.get("ci_state") != "green":
        # A failed narrowed run of an insufficient profile is red:
        # no review is spent on it, and `_park_reason` parks it `ci_failed`.
        if (inspected.get("blocking_reason") != "CI_PROFILE_INSUFFICIENT"
                or _failed_run(inspected)):
            return {"pr": pr, "outcome": "retained",
                    "reason": str(inspected.get("blocking_reason") or "PR_CI_NOT_GREEN")}
    if inspected.get("mergeable") is False:
        return {"pr": pr, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"}
    if inspected.get("mergeable") is None and "mergeable" in inspected:
        return {"pr": pr, "outcome": "retained", "reason": "PR_MERGEABILITY_UNKNOWN"}
    # the base the review reads, when this call reviews over the
    # recorded base instead of main's tip; None otherwise.
    recorded_review: str | None = None
    if inspected.get("review_state") != "approved":
        if inspected.get("claim_error") in CLAIM_ERROR_CODES:
            return {"pr": pr, "outcome": "retained", "reason": "PR_CLAIM_UNAVAILABLE",
                    "claim_error": inspected["claim_error"]}
        executor_run = f"{executor_run_prefix}:{repository.name}:{pr}"
        # A run the gate accepts without green CI may have judged main's tip,
        # which a head forked before it does not contain. As in review ahead,
        # the review reads the forge's fork point, never that tip.
        recorded = inspected.get("recorded_base_sha")
        if (_narrowed_run_accepted(inspected) and isinstance(recorded, str)
                and SHA_RE.fullmatch(recorded) and recorded != inspected["base_sha"]):
            recorded_review = recorded
        reviewed = _review_with_recovery(
            application, repository.name, pr, str(inspected["head_sha"]),
            recorded_review or str(inspected["base_sha"]), executor_run,
        )
        # the executor answers `REVIEW_APPROVAL_REUSED` with the pair
        # the reviewer saw. Kept as a fallback for the journal record in case
        # the re-inspection below cannot repeat it -- the executor is the one
        # caller that had the range objects local for certain, because it
        # fetched them.
        review_reuse = {key: reviewed[key] for key in (
            "approval_basis", "reviewed_head_sha", "reviewed_base_sha",
        ) if reviewed.get(key) is not None}
        if (not reviewed.get("ok")
                or reviewed.get("verdict") == "CHANGES_REQUIRED"):
            return _review_record(pr, reviewed)
    else:
        review_reuse = {}
    current = application.inspect(repository.name, pr)
    if not current.get("ok"):
        return {"pr": pr, "outcome": "retained",
                "reason": str(current.get("code") or "INSPECT_FAILED")}
    if consented_head is not None and current.get("head_sha") != consented_head:
        return {"pr": pr, "outcome": "retained", "reason": "RENOVATE_CONSENT_STALE"}
    if current.get("queue_hold"):
        return {"pr": pr, "outcome": "retained", "reason": QUEUE_HOLD_RESULT}
    # an approval over the recorded base approves the head, not the
    # exact (head, main) pair. `_approved` honours it, as for a review ahead:
    # it starts the full run now, only when the fresh read passes every guard
    # of the next pass's `_mark_due` (a CI listing that failed meanwhile waits).
    if current.get("review_state") != "approved" and not (
            recorded_review is not None and _approved(current)
            and _mark_due(repository, current, red_renovate=red_renovate)):
        return {"pr": pr, "outcome": "retained",
                "reason": str(current.get("blocking_reason") or "PR_REVIEW_NOT_APPROVED")}
    if _mark_needed(repository, current):
        # an approved head starts its full run only now, and the
        # merge needs that run (`_require_merge_profile`): the pass waits.
        # the fresh read still has to land next, as before the
        # review: a CI read that failed or a run that went red meanwhile waits.
        if not _lands_next(repository, current):
            return {"pr": pr, "outcome": "retained",
                    "reason": str(current.get("blocking_reason") or "PR_CI_NOT_GREEN")}
        failed, marked_now = _mark_candidate(application, repository, pr, current)
        unknown = _ci_evidence_unknown(current)
        return failed or {"pr": pr, "outcome": "retained", "reason": CANDIDATE_MARKED,
                          **({"detail": unknown} if unknown else {}),
                          "_marked_now": marked_now}
    merged = application.merge(
        repository.name, pr, str(current["head_sha"]), str(current["base_sha"])
    )
    if merged.get("ok"):
        # when the approval that authorised this merge was carried
        # across a clean refresh rather than issued for this exact head, the
        # journal says so and names the pair the reviewer saw. `current` --
        # the re-inspection the merge decision was actually made on -- wins,
        # with the executor's own answer standing in when the re-inspection
        # did not repeat it. Omitted entirely when neither reported it, so a
        # merge on an exact approval keeps its existing record shape.
        reuse = {**review_reuse, **{key: current[key] for key in (
            "approval_basis", "reviewed_head_sha", "reviewed_base_sha",
        ) if current.get(key) is not None}}
        return {"pr": pr, "outcome": "merged", **reuse}
    return {"pr": pr, "outcome": "retained",
            "reason": str(merged.get("code") or "MERGE_FAILED"),
            "_merge_refused": True}


def _review_record(pr: int, reviewed: dict[str, Any]) -> dict[str, Any]:
    """The retained record of a review that did not approve: its reason and evidence."""
    record = {"pr": pr, "outcome": "retained", "reason": _review_reason(reviewed)}
    for key in ("review_error", "review_error_detail", "retry_refused", "recovery_action"):
        if reviewed.get(key) is not None:
            record[key] = reviewed[key]
    claim_error = reviewed.get("claim_error")
    if isinstance(claim_error, str) and claim_error in CLAIM_ERROR_CODES:
        record["claim_error"] = claim_error
    if not reviewed.get("ok"):
        if _REFUSAL_CODE_RE.fullmatch(record["reason"]) is None:
            # No park or alert keys on free text, so it would keep
            # this PR active and spend the review slot every pass.
            record["review_error"] = record["reason"][:MAX_REASON_CHARS]
            record["reason"] = "REVIEW_EXECUTION_FAILED"
        elif record["reason"] not in _REVIEW_REFUSALS_NOT_WAITED:
            # Any other gate refusal waits like a merge refusal.
            record["_review_refused"] = True
    return record


# review codes that report an existing decision or receipt, not a new review.
_NO_NEW_REVIEW_CODES = frozenset({"REVIEW_NATIVE_REUSED", "REVIEW_REUSED", "REVIEW_RECONCILED"})


def _comment_rereview(application: Any, repository: Repository, pr: int, head: str,
                      inspected: dict[str, Any], executor_run_prefix: str
                      ) -> tuple[dict[str, Any], str | None]:
    """Re-review a standing change request that agent comments answer.

    One review of the same head at the standing review's base, through the
    pass's review budget. It never refreshes and never merges: an approval
    leaves the PR active for the ordinary path of the next pass. Returns the
    record and the park reason.
    """
    try:
        reviewed = _review_with_recovery(
            application, repository.name, pr, head, str(inspected["agent_comment_review_base"]),
            f"{executor_run_prefix}:{repository.name}:{pr}",
        )
    except Exception as exc:  # uncertain dispatch or transport
        result = {**_candidate_exception(pr, exc), "comment_rereview": True}
        return result, _park_reason(result, inspected) or "changes_requested"
    if reviewed.get("code") in _NO_NEW_REVIEW_CODES:
        # The projection saw new comments; the review owner found nothing new to do.
        LOGGER.warning("automerge: %s#%s comment re-review made no new review (%s)",
                       repository.name, pr, reviewed.get("code"))
    if reviewed.get("ok") and reviewed.get("verdict") == "ACCEPT_STATIC_CHANGE":
        result: dict[str, Any] = {"pr": pr, "outcome": "retained",
                                  "reason": "COMMENT_REREVIEW_APPROVED"}
        park = None
    elif reviewed.get("ok") and reviewed.get("verdict") == "CHANGES_REQUIRED":
        result = {"pr": pr, "outcome": "retained", "reason": "CHANGES_REQUIRED"}
        park = "changes_requested"
    else:
        result = _review_record(pr, reviewed)
        # The standing request still binds: the PR never keeps the slot.
        park = _park_reason(result, inspected) or "changes_requested"
    result["comment_rereview"] = True
    if reviewed.get("code") in _NO_NEW_REVIEW_CODES:
        result["comment_rereview_no_new_review"] = True
    return result, park


def _record_automerge_result(
    result: dict[str, Any],
    repository: Repository,
    summaries: dict[str, dict[str, Any]],
) -> bool:
    """Append `result` to `repository`'s summary; report an integrity stop.

    Mark `authority_integrity_failed` and append one record for the candidate.
    """
    code = result.get("reason")
    stop = code in AUTOMERGE_INTEGRITY_CODES or code in AUTOMERGE_AUTHORITY_CODES
    if stop:
        result["authority_integrity_failed"] = True
        summaries[repository.name]["authority_integrity_failed"] = True
    summaries[repository.name]["results"].append(result)
    return stop


def _candidate_exception(pr: int, exc: Exception) -> dict[str, Any]:
    """Retain a candidate failure without hiding a gate authority refusal."""
    if isinstance(exc, QueueStateError):
        raise exc
    code = exc.code if isinstance(exc, GateError) else None
    record = {"pr": pr, "outcome": "retained",
              "reason": code or f"CANDIDATE_ERROR:{type(exc).__name__}"}
    claim_error = exc.context.get("claim_error") if isinstance(exc, GateError) else None
    if isinstance(claim_error, str) and claim_error in CLAIM_ERROR_CODES:
        record["claim_error"] = claim_error
    return record


def _inspect_candidate(application: Any, repository: Repository, pr: int) -> dict[str, Any]:
    """Normalize candidate read failures to the gate inspect envelope."""
    try:
        return application.inspect(repository.name, pr)
    except Exception as exc:
        return {"ok": False, "code": _candidate_exception(pr, exc)["reason"]}


CI_WAIT_SECONDS = 3600
# Forgejo rechecks open PRs after a merge; one `mergeable: false` read is not a
# conflict. The active PR holds its slot this long before it parks `conflict`.
MERGEABLE_SETTLE_SECONDS = 60
# A run that never started (missing or still queued) is parked sooner. The
# wait restarts when a missing run first appears (its run ID changes), so a
# never-started run is parked after about 2 x 20 minutes at worst. "Started"
# is remembered on the active record: a multi-stage run reports
# CI_RUN_QUEUED again between stages, and judging each tick alone parked
# running CI as `ci_stalled` after the start window.
CI_START_WAIT_SECONDS = 1200
_CI_NOT_STARTED = frozenset({"CI_RUN_MISSING", "CI_RUN_QUEUED"})
# what a full-mode train owner reads when none of its runs is queued or
# running. The gate prefers a terminal verdict, then a queued or running run, to a
# run that ended cancelled or skipped (`ci.select_newest_run`), so these readings
# mean no run is left to finish, and nothing starts one. After
# `CI_START_WAIT_SECONDS` of one such reading in a row the family is retired.
# CI_RUN_FAILED reaches this set only when it is not a strict terminal
# red (`ci.terminal_full_failure`); that branch runs first and closes as
# TRAIN_OWNER_RED or parks on the base proof. A run whose failed job left its
# route, c1 and c2 cancelled can keep reading so; parking it without a
# bound would hold the only slot until main moved.
_TRAIN_OWNER_LOST = frozenset(
    {"CI_RUN_CANCELED", "CI_RUN_SKIPPED", "CI_RUN_MISSING", "CI_RUN_FAILED"})
# What `progress` holds once `_end_lost_run` has ended a lost-run interval, or
# once an owner's first reading was not a lost one. A fixed code, not the
# reading that ended it: the queue store accepts only a `QUEUE_CODE` there, and
# a failed inspection reports `CANDIDATE_ERROR:<Type>`.
_TRAIN_OWNER_LOSS_ENDED = "TRAIN_OWNER_LOSS_ENDED"
# both budgets count from the run's last observed state change, not
# from selection. A multi-stage run on a busy runner pool can move between
# queued and running for a long time and still turn green; parking
# it as `ci_stalled` would throw that run away. A run whose state does not change
# for the budget still parks. The state is kept as `progress` on the active
# record.
_CI_RUN_STATES = frozenset({"CI_RUN_MISSING", "CI_RUN_QUEUED", "CI_RUN_IN_PROGRESS"})
# No run at all for the active head this long means no `pull_request` event
# fired: Forgejo's "Update branch" can move a branch without running its
# hooks, so `refs/pull/<n>/head` stays on the old commit and CI never starts.
# Waiting longer cannot help; a new push can. Timed from the first missing
# read of that head (`ci_missing` on the active record).
CI_MISSING_SECONDS = 600
# The head-of-line guard parks the active PR as `stuck` once this many passes
# in a row retained it with one reason code. CI waits keep their own deadlines
# above and never count, nor does the review family, a CI verdict the review
# is waiting on, unknown mergeability, which waits for its deadline, or a
# review the pass deadline deferred, which is the pass's limit, not the PR's.
STUCK_PASS_LIMIT = 10
_STUCK_EXEMPT = frozenset({
    "CI_RUN_IN_PROGRESS", "CI_RUN_QUEUED", "CI_RUN_MISSING", "CI_PROFILE_PENDING",
    "CI_BASE_STALE", "PR_BASE_STALE", "PR_CI_NOT_GREEN", "PR_MERGEABILITY_UNKNOWN",
    "PASS_DEADLINE",
    # The gate raises GATE_LOCK_BUSY at the repository lock, before the
    # operation body runs (in the gate call): nothing was mutated
    # and the refusal says nothing about the PR. A holder that outlasts the
    # window ends in the time-based PR_STALLED / CANDIDATE_STALLED park.
    # FETCH_FAILED stays counted: a fetch that fails on every pass would hold
    # a PR at the head indefinitely, the case this guard exists for.
    "GATE_LOCK_BUSY",
})
REVIEW_AHEAD_INSPECTIONS = 3
# After this many consecutive non-Renovate selections the next selection serves
# Renovate first, so 1 alternates agent, Renovate, agent, Renovate: a one-half
# Renovate share while both queues hold PRs. A larger interval left Renovate
# PRs waiting at the head of the queue for a long time though a selected one
# merged quickly.
RENOVATE_SLOT_EVERY = 1


def _failed_run(inspected: dict[str, Any]) -> bool:
    """True for a confirmed failed CI run on the PR's head, the `ci_failed` park's test."""
    return (inspected.get("ci_run_conclusion") in TERMINAL_FAILURE_CONCLUSIONS
            and inspected.get("ci_state") in {"red", "unavailable"})


def _failed_head(inspected: dict[str, Any]) -> bool:
    """True when the PR's head has a failed run, as the Renovate slot accounting reads it.

    Adds the stale-base projection to `_failed_run`: a trusted run that failed
    on an older base reads `ci_reason` `CI_BASE_STALE` with `ci_state` pending
    beside its terminal failure conclusion. `ci_reason` is the CI evaluator's own
    reason, so a standing change request or a conflict that wins
    `blocking_reason` does not hide it. Any other pending run is not a failed head.
    """
    return _failed_run(inspected) or (
        inspected.get("ci_reason") == "CI_BASE_STALE"
        and inspected.get("ci_state") == "pending"
        and inspected.get("ci_run_conclusion") in TERMINAL_FAILURE_CONCLUSIONS)


def _review_reason(reviewed: dict[str, Any]) -> str:
    """Why a review left the PR unmerged, as the journal and `_park_reason` read it.

    An `ok` review that rejects carries the review's own code (REVIEW_PUBLISHED,
    REVIEW_REUSED, REVIEW_RECONCILED) beside the verdict, so the verdict names
    the rejection. A refusal keeps its code.
    """
    if reviewed.get("ok") and reviewed.get("verdict") == "CHANGES_REQUIRED":
        return "CHANGES_REQUIRED"
    return str(reviewed.get("code") or reviewed.get("verdict") or "REVIEW_NOT_APPROVED")


def _unmergeable_settling(active: dict[str, Any], inspected: dict[str, Any],
                          now: Any) -> bool:
    """Record a `mergeable: false` read of the active PR; True while it settles.

    The window runs from the first such read of one head and base, so a new
    head or base restarts it. The caller clears the marker on any other read.
    """
    key = [str(inspected.get("head_sha") or ""), str(inspected.get("base_sha") or "")]
    seen = active.get("unmergeable")
    current = now()
    if not isinstance(seen, list) or seen[:2] != key:
        active["unmergeable"] = [*key, current]
        return True
    return current - seen[2] < MERGEABLE_SETTLE_SECONDS


def _ci_missing_since(active: dict[str, Any], inspected: dict[str, Any],
                      now: Any) -> float | None:
    """When the active head was first read with no CI run, or None.

    Records the first `CI_RUN_MISSING` read of one head on the active
    record; any other read, a new head or a run seen started clears it.
    """
    head = str(inspected.get("head_sha") or "")
    if inspected.get("blocking_reason") != "CI_RUN_MISSING" or active.get("started"):
        active.pop("ci_missing", None)
        return None
    seen = active.get("ci_missing")
    if not isinstance(seen, list) or seen[0] != head:
        active["ci_missing"] = [head, now()]
    return float(active["ci_missing"][1])


def _stale_pull_ref(inspected: dict[str, Any]) -> bool:
    """True when the gate read `refs/pull/<n>/head` on another commit than the head.

    The forge rewrites that ref, and starts `pull_request` CI, only in the
    post-receive step of a push; the gate reads it for a head with no CI run.
    An absent field (not read, or unreadable) is not evidence of anything.
    """
    pulled = inspected.get("pull_ref_sha")
    return isinstance(pulled, str) and pulled != inspected.get("head_sha")


def _recovery_refresh(
    application: Any, repository: Repository, active: dict[str, Any], pr: int,
    inspected: dict[str, Any], refresh_budget: _RefreshBudget, save: Any,
) -> dict[str, Any] | None:
    """Spend the repository's refresh on the active head once, or None if spent.

    For a PR the queue would otherwise park on evidence a new head can fix
    (an outdated claim head, a run that was never created). `refreshed`
    names the head it was spent on and is saved before the call, so an
    uncertain dispatch is not repeated on that head.
    """
    if refresh_budget.spent:
        return None
    active["refreshed"] = active["head"]
    save()
    try:
        return _perform_refresh(application, repository, pr, inspected, refresh_budget)
    except Exception as exc:  # uncertain dispatch: the head read next pass decides
        return _candidate_exception(pr, exc)


# The narrowing rule: `checks.yml` runs a PR head in
# full only when it is the merge candidate, and the gate merges only on that
# full run. The queue names the candidate with the `gate/candidate` label when
# it is about to land a PR -- after the refresh that one needs, so
# the label's full run starts on the refreshed head -- and clears it when it
# parks or drops the PR.
CANDIDATE_POLICY = "label-v1"
# The retained reason for the pass that marked an up-to-date head: the
# `labeled` event starts its full run, and the queue waits for that run.
CANDIDATE_MARKED = "PR_CANDIDATE_MARKED"
# the retained reason of the pass whose ask the gate answered with a
# started rerun of a CI job lost to infrastructure. That rerun is a run the
# queue must wait for, so the PR keeps its slot instead of parking `ci_failed`.
CI_RERUN_LOST = "CI_RERUN_LOST"


def _lands_next(repository: Repository, inspected: dict[str, Any]) -> bool:
    """Whether `_automerge_candidate` would review or merge this read.

    The mark point (the narrowing rule): a PR about to be parked, or whose narrowed run is
    still running or red, is not marked, so no full run is spent on it. A
    refresh-eligible head is not here: the executor marks it after the
    refresh, so the label's full run starts on the refreshed head.
    The gate reads a gate-C1-class head's profile before its conclusion, so
    CI_PROFILE_INSUFFICIENT counts only beside a successful narrowed run
   : a red one would spend a full run that fails the same way.
    """
    if (inspected.get("queue_hold")
            or inspected.get("blocking_reason") == "PR_REVIEW_CHANGES_REQUESTED"
            or inspected.get("mergeable") is False):
        return False
    return (inspected.get("ci_state") == "green"
            or (inspected.get("blocking_reason") == "CI_PROFILE_INSUFFICIENT"
                and inspected.get("ci_run_conclusion") == "success"))


def _approved(inspected: dict[str, Any]) -> bool:
    """The head is approved: on this base, or by a delegated review on an older one.

    A delegated approval of the head counts, as in `_train_ineligibility`: an
    unrefreshed PR reviewed on an older base reads `not_approved`, and the
    approval survives its clean refresh.
    """
    return (inspected.get("review_state") == "approved"
            or inspected.get("latest_delegated_review_state") == "APPROVED")


def _full_run_due(inspected: dict[str, Any]) -> bool:
    """Whether the queue may start the full run on this head.

    Only on an approved head, or on a head the snapshot review refuses: the
    narrowed run is the feedback that review needs, and a full run on a head
    it then rejects buys nothing.

    `ci_review_accepted` is the gate's own answer, projected from
    the CI-evidence answer -- the exact rule the review applies -- so a
    head the review will accept is reviewed first, and one it will refuse (a
    run that is not the narrowed `c2` shape or a repository without the
    candidate policy) still gets its full run as
    before. A read without the field fails closed: the mark is placed rather
    than a review refused. So does an unknown answer, and the mark's
    result then names it (`_ci_evidence_unknown`).
    """
    return _approved(inspected) or inspected.get("ci_review_accepted") is not True


def _ci_evidence_unknown(inspected: dict[str, Any]) -> str | None:
    """`ci_evidence_unknown:<reason>` for a read whose CI-evidence answer is unknown.

    The gate could not decide this read's CI evidence -- a CI listing
    it could not read, a missing recorded base, main's tip absent from the
    checkout, an unclassifiable range. A pass that acts on it as on a refusal
    names it, so an unknown answer is never a silent full run. None for any
    other answer, and for a read without the field.
    """
    if inspected.get("ci_evidence_answer") != "unknown":
        return None
    return f"ci_evidence_unknown:{inspected.get('ci_evidence_reason')}"


def _mark_needed(repository: Repository, inspected: dict[str, Any]) -> bool:
    """Whether `_mark_candidate` would call the gate for this read.

    Not when the policy is off or the read already shows the mark, nor for an
    up-to-date head whose green run is already the full profile, nor
    for a head on which the gate accepts the narrowed `checks/c2` run
    as the merge evidence, whether that run is green or still pending after a
    refresh. Only the gate's own `ci_merge_profile` says so; an absent or
    unknown value keeps the mark.
    """
    return (repository.merge_candidate_policy == CANDIDATE_POLICY
            and inspected.get("merge_candidate") is not True
            and not (inspected.get("ci_state") == "green"
                     and inspected.get("ci_accepted_profile") == STRONGEST_PROFILE
                     and not _refresh_eligible(repository, inspected))
            and not (inspected.get("ci_state") in {"green", "pending"}
                     and inspected.get("ci_merge_profile") == "checks/c2"))


def _mark_due(repository: Repository, inspected: dict[str, Any], *,
              red_renovate: bool = False) -> bool:
    """Whether the pass marks the active PR as the merge candidate on this read.

    An up-to-date head (a refresh-eligible one is marked after its refresh)
    that lands next (`_lands_next`), whose full run is due
    (`_full_run_due`) and for which the gate would be called (`_mark_needed`).
    The serial path marks at this moment, and `train-v1` may build a train
    instead.
    """
    return (not _refresh_eligible(repository, inspected, red_renovate=red_renovate)
            and _lands_next(repository, inspected)
            and _full_run_due(inspected)
            and _mark_needed(repository, inspected))


def _mark_candidate(application: Any, repository: Repository, pr: int,
                    inspected: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """Mark *pr* as the merge candidate: a retained result if that failed,
    and whether this call added the mark, which starts the full run.

    No call when the policy is off or the gate's read already shows the mark.
    Nothing is persisted: the label on the forge is the record, and the
    gate's next read reports it as `merge_candidate`.

    No call either for an up-to-date head whose green run is already the full
    profile: classify ran it in full for a CI-trust or unmapped
    path, so it is the evidence the merge needs, and a mark would only start
    the same run again. A head this pass refreshes is marked after the
    refresh, never before: the label would start a full run on the
    obsolete head.
    """
    if not _mark_needed(repository, inspected):
        return None, False
    try:
        marked = application.mark_candidate(repository.name, pr, True)
    except Exception as exc:  # uncertain write: the next read decides
        return _candidate_exception(pr, exc), False
    if not marked.get("ok"):
        return {"pr": pr, "outcome": "retained",
                "reason": str(marked.get("code") or "PR_CANDIDATE_UNCONFIRMED")}, False
    return None, bool(marked.get("label_changed"))


def _clear_candidate(application: Any, repository: Repository, pr: int,
                     inspected: dict[str, Any] | None = None, *, marked: bool = False) -> None:
    """Clear the merge-candidate mark of a PR the queue parks or drops.

    Skipped only when this pass's gate read shows no mark and the pass set
    none (*marked*); without a read the call is made, and the gate answers
    an absent label without a write. Best effort: a mark left behind makes
    that PR's later runs full, which costs runner time and never lets
    anything merge, so a failure is logged and the park goes ahead.
    """
    if repository.merge_candidate_policy != CANDIDATE_POLICY:
        return
    if inspected is not None and inspected.get("merge_candidate") is not True and not marked:
        return
    try:
        cleared = application.mark_candidate(repository.name, pr, False)
    except Exception as exc:
        LOGGER.warning("automerge: %s#%s merge-candidate mark not cleared: %s",
                       repository.name, pr, type(exc).__name__)
        return
    if not cleared.get("ok"):
        LOGGER.warning("automerge: %s#%s merge-candidate mark not cleared: %s",
                       repository.name, pr, cleared.get("code") or "PR_CANDIDATE_UNCONFIRMED")


def _rerun_lost_ci(application: Any, repository: Repository, pr: int) -> dict[str, Any] | None:
    """ask the gate for the one rerun of a CI job lost to infrastructure.

    Asked of a PR the pass was about to park `ci_failed`. The gate proves the
    loss from the job's own record and owns the bound (Forgejo's job attempt),
    so this asks once and keeps no state: a retained result when the rerun
    started, which the caller holds like a CI wait, and None for every other
    answer, an error included, so the park stands exactly as before. Only a
    deploy or train stop (`QueueStateError`) leaves the pass.
    """
    try:
        answer = application.rerun_lost(repository.name, pr)
    except QueueStateError:
        raise
    except Exception as exc:  # the gate or its forge call failed: park as before
        LOGGER.warning("automerge: %s#%s lost-job rerun not asked: %s",
                       repository.name, pr, type(exc).__name__)
        return None
    if (not isinstance(answer, dict) or answer.get("ok") is not True
            or answer.get("code") != "RERUN_STARTED"):
        code = answer.get("code") if isinstance(answer, dict) else None
        LOGGER.info("automerge: %s#%s no lost-job rerun: %s",
                    repository.name, pr, code if isinstance(code, str) else "-")
        return None
    LOGGER.info("automerge: %s#%s reran the CI job lost to infrastructure (run %s)",
                repository.name, pr, answer.get("run_id"))
    return {"pr": pr, "outcome": "retained", "reason": CI_RERUN_LOST,
            "run_id": answer.get("run_id"), "head_sha": answer.get("head_sha"),
            "rerun_jobs": answer.get("rerun_jobs")}


def _run_identity(inspected: dict[str, Any]) -> list[int | None]:
    return [inspected.get("ci_run_id"), inspected.get("ci_run_attempt")]


def _ci_started(inspected: dict[str, Any]) -> bool:
    return (inspected.get("blocking_reason") == "CI_RUN_IN_PROGRESS"
            or inspected.get("ci_run_conclusion") is not None)


def _restart_ci_wait(active: dict[str, Any], now: Any) -> None:
    """Start the CI-start window of *active* again from this moment.

    Every event that begins a run the queue must wait for comes through here: a
    changed head, a new run identity, and a newly added merge-candidate mark.
    One place therefore owns what "restarted" means -- the window is measured
    from now, and no run has been observed against it yet.
    """
    active["wait_since"] = now()
    active["started"] = False


def _note_progress(active: dict[str, Any], inspected: dict[str, Any],
                   now: Callable[[], float]) -> None:
    """Record a change of the CI run state between missing, queued and running.

    A record written before this state was kept takes the wait start."""
    state = inspected.get("ci_reason") or inspected.get("blocking_reason")
    previous = active.get("progress")
    if state in _CI_RUN_STATES and (previous is None or previous[0] != state):
        active["progress"] = [state, active["wait_since"] if previous is None else now()]


def _end_lost_run(active: dict[str, Any], reading: str | None,
                  now: Callable[[], float]) -> None:
    """End a train owner's lost-run interval when a pass did not end on its code.

    A lost code in `progress` dates passes that each end on that same
    code. *reading* is the code this pass ended on, None when it read no owner
    code: a failed inspection, a green owner or a strict red. Any other reading
    ends the interval too: another code, an unreadable owner
    (`CI_API_UNAVAILABLE`, `CI_RESPONSE_INVALID`) or a refused proof. A marker
    then replaces the pair, so the next lost reading is dated from itself; a
    cleared pair would let a returning missing run take the wait start again.

    An owner with no pair yet gets the marker as well, unless this pass read a
    lost code. `_note_progress` dates a missing run from the wait start only
    when that run is the first thing the owner ever read; a build or an
    adoption that first read it unreadable, failed, green or red has already
    spent that start.
    """
    held = active.get("progress")
    if held is None:
        ended = reading not in _TRAIN_OWNER_LOST
    else:
        ended = held[0] in _TRAIN_OWNER_LOST and held[0] != reading
    if ended:
        active["progress"] = [_TRAIN_OWNER_LOSS_ENDED, now()]


def _stall_since(active: dict[str, Any]) -> float:
    """When the stall budget started: the later of the wait start (a new head,
    run or mark) and the last run-state change."""
    progress = active.get("progress")
    return max(active["wait_since"], progress[1]) if progress is not None else active["wait_since"]


def _observed_run(inspected: dict[str, Any], previous: list[int | None]) -> list[int | None]:
    observed = _run_identity(inspected)
    if observed[0] is None:
        return previous
    if observed[0] == previous[0] and observed[1] is None:
        return previous
    return observed


def _review_identity(inspected: dict[str, Any]) -> list[Any]:
    delegated = inspected.get("latest_delegated_review_state")
    rejection = ("REQUEST_CHANGES" if delegated == "REQUEST_CHANGES"
                 else "human_changes_requested" if inspected.get("review_state") == "changes_requested"
                 else "none")
    return [inspected.get("latest_delegated_review_id"), rejection]


def _comment_rereview_due(inspected: dict[str, Any]) -> bool:
    """The gate row asks for a comment re-review of its standing change request."""
    base = inspected.get("agent_comment_review_base")
    return (inspected.get("agent_comments_pending") is True
            and isinstance(base, str) and SHA_RE.fullmatch(base) is not None)


def _run_regressed(parked: dict[str, Any], inspected: dict[str, Any]) -> bool:
    """The read names an older run than the park did, on the parked head.

    Narrowing rule: a park clears `gate/candidate`, so the gate reads the head on its
    class profile again, and an older narrowed success can satisfy it. That
    run predates the full run the park was made on: it is no new evidence.
    """
    parked_id, observed_id = parked["run"][0], inspected.get("ci_run_id")
    return (parked["head"] == inspected.get("head_sha")
            and isinstance(parked_id, int) and isinstance(observed_id, int)
            and observed_id < parked_id)


def _stale_base_success(inspected: dict[str, Any]) -> bool:
    """A run that succeeded on the head while main moved (the reopen path).

    `_parked_changed` names only `CI_BASE_STALE`; see its comment.
    """
    return (inspected.get("blocking_reason") in {"CI_BASE_STALE", "PR_BASE_STALE"}
            and inspected.get("ci_run_conclusion") == "success")


def _parked_changed(parked: dict[str, Any], inspected: dict[str, Any]) -> bool:
    # new agent comments on the parked head release a change-request
    # park for one bounded re-review. Every other park reason ignores them.
    if (parked["reason"] == "changes_requested"
            and parked["head"] == inspected.get("head_sha")
            and _comment_rereview_due(inspected)):
        return True
    # An older run on the parked head is neither a run change nor recovery:
    # released, the head would be marked again for another
    # full run with no new head and no repaired evidence.
    regressed = _run_regressed(parked, inspected)
    run = parked["run"] if regressed else _observed_run(inspected, parked["run"])
    # A CI-failure park also ends on the gate's green verdict for the head it
    # was parked on. A rerun can correct a red run without exposing a new
    # attempt, and a corrected forge result changes no identity at all
    # so the run-identity comparison alone would leave the PR parked
    # forever. Only the gate's verdict counts, and only for that head: a red,
    # unavailable or other pending read is not recovery, and a different head
    # is admitted by the comparisons below, red or not. Once main has moved the
    # gate reads that same success as `pending` with `CI_BASE_STALE`,
    # so a stale-base success on the parked head counts too: the park would
    # otherwise wait for a refresh that only follows its release. The set names
    # only `CI_BASE_STALE`: the gate emits `PR_BASE_STALE` solely under a
    # green verdict, which the first disjunct already releases, so it would be
    # an unpinned second path for the same recovery.
    green = (parked["head"] == inspected.get("head_sha") and not regressed
             and (inspected.get("ci_state") == "green"
                  or (inspected.get("blocking_reason") == "CI_BASE_STALE"
                      and inspected.get("ci_run_conclusion") == "success")))
    # A confirmed bot CI failure needs new CI/head evidence or explicit
    # operator recovery. Claim, review and base movement do not fix it. A
    # green read is new CI evidence, not a retry of the unchanged failed head:
    # the release itself issues no refresh, though the ordinary stale-base
    # catch-up refresh can still follow in the same pass.
    if parked["reason"] == "renovate_ci_failed":
        return (parked["head"] != inspected.get("head_sha")
                or parked["run"] != run
                or green)
    # A head-of-line park ends only with a new head (or the operator's
    # `--unpark`): a base, run or review change would return it to the head
    # of the queue after every merge, to block ten more passes.
    if parked["reason"] == "stuck":
        return parked["head"] != inspected.get("head_sha")
    if parked["reason"] == "claim_unavailable" and claim_validation_succeeded(inspected):
        return True
    if parked["reason"] == "ci_failed" and green:
        return True
    if (parked["head"] != inspected.get("head_sha")
            or parked.get("claim_sha") != inspected.get("claim_sha")
            or parked["run"] != run
            or parked["review"] != _review_identity(inspected)):
        return True
    # Evidence repaired without a new run ID, or a late verdict after a stall.
    # A moved base retries unavailable evidence once per move; a record from
    # before `base` was stored retries once. If the evidence is still missing,
    # the same pass parks it again and moves on to the next PR. The state of
    # an older run on the parked head is no verdict on the parked run.
    if parked["reason"] == "evidence_unavailable":
        return (parked.get("base") != inspected.get("base_sha")
                or not regressed and (
                    inspected.get("ci_state") in {"green", "pending", "red"}
                    or inspected.get("blocking_reason") in {"CI_BASE_STALE", "PR_BASE_STALE"}
                    and inspected.get("ci_run_conclusion") == "success"))
    if parked["reason"] == "ci_stalled":
        return not regressed and (inspected.get("ci_state") == "green" or (
            inspected.get("ci_state") == "red"
            and inspected.get("ci_run_conclusion") in TERMINAL_FAILURE_CONCLUSIONS) or (
            inspected.get("blocking_reason") in {"CI_BASE_STALE", "PR_BASE_STALE"}
            and inspected.get("ci_run_conclusion") == "success"))
    if parked["reason"] == "mergeability_unknown":
        return inspected.get("mergeable") is True
    # CI for the parked head appeared (queued, running or finished).
    if parked["reason"] == "ci_run_missing":
        return inspected.get("blocking_reason") != "CI_RUN_MISSING"
    # A stale `refs/pull` head also ends when main moves: the next recovery
    # refresh is then a real push, which rewrites the ref and starts CI.
    if parked["reason"] == "pull_ref_stale":
        return (inspected.get("blocking_reason") != "CI_RUN_MISSING"
                or parked.get("base") != inspected.get("base_sha"))
    # A park made on `mergeable: false` after the settle window can still end
    # when Forgejo later reads it mergeable. A conflict found by a refresh or a
    # train build has no code: only the changed evidence above ends it.
    if parked["reason"] == "conflict" and parked.get("code") == "PR_NOT_MERGEABLE":
        return inspected.get("mergeable") is True
    return False


def _stuck(active: dict[str, Any], result: dict[str, Any], ci_wait: bool) -> str | None:
    """Count one more pass that kept the active PR, as `result` says.

    The counted code once `STUCK_PASS_LIMIT` passes in a row retained it
    with it, else None. A refresh, a CI wait, an exempt code or a reason that is
    no code restarts the count.
    """
    reason = result.get("reason")
    if (ci_wait or result["outcome"] != "retained" or not isinstance(reason, str)
            or QUEUE_CODE.fullmatch(reason) is None or reason in _STUCK_EXEMPT
            or reason.startswith(("REVIEW_", "PR_REVIEW_"))):
        active.pop("retained", None)
        return None
    previous = active.get("retained")
    passes = previous[1] + 1 if previous is not None and previous[0] == reason else 1
    active["retained"] = [reason, passes]
    return reason if passes >= STUCK_PASS_LIMIT else None


def _park_reason(result: dict[str, Any], inspected: dict[str, Any]) -> str | None:
    if result.get("outcome") != "retained":
        return None
    reason = result.get("reason")
    if reason == QUEUE_HOLD_RESULT:
        return AUTHOR_HOLD
    if reason == "PR_CLAIM_UNAVAILABLE":
        return "claim_unavailable"
    if reason in {"PR_REVIEW_CHANGES_REQUESTED", "CHANGES_REQUIRED"}:
        return "changes_requested"
    if reason == "PR_NOT_MERGEABLE":
        return "conflict"
    if reason == "PR_REFRESH_CONFLICT":
        return "conflict"
    if _failed_run(inspected):
        return "ci_failed"
    if (reason in REVIEW_EXECUTION_FAILURE_CODES
            and (reason != "REVIEW_INCOMPLETE" or result.get("recovery_action") == "operator")):
        return "review_execution_failed"
    if (inspected.get("ci_state") in {"unavailable", "no_run"}
            and isinstance(reason, str) and reason.startswith("CI_")
            and reason not in {"CI_BASE_STALE", "PR_BASE_STALE", "CI_API_UNAVAILABLE", "CI_HEAD_STALE"}):
        return "evidence_unavailable"
    return None


def _queue_row(state: dict[str, Any], name: str) -> dict[str, Any]:
    return state["repositories"].setdefault(name, {"active": None, "parked": {}})


def _slot_order(numbers: list[int], renovate: set[int], row: dict[str, Any],
                priorities: dict[int, int]) -> list[int]:
    """The queue order for the next selection: high, normal, then low PRs
    (`_queue_priorities`), and inside each group the Renovate slot moves
    Renovate first.

    Once `agent_streak` non-Renovate PRs were selected in a row (a selection
    that parks in its own pass is not one), each group's Renovate
    PRs come before its other PRs, so the slot never lifts a PR above a
    higher group. Everything else keeps the order of `numbers`:
    without a `queue/*` label and before the slot is due, `numbers` is
    returned unchanged.
    """
    due = row.get("agent_streak", 0) >= RENOVATE_SLOT_EVERY
    return sorted(numbers, key=lambda number: (priorities.get(number, PRIORITY_NORMAL),
                                               due and number not in renovate))


# how many PRs without a park one selection inspects to rank them by
# readiness. A parked PR is inspected as before and does not count.
SELECTION_INSPECTIONS = 10


def _readiness(inspected: dict[str, Any]) -> int:
    """The selection tier of an electable read; the lowest is elected.

    0: approved with green CI; 1: approved; 2: not reviewed yet.
    """
    if not _approved(inspected):
        return 2
    return 0 if inspected.get("ci_state") == "green" else 1


def _select_candidate(
    application: Any, budget: Any, repository: Repository, row: dict[str, Any],
    numbers: list[int], queued: list[int], renovate: set[int],
    summaries: dict[str, dict[str, Any]], now: Any, executor_run_prefix: str,
    priorities: dict[int, int],
) -> tuple[int | None, dict[str, Any] | None, bool]:
    """Elect the next active PR by priority, then readiness:
    its number, its read, a stop.

    Inspects `_slot_order` up to `SELECTION_INSPECTIONS` PRs without a park,
    and every parked PR on the way, which is released as before. Ranks by
    the `queue/*` priority (`_queue_priorities`), then `_readiness`, then
    the Renovate slot (`_slot_order`), then queue position, and stops at the
    first PR nothing can outrank: `order` is sorted by priority, so the scan
    also ends at the first PR of a lower priority than the best one. A PR
    with a standing change request is never elected (`_answer_change_request`).
    """
    order = _slot_order(numbers, renovate, row, priorities)
    slot_due = (row.get("agent_streak", 0) >= RENOVATE_SLOT_EVERY
                and any(number in renovate for number in numbers))
    best: tuple[tuple[int, int, int, int], int, dict[str, Any]] | None = None
    scanned = 0
    for position, candidate in enumerate(order):
        priority = priorities.get(candidate, PRIORITY_NORMAL)
        if best is not None and priority > best[0][0]:
            break  # A lower priority group: nothing in it outranks the best.
        parked = row["parked"].get(str(candidate))
        if parked is not None and parked["reason"] in HOLD_PARK_REASONS:
            continue  # Only `_park_renovate_holds` ends it.
        if parked is None:
            if scanned >= SELECTION_INSPECTIONS:
                LOGGER.info("automerge: %s selection ranked %d PRs; %d later PRs wait",
                            repository.name, scanned, len(order) - position)
                break
            scanned += 1
        observation = _inspect_candidate(application, repository, candidate)
        if not observation.get("ok"):
            if _record_automerge_result(
                    {"pr": candidate, "outcome": "retained",
                     "reason": str(observation.get("code") or "INSPECT_FAILED")},
                    repository, summaries):
                return None, None, True
            numbers.remove(candidate)
            continue
        if parked is not None and not _parked_changed(parked, observation):
            continue
        if observation.get("pr_state") in {"draft", "closed", "merged"}:
            row["parked"].pop(str(candidate), None)
            numbers.remove(candidate)
            queued.remove(candidate)
            continue
        if observation.get("blocking_reason") == "PR_REVIEW_CHANGES_REQUESTED":
            answered, stop = _answer_change_request(
                application, budget, repository, row, candidate, observation, numbers,
                candidate in renovate, summaries, now, executor_run_prefix)
            if stop:
                return None, None, True
            if answered is None:
                continue
            observation = answered
        row["parked"].pop(str(candidate), None)
        rank = (priority, _readiness(observation),
                0 if not slot_due or candidate in renovate else 1, position)
        if best is None or rank < best[0]:
            best = (rank, candidate, observation)
        if rank[1:3] == (0, 0):
            break
    if best is None:
        return None, None, False
    return best[1], best[2], False


def _answer_change_request(
    application: Any, budget: Any, repository: Repository, row: dict[str, Any], pr: int,
    inspected: dict[str, Any], numbers: list[int], renovate: bool,
    summaries: dict[str, dict[str, Any]], now: Any, executor_run_prefix: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Keep a PR with a standing change request out of the election.

    Agent comments that answer the request get one re-review of the same head
    first, through the pass's review budget; it never refreshes,
    marks or merges. Returns the read to rank when that review approved,
    else None after a park, and whether the pass stops. A `_TrainStop`
    propagates with nothing written for this PR.
    """
    if _comment_rereview_due(inspected) and not renovate:
        result, park = _comment_rereview(budget, repository, pr, str(inspected["head_sha"]),
                                         inspected, executor_run_prefix)
    else:
        result = {"pr": pr, "outcome": "retained", "reason": "PR_REVIEW_CHANGES_REQUESTED"}
        park = "changes_requested"
    if _record_automerge_result(result, repository, summaries):
        return None, True
    observed = inspected
    if result.get("comment_rereview") and park in {None, "changes_requested"}:
        # A fresh review was just published: rank or park on the read that shows it.
        observed = _inspect_candidate(application, repository, pr)
        if not observed.get("ok"):
            if _record_automerge_result(
                    {"pr": pr, "outcome": "retained",
                     "reason": str(observed.get("code") or "INSPECT_FAILED")},
                    repository, summaries):
                return None, True
            if park is None:
                return None, False  # Unparked: the next selection reads it again.
            observed = inspected
    if park is None:
        if (observed.get("blocking_reason") != "PR_REVIEW_CHANGES_REQUESTED"
                and observed.get("pr_state") not in {"draft", "closed", "merged"}):
            return observed, False
        park = "changes_requested"
    if park == "changes_requested" and renovate:
        # Its bot author never answers a review, so the operator owns it.
        park = "renovate_changes_requested"
    row["parked"][str(pr)] = {**_park_record(inspected, park, now),
                              "review": _review_identity(observed),
                              "claim_sha": observed.get("claim_sha")}
    _clear_candidate(application, repository, pr, inspected)
    numbers.remove(pr)
    return None, False


def _queue_descriptions(
    row: dict[str, Any], queued: list[int], active_reason: str,
    eligible_reasons: dict[int, str] | None = None,
) -> dict[int, tuple[str, str | None]]:
    """The `gate/queue` status description of each PR still in the queue.

    Positions count the active PR first, then eligible PRs in the order of
    `queued`, which `_slot_order` gives: by priority group, and inside each
    non-Renovate PRs in ascending number, then Renovate PRs in ascending
    number, except that a due Renovate slot moves each group's Renovate PRs
    first; a parked PR has none. Each description travels with the head a
    parked PR was parked on (None otherwise). The forge write can see an
    author's push before the next candidate inspection.
    """
    active = row["active"]
    active_pr = active["pr"] if active is not None else None
    if active is not None and "train" in active:
        # The saved leader stays the chain owner after it lands. Display the
        # next queued rider as active so positions describe remaining work.
        active_pr = next((member["pr"] for member in active["train"]["members"]
                          if member["pr"] in queued), None)
    position = 1 if active_pr is not None else 0
    descriptions: dict[int, tuple[str, str | None]] = {}
    for pr in queued:
        parked = row["parked"].get(str(pr))
        if pr == active_pr:
            descriptions[pr] = (f"queue=active reason={active_reason} owner=queue pos=1", None)
        elif parked is not None:
            # A held park holds on every head, so it names none.
            details = parked_details(parked, row.get("consented", {}).get(str(pr)))
            consent = (f" consented={details['consented_head']} hold={details['hold_label']}"
                       if "consented_head" in details else "")
            descriptions[pr] = (f"queue=parked reason={details['reason']} "
                                f"owner={details['owner']} pos=-{consent}",
                                None if parked["reason"] in HOLD_PARK_REASONS
                                else parked["head"])
        else:
            position += 1
            reason = (eligible_reasons or {}).get(pr, "-")
            descriptions[pr] = (f"queue=eligible reason={reason} owner=queue pos={position}", None)
    return descriptions


def _publish_queue(
    application: Any, repository: Repository, row: dict[str, Any],
    descriptions: dict[int, tuple[str, str | None]],
    *, reassert_held: bool = False,
) -> bool:
    """Publish changed queue statuses; a failure is logged, never raised.

    True when every write is proven.

    A PR whose write is unproven is left out of `published`, so the next
    pass writes it again. A failed release of a held status keeps the prior
    held pair instead, so the next pass retries it before any paid review.
    A PR with a record but no description has left the
    queue, merged by this pass or closed, merged or drafted elsewhere: it
    gets one best-effort `QUEUE_DONE` write on the head its record names,
    never on whatever its branch heads now (a reused Renovate branch may
    head another PR), and leaves `published` once the
    gate has attempted it, even if it failed: its branch may be gone. A call
    the gate refuses as a whole attempts nothing, so its records stay.

    A lost release response can leave a cached held pair after the forge
    became eligible. Reasserting a hold cannot trust those pairs for dedup.
    """
    published_before = row.get("published", {})
    finished = [int(key) for key in published_before if int(key) not in descriptions]
    descriptions = {**descriptions, **{pr: (QUEUE_DONE, published_before[str(pr)][0])
                                       for pr in finished}}
    if not descriptions:
        row["published"] = {}
        return True
    dedup = ({key: pair for key, pair in published_before.items()
              if descriptions[int(key)][0] != QUEUE_HELD}
             if reassert_held else published_before)
    result: dict[str, Any]
    try:
        result = application.queue_status(repository.name, descriptions, dedup)
    except Exception as exc:  # metadata only: the pass must still save its state
        result = {"ok": False, "code": _candidate_exception(0, exc)["reason"]}
    if not result.get("ok"):
        LOGGER.warning("automerge queue status not published: %s %s",
                       repository.name, result.get("code"))
        return False
    failed = sorted((pr for pr, value in result["published"].items() if value is None), key=int)
    published = {pr: value for pr, value in result["published"].items()
                 if value is not None and int(pr) not in finished}
    for pr in failed:
        previous = published_before.get(pr)
        if int(pr) not in finished and previous is not None and previous[1] == QUEUE_HELD:
            published[pr] = previous
    if failed:
        LOGGER.warning("automerge queue status not published: %s#%s",
                       repository.name, ",".join(failed))
    row["published"] = published
    return not failed


def _publish_hold(
    repositories: list[Repository], deploy: dict[str, Any],
    application: Any | None = None, *, queue_store: QueueStore | None = None,
) -> None:
    """Show a deploy hold on every queued PR's `gate/queue` status, once.

    A skipped pass writes nothing else, so another host would keep reading
    the last pass's queue position. Only queue-owned descriptions become
    `queue=held`: an author- or operator-owned park still names who acts.
    The row's `held` records the hold whose writes were all proven, so a
    long hold costs no forge call per pass. `published` keeps each held
    pair, so the next normal pass rewrites every one. Metadata only: a
    failure is logged, never raised, and a busy lock retries next pass.
    """
    plan = (deploy.get("deploy") or {}).get("plan_id")
    hold = plan if isinstance(plan, str) and HOLD_ID.fullmatch(plan) else "-"
    queue_store = queue_store or QueueStore(
        repositories=[repository.name for repository in repositories])
    try:
        with queue_store.locked() as state:
            for repository in repositories:
                if state["repositories"].get(repository.name, {}).get("held") == hold:
                    continue
                application = application or _gate_application()
                listed = application.candidates(repository.name)
                if not listed.get("ok"):
                    LOGGER.warning("automerge hold not published: %s %s",
                                   repository.name, listed.get("code") or "PR_LIST_FAILED")
                    continue
                numbers = sorted(set(listed.get("pull_requests", [])))
                trains = _train_numbers(listed, numbers)
                queued = [number for number in numbers if number not in trains]
                row = _queue_row(state, repository.name)
                descriptions = _queue_descriptions(row, queued, "-")
                for pr in queued:
                    parked = row["parked"].get(str(pr))
                    if parked is None or PARK_OWNERS[parked["reason"]] == "queue":
                        descriptions[pr] = (QUEUE_HELD, None)
                if _publish_queue(application, repository, row, descriptions, reassert_held=True):
                    row["held"] = hold
                    LOGGER.info("automerge hold published: %s plan %s", repository.name, hold)
                queue_store.save(state)
    except QueueStateError as exc:
        if str(exc) != "AUTOMERGE_PASS_BUSY":
            LOGGER.warning("automerge hold not published: %s", exc)
    except Exception as exc:  # metadata only: the hold itself must stand
        LOGGER.warning("automerge hold not published: %s", type(exc).__name__)


def _park_renovate_holds(
    application: Any, repository: Repository, row: dict[str, Any], numbers: list[int],
    queued: list[int], holds: dict[int, str], summaries: dict[str, dict[str, Any]], now: Any,
    renovate: set[int],
) -> bool:
    """Park each listed held Renovate PR for the operator; report an integrity stop.

    It runs before selection, so neither the queue nor `_review_ahead` spends
    a refresh or a paid review on one. A label park holds through every head,
    CI or review change and ends only when every hold label is gone. A PR
    that was already parked keeps its record under the new reason; an active
    one gives up its slot. A PR whose head cannot be read this pass leaves
    the pass's candidates and its queue statuses instead, and the next pass
    parks it.

    Removing the label is the operator's consent, and it binds the head the
    PR has when a pass first sees the label gone: the row's `consented` entry
    keeps that head and the hold reason. The executor's own refresh moves the
    entry to the head it made (`_consent_refreshed`). An external refresh
    carries consent only after the gate proves unchanged content and refs.
    Any unproven head parks under its hold reason; neither an absent label
    nor `--unpark` is consent. Re-adding the label withdraws the consent, so
    removing it again consents to the head the PR has then.

    A PR whose labels the listing could not read (`renovate_labels_unknown`)
    keeps an existing hold park's reason, so a label removed meanwhile is
    still a consent; once the labels read again without a hold label, that
    park ends with no consent of its own, and the operation-time check parks
    the PR as `renovate_consent_missing` if its label history shows a hold
    label. That park is no removed label either: only adding the label back
    and removing it again consents. A held PR the listing no longer
    marks as Renovate's (`renovate`) keeps its park: that is an unread
    listing, not a removed label.
    """
    consents = row.pop("consented", {})
    try:
        return _park_held(application, repository, row, numbers, queued, holds, summaries,
                          now, renovate, consents)
    finally:
        if consents:
            row["consented"] = consents


def _park_held(
    application: Any, repository: Repository, row: dict[str, Any], numbers: list[int],
    queued: list[int], holds: dict[int, str], summaries: dict[str, dict[str, Any]], now: Any,
    renovate: set[int], consents: dict[str, list[str]],
) -> bool:
    """`_park_renovate_holds` with the row's consents held apart."""
    for pr, reason in holds.items():
        if reason in RENOVATE_LABEL_HOLD_REASONS:
            consents.pop(str(pr), None)  # The label is back: consent is withdrawn.
    for key, parked in list(row["parked"].items()):
        if parked["reason"] in LABEL_HOLD_REASONS and int(key) not in holds:
            # Released, or the labels read again: normal admission, by the
            # existing queue order. Nothing was consented to.
            del row["parked"][key]

    def unreadable(pr: int, inspected: dict[str, Any]) -> bool:
        """Record a failed read; drop `pr` from this pass. True on an integrity stop."""
        if _record_automerge_result(
                {"pr": pr, "outcome": "retained",
                 "reason": str(inspected.get("code") or "INSPECT_FAILED")},
                repository, summaries):
            return True
        skip(pr)
        return False

    def skip(pr: int) -> None:
        """No turn this pass, the active slot included: nothing proved its head."""
        numbers.remove(pr)
        queued.remove(pr)
        if row["active"] is not None and row["active"]["pr"] == pr:
            row["active"] = None

    consented_now: set[str] = set()
    for key, parked in list(row["parked"].items()):
        if (parked["reason"] not in RENOVATE_HOLD_REASONS or int(key) in holds
                or int(key) not in renovate or key in consents):
            continue
        if parked["reason"] == RENOVATE_CONSENT_MISSING:
            continue  # No label was removed this pass: no consent.
        if parked["reason"] == RENOVATE_LABELS_UNKNOWN:
            # The labels read without a hold label; the operation-time
            # history check parks the PR again if one was ever there.
            del row["parked"][key]
            continue
        # The operator removed the hold label: consent binds the current head.
        inspected = _inspect_candidate(application, repository, int(key))
        if not inspected.get("ok") or not SHA_RE.fullmatch(str(inspected.get("head_sha") or "")):
            if unreadable(int(key), inspected):
                return True
            continue  # Still parked: no consent without the head it binds.
        if inspected.get("pr_state") in {"closed", "merged"}:
            continue  # It left the queue; `_note_renovate_bypass` judges that.
        consents[key] = [str(inspected["head_sha"]), parked["reason"]]
        consented_now.add(key)
        del row["parked"][key]
        LOGGER.info("automerge: renovate consent %s#%s %s on %s", repository.name, key,
                    parked["reason"], inspected["head_sha"])
    for key, (head, reason) in list(consents.items()):
        pr = int(key)
        parked = row["parked"].get(key)
        if key in consented_now or pr not in numbers or pr in holds:
            continue
        if pr not in renovate:  # An unread listing: no head check, so no turn.
            skip(pr)
            continue
        inspected = _inspect_candidate(application, repository, pr)
        if not inspected.get("ok"):
            if unreadable(pr, inspected):
                return True
            continue
        if inspected.get("pr_state") in {"closed", "merged"}:
            continue
        if inspected.get("head_sha") == head:
            if parked is not None and parked["reason"] in RENOVATE_HOLD_REASONS:
                del row["parked"][key]  # Labels read again on the consented head.
            continue
        # One proof attempt per observed head. Existing parks without a code
        # get a first attempt; unpark retries unavailable proof, not consent.
        if (parked is not None and parked["reason"] == reason
                and parked["head"] == inspected.get("head_sha")
                and parked.get("code") == "RENOVATE_CONSENT_STALE"):
            continue
        try:
            preserved = application.consent_preserved(repository.name, pr, head,
                str(inspected.get("head_sha") or ""), str(inspected.get("base_sha") or ""))
        except ValueError:
            # A malformed identity proves nothing; park this PR, not the pass.
            preserved = False
        if preserved is True:
            consents[key][0] = str(inspected["head_sha"])
            if parked is not None and parked["reason"] in RENOVATE_HOLD_REASONS:
                del row["parked"][key]
            LOGGER.info("automerge: renovate consent preserved %s#%s %s -> %s basis=clean_refresh",
                        repository.name, pr, head, inspected["head_sha"])
            continue
        # A head whose equivalence was not proven: back to its hold park.
        active = row["active"]
        if active is not None and active["pr"] == pr:
            row["active"] = None
        row["parked"][key] = {**_park_record(inspected, reason, now), "review": [None, None],
                               "code": "RENOVATE_CONSENT_STALE"}
        _record_automerge_result({"pr": pr, "outcome": "retained",
                                  "reason": "RENOVATE_CONSENT_STALE", "code": reason.upper()},
                                 repository, summaries)
    for pr in [number for number in numbers if number in holds]:
        parked = row["parked"].get(str(pr))
        if parked is not None:
            if not (holds[pr] in {RENOVATE_LABELS_UNKNOWN, HOLD_LABELS_UNKNOWN}
                    and parked["reason"] in RENOVATE_HOLD_REASONS):
                parked["reason"] = holds[pr]
            continue
        active = row["active"]
        if active is not None and active["pr"] == pr:
            # An author's hold never tears down a train that is running: the
            # park only records it, and `_advance_train` merges the members
            # ahead of the held one and then closes the train.
            keep_train = "train" in active and holds[pr] in LABEL_HOLD_REASONS
            if "train" in active and not keep_train:
                # A held leader takes its train with it: the slot it frees
                # must not leave an open train PR nothing drives any more.
                if not _close_train(application, repository, active["train"], "RENOVATE_HOLD",
                                    summaries, "closed", None):
                    return True
            record = {"head": active["head"], "base": active["base"], "run": active["run"]}
            if not keep_train:
                _clear_candidate(application, repository, pr)
                row["active"] = None
        else:
            inspected = _inspect_candidate(application, repository, pr)
            if not inspected.get("ok"):
                if unreadable(pr, inspected):
                    return True
                continue
            record = {"head": str(inspected.get("head_sha") or ""),
                      "base": str(inspected.get("base_sha") or ""),
                      "run": _run_identity(inspected)}
        row["parked"][str(pr)] = {"reason": holds[pr], **record,
                                  "review": [None, None], "since": now()}
        _record_automerge_result({"pr": pr, "outcome": "retained", "reason": holds[pr].upper()},
                                 repository, summaries)
    return False


def _consent_refreshed(row: dict[str, Any], pr: int, result: dict[str, Any]) -> None:
    """Carry a Renovate PR's consent to the head the executor's refresh made.

    The gate refreshes only from the head the pass selected, by a forge
    merge of main, so the new head adds no Renovate commit. A refresh that
    reports no head leaves the entry, and the next pass parks the PR again.
    """
    consent = row.get("consented", {}).get(str(pr))
    head = result.get("head_sha")
    if (consent is not None and result.get("outcome") == "refreshed"
            and isinstance(head, str) and SHA_RE.fullmatch(head)):
        consent[0] = head


def _note_refresh(row: dict[str, Any], active: dict[str, Any], pr: int,
                  result: dict[str, Any], renovate: bool) -> None:
    """Record a refresh the executor made, in memory: consent and Renovate retry.

    It runs as soon as a refresh returns, before any later gate call: an
    integrity stop or a deploy checkpoint
    after the refresh must still keep the executor-made head, or the next
    pass would spend the Renovate retry on it again. Idempotent.
    """
    _consent_refreshed(row, pr, result)
    if (renovate and result.get("outcome") == "refreshed"
            and SHA_RE.fullmatch(str(result.get("head_sha") or ""))):
        active["refreshed_head"] = result["head_sha"]


def _forget_consent(row: dict[str, Any], key: str) -> None:
    consents = row.get("consented", {})
    consents.pop(key, None)
    if not consents:
        row.pop("consented", None)


def _note_renovate_bypass(
    application: Any, repository: Repository, row: dict[str, Any], numbers: list[int],
) -> None:
    """Warn once when a held or consented Renovate PR merged outside the queue.

    It runs before a departed PR's park is dropped. The executor never merges
    a held PR, so any merge of one is a bypass; a consented PR it merges
    itself loses its entry first (`run_automerge_pass`), so a crash after
    that merge is told apart by the merging login. A read that fails is
    logged and dropped: the PR has left the queue either way.
    """
    held = {key for key, parked in row["parked"].items()
            if parked["reason"] in RENOVATE_HOLD_REASONS}
    for key in sorted(held | set(row.get("consented", {})), key=int):
        if int(key) in numbers:
            continue
        _forget_consent(row, key)
        inspected = _inspect_candidate(application, repository, int(key))
        if not inspected.get("ok"):
            LOGGER.warning("automerge: renovate hold left the queue unread %s#%s %s",
                           repository.name, key, inspected.get("code"))
            continue
        merged_by = inspected.get("merged_by")
        if inspected.get("pr_state") == "merged" and (key in held or merged_by != FORGEJO_LOGIN):
            LOGGER.warning("automerge: renovate hold merged outside the queue %s#%s by %s",
                           repository.name, key, merged_by or "unknown")


def _narrowed_run_accepted(inspected: dict[str, Any]) -> bool:
    """Whether a head that is not green has CI the snapshot review accepts.

    A gate-C1-class head reads CI_PROFILE_INSUFFICIENT, not green, yet the
    review accepts its successful narrowed run over the recorded base, as
    it accepts a green run on that base once main moved.
    `ci_review_accepted` is the gate's own answer, projected from the
    CI-evidence rule the review applies. A read without the field, or
    with it false, is not accepted: the head is handled as it was before
    the gate answered.
    """
    return inspected.get("ci_review_accepted") is True and inspected.get("ci_state") != "green"


def _review_ahead_base(repository: Repository, inspected: dict[str, Any],
                       pr: int | None = None) -> str | None:
    """The base a review ahead of the PR's turn reviews over, or None for no review.

    The current base for green CI; the recorded base for a narrowed run the
    gate accepts, or a green run on a moved base. The forge's fork
    point proves that the head contains the recorded base. None
    when the review owner would refuse the pair or the PR needs no review:
    a claim error, a green head that does not contain main (`PR_BASE_STALE`)
    or whose base the gate cannot prove (`PR_BASE_UNVERIFIED`), red or pending
    CI without accepted review evidence, a closed or draft PR, an
    approval of this head, a known conflict, a standing change request, or an
    unmarked head whose class needs the full profile and whose narrowed run
    the gate does not accept. Given `pr`, a skipped head is logged
    with its reason. Shared by `_review_ahead` and the concurrent lookahead
    (`_run_automerge_pass`).
    """
    def skip(reason: str) -> None:
        if pr is not None:
            LOGGER.info("automerge: review ahead skipped %s#%s %s", repository.name, pr, reason)

    claim_error = inspected.get("claim_error")
    if claim_error in CLAIM_ERROR_CODES:
        return skip(f"claim_error:{claim_error}")
    # The PRs that need no review come first, so the log names the reason
    # that matters when one of them also has a base the gate cannot prove.
    blocker = inspected.get("blocking_reason")
    pr_state = inspected.get("pr_state")
    if pr_state in {"draft", "closed", "merged"}:
        return skip(f"pr_{pr_state}")
    if (inspected.get("review_state") == "approved"
            or inspected.get("latest_delegated_review_state") == "APPROVED"):
        return skip("approved")
    if inspected.get("mergeable") is False:
        return skip("not_mergeable")
    if blocker == "PR_REVIEW_CHANGES_REQUESTED":
        return skip("changes_requested")
    # A run on current main can be green while the submitted head
    # still forks before main. The snapshot reviewer cannot prove
    # that pair as a recorded-base review; serial refresh owns it. A base
    # the gate cannot prove either is refused the same way.
    if blocker in {"PR_BASE_STALE", "PR_BASE_UNVERIFIED"}:
        return skip(str(blocker))
    narrowed = _narrowed_run_accepted(inspected)
    # without the gate's accepted review evidence, a profile
    # shortfall still waits for the serial queue's full run. An answer the
    # gate could not decide is named as such.
    if (not narrowed
            and repository.merge_candidate_policy == CANDIDATE_POLICY
            and inspected.get("ci_state") != "green"
            and inspected.get("merge_candidate") is not True
            and inspected.get("ci_required_profile") == STRONGEST_PROFILE):
        return skip(_ci_evidence_unknown(inspected) or "needs_full_run")
    # The gate owns CI acceptance; the executor only selects the range. A
    # narrowed head is reviewed over the forge's fork point, never a main
    # tip it does not contain. The review owner rechecks ancestry.
    if narrowed:
        recorded = inspected.get("recorded_base_sha")
        if not isinstance(recorded, str) or not SHA_RE.fullmatch(recorded):
            return skip("recorded_base_unverified")
        return recorded
    base = (inspected.get("base_sha") if inspected.get("ci_state") == "green"
            else inspected.get("recorded_base_sha")
            if _refresh_eligible(repository, inspected) else None)
    return (str(base) if base
            else skip(_ci_evidence_unknown(inspected) or str(blocker or "ci_not_green")))


def _review_ahead_result(pr: int, reviewed: dict[str, Any]) -> dict[str, Any]:
    """The record of one review ahead of the PR's turn, serial or lookahead."""
    if reviewed.get("ok") and reviewed.get("verdict") != "CHANGES_REQUIRED":
        return {"pr": pr, "outcome": "reviewed_ahead"}
    result = {"pr": pr, "outcome": "retained", "reason": _review_reason(reviewed),
              "review_ahead": True}
    for key in ("review_error", "review_error_detail", "retry_refused", "recovery_action"):
        if reviewed.get(key) is not None:
            result[key] = reviewed[key]
    return result


def _record_review_ahead(
    repository: Repository,
    row: dict[str, Any],
    pr: int,
    inspected: dict[str, Any],
    result: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
    eligible_reasons: dict[str, dict[int, str]],
    save: Callable[[], None],
    now: Callable[[], float],
) -> bool:
    """Park, record and queue-status one review-ahead outcome; True on an integrity stop.

    Shared by `_review_ahead` and the concurrent lookahead
    (`_run_automerge_pass`), so both park an unsafe execution failure and
    stop the pass on an authority or integrity code alike.
    """
    if _park_reason(result, inspected) == "review_execution_failed":
        row["parked"][str(pr)] = _park_record(inspected, "review_execution_failed", now)
        save()
    stop = _record_automerge_result(result, repository, summaries)
    eligible_reasons.setdefault(repository.name, {})[pr] = (
        "-" if result["outcome"] == "reviewed_ahead" else "waiting_review")
    return stop


def _rotating_order(numbers: list[int], cursor: int, priorities: dict[int, int]) -> list[int]:
    """*numbers* from just after *cursor*, wrapping: a bounded scan resumes where the last one stopped.

    The rotation runs inside each `queue/*` priority group
    (`_queue_priorities`), higher groups first, so a scan reaches every
    `queue/high` PR before any other.
    """
    start = numbers.index(cursor) + 1 if cursor in numbers else 0
    return sorted(numbers[start:] + numbers[:start],
                  key=lambda number: priorities.get(number, PRIORITY_NORMAL))


def _review_ahead(
    budget: _ReviewBudget,
    repositories: list[Repository],
    listed_by_repo: dict[str, list[int]],
    priorities: dict[str, dict[int, int]],
    renovate: dict[str, set[int]],
    state: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
    executor_run_prefix: str,
    eligible_reasons: dict[str, dict[int, str]],
    save: Callable[[], None],
    now: Callable[[], float] = time.time,
) -> None:
    """Spend an unused review slot on a later queued PR that is green now,
    or green on its recorded base.

    A PR whose trusted run succeeded on the base it was pushed on is
    reviewed over its recorded base, its fork point from main, which is
    what lets it ride a train without a refresh; one whose latest
    delegated review already approves its head needs no review. It runs after the per-repository loop because
    the review budget is shared by the whole pass: inside the loop it could
    take a slot from a later repository's active PR. It is safe
    because an approval survives a later clean refresh, and it
    only reviews: it never refreshes, merges or activates the PR. An unsafe
    or exhausted execution failure is parked under operator ownership, as is a
    Renovate PR whose head has a confirmed failed run and no catch-up refresh
    left (`renovate_ci_failed`), so it never takes a slot. A failed run the
    infrastructure lost gets the gate's one rerun first and the
    PR stays unparked.
    It can release a repaired claim park from current validation. The
    `review_ahead` cursor records the last PR either bounded scan inspected --
    this one or the concurrent lookahead -- so the next pass resumes from the
    PR after it in queue order and wraps around, and the inspection cap does
    not keep either scan on the head of the queue. A cursor PR that left the
    queue restarts at the head. The normal path owns the PR once it is active.
    The caller passes only repositories whose active PR is not itself
    waiting on review -- it waits on CI, a refresh, a merge or a train, or
    the repository has none: a busy or failed review of the active PR keeps
    the slot idle. Each lending repository reviews at most one PR here and
    never past its per-repository cap; the pass-wide inspection cap still
    bounds the whole call. A refusal the review owner marks
    `dispatched: false` is no review, so the repository's next candidate
    may still take its turn.
    """
    inspections = 0
    for repository in repositories:
        if budget.checkpoint is not None:
            budget.checkpoint()  # A waiting deploy is not kept for more inspections.
        if budget.exhausted(repository.name):
            continue
        row = _queue_row(state, repository.name)
        active_pr = row["active"]["pr"] if row["active"] is not None else None
        # A train member's approval already reaches its train position.
        riding = ({member["pr"] for member in row["active"]["train"]["members"]}
                  if row["active"] is not None and "train" in row["active"] else set())
        listed = listed_by_repo[repository.name]
        for pr in _rotating_order(listed, row.get("review_ahead", 0),
                                  priorities[repository.name]):
            if inspections >= REVIEW_AHEAD_INSPECTIONS:
                return
            parked = row["parked"].get(str(pr))
            if (pr == active_pr or pr in riding
                    or (repository.name, pr) in budget.lookahead_attempted
                    or parked is not None and parked["reason"] != "claim_unavailable"):
                continue
            inspections += 1
            row["review_ahead"] = pr
            inspected = _inspect_candidate(budget, repository, pr)
            if not inspected.get("ok"):
                if _record_automerge_result({"pr": pr, "outcome": "retained",
                                             "reason": str(inspected.get("code") or "INSPECT_FAILED"),
                                             "review_ahead": True}, repository, summaries):
                    return
                continue
            if parked is not None:
                if not claim_validation_succeeded(inspected):
                    continue
                del row["parked"][str(pr)]
                save()
            consent = row.get("consented", {}).get(str(pr))
            if consent is not None and inspected.get("head_sha") != consent[0]:
                continue  # The active path re-parks it; no review on an unapproved head.
            # The gate reports `label_history` only for a Renovate PR. One it
            # omits still meets the active path's check before any merge.
            if (consent is None and "label_history" in inspected
                    and _consent_missing(inspected)):
                continue  # The active path parks it; no review without consent.
            if (pr in renovate[repository.name] and _failed_run(inspected)
                    and not _refresh_eligible(repository, inspected, red_renovate=True)):
                # a run the infrastructure lost gets its one rerun first
                # and the PR keeps its place; no review is spent on it.
                rerun = _rerun_lost_ci(budget, repository, pr)
                if rerun is not None:
                    if _record_automerge_result({**rerun, "review_ahead": True},
                                                repository, summaries):
                        return
                    continue
                # Parked before it can take a slot; a red run on a moved base
                # keeps its one catch-up refresh on the active path.
                row["parked"][str(pr)] = _park_record(inspected, "renovate_ci_failed", now)
                save()
                if _record_automerge_result(
                        {"pr": pr, "outcome": "retained", "review_ahead": True,
                         "reason": str(inspected.get("blocking_reason") or "PR_CI_NOT_GREEN")},
                        repository, summaries):
                    return
                continue
            base = _review_ahead_base(repository, inspected, pr)
            if base is None:
                continue
            try:
                reviewed = _review_with_recovery(
                    budget, repository.name, pr, str(inspected["head_sha"]),
                    str(base), f"{executor_run_prefix}:{repository.name}:{pr}",
                )
            except Exception as exc:
                reviewed = None
                result = {**_candidate_exception(pr, exc), "review_ahead": True}
            else:
                result = _review_ahead_result(pr, reviewed)
            stop = _record_review_ahead(repository, row, pr, inspected, result, summaries,
                                        eligible_reasons, save, now)
            if stop or budget.spent() or budget.out_of_time():
                return
            if reviewed is not None and reviewed.get("dispatched") is False:
                # Refused before any dispatch: this repository's one review
                # ahead is still unused, so try its next candidate.
                continue
            break  # One review ahead per repository; the next one may lend.


# ---------------------------------------------------------------- merge train
#
# Both train policies may replace a serial refresh or candidate mark with a
# batch of up to three approved members. v1 starts one run per prefix, so its
# election-time cap remains two (`TRAIN_ELECTION_MEMBERS`). v2 starts
# only the full owner's run and keeps the three-member cap at either moment.
# The gate builds, verifies and merges; the executor chooses queue-ordered
# members and persists the active train. A train forms at most once per pass.

TRAIN_POLICIES = frozenset({"train-v1", "train-v2"})
TRAIN_MAX_MEMBERS = 3
# a train formed in the place of a candidate mark (election time) starts
# one prefix run per member at once, on a pool whose free slots the executor
# cannot read, so it takes at most this many members and journals the cap.
TRAIN_ELECTION_MEMBERS = 2
TRAIN_CAP_REASON = "strong_ci_slots_unreadable"
TRAIN_BRANCH_PREFIX = "train/"
#: A v2 build refusal after the owner ref may already exist: the `open_pr`
#: step fails after `train_open_owner_pr` pushed the branch, so the pass must
#: not start a serial run beside a possibly live owner.
TRAIN_UNCERTAIN_PUBLICATION_STEPS = frozenset({"open_pr"})
# Later queued PRs inspected when choosing train members, beyond the active one.
TRAIN_SCAN_INSPECTIONS = 4


def _train_numbers(listed: dict[str, Any], numbers: list[int]) -> set[int]:
    """The listed PRs that are merge trains: never queued, reviewed or merged."""
    heads = listed.get("head_refs")
    heads = heads if isinstance(heads, dict) else {}
    return {number for number in numbers
            if isinstance(head := heads.get(str(number)), str)
            and head.startswith(TRAIN_BRANCH_PREFIX)}


def _train_ineligibility(repository: Repository, inspected: dict[str, Any]) -> str | None:
    """Why a queued PR may not ride a train, as far as the executor can tell.

    None when it may: trusted successful CI on its own head for any base,
    and a delegated approval of that head for any base: an unrefreshed PR
    reviewed on an older base reads `review_state: not_approved`, but its
    latest delegated review still approves its head. The gate re-verifies
    both and carries the approval to the PR's train position only by digest.

    A head whose narrowed run the snapshot review accepts lacks
    only a review, so it reads `not_approved` until it has one, and rides
    like a green one after it: the gate's train build admits it on
    the same rule, and the member's own merge needs its prefix run, which is
    the full profile, not its narrowed run.

    Under `train-v2` the CI condition is the gate's own: the record's
    `ci_evidence_answer` must be `accepted`, because the build
    admits a member on exactly that answer.
    """
    if not inspected.get("ok"):
        return "inspect_failed"
    if inspected.get("pr_state") in {"draft", "closed", "merged"}:
        return "draft_or_closed"
    if inspected.get("queue_hold"):
        return "queue_hold"
    if inspected.get("blocking_reason") == "PR_REVIEW_CHANGES_REQUESTED":
        return "changes_requested"
    if repository.merge_batch_policy == "train-v2":
        # train-v2's member refusal admits exactly what the record's
        # CI-evidence answer accepts. Offering any other head --
        # a success on an older base the answer rejects, for one -- spends a
        # pass on a build the gate refuses. A read without the answer fails
        # closed.
        if inspected.get("ci_evidence_answer") != "accepted":
            return _ci_evidence_unknown(inspected) or "ci_not_green"
    elif (inspected.get("ci_state") != "green" and not _narrowed_run_accepted(inspected)
            and not _refresh_eligible(repository, inspected)):
        # train-v1 admits a success on any base (`_train_member_refusal`), so a
        # refresh-eligible head rides whatever the answer.
        return _ci_evidence_unknown(inspected) or "ci_not_green"
    if (inspected.get("review_state") != "approved"
            and inspected.get("latest_delegated_review_state") != "APPROVED"):
        return "not_approved"
    if inspected.get("mergeable") is False:
        return "not_mergeable"
    return None


def _park_record(inspected: dict[str, Any], reason: str, now: Any) -> dict[str, Any]:
    return {"reason": reason, "head": str(inspected.get("head_sha") or ""),
            "base": str(inspected.get("base_sha") or ""), "run": _run_identity(inspected),
            "review": _review_identity(inspected), "claim_sha": inspected.get("claim_sha"),
            "since": now()}


def _head_adds_nothing(repository: Repository, inspected: dict[str, Any]) -> bool:
    """True when the PR head changes nothing against its base.

    Covers a head already contained in the base (merged by hand while the forge
    still lists the PR as open) and a no-op merge head, whose tree equals the
    base. A diff git cannot compute proves nothing, so it reads False and the PR
    proceeds as before.
    """
    head, base = inspected.get("head_sha"), inspected.get("base_sha")
    if not (isinstance(head, str) and isinstance(base, str)
            and SHA_RE.fullmatch(head) and SHA_RE.fullmatch(base)):
        return False
    try:
        return not _diff_paths(LocalRunner(), str(repository.path), base, head, merge_base=True)
    except HostDeployError:
        return False


def _train_journal(train: dict[str, Any], outcome: str, **extra: Any) -> dict[str, Any]:
    return {"pr": train["pr"], "members": [member["pr"] for member in train["members"]],
            "candidate_sha": train["members"][-1]["candidate"],
            "candidate_tree": extra.pop("candidate_tree", None), "outcome": outcome,
            **{key: train[key] for key in ("mode", "generation", "phase") if key in train}, **extra}


def _adopt_orphan_v2_owner(
    application: Any, repository: Repository, pr: int, row: dict[str, Any],
    summaries: dict[str, dict[str, Any]], now: Any,
) -> str:
    """Classify an unsaved train PR before the legacy orphan sweep retires it.

    Returns ``adopted`` after holding a recoverable v2 owner in the active
    slot, ``occupied`` for an owner a persisted train already outranks,
    ``unreadable`` when this pass cannot judge it, and ``invalid`` when proof
    positively shows the
    owner is not a driveable full-mode generation, which the caller closes as
    before.

    The legacy sweep retires an unsaved train PR. For a v2 full-mode
    generation the gate can still drive, closing it would destroy the only
    native proof of its member-head set: the next formation finds that closed
    owner in the gate's closed-batch guard and parks the same batch until a
    head or a base moves. So a readable, open, main-carried full-mode
    generation is adopted instead. Unreadable evidence is not proof of waste,
    and an occupied slot is not permission to replace the train already there;
    both defer rather than close.

    The wait clock starts here: the dispatch it recovers was real, but its
    original start time died with the lost response, and inventing an older
    one would shorten the wait it is entitled to.
    """
    if repository.merge_batch_policy != "train-v2":
        return "invalid"
    active = row.get("active")
    if active is not None and "train" in active:
        # One slot, one train. Never replace a persisted train with an orphan,
        # and never retire one that a later pass could still adopt.
        return "occupied"
    try:
        observed = application.train_inspect(repository.name, pr)
    except Exception as exc:  # unreadable is not proof of waste
        code = exc.code if isinstance(exc, GateError) else None
        if code in {"TRAIN_INVALID", "REMOTE_HEAD_INVALID"}:
            return "invalid"
        return "unreadable"
    if not isinstance(observed, dict):
        return "unreadable"
    if observed.get("ok") is False:
        code = observed.get("code")
        return "invalid" if code in {"TRAIN_INVALID", "REMOTE_HEAD_INVALID"} else "unreadable"
    members, prefixes = observed.get("members"), observed.get("prefixes")
    generation = observed.get("generation")
    if (observed.get("mode") != "full"
            or not isinstance(generation, str) or GENERATION_RE.fullmatch(generation) is None
            or observed.get("pr_state") != "open"
            or observed.get("main_on_chain") is not True
            or not isinstance(observed.get("train_branch"), str)
            or not isinstance(observed.get("base_sha"), str)
            or SHA_RE.fullmatch(observed["base_sha"]) is None
            or not isinstance(members, list) or not 2 <= len(members) <= TRAIN_MAX_MEMBERS
            or not isinstance(prefixes, list) or len(prefixes) != len(members)):
        return "invalid"
    kept: list[dict[str, Any]] = []
    for member in members:
        if (not isinstance(member, dict) or type(member.get("pr")) is not int
                or not isinstance(member.get("head"), str)
                or SHA_RE.fullmatch(member["head"]) is None
                or not isinstance(member.get("candidate"), str)
                or SHA_RE.fullmatch(member["candidate"]) is None):
            return "invalid"
        kept.append({"pr": member["pr"], "head": member["head"],
                     "candidate": member["candidate"]})
    projected: list[dict[str, Any]] = []
    for position, prefix in enumerate(prefixes, 1):
        if (not isinstance(prefix, dict) or prefix.get("position") != position
                or type(prefix.get("pr")) is not int
                or not isinstance(prefix.get("branch"), str)
                or not isinstance(prefix.get("candidate"), str)
                or SHA_RE.fullmatch(prefix["candidate"]) is None):
            return "invalid"
        projected.append({key: prefix[key] for key in ("position", "pr", "branch", "candidate")})
    # A pre-build serial locator is not a train: the owner is authoritative even
    # when admission dropped the locator's own PR from the batch. Replacing the
    # locator leaves that dropped PR queued for ordinary serial recovery.
    train = {"pr": pr, "branch": observed["train_branch"], "base": observed["base_sha"],
             "started": now(), "members": kept, "mode": "full",
             "generation": generation, "phase": "owner_wait", "prefixes": projected}
    row["active"] = {"pr": kept[0]["pr"], "head": kept[0]["head"], "base": train["base"],
                     "run": _observed_run(observed, [None, None]),
                     "wait_since": now(), "started": False, "train": train}
    row["agent_streak"] = row.get("agent_streak", 0) + len(kept) - 1
    summaries[repository.name]["train"] = _train_journal(
        train, "adopted", candidate_tree=observed.get("candidate_tree"))
    return "adopted"


def _form_train(
    application: Any,
    repository: Repository,
    row: dict[str, Any],
    pr: int,
    inspected: dict[str, Any],
    numbers: list[int],
    priorities: dict[int, int],
    no_seat: set[int],
    summaries: dict[str, dict[str, Any]],
    refresh_budget: _RefreshBudget,
    now: Any,
    max_members: int = TRAIN_MAX_MEMBERS,
) -> str:
    """Try to replace the active PR's refresh, or its candidate mark, with a train build.

    Returns `built` or `cleanup` when the train now holds the active slot, `stop` on an
    authority or integrity refusal, and `serial` when the pass continues
    with the unchanged serial path. An uncertain build (the gate may have
    pushed) also returns `serial` but spends `refresh_budget`: the caller
    then leaves a candidate mark to the next pass. A member the gate dropped
    for a conflict is parked like a refresh conflict, except the active PR on
    a failed build: the serial refresh that follows parks it itself.
    `no_seat` names the PRs that never ride: Renovate's, and a `queue/low`
    PR the pass defers while the queue is deep (`_queue_priorities`).
    A `max_members` below `TRAIN_MAX_MEMBERS` is journaled as `member_cap`
    with its reason, whatever the outcome.
    """
    capped = ({"member_cap": max_members, "cap_reason": TRAIN_CAP_REASON}
              if max_members < TRAIN_MAX_MEMBERS else {})
    observed = {pr: inspected}
    members = [(pr, str(inspected["head_sha"]))]
    refused: list[dict[str, Any]] = []
    later = [number for number in numbers
             if number != pr and number not in no_seat and str(number) not in row["parked"]]
    # The readiness order is the v2 contract. v1 keeps its bounded
    # in-order election: inspect until the cap is filled, then stop, so a later
    # PR's refusal cannot stop an already complete v1 formation.
    ranked = repository.merge_batch_policy == "train-v2"
    eligible: list[tuple[int, int, int, int]] = []
    for position, candidate in enumerate(later[:TRAIN_SCAN_INSPECTIONS]):
        if not ranked and len(members) == max_members:
            break
        observed[candidate] = _inspect_candidate(application, repository, candidate)
        failure = None if observed[candidate].get("ok") else str(
            observed[candidate].get("code") or "INSPECT_FAILED")
        if failure in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
            # Before any train mutation. Any other failure only reads
            # `inspect_failed` below and keeps its own turn in the pass.
            _record_automerge_result({"pr": candidate, "outcome": "retained", "reason": failure},
                                     repository, summaries)
            return "stop"
        why = _train_ineligibility(repository, observed[candidate])
        if why is None:
            if ranked:
                eligible.append((priorities.get(candidate, PRIORITY_NORMAL),
                                 _readiness(observed[candidate]), position, candidate))
            else:
                members.append((candidate, str(observed[candidate]["head_sha"])))
        else:
            refused.append({"pr": candidate, "why": why})
    if ranked:
        for _priority, _readiness_tier, _position, candidate in sorted(eligible)[:max_members - 1]:
            members.append((candidate, str(observed[candidate]["head_sha"])))
    if len(members) < 2:
        # No later candidate at all (a lone PR, or only Renovate or parked
        # ones) reads differently from later PRs that were not eligible.
        summaries[repository.name]["train"] = {
            "pr": None, "members": [pr], "outcome": "not_formed",
            "reason": "TRAIN_TOO_SHORT" if refused else "TRAIN_NO_CANDIDATE",
            "inspected": refused, **capped}
        return "serial"
    built: dict[str, Any]
    try:
        built = application.train_build(repository.name, members, str(inspected["base_sha"]))
    except _DeployYield:
        # The budget's deployment checkpoint yields before the gate is called.
        raise
    except Exception as exc:  # uncertain dispatch: the gate may have pushed
        refresh_budget.spent = True
        built = {"ok": False, "code": _candidate_exception(pr, exc)["reason"]}
    code = str(built.get("code") or "TRAIN_BUILD_FAILED")
    detail: dict[str, Any] = built["detail"] if isinstance(built.get("detail"), dict) else {}
    dropped = built.get("dropped") if built.get("ok") else detail.get("dropped")
    for record in dropped if isinstance(dropped, list) else []:
        number = record.get("pr") if isinstance(record, dict) else None
        if type(number) is not int or number not in observed:
            continue
        _record_automerge_result({"pr": number, "outcome": "retained",
                                  "reason": str(record.get("reason")), "train_dropped": True},
                                 repository, summaries)
        if record.get("reason") == "PR_REFRESH_CONFLICT" and (built.get("ok") or number != pr):
            row["parked"][str(number)] = _park_record(observed[number], "conflict", now)
            numbers.remove(number)
    if not built.get("ok"):
        # The gate's refusal step and retired owner name the cause in the
        # journal; without them a parked build cannot be diagnosed.
        cause = {key: detail[key] for key in ("step", "train_pr")
                 if type(detail.get(key)) in (str, int)}
        summaries[repository.name]["train"] = {
            "pr": None, "members": [number for number, _head in members],
            "candidate_sha": None, "candidate_tree": None, "outcome": "build_refused",
            "reason": code, **cause, **capped}
        if code in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
            summaries[repository.name]["authority_integrity_failed"] = True
            return "stop"
        if (repository.merge_batch_policy == "train-v2"
                and str(detail.get("step") or "") in TRAIN_UNCERTAIN_PUBLICATION_STEPS):
            # The owner ref may already be pushed even though the response was
            # lost or the PR listing lagged. A serial mark or refresh would start
            # a second run beside it; the next pass adopts the owner instead.
            refresh_budget.spent = True
            summaries[repository.name]["train"]["uncertain_publication"] = True
            return "serial"
        if repository.merge_batch_policy == "train-v2" and detail.get("park") is True:
            # An unreadable or unprovable native owner is not permission to
            # start member CI.
            # Remove all selected members so selection cannot release their
            # parks again in this same pass on their unchanged green evidence.
            for number, _head in members:
                row["parked"][str(number)] = _park_record(observed[number], "evidence_unavailable", now)
                if number in numbers:
                    numbers.remove(number)
            refresh_budget.spent = True
            summaries[repository.name]["train"]["outcome"] = "parked"
            return "parked"
        if repository.merge_batch_policy == "train-v2" and detail.get("serial_only") is True:
            summaries[repository.name]["train"]["serial_return"] = True
        if (detail.get("cleanup_failed") and type(detail.get("train_pr")) is int
                and isinstance(detail.get("members"), list) and len(detail["members"]) >= 2):
            kept = detail["members"]
            train = {"pr": detail["train_pr"], "branch": detail["train_branch"],
                     "base": detail["base_sha"], "started": now(), "members": kept,
                     "cleanup": {"reason": "TRAIN_BUILD_FAILED", "outcome": "closed"}}
            row["active"] = {"pr": kept[0]["pr"], "head": kept[0]["head"], "base": train["base"],
                             "run": [None, None], "wait_since": now(), "started": False,
                             "train": train}
            refresh_budget.spent = True
            summaries[repository.name]["train"] = _train_journal(
                train, "build_refused", reason=code, close_failed=detail["cleanup_failed"],
                **cause, **capped)
            return "cleanup"
        return "serial"
    refresh_budget.spent = True
    kept = [{"pr": member["pr"], "head": member["head"], "candidate": member["candidate"]}
            for member in built["members"]]
    train = {"pr": built["train_pr"], "branch": built["train_branch"],
             "base": built["base_sha"], "started": now(), "members": kept}
    if built.get("mode") == "full":
        train.update(mode="full", generation=built["generation"], phase="owner_wait")
    if isinstance(built.get("prefixes"), list):
        train["prefixes"] = [
            {key: prefix[key] for key in ("position", "pr", "branch", "candidate")}
            for prefix in built["prefixes"]
        ]
    row["active"] = {"pr": kept[0]["pr"], "head": kept[0]["head"], "base": built["base_sha"],
                     "run": [None, None], "wait_since": now(), "started": False,
                     "train": train}
    # The riders are non-Renovate PRs that merge with the leader.
    row["agent_streak"] = row.get("agent_streak", 0) + len(kept) - 1
    summaries[repository.name]["train"] = _train_journal(
        train, "built", candidate_tree=built.get("candidate_tree"), **capped)
    return "built"


def _close_train(
    application: Any, repository: Repository, train: dict[str, Any], reason: str,
    summaries: dict[str, dict[str, Any]], outcome: str, candidate_tree: Any,
) -> bool:
    """Close the family; retain failed cleanup in the existing active slot."""
    if train.get("mode") == "full":
        train["phase"] = "cleanup"
    try:
        closed = application.train_close(repository.name, train["pr"], reason)
    except Exception as exc:
        closed = {"ok": False, "code": _candidate_exception(train["pr"], exc)["reason"]}
    journal = _train_journal(train, outcome, candidate_tree=candidate_tree, reason=reason)
    detail = closed.get("detail")
    if (train.get("mode") == "full" and not closed.get("ok")
            and isinstance(detail, dict) and detail.get("cleanup_started") is False):
        # The gate re-read proof and refused before any cleanup mutation.
        # A locator/old inspection must not become cleanup authority.
        train.pop("cleanup", None)
        train["phase"] = "owner_parked"
        summaries[repository.name]["train"] = _train_journal(
            train, "parked", candidate_tree=candidate_tree,
            reason=str(closed.get("code") or "TRAIN_BUILD_FAILED"))
        return False
    if not closed.get("ok") or closed.get("branch_deleted") is False:
        train["cleanup"] = {"reason": reason, "outcome": outcome}
        journal["close_failed"] = (
            "TRAIN_CLOSE_FAILED" if closed.get("ok")
            else str(closed.get("code") or "TRAIN_CLOSE_FAILED"))
        LOGGER.warning("automerge train not closed: %s#%s %s", repository.name, train["pr"],
                       journal["close_failed"])
    summaries[repository.name]["train"] = journal
    if "close_failed" in journal:
        code = journal["close_failed"]
        if code in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
            _record_automerge_result({"pr": train["pr"], "outcome": "retained", "reason": code},
                                     repository, summaries)
        return False
    train.pop("cleanup", None)
    return True


def _settled_train_member_merge(
    application: Any, repository: Repository, train: dict[str, Any], member: dict[str, Any],
    now: Any, settle_since: float | None,
) -> dict[str, Any]:
    """Poll only the gate's pre-merge mergeability refusal, for at most 60 s.

    The gate re-derives CI, refs and trees on every attempt. A transport
    exception, another refusal or an irreversible postcondition failure is
    never retried here. V1 keeps its single-attempt, later-pass behavior.
    """
    deadline = None if settle_since is None else time.monotonic() + min(
        MERGEABLE_SETTLE_SECONDS,
        max(0, MERGEABLE_SETTLE_SECONDS - (now() - settle_since)))
    while True:
        result = application.train_member_merge(
            repository.name, train["pr"], member["pr"], member["head"])
        detail = result.get("detail")
        if (deadline is None or result.get("ok") or result.get("code") != "PR_MERGE_FAILED"
                or not isinstance(detail, dict) or "mergeable" not in detail
                or detail["mergeable"] is True):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        time.sleep(min(2.0, remaining))


def _advance_train(
    application: Any,
    repository: Repository,
    row: dict[str, Any],
    numbers: list[int],
    queued: list[int],
    summaries: dict[str, dict[str, Any]],
    now: Any,
    save: Any,
) -> str:
    """Drive the persisted train one step: wait, merge members, or end it.

    Returns `waiting_ci` or `merging` while the train keeps the slot, `stop`
    on an integrity or authority refusal (the train stays persisted), and
    `released` once the train is closed and the slot is free for the serial
    path. A member that is no longer open was merged by an earlier pass or
    closed; the gate's own pre-merge tree check tells the two apart.
    """
    active = row["active"]
    train = active["train"]
    full = train.get("mode") == "full"
    observed: dict[str, Any] = {}

    def release(reason: str, outcome: str) -> str:
        closed = _close_train(application, repository, train, reason, summaries, outcome,
                              observed.get("candidate_tree"))
        if full:
            summaries[repository.name]["train"].update(
                selected_run=active["run"][0], selected_attempt=active["run"][1])
        if reason == "TRAIN_OWNER_RED":
            summaries[repository.name]["train"]["serial_return"] = closed
            if observed:
                # the red owner's failed job names, for attribution
                # only (a list, or None when unknown); no decision reads them.
                # A retained-cleanup retry reads no owner and records none:
                # the pass that observed the red owner journaled them.
                summaries[repository.name]["train"]["failed_jobs"] = observed.get(
                    "owner_failed_jobs")
        if closed:
            row["active"] = None
        save()
        if summaries[repository.name]["authority_integrity_failed"]:
            return "stop"
        return "released" if closed else "merging"

    def park(reason: str, *, waiting: bool = False) -> str:
        _end_lost_run(active, reason, now)
        train["phase"] = "owner_wait" if waiting else "owner_parked"
        summaries[repository.name]["train"] = _train_journal(
            train, "waiting" if waiting else "parked", reason=reason,
            candidate_tree=observed.get("candidate_tree"),
            selected_run=active["run"][0], selected_attempt=active["run"][1])
        save()
        return "waiting_ci"

    cleanup = train.get("cleanup")
    if cleanup is not None:
        # Closing the final PR can succeed while deleting a prefix ref fails.
        # Retry the retained cleanup before inspecting or merging any member.
        return release(cleanup["reason"], cleanup["outcome"])
    try:
        observed = application.train_inspect(repository.name, train["pr"])
    except Exception as exc:
        observed = {"ok": False, "code": _candidate_exception(train["pr"], exc)["reason"]}

    if not observed.get("ok"):
        code = str(observed.get("code") or "INSPECT_FAILED")
        if full:
            _end_lost_run(active, None, now)  # This covers the stop below too, which does not park.
        if code in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
            _record_automerge_result({"pr": active["pr"], "outcome": "retained", "reason": code,
                                      "train_pr": train["pr"]}, repository, summaries)
            return "stop"
        if full:
            return park(code)
        if code == "TRAIN_INVALID":
            return release(code, "closed")
        if now() - active["wait_since"] >= CI_WAIT_SECONDS:
            return release("INSPECT_STALLED", "closed")
        summaries[repository.name]["train"] = _train_journal(train, "waiting", reason=code)
        return "waiting_ci"
    if observed.get("pr_state") != "open" and not full:
        return release("TRAIN_PR_CLOSED", "closed")
    if full and (observed.get("mode") != "full"
                 or observed.get("generation") != train["generation"]):
        return park("TRAIN_INVALID")
    if (observed.get("head_sha") != train["members"][-1]["candidate"]
            or observed.get("base_sha") != train["base"]
            or [(member.get("pr"), member.get("head")) for member in observed.get("members", [])]
            != [(member["pr"], member["head"]) for member in train["members"]]):
        return park("TRAIN_INVALID") if full else release("TRAIN_INVALID", "closed")
    prefixes = observed.get("prefixes")
    if (not isinstance(prefixes, list) or not isinstance(train.get("prefixes"), list)
            or len(prefixes) != len(train["members"])
            or [{key: prefix.get(key) for key in ("position", "pr", "branch", "candidate")}
                for prefix in prefixes if isinstance(prefix, dict)] != train["prefixes"]):
        # A final-only legacy train has no proof for intermediate main trees.
        return park("TRAIN_INVALID") if full else release("TRAIN_INVALID", "closed")
    if observed.get("main_on_chain") is False:
        return release("TRAIN_BASE_MOVED", "closed")

    if full:
        # The journal's run is only a locator. Read the one owner's current
        # verdict; never consult advisory classification or a prefix result.
        active.update(run=_observed_run(observed, active["run"]),
                      started=active["started"] or _ci_started(observed))
        _note_progress(active, observed, now)
        if observed.get("pr_state") != "open":
            if observed.get("pr_state") == "closed" and observed.get("main_is_final") is True:
                return release("TRAIN_MEMBERS_MERGED", "merged")
            if not (observed.get("ci_state") == "red" and observed.get("ci_terminal_red") is True):
                return park("TRAIN_CLOSED_PROOF_UNAVAILABLE")
        if observed.get("ci_state") != "green":
            if observed.get("ci_state") == "red" and observed.get("ci_terminal_red") is True:
                if observed.get("base_failure_match") is True:
                    return park("TRAIN_BASE_RED")
                if observed.get("base_failure_match") is not False:
                    return park("TRAIN_BASE_PROOF_UNAVAILABLE")
                # The gate can refuse this close before it writes and park the
                # family in `_close_train`, not in `park`; that pass read no lost
                # code either.
                _end_lost_run(active, None, now)
                return release("TRAIN_OWNER_RED", "closed")
            reason = str(observed.get("blocking_reason") or "PR_CI_NOT_GREEN")
            if reason in _TRAIN_OWNER_LOST:
                # the park below would hold the one slot for good. The
                # loss is dated from its first reading, kept in `progress` (a
                # missing run is already dated there by `_note_progress`, from
                # the wait start when it is the owner's first reading; a run
                # back in flight replaces it). Any other pass that does not park
                # on this code ends the interval (`_end_lost_run`), the owner's
                # first unreadable, failed or green pass included, so the window
                # counts unbroken readings. Every lost reading is dated, so a
                # family that cannot close now leaves no unread owner for a later
                # missing run to backdate. A member that merged, or left the
                # listing with its merge response lost, makes the family
                # partial: it keeps the park.
                at = now()
                lost = active.get("progress")
                if lost is None or lost[0] != reason:
                    lost = active["progress"] = [reason, at]
                if (not train.get("merged")
                        and all(member["pr"] in numbers for member in train["members"])
                        and at - lost[1] >= CI_START_WAIT_SECONDS):
                    LOGGER.warning("automerge train owner lost its CI run: %s#%s %s for %.0f s",
                                   repository.name, train["pr"], reason, at - lost[1])
                    return release(f"TRAIN_OWNER_CI_LOST: {reason}", "closed")
            deadline = (CI_START_WAIT_SECONDS
                        if not active["started"] and reason in _CI_NOT_STARTED else CI_WAIT_SECONDS)
            waiting = (observed.get("ci_state") == "pending"
                       and reason in _CI_NOT_STARTED | {"CI_RUN_IN_PROGRESS", "CI_PROFILE_PENDING"}
                       and now() - train["started"] < deadline)
            return park(reason, waiting=waiting)
        if train["phase"] != "merging":
            train["phase"] = "merging"
            active["wait_since"] = now()
        _end_lost_run(active, None, now)  # A green owner ends an earlier lost reading too.
        save()

    for member, prefix in zip(train["members"], prefixes):
        if member["pr"] not in numbers:
            continue
        held = row["parked"].get(str(member["pr"]))
        if held is not None and held["reason"] in LABEL_HOLD_REASONS:
            # An author's hold reached a member after the train was built: it
            # merges nothing from here on. The members ahead of it have merged,
            # and the tail below closes the train and leaves the rest to the
            # serial queue.
            break
        if not full:
            run = _observed_run(prefix, active["run"])
            if active["run"] != run:
                _restart_ci_wait(active, now)
            active.update(run=run, started=active["started"] or _ci_started(prefix))
            _note_progress(active, prefix, now)
            save()
            if prefix.get("ci_state") != "green":
                if (prefix.get("ci_state") == "red"
                        or prefix.get("ci_run_conclusion") in TERMINAL_FAILURE_CONCLUSIONS
                        or prefix.get("blocking_reason") in {"CI_BASE_STALE", "PR_BASE_STALE", "TRAIN_INVALID"}):
                    return release(str(prefix.get("blocking_reason") or "CI_RUN_FAILED"), "closed")
                deadline = (CI_START_WAIT_SECONDS
                            if not active["started"] and prefix.get("blocking_reason") in _CI_NOT_STARTED
                            else CI_WAIT_SECONDS)
                if now() - _stall_since(active) >= deadline:
                    return release("CI_STALLED", "closed")
                summaries[repository.name]["train"] = _train_journal(
                    train, "waiting", candidate_tree=observed.get("candidate_tree"),
                    prefix_position=prefix["position"], prefix_pr=prefix["pr"],
                    reason=str(prefix.get("blocking_reason") or "PR_CI_NOT_GREEN"))
                return "waiting_ci"
        merged: dict[str, Any]
        try:
            merged = _settled_train_member_merge(
                application, repository, train, member, now,
                active["wait_since"] if full else None)
        except Exception as exc:  # uncertain dispatch: the next pass re-derives
            result = {**_candidate_exception(member["pr"], exc), "train_pr": train["pr"]}
            stop = _record_automerge_result(result, repository, summaries)
            journal = _train_journal(
                train, "integrity_failed" if stop else "merging",
                candidate_tree=observed.get("candidate_tree"))
            if stop:
                journal["reason"] = result["reason"]
            summaries[repository.name]["train"] = journal
            return "stop" if stop else "merging"
        if merged.get("ok"):
            _record_automerge_result({"pr": member["pr"], "outcome": "merged",
                                      "ci_basis": "train",
                                      "approval_basis": merged.get("approval_basis"),
                                      "train_pr": train["pr"]}, repository, summaries)
            numbers.remove(member["pr"])
            if member["pr"] in queued:
                queued.remove(member["pr"])
            train["merged"] = [*train.get("merged", []), member["pr"]]
            active["wait_since"] = now()
            save()
            continue
        code = str(merged.get("code") or "MERGE_FAILED")
        raw_detail = merged.get("detail")
        detail = raw_detail if isinstance(raw_detail, dict) else {}
        if _record_automerge_result({"pr": member["pr"], "outcome": "retained", "reason": code,
                                     "train_pr": train["pr"]}, repository, summaries):
            summaries[repository.name]["train"] = _train_journal(
                train, "integrity_failed", candidate_tree=observed.get("candidate_tree"),
                reason=code)
            return "stop"
        if full and (code.startswith("CI_") or code == "PR_CI_NOT_GREEN"):
            # Proof may have changed between native merges. `train_member_merge`
            # reports a lost or non-green owner verdict as `PR_CI_NOT_GREEN`,
            # which is evidence about the read, not a strict red verdict. Re-read
            # the owner on the next pass; never close the family on it.
            return park(code)
        # Forgejo recomputes a member's mergeability after the previous one
        # lands and reports `false` while it checks. The gate names
        # `mergeable` only after main proved to carry the preceding
        # candidate's tree, so the forge's merge has the inputs of a link
        # that already merged cleanly: a real conflict is impossible, and
        # the answer arrives within a later pass.
        if (code == "PR_MERGE_FAILED" and "mergeable" in detail
                and detail["mergeable"] is not True
                and now() - active["wait_since"] < (
                    MERGEABLE_SETTLE_SECONDS if full else CI_WAIT_SECONDS)):
            summaries[repository.name]["train"] = _train_journal(
                train, "merging", candidate_tree=observed.get("candidate_tree"))
            return "merging"
        return release(code, "closed")
    # A member that left the queue before its turn was merged or closed by
    # someone else; the journal names only what this executor merged.
    # `train_close` admits no `;`, so ` - ` separates the two lists.
    mine = train.get("merged", [])
    merged_text = ", ".join(f"#{number}" for number in mine)
    left_text = ", ".join(f"#{member['pr']}" for member in train["members"]
                          if member["pr"] not in mine)
    if not mine:
        return release(f"TRAIN_MEMBERS_LEFT: left {left_text}", "closed")
    return release(f"merged {merged_text}" + (f" - left {left_text}" if left_text else ""),
                   "merged")


#: How a running deploy marker pauses automerge, for at most
#: `DEPLOY_MARKER_TTL_SECONDS`.
#: The deploy step claims the marker before its first mutation
#: and releases it after COMPLETE. The pause exists only while a deploy may be
#: swapping this executor's own code, so it covers every repository, and it
#: ends earlier than COMPLETE: once the deploy has applied source and
#: reconciled the Python environments (the executor's own venv)
#: it writes `applied_at` into the still-running marker, and a marker carrying
#: it no longer pauses, because the steps that follow leave the executor's
#: code alone. A resumed deploy re-claims the marker
#: without `applied_at`, so the pause returns until it passes that point again.
#: A run that stops without COMPLETE keeps the marker -- it still
#: refuses every other deploy -- but rewrites its `state` to
#: `failed`; such a marker never pauses automerge, and the pass logs one
#: warning naming it instead. A running marker, a legacy one without `state`,
#: or one this reader cannot parse pauses while it is fresh. Its age runs from
#: `claimed_at` when that parses, else from the file's mtime, so a fresh but
#: unreadable marker still pauses. `claimed_at` is the claim that made the
#: marker running: re-claiming a still-running marker of the same deploy
#: plan keeps it, so a periodic resume of a plan
#: left in a running or uncertain state does not renew the pause. An older marker is
#: presumed abandoned (a crash that could not mark it failed): the pass runs,
#: with a warning.


def _marker_claim_time(value: Any) -> float | None:
    """The POSIX time of an ISO `claimed_at` with an offset; None otherwise."""
    if not isinstance(value, str):
        return None
    try:
        claimed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return claimed.timestamp() if claimed.tzinfo is not None else None


def _deploy_in_progress() -> dict[str, Any] | None:
    """The skip record fields while a running deploy marker is fresh here.

    Read-only: the automerge service has read-only access to the marker's
    directory and never writes or removes the marker. None when there is no
    marker, when it cannot be
    examined, when it records a failed deploy, when it records that source is
    applied (an offset-aware ISO `applied_at`), or when it is older than DEPLOY_MARKER_TTL_SECONDS.
    """
    try:
        mtime = DEPLOY_MARKER_PATH.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as exc:
        LOGGER.warning("automerge: cannot examine deploy marker %s: %s",
                       DEPLOY_MARKER_PATH, exc)
        return None
    try:
        marker = json.loads(DEPLOY_MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = None
    if not isinstance(marker, dict):
        marker = {}
    # Only the writer's own schema may lift the pause or move its clock.
    current = marker.get("schema") == "deploy-marker.v1"
    if current and marker.get("state") == "failed":
        LOGGER.warning("automerge: deploy marker %s records a failed deploy (origin %s, "
                       "plan %s, failed at %s); automerge continues; an operator must "
                       "finish or remove it", DEPLOY_MARKER_PATH, marker.get("origin"),
                       marker.get("plan_id"), marker.get("failed_at"))
        return None
    applied_at = marker.get("applied_at") if current and marker.get("state") == "running" else None
    if _marker_claim_time(applied_at) is not None:
        LOGGER.info("automerge: deploy marker %s records applied source (origin %s, plan %s, "
                    "applied at %s); later deploy steps still running; automerge continues",
                    DEPLOY_MARKER_PATH, marker.get("origin"), marker.get("plan_id"), applied_at)
        return None
    claimed = _marker_claim_time(marker.get("claimed_at")) if current else None
    age = time.time() - (mtime if claimed is None else claimed)
    if age >= DEPLOY_MARKER_TTL_SECONDS:
        LOGGER.warning("automerge: ignoring stale deploy marker %s (%d minutes old); "
                       "remove it once no deploy is running", DEPLOY_MARKER_PATH, age // 60)
        return None
    fields: dict[str, Any] = {"reason": "DEPLOY_IN_PROGRESS"}
    held = {key: marker[key] for key in ("origin", "plan_id", "started_at",
                                         "claimed_at", "state")
            if isinstance(marker.get(key), str)}
    if held:
        fields["deploy"] = held
    return fields


def _deploy_source_applied() -> bool:
    """True when the outer deploy lock belongs to a run past its release point.

    Only a current-schema `running` marker whose `applied_at` and `claimed_at`
    are offset-aware ISO timestamps, claimed less than
    DEPLOY_MARKER_TTL_SECONDS ago, qualifies. Anything else, an absent or
    failed marker included, may mean a new deploy holds the outer lock and
    waits for the executor lock, so the pass must still yield to it.
    """
    try:
        marker = json.loads(DEPLOY_MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not (isinstance(marker, dict) and marker.get("schema") == "deploy-marker.v1"
            and marker.get("state") == "running"
            and _marker_claim_time(marker.get("applied_at")) is not None):
        return False
    claimed = _marker_claim_time(marker.get("claimed_at"))
    return claimed is not None and time.time() - claimed < DEPLOY_MARKER_TTL_SECONDS


# A pass starts one timer interval (`OnUnitInactiveSec` in
# merge-guard-automerge.timer) after the previous one ended, and `last_pass`
# records that end, a deploy yield included. A pass that starts longer than
# this after it means the executor was not running (a stall, a deploy hold,
# a crash), and that time is nobody's waiting.
PASS_GAP_SECONDS = 300


def _rebase_wait_clocks(state: dict[str, Any], gap: float, ceiling: float) -> int:
    """Shift every persisted wait timestamp forward by `gap`; return how many.

    Each is a wall-clock stamp that a deadline or a `*_wait_s` metric reads
    as `now() - stamp`, so downtime would otherwise count as waiting. No stamp
    moves past `ceiling` (the pass's now).
    """
    moved = 0

    def later(value: float) -> float:
        return min(value + gap, max(value, ceiling))

    for row in state["repositories"].values():
        active = row["active"]
        if active is not None:
            active["wait_since"] = later(active["wait_since"])
            moved += 1
            if "ci_missing" in active:
                active["ci_missing"][1] = later(active["ci_missing"][1])
                moved += 1
            if "unmergeable" in active:
                active["unmergeable"][2] = later(active["unmergeable"][2])
                moved += 1
            # the stall budget and a train's own start are read the
            # same way (`_stall_since`, the owner-wait deadline).
            if "progress" in active:
                active["progress"][1] = later(active["progress"][1])
                moved += 1
            if "train" in active:
                active["train"]["started"] = later(active["train"]["started"])
                moved += 1
        for parked in row["parked"].values():
            if "since" in parked:
                parked["since"] = later(parked["since"])
                moved += 1
    return moved


class _DeployYield(QueueStateError):
    """Leave a pass through its normal lock cleanup, preserving queue state."""

    def __init__(self, reason: str, detail: str, summaries: list[dict[str, Any]]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.summaries = summaries


class _TrainStop(QueueStateError):
    """A train advanced around a review, or a lookahead inspection or review
    beside it, hit an integrity or authority stop.

    Raised out of `_ReviewBudget.review` so the candidate being reviewed
    never merges after the stop. A stop met by the pre-review check also
    keeps its review from dispatching. One met while the review runs, or by
    the post-review check, comes after the paid review dispatched: the
    review still ends and spends its slot, and the pass discards its
    verdict. The failing result already carries `authority_integrity_failed`.
    """


def run_automerge_pass(
    repositories: list[Repository],
    application: Any | None = None,
    *,
    max_reviews: int | None = None,
    max_reviews_per_repo: int | None = None,
    executor_run_prefix: str = DEFAULT_EXECUTOR_RUN_PREFIX,
    queue_store: QueueStore | None = None,
    now: Any = time.time,
    pass_deadline_seconds: int | None = None,
    review_workers: int = 1,
) -> list[dict[str, Any]]:
    queue_store = queue_store or QueueStore(repositories=[repo.name for repo in repositories])
    try:
        return _run_automerge_pass(
            repositories, application, max_reviews=max_reviews,
            max_reviews_per_repo=max_reviews_per_repo, executor_run_prefix=executor_run_prefix,
            queue_store=queue_store, now=now, pass_deadline_seconds=pass_deadline_seconds,
            review_workers=review_workers,
        )
    except _DeployYield as hold:
        # QueueStore.locked has unwound. A recursive hold publication would
        # contend with this pass's own executor lock and never update statuses.
        if hold.reason == "DEPLOY_WAITING":
            _publish_hold(repositories, {"reason": hold.reason}, application, queue_store=queue_store)
        for summary in hold.summaries:
            summary.update(ok=hold.reason == "DEPLOY_WAITING", skipped=True,
                           executor_run=executor_run_prefix,
                           hold={"reason": hold.reason, "detail": hold.detail})
            if hold.reason != "DEPLOY_WAITING":
                summary["error"] = hold.reason
        return hold.summaries


def _run_automerge_pass(
    repositories: list[Repository],
    application: Any | None = None,
    *,
    max_reviews: int | None = None,
    max_reviews_per_repo: int | None = None,
    executor_run_prefix: str = DEFAULT_EXECUTOR_RUN_PREFIX,
    queue_store: QueueStore | None = None,
    now: Any = time.time,
    pass_deadline_seconds: int | None = None,
    review_workers: int = 1,
) -> list[dict[str, Any]]:
    """Advance one active PR per gate repository; persist before any mutation."""
    deadline = None if pass_deadline_seconds is None else now() + pass_deadline_seconds
    summaries: dict[str, dict[str, Any]] = {
        repo.name: {"repository": repo.name, "results": [],
                    "authority_integrity_failed": False}
        for repo in repositories
    }
    queue_store = queue_store or QueueStore(
        repositories=[repository.name for repository in repositories]
    )
    with queue_store.locked() as state:
        def save() -> None:
            queue_store.save(state)

        entered = False

        def checkpoint() -> None:
            deploy = _deploy_in_progress()
            status, detail = probe_deploy_lock()
            if deploy is not None:
                # A remote deploy announces through its marker.
                # It never holds the outer deployment lock.
                status, detail = "held", "deployment intent is active on the executor host"
            elif status == "held" and _deploy_source_applied():
                # The holder has applied source and released the executor
                # lock; its effects leave the executor's code alone.
                return
            if status in {"held", "unavailable"}:
                # The pass worked until now, so only the time after it is a
                # gap. A yield before the entry re-base keeps the old stamp:
                # stamping it would erase a gap that was never re-based.
                if entered:
                    state["last_pass"] = time.time()
                save()
                reason = "DEPLOY_WAITING" if status == "held" else "DEPLOY_LOCK_UNAVAILABLE"
                raise _DeployYield(reason, detail, [summaries[repo.name] for repo in repositories])

        application = application or _gate_application()
        budget = _ReviewBudget(application, max_reviews, max_reviews_per_repo,
                               deadline=deadline, now=now, workers=review_workers)
        budget.checkpoint = checkpoint
        application = budget
        checkpoint()  # The executor lock is already held; deploy can wait safely.
        try:
            followups = application.retry_review_followups()
            if not followups.get("ok"):
                LOGGER.warning("review follow-up retry pending: %s", followups.get("code"))
        except (OSError, ValueError, RuntimeError, GateError, subprocess.SubprocessError) as exc:
            LOGGER.warning("review follow-up retry pending: %s", type(exc).__name__)

        # The gap is measured on the host clock, not on `now`: `now` is the
        # pass's own clock for deadlines, and the two are one clock in
        # production. A pass stamps `last_pass` on entry as well as on exit, so
        # a crash after the shift cannot shift the same gap twice.
        host_now = time.time()
        last_pass = state.get("last_pass")
        if last_pass is not None and host_now - last_pass > PASS_GAP_SECONDS:
            gap = host_now - last_pass
            moved = _rebase_wait_clocks(state, gap, now())
            LOGGER.info("automerge: pass gap %d s; re-based %d wait timestamps",
                        round(gap), moved)
        state["last_pass"] = host_now
        entered = True

        if repositories:
            rotation = state["cursor"] % len(repositories)
            ordered = repositories[rotation:] + repositories[:rotation]
            state["cursor"] = (rotation + 1) % len(repositories)
        else:
            ordered = []

        listed_by_repo: dict[str, list[int]] = {}
        trains_by_repo: dict[str, set[int]] = {}
        # Renovate PRs queue behind every other PR (see `_renovate_numbers`),
        # and a held one waits for the operator (`_renovate_holds`).
        renovate_by_repo: dict[str, set[int]] = {}
        renovate_branches_by_repo: dict[str, set[int]] = {}
        holds_by_repo: dict[str, dict[int, str]] = {}
        # each PR's priority rank (`_queue_priorities`), and the
        # `queue/low` PRs that take no review ahead, lookahead review or train
        # seat while the queue is deeper than `QUEUE_BACKPRESSURE_THRESHOLD`.
        priorities_by_repo: dict[str, dict[int, int]] = {}
        withheld_by_repo: dict[str, set[int]] = {}
        for repository in ordered:
            try:
                listed = application.candidates(repository.name)
            except Exception as exc:
                if isinstance(exc, QueueStateError):
                    raise
                code = exc.code if isinstance(exc, GateError) else None
                summaries[repository.name]["error"] = code or f"PR_LIST_ERROR:{type(exc).__name__}"
                if code in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
                    summaries[repository.name]["authority_integrity_failed"] = True
                continue
            if listed.get("ok"):
                numbers = sorted(set(listed.get("pull_requests", [])))
                # A merge train's own PR is the gate's vehicle, never a candidate.
                trains = _train_numbers(listed, numbers)
                trains_by_repo[repository.name] = trains
                numbers = [number for number in numbers if number not in trains]
                renovate = renovate_by_repo[repository.name] = _renovate_numbers(listed, numbers)
                renovate_branches_by_repo[repository.name] = _renovate_branches(listed, numbers)
                holds = holds_by_repo[repository.name] = _renovate_holds(listed, renovate)
                holds.update(dict.fromkeys(
                    _renovate_labels_unknown(listed, renovate) - holds.keys(),
                    RENOVATE_LABELS_UNKNOWN))
                holds.update(_author_holds(listed, numbers))
                summary = summaries[repository.name]
                priorities, unverified, untrusted = _queue_priorities(listed, numbers)
                priorities_by_repo[repository.name] = priorities
                if unverified:
                    summary["priority_unverified"] = unverified
                if untrusted:
                    summary["priority_untrusted"] = untrusted
                deep = sum(number not in renovate for number in numbers) > QUEUE_BACKPRESSURE_THRESHOLD
                withheld_by_repo[repository.name] = {
                    number for number in numbers if deep and priorities[number] == PRIORITY_LOW}
                listed_by_repo[repository.name] = sorted(
                    [number for number in numbers if number not in renovate]
                    + [number for number in numbers if number in renovate],
                    key=priorities.__getitem__)
            else:
                code = str(listed.get("code") or "PR_LIST_FAILED")
                summaries[repository.name]["error"] = code
                if code in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
                    summaries[repository.name]["authority_integrity_failed"] = True
        if listed_by_repo:
            save()  # Do not initialize scheduling state before live enumeration.

        stop_all = any(summary["authority_integrity_failed"] for summary in summaries.values())
        if not stop_all:
            # An abandoned open family PR is discovered without a tracker.
            # A saved train retains its slot until every cleanup step succeeds.
            for repository in ordered:
                if repository.merge_batch_policy not in TRAIN_POLICIES:
                    continue
                row = _queue_row(state, repository.name)
                active = row["active"]
                protected = {active["pr"]} if active is not None else set()
                if active is not None and "train" in active:
                    protected.add(active["train"]["pr"])
                    protected.update(member["pr"] for member in active["train"]["members"])
                    protected.update(prefix["pr"] for prefix in active["train"].get("prefixes", []))
                    if "cleanup" in active["train"]:
                        # A partial build may not have every prefix PR id.
                        # Finish retained cleanup before another orphan close.
                        protected.update(trains_by_repo.get(repository.name, set()))
                orphaned = trains_by_repo.get(repository.name, set()) - protected
                attempted = False
                for pr in sorted(orphaned):
                    verdict = _adopt_orphan_v2_owner(
                        application, repository, pr, row, summaries, now)
                    if verdict == "adopted":
                        # The owner is a recoverable v2 generation, not waste:
                        # the main loop below drives it from this slot.
                        save()
                        break
                    if verdict == "occupied":
                        continue  # One slot, one train: leave it for a later pass.
                    if verdict == "unreadable":
                        # Unreadable proof is not waste, and an owner that does
                        # not match the pre-build serial locator cannot be
                        # judged this pass. Defer the whole repository, so the
                        # legacy close cannot retire the owner and no serial
                        # run starts beside it.
                        summaries[repository.name]["train_cleanup"] = {
                            "pr": pr, "outcome": "deferred",
                            "reason": "TRAIN_OWNER_INSPECT_UNAVAILABLE"}
                        listed_by_repo.pop(repository.name, None)
                        break
                    try:
                        closed = application.train_close(repository.name, pr, "TRAIN_ORPHANED")
                    except Exception as exc:
                        closed = {"ok": False, "code": _candidate_exception(pr, exc)["reason"]}
                    journal: dict[str, Any] = {"pr": pr, "outcome": "closed"}
                    if not closed.get("ok"):
                        code = str(closed.get("code") or "TRAIN_CLOSE_FAILED")
                        journal.update(outcome="close_failed", reason=code)
                        LOGGER.warning("automerge orphan train not closed: %s#%s %s",
                                       repository.name, pr, code)
                        if code in AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES:
                            stop_all = _record_automerge_result(
                                {"pr": pr, "outcome": "retained", "reason": code}, repository, summaries)
                    summaries[repository.name]["train_cleanup"] = journal
                    # The gate refuses a malformed train (a head it did not
                    # build, or a base other than main) before any write, and
                    # on every pass; it must not starve later ones.
                    if journal.get("reason") != "TRAIN_INVALID":
                        attempted = True
                        break
                if attempted:
                    break  # One close attempt across the whole rotated pass.
        # Open PRs still in each visited queue (merged, closed and draft PRs
        # leave it), and why each active PR is still active.
        queued_by_repo: dict[str, list[int]] = {}
        active_reasons: dict[str, str] = {}
        eligible_reasons: dict[str, dict[int, str]] = {}

        def before_review(name: str, pr: int, head: str) -> None:
            repository = next(repo for repo in ordered if repo.name == name)
            row = _queue_row(state, name)
            active = row["active"]
            reasons = eligible_reasons.setdefault(name, {})
            if active is not None and active["pr"] == pr:
                active_reasons[name] = "reviewing"
            else:
                reasons[pr] = "reviewing"
            descriptions = _queue_descriptions(row, queued_by_repo[name], active_reasons.get(name, "-"), reasons)
            descriptions[pr] = (descriptions[pr][0], head)
            _publish_queue(application, repository, row, descriptions)
            save()

        budget.before_review = before_review
        # Prepare every successfully enumerated row before paid reviews can
        # block the pass. An unread listing retains its hold and queue state.
        for repository in ordered:
            if stop_all:
                break
            checkpoint()
            if repository.name not in listed_by_repo:
                continue
            numbers = listed_by_repo[repository.name]
            row = _queue_row(state, repository.name)
            queued = queued_by_repo[repository.name] = _slot_order(
                numbers, renovate_by_repo[repository.name], row,
                priorities_by_repo[repository.name])
            # A finished PR keeps its published record until `_publish_queue`
            # attempts its done write: an integrity stop later in this pass
            # skips every write, and the record is what retries it.
            _note_renovate_bypass(application, repository, row, numbers)
            for key in list(row["parked"]):
                if int(key) not in numbers:
                    del row["parked"][key]
            for key, parked in row["parked"].items():
                parked.setdefault("since", now())  # A park from before `since`.
                if (parked["reason"] == "ci_failed"
                        and int(key) in renovate_by_repo[repository.name]):
                    # The existing park records a confirmed CI failure; the
                    # live listing establishes its bot ownership. Change only
                    # the reason so the age and failure evidence survive.
                    parked["reason"] = "renovate_ci_failed"
                    LOGGER.info("automerge park reclassified: %s#%s ci_failed -> renovate_ci_failed",
                                repository.name, key)
            active = row["active"]
            if active is not None and active["pr"] not in numbers and "train" not in active:
                # A crash after merge or an externally closed/drafted PR. A
                # train keeps its slot: its first member may have merged.
                row["active"] = None
                active = None
            stop_all = _park_renovate_holds(
                application, repository, row, numbers, queued,
                holds_by_repo[repository.name], summaries, now,
                renovate_by_repo[repository.name])
            if stop_all:
                break
            save()
        if not stop_all:
            for repository in ordered:
                if repository.name not in queued_by_repo:
                    continue
                row = _queue_row(state, repository.name)
                if ("held" not in row and not any(
                        pair[1] == QUEUE_HELD for pair in row.get("published", {}).values())):
                    continue
                checkpoint()
                # A partial release invalidates the hold's dedup marker.
                # Failed held pairs retain the retry evidence in `published`.
                row.pop("held", None)
                save()  # A crash during publication must not restore the old marker.
                checkpoint()
                _publish_queue(application, repository, row,
                               _queue_descriptions(row, queued_by_repo[repository.name], "-"))
                save()

        # A train closed in this pass is not formed again in it: its members
        # take the serial path first.
        trains_closed: set[str] = set()

        def advance_trains(reviewing: str | None = None,
                           lookahead: frozenset[str] = frozenset()) -> None:
            """Advance every active train, as its own turn would, around a review.

            Merge-only: `_advance_train` never builds, refreshes or reviews,
            and a member still merges only once its own prefix is green. A
            released slot waits for the repository's turn or the next pass;
            a retained cleanup retries on the train's own turn only.

            `reviewing` names the repository whose review is still running,
            and `lookahead` those of the lookahead reviews still running
           . Their trains are skipped: a member merge would move
            the main a review is bound to, and the gate's fetches would share
            a checkout with the review's own.
            """
            nonlocal stop_all
            if any(summary["authority_integrity_failed"] for summary in summaries.values()):
                return
            for repository in ordered:
                if (repository.name not in queued_by_repo or repository.name == reviewing
                        or repository.name in lookahead):
                    continue
                row = _queue_row(state, repository.name)
                active = row["active"]
                if active is None or "train" not in active or "cleanup" in active["train"]:
                    continue
                advanced = _advance_train(
                    application, repository, row, listed_by_repo[repository.name],
                    queued_by_repo[repository.name], summaries, now, save)
                if advanced == "stop":
                    stop_all = True
                    raise _TrainStop(repository.name)
                if advanced == "released":
                    trains_closed.add(repository.name)
                    active_reasons.pop(repository.name, None)
                else:
                    active_reasons[repository.name] = advanced

        budget.advance_trains = advance_trains

        # the PRs a lookahead inspected in this pass, per repository,
        # and the inspection each chosen one is reviewed on, until it ends.
        looked_ahead: dict[str, set[int]] = {}
        lookahead_inspected: dict[tuple[str, int], dict[str, Any]] = {}

        def lookahead_candidate(
            reviewing: str, busy: set[tuple[str, int]],
        ) -> tuple[str, int, str, str, str] | None:
            """The next queued PR to review while `reviewing`'s review runs, or None.

            The repository under review first, then the pass order. A
            candidate is a listed PR that is not active, riding a train,
            Renovate, held, parked or already under review, and whose
            inspection makes it train-eligible except for its approval
            (`_train_ineligibility`; a narrowed run the gate accepts for
            review counts as eligible here) over a pair the review
            owner accepts (`_review_ahead_base`). Each repository inspects at most
            `TRAIN_SCAN_INSPECTIONS` PRs per pass, the window `_form_train`
            reads, and never one PR twice. It shares `_review_ahead`'s
            `review_ahead` cursor and starts after the last PR either scan
            inspected, so a permanently blocked queue head cannot hold the
            window on the same ineligible PRs every pass.

            A failed inspection is recorded as `_review_ahead` records one.
            An authority or integrity code stops the pass (`_TrainStop`);
            any other skips the PR, as `_form_train` and `_review_ahead` do.
            """
            nonlocal stop_all
            if any(summary["authority_integrity_failed"] for summary in summaries.values()):
                return None
            for repository in sorted(ordered, key=lambda repo: repo.name != reviewing):
                name = repository.name
                if name not in queued_by_repo or budget.exhausted(name):
                    continue
                checkpoint()  # A waiting deploy is not kept for more inspections.
                row = _queue_row(state, name)
                active = row["active"]
                skipped: set[int] = set()
                if active is not None:
                    skipped.add(active["pr"])
                    if "train" in active:
                        skipped.update(member["pr"] for member in active["train"]["members"])
                seen = looked_ahead.setdefault(name, set())
                for pr in _rotating_order(listed_by_repo[name], row.get("review_ahead", 0),
                                          priorities_by_repo[name]):
                    if len(seen) >= TRAIN_SCAN_INSPECTIONS:
                        break
                    if (pr in seen or pr in skipped or (name, pr) in busy
                            or pr in renovate_by_repo[name] or pr in holds_by_repo[name]
                            or pr in withheld_by_repo[name] or str(pr) in row["parked"]):
                        continue
                    seen.add(pr)
                    row["review_ahead"] = pr
                    inspected = _inspect_candidate(budget, repository, pr)
                    if not inspected.get("ok"):
                        if _record_automerge_result(
                                {"pr": pr, "outcome": "retained",
                                 "reason": str(inspected.get("code") or "INSPECT_FAILED"),
                                 "review_ahead": True}, repository, summaries):
                            stop_all = True
                            raise _TrainStop(name)
                        continue
                    if _train_ineligibility(repository, inspected) != "not_approved":
                        continue
                    base = _review_ahead_base(repository, inspected, pr)
                    if base is not None:
                        lookahead_inspected[(name, pr)] = inspected
                        return (name, pr, str(inspected["head_sha"]), base,
                                f"{executor_run_prefix}:{name}:{pr}")
            return None

        def after_lookahead(name: str, pr: int, reviewed: dict[str, Any]) -> bool:
            """Record one lookahead outcome as `_review_ahead` does; True on a stop."""
            nonlocal stop_all
            repository = next(repo for repo in ordered if repo.name == name)
            stop = _record_review_ahead(
                repository, _queue_row(state, name), pr, lookahead_inspected.pop((name, pr)),
                _review_ahead_result(pr, reviewed), summaries, eligible_reasons, save, now)
            stop_all = stop_all or stop
            return stop

        budget.lookahead_candidate = lookahead_candidate
        budget.after_lookahead = after_lookahead
        for repository in ordered:
            if stop_all:
                break
            checkpoint()
            if repository.name not in queued_by_repo:
                continue
            refresh_budget = _RefreshBudget()
            numbers = listed_by_repo[repository.name]
            queued = queued_by_repo[repository.name]
            row = _queue_row(state, repository.name)
            active = row["active"]
            while True:
                checkpoint()
                inspected = None
                took_agent_slot = False
                if active is None:
                    try:
                        pr, inspected, stop_all = _select_candidate(
                            application, budget, repository, row, numbers, queued,
                            renovate_by_repo[repository.name], summaries, now,
                            executor_run_prefix, priorities_by_repo[repository.name])
                    except _TrainStop:
                        break  # Nothing more is written for this repository.
                    if stop_all:
                        break
                    if pr is None:
                        save()
                        break
                    assert inspected is not None
                    # The log evidence that readiness, not queue position, picks
                    # the PR: one entry per election of this pass.
                    summaries[repository.name].setdefault("elected", []).append(
                        {"pr": pr, "tier": _readiness(inspected)})
                    active = {"pr": pr, "head": str(inspected.get("head_sha") or ""),
                              "base": str(inspected.get("base_sha") or ""),
                              "run": _run_identity(inspected), "wait_since": now(),
                              "started": _ci_started(inspected)}
                    row["active"] = active
                    # A Renovate PR with a failed run on its head merges nothing
                    # this turn (it parks, or takes its one refresh): it neither
                    # spends the Renovate slot nor counts as agent work.
                    if pr not in renovate_by_repo[repository.name]:
                        row["agent_streak"] = row.get("agent_streak", 0) + 1
                        # an agent PR parked in the pass that elected
                        # it merges nothing either, so it gives the count back
                        # below. A moved base releases an `evidence_unavailable`
                        # park once and the same pass parks it again; counted,
                        # it made every next election a Renovate PR and no other
                        # agent PR reached the slot.
                        took_agent_slot = True
                    elif not _failed_head(inspected):
                        row["agent_streak"] = 0
                    save()  # Selection is durable before refresh/review/merge.
                elif "train" in active:
                    advanced = _advance_train(application, repository, row, numbers, queued,
                                              summaries, now, save)
                    if advanced == "stop":
                        stop_all = True
                        break
                    if advanced != "released":
                        active_reasons[repository.name] = advanced
                        break
                    trains_closed.add(repository.name)
                    active = None
                    continue
                else:
                    pr = active["pr"]
                    inspected = _inspect_candidate(application, repository, pr)
                    if not inspected.get("ok"):
                        code = str(inspected.get("code") or "INSPECT_FAILED")
                        stalled = now() - active["wait_since"] >= CI_WAIT_SECONDS
                        result = {"pr": pr, "outcome": "retained",
                                  "reason": "INSPECT_STALLED" if stalled and code not in (
                                      AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES
                                  ) else code}
                        stop_all = _record_automerge_result(result, repository, summaries)
                        if stalled and not stop_all:
                            row["parked"][str(pr)] = {
                                "reason": "evidence_unavailable", "head": active["head"],
                                "base": active["base"], "run": active["run"],
                                "review": [None, None], "since": now(),
                            }
                            _clear_candidate(application, repository, pr)
                            row["active"] = None
                            active = None
                            numbers.remove(pr)
                            save()
                            continue
                        break
                    if inspected.get("pr_state") in {"draft", "closed", "merged"}:
                        if inspected.get("pr_state") == "draft":
                            _clear_candidate(application, repository, pr, inspected)
                        row["active"] = None
                        active = None
                        numbers.remove(pr)
                        queued.remove(pr)
                        save()
                        continue
                    run = _observed_run(inspected, active["run"])
                    head = str(inspected.get("head_sha") or "")
                    if active["head"] != head or active["run"] != run:
                        _restart_ci_wait(active, now)
                    if inspected.get("mergeable") is not False:
                        active.pop("unmergeable", None)  # The settle window restarts.
                    active.update(head=head, base=str(inspected.get("base_sha") or ""), run=run,
                                  started=active.get("started", False) or _ci_started(inspected))
                    _note_progress(active, inspected, now)
                    save()

                # nothing to land (merged by hand while the forge lists the PR as
                # open). A refresh, CI run or paid review would only churn it.
                if _head_adds_nothing(repository, inspected):
                    _record_automerge_result(
                        {"pr": pr, "outcome": "retained", "reason": "PR_ALREADY_IN_BASE"},
                        repository, summaries)
                    row["parked"][str(pr)] = _park_record(inspected, "already_in_base", now)
                    _clear_candidate(application, repository, pr, inspected)
                    row["active"] = None
                    active = None
                    numbers.remove(pr)
                    if took_agent_slot:
                        row["agent_streak"] -= 1
                    save()
                    continue

                # Form before a serial candidate mark can dispatch full CI.
                # Only v1's election-time build needs the prefix-run cap.
                at_refresh = _refresh_eligible(repository, inspected)
                if (repository.merge_batch_policy in TRAIN_POLICIES
                        and pr not in renovate_by_repo[repository.name]
                        and repository.name not in trains_closed
                        and not refresh_budget.spent
                        and (at_refresh or _mark_due(repository, inspected))
                        and _train_ineligibility(repository, inspected) is None):
                    formed = _form_train(application, repository, row, pr, inspected, numbers,
                                         priorities_by_repo.get(repository.name, {}),
                                         renovate_by_repo[repository.name]
                                         | withheld_by_repo[repository.name], summaries,
                                         refresh_budget, now,
                                         TRAIN_MAX_MEMBERS
                                         if at_refresh or repository.merge_batch_policy == "train-v2"
                                         else TRAIN_ELECTION_MEMBERS)
                    save()
                    if formed == "stop":
                        stop_all = True
                        break
                    if formed == "parked":
                        row["active"] = None
                        active = None
                        if took_agent_slot:
                            row["agent_streak"] -= 1
                        save()
                        continue
                    if formed in {"built", "cleanup"}:
                        active = row["active"]
                        active_reasons[repository.name] = "merging" if formed == "cleanup" else "waiting_ci"
                        break
                    if refresh_budget.spent:
                        # A build whose outcome is uncertain, at either moment:
                        # the gate may have pushed before the response was lost.
                        # The refresh or mark below would start a second full run
                        # beside it, so the next pass elects again.
                        break

                # Either cap: a repository at its own cap stops only itself.
                if (budget.exhausted(repository.name)
                        and inspected.get("ci_state") == "green"
                        and not _refresh_eligible(repository, inspected)
                        and inspected.get("blocking_reason") != "PR_REVIEW_CHANGES_REQUESTED"
                        and inspected.get("claim_error") not in CLAIM_ERROR_CODES
                        and inspected.get("review_state") != "approved"):
                    active_reasons[repository.name] = "waiting_review"
                    break
                dispatch_error = False
                ci_wait = False
                marked_now = False  # this pass added `gate/candidate`
                # Refresh a red Renovate head once, then park a red result.
                # A changed head, new trusted run/attempt or explicit unpark
                # can readmit an executor-created head; base movement cannot.
                # A red non-Renovate PR is never refreshed.
                red_renovate = (pr in renovate_branches_by_repo[repository.name]
                                and active.get("refreshed_head") != inspected.get("head_sha"))
                red_refresh = (_refresh_eligible(repository, inspected, red_renovate=red_renovate)
                               and not _refresh_eligible(repository, inspected))
                missing_since = _ci_missing_since(active, inspected, now)
                # Recovery refreshes also operate on a head: missing CI must
                # not let a mid-pass push bypass the operator's consent.
                consent = row.get("consented", {}).get(str(pr))
                if consent is not None and inspected.get("head_sha") != consent[0]:
                    result = {"pr": pr, "outcome": "retained",
                              "reason": "RENOVATE_CONSENT_STALE", "code": consent[1].upper()}
                    park = consent[1]
                # A hold label removed before any pass parked the PR leaves
                # no consent record; the label history still shows it.
                elif (consent is None and pr in renovate_by_repo[repository.name]
                        and _consent_missing(inspected)):
                    result = {"pr": pr, "outcome": "retained",
                              "reason": "RENOVATE_CONSENT_MISSING"}
                    park = RENOVATE_CONSENT_MISSING
                # the gate row owns what a binding rejection is. Its
                # comment re-review runs at selection, never here.
                elif inspected.get("blocking_reason") == "PR_REVIEW_CHANGES_REQUESTED":
                    result = {"pr": pr, "outcome": "retained",
                              "reason": "PR_REVIEW_CHANGES_REQUESTED"}
                    park = "changes_requested"
                # The gate reports `mergeable: false` ahead of any CI blocker,
                # so a stale-base green run reads `pending` here; waiting on
                # that CI would hold the slot. Park whatever `ci_state` says,
                # once the read has held for `MERGEABLE_SETTLE_SECONDS`
                # (below): Forgejo also reads `false` while it rechecks open
                # PRs after a merge, as for the next PR in this pass. A real
                # conflict then waits on its author.
                elif inspected.get("blocking_reason") == "PR_NOT_MERGEABLE":
                    result = {"pr": pr, "outcome": "retained", "reason": "PR_NOT_MERGEABLE"}
                    park = "conflict"
                # A failed run on a moved base cannot turn green by waiting.
                elif (inspected.get("blocking_reason") in {"CI_BASE_STALE", "PR_BASE_STALE"}
                        and inspected.get("ci_run_conclusion") in TERMINAL_FAILURE_CONCLUSIONS
                        and not red_refresh):
                    result = {"pr": pr, "outcome": "retained",
                              "reason": str(inspected["blocking_reason"])}
                    park = "ci_failed"
                # The gate reports a green run on a moved base as `pending`;
                # waiting on it would never end, so it goes to the refresh,
                # or to the conflict park above when the forge cannot merge it.
                elif not _refresh_eligible(repository, inspected,
                                           red_renovate=red_renovate) and (
                        inspected.get("ci_state") == "pending"
                        or (inspected.get("ci_state") == "red"
                            and inspected.get("ci_run_conclusion") not in TERMINAL_FAILURE_CONCLUSIONS)
                        or (inspected.get("blocking_reason") in {
                            "CI_API_UNAVAILABLE", "CI_HEAD_STALE"}
                            and inspected.get("ci_run_conclusion") not in TERMINAL_FAILURE_CONCLUSIONS)):
                    deadline = (CI_START_WAIT_SECONDS
                                if not active["started"]
                                and inspected.get("blocking_reason") in _CI_NOT_STARTED
                                else CI_WAIT_SECONDS)
                    park = None
                    refreshed = None
                    missing = (missing_since is not None
                               and now() - missing_since >= CI_MISSING_SECONDS)
                    recorded = inspected.get("recorded_base_sha")
                    if (missing and isinstance(recorded, str)
                            and recorded != inspected.get("base_sha")
                            and active.get("refreshed") != active["head"]):
                        # A real push re-fires the hooks the branch update
                        # skipped; worth one only when main moved past the
                        # PR's recorded base. Spent this pass: wait for the next.
                        if _stale_pull_ref(inspected):
                            LOGGER.warning(
                                "automerge: %s#%s refs/pull head %s is not the branch head %s; "
                                "one refresh repairs it",
                                repository.name, pr, inspected["pull_ref_sha"],
                                inspected["head_sha"])
                        refreshed = _recovery_refresh(application, repository, active, pr,
                                                      inspected, refresh_budget, save)
                        missing = refreshed is not None
                    if refreshed is not None and (
                            refreshed["outcome"] == "refreshed"
                            or refreshed.get("reason") in (
                                AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES)):
                        result = refreshed
                    elif missing:
                        stale_ref = _stale_pull_ref(inspected)
                        if stale_ref:
                            LOGGER.warning(
                                "automerge: %s#%s refs/pull head %s is not the branch head %s; "
                                "the queue has no push that repairs it (refresh %s)",
                                repository.name, pr, inspected["pull_ref_sha"],
                                inspected["head_sha"],
                                "refused" if refreshed is not None else "not eligible")
                        result = {"pr": pr, "outcome": "retained",
                                  "reason": ("PULL_REF_STALE" if stale_ref
                                             else "CI_RUN_MISSING_STALLED"),
                                  **({"refresh_refused": refreshed["reason"]}
                                     if refreshed is not None else {})}
                        park = "pull_ref_stale" if stale_ref else "ci_run_missing"
                    elif now() - _stall_since(active) >= deadline:
                        result = {"pr": pr, "outcome": "retained", "reason": "CI_STALLED"}
                        park = "ci_stalled"
                    else:
                        result = {"pr": pr, "outcome": "retained",
                                  "reason": str(inspected.get("blocking_reason") or "PR_CI_NOT_GREEN")}
                        park = None
                        ci_wait = True
                else:
                    try:
                        # Narrowing rule: mark an up-to-date head this pass is about to
                        # merge; the `labeled` event starts its full run and
                        # the queue waits for it rather than merging on the
                        # narrowed run. A head the pass refreshes is marked
                        # only after the refresh: a mark first would
                        # start a full run on the obsolete head and make the
                        # refresh refuse it as an in-progress run. A head not
                        # yet approved is reviewed first and marked by
                        # `_automerge_candidate` on approval.
                        if _mark_due(repository, inspected, red_renovate=red_renovate):
                            result, marked_now = _mark_candidate(
                                budget, repository, pr, inspected)
                        else:
                            result = None
                        if result is None and marked_now:
                            # a mark placed on an unknown CI answer
                            # names it; the full run is the conservative wait.
                            unknown = _ci_evidence_unknown(inspected)
                            result = {"pr": pr, "outcome": "retained",
                                      "reason": CANDIDATE_MARKED,
                                      **({"detail": unknown} if unknown else {})}
                            ci_wait = True
                        if result is None:
                            result = _automerge_candidate(
                                budget, repository, pr, executor_run_prefix,
                                refresh_budget=refresh_budget, inspected=inspected,
                                red_renovate=red_renovate,
                                consented_head=(row.get("consented", {}).get(str(pr))
                                                or [None])[0],
                            )
                            marked_now = result.pop("_marked_now", marked_now)
                            ci_wait = result.get("reason") == CANDIDATE_MARKED
                            # Before the mark below: its deploy checkpoint may
                            # save and yield the pass.
                            _note_refresh(row, active, pr, result,
                                          pr in renovate_by_repo[repository.name])
                            if result["outcome"] == "refreshed" and not red_refresh:
                                # a refreshed head is a new head. Whether
                                # the approval survived it is decided by the reuse
                                # proof, not by this pass's read of the head it
                                # replaced: `_range_diff_digest` refuses reuse for
                                # a range it cannot digest -- a binary change, for
                                # one -- so the pre-refresh read can report a
                                # delegated approval the new head will not carry.
                                # Mark only from a read taken after the refresh
                                # that shows an approval; a failed read or one
                                # without an approval keeps the refresh and leaves
                                # the mark to a later pass.
                                post_refresh = _inspect_candidate(application, repository, pr)
                                if post_refresh.get("ok") and _approved(post_refresh):
                                    # The refreshed head's `synchronize` run may be
                                    # narrowed; the label's full run supersedes it
                                    # in the PR's concurrency group. A red Renovate
                                    # head is never marked, nor one whose approval
                                    # did not survive the refresh: it is reviewed
                                    # on its new narrowed run first. A refused mark
                                    # keeps the refresh: the next pass marks the
                                    # new head.
                                    failed, marked_now = _mark_candidate(
                                        budget, repository, pr, post_refresh)
                                    if failed is not None and failed["reason"] in (
                                            AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES):
                                        # The refresh stays recorded and active,
                                        # but an authority or integrity refusal
                                        # still stops the pass, as on every
                                        # other candidate path.
                                        result = {**result, "reason": failed["reason"]}
                                    elif failed is not None:
                                        LOGGER.warning(
                                            "automerge: %s#%s refreshed but not marked: %s",
                                            repository.name, pr, failed["reason"])
                                else:
                                    LOGGER.info(
                                        "automerge: %s#%s refreshed and not marked: no "
                                        "approved read of the refreshed head",
                                        repository.name, pr)
                    except _TrainStop:
                        break  # This PR stays active; nothing more is written.
                    except Exception as exc:  # uncertain dispatch or transport: keep active
                        result = _candidate_exception(pr, exc)
                        dispatch_error = True
                    if marked_now:
                        # Promotion starts a run before its ID is visible, so
                        # the narrowed run's wait must not consume its window.
                        _restart_ci_wait(active, now)
                    park = _park_reason(result, inspected)
                    if result.get("reason") == "RENOVATE_CONSENT_STALE":
                        park = row["consented"][str(pr)][1]
                        result["code"] = park.upper()
                    if red_refresh and park == "ci_failed":
                        park = None  # Its refresh waits for an unspent budget.
                    if park == "ci_failed":
                        # a run Forgejo's lost-runner sweep closed is no
                        # verdict on the head. The gate proves the loss and reruns
                        # the job once; the PR keeps its slot and the queue waits
                        # for the new run. Any other answer parks it as before.
                        rerun = _rerun_lost_ci(application, repository, pr)
                        if rerun is not None:
                            result, park, ci_wait = rerun, None, True
                            _restart_ci_wait(active, now)
                    # the run list is in transition right after a mark
                    # or refresh. An evidence-unavailable read of the marked PR
                    # inside the start window waits like pending CI: parking
                    # it would clear the mark and abandon the full run it
                    # started. Past the window the park applies.
                    if (park == "evidence_unavailable"
                            and inspected.get("merge_candidate") is True
                            and result.get("reason") not in (
                                AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES)
                            and now() - active["wait_since"] < CI_START_WAIT_SECONDS):
                        park = None
                        ci_wait = True
                    # An operator's "Update branch" leaves the claim on the old
                    # head; the gate refresh re-projects it when the update is
                    # tree-identical. Once per head, before an author park.
                    if (park == "claim_unavailable"
                            and result.get("claim_error") == "PR_CLAIM_OUTDATED_HEAD"
                            and active.get("refreshed") != active["head"]):
                        refreshed = _recovery_refresh(application, repository, active, pr,
                                                      inspected, refresh_budget, save)
                        if refreshed is None:  # Spent this pass: try the next one.
                            park = None
                        elif (refreshed["outcome"] == "refreshed"
                                or refreshed.get("reason") in (
                                    AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES)):
                            result, park, dispatch_error = refreshed, None, False
                        else:
                            result["refresh_refused"] = refreshed["reason"]
                # Either path above reads the forge flag. Hold the slot like a
                # CI wait until the same head and base stay unmergeable for the
                # settle window; a refresh or train conflict parks at once.
                if (park == "conflict" and result.get("reason") == "PR_NOT_MERGEABLE"
                        and _unmergeable_settling(active, inspected, now)):
                    park = None
                    ci_wait = True
                if active_reasons.get(repository.name) == "reviewing":
                    active_reasons[repository.name] = "waiting_review"
                merge_refused = result.pop("_merge_refused", False)
                review_refused = result.pop("_review_refused", False)
                reason = result.get("reason")
                refresh_refused = (isinstance(reason, str) and reason.startswith("PR_REFRESH_")
                                   and reason != "PR_REFRESH_CONFLICT")
                if (park is None and result.get("reason") not in (
                        AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES)
                        and (merge_refused or refresh_refused or review_refused
                             or result.get("reason") == "PR_MERGEABILITY_UNKNOWN")
                        and now() - active["wait_since"] >= CI_WAIT_SECONDS):
                    park = ("gate_blocked" if merge_refused or refresh_refused or review_refused
                            else "mergeability_unknown")
                    result["reason"] = "PR_STALLED"
                if (park is None and dispatch_error
                        and result.get("reason") not in (
                            AUTOMERGE_INTEGRITY_CODES | AUTOMERGE_AUTHORITY_CODES)
                        and now() - active["wait_since"] >= CI_WAIT_SECONDS):
                    park = "gate_blocked"
                    result["reason"] = "CANDIDATE_STALLED"
                stop_all = _record_automerge_result(result, repository, summaries) or stop_all
                # Every refresh path, the recovery refresh included, before the stop.
                _note_refresh(row, active, pr, result, pr in renovate_by_repo[repository.name])
                if stop_all:
                    save()
                    break
                if result["outcome"] == "merged":
                    row["active"] = None
                    active = None
                    numbers.remove(pr)
                    queued.remove(pr)
                    _forget_consent(row, str(pr))
                    save()
                    continue
                if park is None and (stuck := _stuck(active, result, ci_wait)) is not None:
                    park = "stuck"
                    result.update(reason="PR_STUCK", code=stuck)
                if park is not None:
                    if park == "ci_failed" and pr in renovate_by_repo[repository.name]:
                        park = "renovate_ci_failed"
                    if park == "changes_requested":
                        # A fresh review was just published; record its native
                        # identity, not the pre-review inspect, so an unchanged
                        # rejection does not reenter on the next cron pass.
                        observed = _inspect_candidate(application, repository, pr)
                        if not observed.get("ok"):
                            # The first inspect already proved rejection; keep
                            # its identity and report the failed readback.
                            stop_all = _record_automerge_result(
                                {"pr": pr, "outcome": "retained",
                                 "reason": str(observed.get("code") or "INSPECT_FAILED")},
                                repository, summaries)
                            if stop_all:
                                break
                        else:
                            inspected = observed
                        # Its bot author never answers a review, so the
                        # operator owns it (PARK_OWNERS).
                        if pr in renovate_by_repo[repository.name]:
                            park = "renovate_changes_requested"
                    row["parked"][str(pr)] = {
                        "reason": park, "head": active["head"], "base": active["base"],
                        "run": active["run"], "review": _review_identity(inspected),
                        "claim_sha": inspected.get("claim_sha"),
                        "since": now(),
                        # The origin of a forge-flag conflict that held for the
                        # settle window; `_parked_changed` still releases it on
                        # `mergeable is True`.
                        **({"code": result["code"]} if park == "stuck"
                           else {"code": "PR_NOT_MERGEABLE"}
                           if park == "conflict" and result.get("reason") == "PR_NOT_MERGEABLE"
                           else {}),
                    }
                    _clear_candidate(application, repository, pr, inspected, marked=marked_now)
                    row["active"] = None
                    active = None
                    numbers.remove(pr)
                    if took_agent_slot:
                        row["agent_streak"] -= 1
                    save()
                    continue
                # Refresh, pending CI, transient Gate refusal or review receipt
                # recovery all keep this candidate's place until the next pass.
                active_reasons[repository.name] = (
                    "waiting_ci" if ci_wait
                    else "refreshing" if result["outcome"] == "refreshed"
                    else "merging" if merge_refused
                    else "waiting_review" if inspected.get("ci_state") == "green"
                    and inspected.get("review_state") != "approved"
                    else "-")
                break
        # A bounded pass lends each unspent slot to `_review_ahead`, from
        # every visited repository whose active PR is not itself waiting on
        # review (a busy or failed review of it keeps the slot idle), while
        # the pass deadline still leaves room for one.
        if (not stop_all and max_reviews is not None and not budget.spent()
                and not budget.out_of_time()):
            checkpoint()
            try:
                _review_ahead(
                    budget, [repository for repository in ordered
                             if repository.name in queued_by_repo
                             and active_reasons.get(repository.name) != "waiting_review"],
                    {name: [number for number in listed if number not in withheld_by_repo[name]]
                     for name, listed in listed_by_repo.items()},
                    priorities_by_repo, renovate_by_repo, state, summaries,
                    executor_run_prefix, eligible_reasons, save, now)
            except _TrainStop:
                pass  # The train's result carries the stop the next line reads.
        stop_all = any(summary["authority_integrity_failed"] for summary in summaries.values())
        for repository in ordered:
            if repository.name not in queued_by_repo:
                continue
            row = _queue_row(state, repository.name)
            descriptions = _queue_descriptions(
                row, queued_by_repo[repository.name], active_reasons.get(repository.name, "-"),
                eligible_reasons.get(repository.name))
            summaries[repository.name]["queue_depth"] = len(descriptions)
            summaries[repository.name]["head_wait_s"] = (
                round(now() - row["active"]["wait_since"]) if row["active"] is not None else 0)
            # An author-owned park waits on its author by design, and a major
            # Renovate PR waits on the operator's reading by design: neither
            # is a stall. Unread Renovate labels are no design and count. A
            # queue-owned park is re-checked once no PR is active, so it
            # counts only then; while one is, `head_wait_s` covers a stall.
            owners = {"operator"} if row["active"] is not None else {"operator", "queue"}
            summaries[repository.name]["park_wait_s"] = round(max(
                (now() - parked["since"] for parked in row["parked"].values()
                 if PARK_OWNERS[parked["reason"]] in owners
                 and parked["reason"] not in RENOVATE_LABEL_HOLD_REASONS), default=0))
            if not stop_all:  # An integrity stop ends every forge write.
                _publish_queue(application, repository, row, descriptions)
        state["last_pass"] = time.time()
        if listed_by_repo:
            save()
    return [summaries[repository.name] for repository in repositories]


def _queue_command(args: argparse.Namespace) -> int:
    """Show the automerge queue, or unpark PRs under the executor's lock.

    The store is opened with the whole automerge selection, the same one the
    executor validates rows against; naming fewer repositories only filters
    what is printed or changed.
    """
    try:
        records = load_repositories()
        managed = automerge_selection(records)
        names = [repository.name for repository in
                 resolve_selection(args.repository or managed, records)]
    except (RuntimeError, OSError, yaml.YAMLError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    outside = [name for name in names if name not in managed]
    if outside:
        print(f"FATAL: not in the automerge queue: {', '.join(outside)}", file=sys.stderr)
        return 2
    store = QueueStore(repositories=managed)
    try:
        if args.queue_show:
            rows = store.read()["repositories"]
            print(json.dumps({"repositories": {name: rows.get(name) for name in names}}, indent=2))
            return 0
        (name,) = names
        with store.locked() as state:
            parked = state["repositories"].get(name, {"parked": {}})["parked"]
            keys = list(parked) if args.unpark_all else [
                str(pr) for pr in args.unpark if str(pr) in parked]
            removed = {key: parked.pop(key) for key in keys}
            if removed:
                store.save(state)
    except QueueStateError as exc:
        if str(exc) == "AUTOMERGE_PASS_BUSY":
            # Nothing was removed, so unlike a skipped timer pass this fails.
            print(json.dumps({"repositories": [
                {"repository": name, "skipped": True, "reason": str(exc)} for name in names]}))
            return 1
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"repositories": [{"repository": name, "unparked": removed}]}, indent=2))
    return 0


# The park `--reopen-review-all` selects and a reopen ends (`_park_reason`).
REOPEN_PARK_REASON = "review_execution_failed"
# the answer for a parked PR whose head succeeded on a stale base. The
# review owner would refuse it PR_CI_NOT_GREEN; the park is released instead and
# the queue refreshes the head, which starts a new review chain.
REOPEN_STALE_BASE = "REVIEW_REOPEN_STALE_BASE"


def _operator_name() -> str:
    """Who runs the reopen: the sudo caller, else the login. Empty when unknown;
    the review owner then refuses."""
    if os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return ""


def _reopen_one(application: Any, repository: str, pr: int, args: argparse.Namespace,
                operator: str, *, parked: bool = False) -> dict[str, Any]:
    """Reopen one PR's chain for the head and base the executor would review.

    A *parked* PR whose head succeeded on a stale base needs no reopen
   : the refresh that follows its release makes a new head and so a
    new chain, and the owner could not derive a review key on the stale base.
    """
    inspected = application.inspect(repository, pr)
    head, base = inspected.get("head_sha"), inspected.get("base_sha")
    if (not inspected.get("ok") or inspected.get("pr_state") != "open"
            or not isinstance(head, str) or not isinstance(base, str)):
        return {"ok": False, "code": str(inspected.get("code") or inspected.get("blocking_reason")
                                         or "INSPECT_FAILED")}
    if parked and _stale_base_success(inspected):
        return {"ok": True, "code": REOPEN_STALE_BASE}
    return private_reopen_review([
        repository, str(pr), head, base, args.reason, operator,
        "1" if args.allow_paid_attempts else "0", "1" if args.yes else "0",
    ])


def _reopen_command(args: argparse.Namespace) -> int:
    """Reopen exhausted automatic-review chains: an operator recovery.

    Runs under the executor's queue lock, like `--unpark`, so no pass can
    review while a chain moves. The review owner (its reopen operation
    in the review module) decides eligibility, archives
    the chain and writes the audit record; it refuses off the primary
    executor. Without `--yes` nothing changes. With it, a reopened PR loses
    its `review_execution_failed` park, so the next pass starts a fresh first
    attempt. A parked PR whose head succeeded on a stale base is released
    without the owner; the queue's refresh follows. A PR parked for
    any other reason is refused before the owner is
    asked: its chain is not what holds it, and a reopen spent there is lost.
    """
    try:
        records = load_repositories()
        managed = automerge_selection(records)
        (name,) = [repository.name for repository in resolve_selection(args.repository, records)]
    except (RuntimeError, OSError, yaml.YAMLError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    if name not in managed:
        print(f"FATAL: not in the automerge queue: {name}", file=sys.stderr)
        return 2
    operator = _operator_name()
    application = _gate_application()
    store = QueueStore(repositories=managed)
    results: dict[str, dict[str, Any]] = {}
    unparked: dict[str, Any] = {}
    save_error: str | None = None
    try:
        with store.locked() as state:
            row = state["repositories"].get(name)
            parked = row["parked"] if row is not None else {}
            numbers = (sorted(int(key) for key, park in parked.items()
                              if park["reason"] == REOPEN_PARK_REASON)
                       if args.reopen_review_all else list(dict.fromkeys(args.reopen_review)))
            for pr in numbers:
                park = parked.get(str(pr))
                result = (_reopen_one(application, name, pr, args, operator,
                                      parked=park is not None)
                          if park is None or park["reason"] == REOPEN_PARK_REASON
                          else {"ok": False, "code": "REVIEW_REOPEN_PARKED_OTHER"})
                results[str(pr)] = {**result, "park": park["reason"] if park else None}
                if (args.yes and result.get("ok")
                        and result.get("code") in {"REVIEW_REOPENED", REOPEN_STALE_BASE}
                        and park is not None and park["reason"] == REOPEN_PARK_REASON):
                    unparked[str(pr)] = parked.pop(str(pr))
            if unparked:
                try:
                    store.save(state)
                except QueueStateError as exc:
                    # The chains are archived; the parks stay. `--unpark` ends them.
                    save_error, unparked = str(exc), {}
    except QueueStateError as exc:
        if str(exc) == "AUTOMERGE_PASS_BUSY":
            print(json.dumps({"repositories": [
                {"repository": name, "skipped": True, "reason": str(exc)}]}))
            return 1
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"repositories": [{
        "repository": name, "applied": args.yes, "operator": operator, "reason": args.reason,
        "reopen": results, "unparked": unparked,
        **({"park_removal_failed": save_error} if save_error else {}),
    }]}, indent=2))
    return 0 if save_error is None and all(result.get("ok") for result in results.values()) else 1


def _pass_deadline_from_environment() -> int | None:
    """`PASS_DEADLINE_ENV` as a positive integer; None when unset or malformed."""
    raw = os.environ.get(PASS_DEADLINE_ENV)
    if raw is None:
        return None
    try:
        return _positive_int(raw)
    except argparse.ArgumentTypeError:
        LOGGER.warning("automerge: ignoring %s=%r: not a positive integer; "
                       "the pass runs without a deadline", PASS_DEADLINE_ENV, raw[:64])
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge every open PR, independent ones first."
    )
    parser.add_argument(
        "repository", nargs="*",
        help="repositories to merge, by catalog name or the aliases "
             f"{'/'.join(sorted(ALIASES))} (default: "
             f"{' '.join(DEFAULT_REPOSITORIES)})",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and the collision degrees; merge nothing")
    parser.add_argument("--require-green", action="store_true",
                        help="skip any PR whose checks are not all passing")
    parser.add_argument("--include-drafts", action="store_true",
                        help="also merge draft pull requests")
    parser.add_argument("--include-renovate", action="store_true",
                        help="also merge Renovate's dependency pull requests")
    parser.add_argument("--json", action="store_true",
                        help="emit a JSON summary")
    parser.add_argument(
        "--automerge",
        action="store_true",
        help="merge eligible pull requests of every configured repository "
             "with exact-head green CI and review",
    )
    parser.add_argument(
        "--max-reviews",
        type=_positive_int,
        default=None,
        help="stop the pass once this many reviewed candidates have finished "
             "(only with --automerge)",
    )
    parser.add_argument(
        "--max-reviews-per-repo",
        type=_positive_int,
        default=None,
        help="start at most this many reviews per repository in one pass; "
             f"default {DEFAULT_MAX_REVIEWS_PER_REPO} when --max-reviews is set "
             "(only with --automerge)",
    )
    parser.add_argument(
        "--review-workers",
        type=_positive_int,
        default=None,
        help="run up to this many reviews at once: while one review runs, review "
             "later queued PRs that lack only an approval, within both review caps; "
             "default 1 (only with --automerge)",
    )
    parser.add_argument(
        "--pass-deadline-seconds",
        type=_positive_int,
        default=None,
        help="start no review once fewer than one review's hard bound "
             f"({PRIVATE_REVIEW_SUBPROCESS_TIMEOUT_SECONDS}s) plus "
             f"{PASS_DEADLINE_MARGIN_SECONDS}s remain of this many seconds from the "
             f"pass start; merges continue (only with --automerge; default "
             f"${PASS_DEADLINE_ENV} when set)",
    )
    parser.add_argument(
        "--executor-run",
        type=_executor_run_prefix,
        default=None,
        dest="executor_run",
        help="prefix for the per-candidate review executor-run identity "
             f"(default {DEFAULT_EXECUTOR_RUN_PREFIX!r}; only with --automerge)",
    )
    parser.add_argument(
        "--level-prefix",
        action="store_true",
        help="force syslog-priority-prefixed logging even without JOURNAL_STREAM "
             "(only with --automerge)",
    )
    parser.add_argument("--queue-show", action="store_true",
                        help="print the automerge queue state and exit; reads only")
    parser.add_argument("--unpark", type=_positive_int, action="append", default=[],
                        metavar="PR",
                        help="remove PR's parked entry from the automerge queue of the one "
                             "named repository (repeatable)")
    parser.add_argument("--unpark-all", action="store_true",
                        help="remove every parked entry from the automerge queue of the one "
                             "named repository")
    parser.add_argument("--reopen-review", type=_positive_int, action="append", default=[],
                        metavar="PR",
                        help="operator recovery: archive the PR's exhausted "
                             "automatic-review chain so the next pass starts a fresh attempt "
                             "(repeatable; a dry run without --yes)")
    parser.add_argument("--reopen-review-all", action="store_true",
                        help="--reopen-review for every PR parked review_execution_failed in "
                             "the one named repository")
    parser.add_argument("--reason", default=None,
                        help="the reopen's audit reason: one line of at most 200 characters "
                             "(required with --reopen-review)")
    parser.add_argument("--yes", action="store_true",
                        help="apply the reopen; without it nothing changes")
    parser.add_argument("--allow-paid-attempts", action="store_true",
                        help="also reopen a chain whose REVIEW_INCOMPLETE attempt ran a model")
    args = parser.parse_args(argv)

    if args.max_reviews is not None and not args.automerge:
        parser.error("--max-reviews requires --automerge")
    if args.max_reviews_per_repo is not None and not args.automerge:
        parser.error("--max-reviews-per-repo requires --automerge")
    if args.review_workers is not None and not args.automerge:
        parser.error("--review-workers requires --automerge")
    if args.pass_deadline_seconds is not None and not args.automerge:
        parser.error("--pass-deadline-seconds requires --automerge")
    if args.executor_run is not None and not args.automerge:
        parser.error("--executor-run requires --automerge")
    if args.level_prefix and not args.automerge:
        parser.error("--level-prefix requires --automerge")

    queue_command = [args.queue_show, bool(args.unpark), args.unpark_all]
    reopen_command = [bool(args.reopen_review), args.reopen_review_all]
    if any(reopen_command):
        if (sum(reopen_command) > 1 or any(queue_command) or args.automerge or args.dry_run
                or args.require_green or args.include_drafts or args.include_renovate):
            parser.error("--reopen-review and --reopen-review-all take no other command flag")
        if len(args.repository) != 1:
            parser.error("--reopen-review and --reopen-review-all need exactly one repository")
        if args.reason is None or not args.reason.strip():
            parser.error("--reopen-review and --reopen-review-all need --reason")
        return _reopen_command(args)
    if args.reason is not None or args.yes or args.allow_paid_attempts:
        parser.error("--reason, --yes and --allow-paid-attempts need --reopen-review")
    if any(queue_command):
        if (sum(queue_command) > 1 or args.automerge or args.dry_run or args.require_green
                or args.include_drafts or args.include_renovate):
            parser.error("--queue-show, --unpark and --unpark-all take no other command flag")
        if not args.queue_show and len(args.repository) != 1:
            parser.error("--unpark and --unpark-all need exactly one repository")
        return _queue_command(args)

    if args.automerge:
        formatter = (_JournalPriorityFormatter("%(levelname)s %(message)s")
                     if (os.environ.get("JOURNAL_STREAM") or args.level_prefix)
                     else logging.Formatter("%(levelname)s %(message)s"))
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logging.basicConfig(level=logging.INFO, handlers=[handler])

    if args.automerge and (
        args.dry_run
        or args.require_green
        or args.include_drafts
        or args.include_renovate
    ):
        parser.error("--automerge cannot be combined with interactive batch flags")
    if args.automerge and args.repository:
        parser.error(
            "--automerge always selects the configured repositories"
        )

    try:
        records = load_repositories()
        requested = automerge_selection(records) if args.automerge else args.repository
        selected = resolve_selection(requested, records)
    except (RuntimeError, OSError, yaml.YAMLError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        if args.automerge:
            # An unprefixed stderr line reaches the journal at info, so
            # also log at error level.
            LOGGER.error("automerge: %s", exc)
        return 2

    summaries: list[dict[str, Any]] = []
    worst = 0
    pass_error_logged = False
    if args.automerge:
        pass_kwargs: dict[str, Any] = {}
        if args.max_reviews is not None:
            pass_kwargs["max_reviews"] = args.max_reviews
            pass_kwargs["max_reviews_per_repo"] = DEFAULT_MAX_REVIEWS_PER_REPO
        if args.max_reviews_per_repo is not None:
            pass_kwargs["max_reviews_per_repo"] = args.max_reviews_per_repo
        if args.review_workers is not None:
            pass_kwargs["review_workers"] = args.review_workers
        deadline = (args.pass_deadline_seconds if args.pass_deadline_seconds is not None
                    else _pass_deadline_from_environment())
        if deadline is not None:
            pass_kwargs["pass_deadline_seconds"] = deadline
        if args.executor_run is not None:
            pass_kwargs["executor_run_prefix"] = args.executor_run
        try:
            # no merge lands while a deploy is in progress here.
            deploy = _deploy_in_progress()
            if deploy is not None:
                summaries = [{"repository": repository.name, "skipped": True, **deploy}
                             for repository in selected]
                _publish_hold(selected, deploy)
            else:
                summaries = run_automerge_pass(selected, **pass_kwargs)
        except QueueStateError as exc:
            if str(exc) == "AUTOMERGE_PASS_BUSY":
                summaries = [{"repository": repository.name, "skipped": True,
                              "reason": str(exc)}
                             for repository in selected]
            else:
                LOGGER.error("automerge: %s", exc)
                summaries = [{"repository": repository.name, "error": str(exc)}
                             for repository in selected]
                worst = 2
                pass_error_logged = True
        except RuntimeError as exc:
            LOGGER.error("automerge: %s", exc)
            summaries = [{"repository": repository.name, "error": str(exc)}
                         for repository in selected]
            worst = 2
            pass_error_logged = True
        for summary in summaries:
            for record in summary.get("results", []):
                reason = record.get("reason")
                claim_error = record.get("claim_error")
                if (reason == "PR_CLAIM_UNAVAILABLE" and isinstance(claim_error, str)
                        and claim_error in CLAIM_ERROR_CODES):
                    LOGGER.info("automerge claim unavailable: %s#%s %s", summary["repository"],
                                record["pr"], claim_error)
                if record.get("outcome") == "retained" and reason in REVIEW_EXECUTION_FAILURE_CODES:
                    evidence = str(record["review_error"]) if record.get("review_error") else ""
                    if record.get("review_error_detail"):
                        evidence = f"{evidence}: {record['review_error_detail']}" if evidence \
                            else str(record["review_error_detail"])
                    LOGGER.error("automerge review failed: %s#%s %s%s",
                                 summary["repository"], record["pr"], reason,
                                 f" {evidence}" if evidence else "")
                if record.get("authority_integrity_failed"):
                    LOGGER.error("automerge integrity stop: %s#%s %s",
                                 summary["repository"], record.get("pr"), reason)
            if summary.get("authority_integrity_failed") and not any(
                    record.get("authority_integrity_failed")
                    for record in summary.get("results", [])):
                # A stop with no candidate record: a refused list or train build.
                LOGGER.error("automerge integrity stop: %s %s", summary["repository"],
                             summary.get("error") or (summary.get("train") or {}).get("reason"))
            if (summary.get("error") and not pass_error_logged
                    and not summary.get("authority_integrity_failed")):
                # A repository that fails every pass must leave one error-level
                # line.
                LOGGER.error("automerge repository error: %s %s",
                             summary["repository"], summary["error"])
        if (any(summary.get("authority_integrity_failed") for summary in summaries)
                or any(summary.get("error") for summary in summaries)):
            worst = 2
        payload = {
            "repositories": summaries,
            "executor_run": args.executor_run or DEFAULT_EXECUTOR_RUN_PREFIX,
            "ok": worst == 0 and not any(
                record.get("reason") in REVIEW_EXECUTION_FAILURE_CODES
                for summary in summaries for record in summary.get("results", [])
            ),
        }
        LOGGER.info("automerge result %s", json.dumps(payload, separators=(",", ":")))
        print(json.dumps(payload, indent=2) if args.json else json.dumps(payload))
        return worst
    for repository in selected:
        if not args.json:
            print(f"== {repository.name} ==", flush=True)
        try:
            summary = run_one(repository, args)
        except RuntimeError as exc:
            # Report and continue, the way the deploy step treats its second
            # repository: one unreachable forge must not withhold the merges
            # the other one would have done.
            if args.automerge:
                LOGGER.error("%s: %s", repository.name, exc)
            else:
                print(f"FATAL: {repository.name}: {exc}", file=sys.stderr)
            summaries.append({"repository": repository.name, "error": str(exc)})
            worst = 2
            continue
        summaries.append(summary)
        if not args.automerge and any(
            record["outcome"] != "merged" for record in summary["results"]
        ):
            worst = max(worst, 1)
        if summary.get("authority_integrity_failed"):
            worst = 2
            if not args.json:
                print(
                    "FATAL: stopping remaining repositories because canonical "
                    "history preservation is unproven",
                    file=sys.stderr,
                )
            break

    if args.json:
        payload = {"repositories": summaries}
        if args.automerge:
            LOGGER.info("automerge result %s", json.dumps(payload, separators=(",", ":")))
        print(json.dumps(payload, indent=2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
