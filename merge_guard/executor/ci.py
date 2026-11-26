"""Read-only Forgejo Actions evidence provider.

The provider is pure: it receives already-fetched API payloads and
gate-derived identity, and returns the closed ``ci-evidence.v1`` and
``ci-failure.v1`` records. It performs no network calls itself — the fetch
functions live in the Forgejo backend module
and the ``ci-status`` operation orchestrates them. Keeping the
decision logic free of I/O is what makes the
four-state mapping, the closed reason codes, the profile-strength table, and
the redaction/capping rules table-testable against captured API shapes.

Closed identity: the gate derives repository, pull request, base
SHA, head SHA, and the required profile; the provider never accepts any of
them from a caller. The trusted-workflow proof is mechanically observable
from the run's own record: the run must belong to the trusted launcher
path, its event must be the profile's trusted event
(``pull_request_target``, which is defined to load the workflow file from
the BASE ref), and the launcher must really hold that file at the base
commit — the file-existence probe, the Forgejo route's equivalent of
GitHub's resolved-reusable-workflow proof. Exact pull-request/base/head
binding comes from the run's own ``pull_requests`` record compared against
the current pull request, base, and pushed head.

Four states only: PASS, FAIL, NOT_RUN, UNAVAILABLE. Pending is not a
fifth state — queued and in-progress runs are UNAVAILABLE with
``CI_RUN_QUEUED``/``CI_RUN_IN_PROGRESS`` and a bounded retry hint. Reason
codes are the closed set in ``CI_REASON_CODES``, which includes
``CI_RUN_SKIPPED`` for the "skipped unexpectedly" cause.

Profiles: ``checks/c1 > checks/c2 > checks/c3``, fixed strength
order, no aliases or substring matching. A stronger profile satisfies a
weaker requirement only after every base/head/trusted-workflow check above
has already passed — those checks are unconditional, so "bound to the same
current base and head and the same trusted workflow contract" holds by
construction.

Failure evidence: failed terminal jobs and failed steps only,
20,000-byte cap across the response, deterministic sanitization and
redaction before any text leaves this module, and no log content at all
unless the run identity was accepted by ``ci-evidence.v1`` first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import NamedTuple

from .contracts import SHA_RE

CI_EVIDENCE_SCHEMA = "ci-evidence.v1"
CI_FAILURE_SCHEMA = "ci-failure.v1"
CI_PROGRESS_SCHEMA = "ci-progress.v1"

# --- closed workflow identity ----------------------------------------------
# The launcher path the provider proves. This binds the workflow shape
# delivered by the Forgejo checks workflow; a change to the workflow file is
# a change to this trust boundary and must update this constant and the
# trust tests together (Forgejo route: the launcher is taken
# from the base ref by `pull_request_target` and proved present there).
FORGEJO_LAUNCHER_PATH = ".forgejo/workflows/checks.yml"


class TrustProfile(NamedTuple):
    """How ONE forge proves that the CI which judged a pull request is trusted.

    The property the profile must deliver: **a pull request must not be able
    to weaken the CI that judges it.**

    Forgejo cannot use GitHub's route: `uses: <owner>/<repo>/.forgejo/workflows/x.yml@main`
    fails at plan time before any job is created, and
    `uses: ./...` executes the pull request's own copy — the shape the rule
    exists to forbid. It carries no `referenced_workflows` field on ANY
    endpoint, so GitHub's resolved-reusable proof has no input to satisfy and
    cannot simply be "widened" to fit.

    What Forgejo has instead is `pull_request_target`, which is DEFINED to take
    the workflow file from the BASE ref (see the Forgejo documentation for
    that event). That is the same guarantee by a different mechanism: the
    base copy of the workflow executes while the job's own checkout is the
    pull request head.
    So the binding is verified against the base SHA instead of a resolved
    reusable SHA: the file-existence probe targets the launcher at the base
    commit, and an identity job is proved by belonging to this run.

    `pull_request_target` is the known GitHub footgun, so this profile is only
    sound if the runners that execute the checks workflow hold no
    write-capable credential: no repository write token and no forge admin
    credential. The workflow's checkout should take no credential
    (`persist-credentials: false`); a read-only token may reach the checkout
    step alone. **If a write-capable secret is ever added to those runners,
    this profile has to be revisited in the same change.**
    """

    event: str
    launcher_path: str


FORGEJO_TRUST_PROFILE = TrustProfile(
    event="pull_request_target",
    launcher_path=FORGEJO_LAUNCHER_PATH,
)
PRODUCT_TRUST_PROFILE = TrustProfile(
    event="pull_request",
    launcher_path=FORGEJO_LAUNCHER_PATH,
)

#: Keyed by the forge name in the repository catalog. A forge absent from this
#: table has no trust proof, which is not the same as having a weak one: the
#: caller must refuse rather than pick a neighbour's profile.
TRUST_PROFILES: dict[str, TrustProfile] = {
    "forgejo": FORGEJO_TRUST_PROFILE,
}

# Job names the checks workflow owns for the CI evidence provider.
PROFILE_JOBS = ("c1", "c2", "c3")

# Fixed strength order. No aliases, no substring matching, no lexical
# ordering: the table is the whole definition.
PROFILE_STRENGTH = {"checks/c1": 3, "checks/c2": 2, "checks/c3": 1}
# The full-manifest profile. The narrowing rule: in a
# repository whose CI narrows every non-candidate PR head
# (`catalog.merge_candidate_enabled`), a merge requires it whatever the
# change class; a narrowed run reports `checks/c2` and can never satisfy it.
STRONGEST_PROFILE = max(PROFILE_STRENGTH, key=PROFILE_STRENGTH.__getitem__)
# The one narrowed profile a `label-v1` non-candidate head runs. A trusted
# success at this profile is review evidence and, under `train-v2`,
# admission evidence for a batch. It never satisfies a merge: merge still
# requires `STRONGEST_PROFILE`.
NARROWED_PROFILE = "checks/c2"
# The identity job a deploy of `main` requires by name: the strongest profile,
# the same job the gate's merge evidence requires (`checks/c1`). The deploy step
# reads it from here instead of keeping a second copy of the name. On a push
# to main that job passes either on a full run of its own or on a verified
# exact-tree reuse of the pull request's full run (checks.yml `reuse`).
STRONGEST_PROFILE_JOB = STRONGEST_PROFILE.partition("/")[2]
PRODUCT_JOB_PROFILES = {"checks": "product/checks"}
#: The aggregate identity jobs. They fail whenever a needed leaf fails, so a
#: failure shared there does not prove that a batch reproduced the base's own
#: failure; only a shared concrete leaf does. `train_v2`'s base-red comparison
#: subtracts these names before it decides a match.
IDENTITY_JOB_NAMES = frozenset({STRONGEST_PROFILE_JOB, NARROWED_PROFILE.partition("/")[2]})


def admits_narrowed_run(evidence: dict[str, object] | None) -> bool:
    """Whether *evidence* is a trusted narrowed run a gate step may act on.

    A `label-v1` repository runs `checks/c2` for every non-candidate head,
    including one whose change class requires `checks/c1`, so the evaluator
    reports ``CI_PROFILE_INSUFFICIENT``. This predicate says that run was
    trusted and its identity job succeeded, which is enough for snapshot
    review and for a `train-v2` admission. It grants no merge: the
    merge keeps requiring ``STRONGEST_PROFILE``.
    """
    if not isinstance(evidence, dict):
        return False
    return (
        evidence.get("result") == "UNAVAILABLE"
        and evidence.get("reason_code") == "CI_PROFILE_INSUFFICIENT"
        and evidence.get("required_profile") == STRONGEST_PROFILE
        and evidence.get("observed_profiles") == [NARROWED_PROFILE]
        and evidence.get("accepted_profile") == NARROWED_PROFILE
        and evidence.get("workflow_run_conclusion") == "success"
    )

def terminal_full_failure(evidence: dict[str, object] | None) -> bool:
    """A strict terminal full-C1 failure, not an advisory failure kind.

    FAIL is terminal in this owner's evaluator. Require its full profile,
    failure conclusion and selected native run; cancelled, skipped, pending
    and insufficient records do not qualify. Forgejo may omit an aggregate
    attempt, so preserve None instead of manufacturing one. This predicate
    grants no merge or prefix-publication authority.
    """
    if not isinstance(evidence, dict):
        return False
    run, attempt = evidence.get("workflow_run_id"), evidence.get("workflow_run_attempt")
    return (
        evidence.get("result") == "FAIL"
        and evidence.get("reason_code") == "CI_RUN_FAILED"
        and evidence.get("required_profile") == STRONGEST_PROFILE
        and evidence.get("observed_profiles") == [STRONGEST_PROFILE]
        and evidence.get("workflow_run_conclusion") == "failure"
        and evidence.get("retry_after_seconds") is None
        and type(run) is int and run > 0
        and (attempt is None or type(attempt) is int and attempt > 0)
    )


def complete_failed_job_names(
    jobs: dict[str, object] | None, run_id: int,
) -> frozenset[str] | None:
    """Complete native failure-job names, not an inferred failure kind.

    Native dependency-field presence proves inventory shape, not trusted
    execution. Unknown, partial, foreign or ambiguous rows cannot compare.
    """
    if (type(run_id) is not int or run_id <= 0 or not isinstance(jobs, dict)
            or jobs.get("native_graph_complete") is not True):
        return None
    rows = jobs.get("jobs")
    if (not isinstance(rows, list) or not rows or len(rows) > 128
            or type(jobs.get("total_count")) is not int or jobs["total_count"] != len(rows)):
        return None
    identities: set[int] = set()
    names: set[str] = set()
    failures: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        ident, name = row.get("id"), row.get("name")
        if (type(ident) is not int or ident <= 0 or ident in identities
                or type(row.get("run_id")) is not int or row["run_id"] != run_id
                or not isinstance(name, str) or not name or name in names):
            return None
        identities.add(ident)
        names.add(name)
        status, conclusion = row.get("status"), row.get("conclusion")
        if status == "queued" and row.get("forge_status") == "blocked":
            continue
        if status != "completed" or not isinstance(conclusion, str):
            return None
        if conclusion in FAILED_JOB_CONCLUSIONS:
            failures.add(name)
    return frozenset(failures)


def base_push_failed_jobs(
    *, base_sha: str, run: dict[str, object] | None, jobs: dict[str, object] | None,
    launcher_file_exists: bool | None,
) -> frozenset[str] | None:
    """Failed jobs of a trusted full push at canonical B, or unknown.

    The gate derives B from main and queries the canonical repository. A
    push loads its workflow at its own head B; task and run-endpoint commits
    must independently agree. This grants no merge or classification
    authority. A successful C1 has an empty failure signature.
    """
    if not isinstance(run, dict):
        return None
    run_id = run.get("id")
    if (launcher_file_exists is not True
            or run.get("event") != "push" or run.get("path") != FORGEJO_LAUNCHER_PATH
            or run.get("head_sha") != base_sha or run.get("push_head_sha") != base_sha
            or run.get("status") != "completed" or run.get("conclusion") not in ("success", "failure")
            or type(run_id) is not int or run_id <= 0):
        return None
    failures = complete_failed_job_names(jobs, run_id)
    if failures is None or not isinstance(jobs, dict):
        return None
    settled = with_job_lifecycle(run, jobs)
    if settled.get("status") != "completed":
        return None
    identity_jobs, observed, _accepted = _profile_jobs(jobs, settled)
    if observed != [STRONGEST_PROFILE]:
        return None
    identity = _identity_job_for(identity_jobs, STRONGEST_PROFILE)
    if identity is None:
        return None
    if identity.get("conclusion") == "success":
        return frozenset()
    if identity.get("conclusion") == "failure" and settled.get("conclusion") == "failure":
        return failures
    return None


# --- bounded failure evidence ----------------------------------------------
FAILURE_EVIDENCE_MAX_BYTES = 20_000
# In-memory bound on the redacted text kept from one job log before excerpt
# selection. 512 KiB is generous for the failure tail of any job in
# the repository's CI profile while keeping the fetch processing bounded.
JOB_LOG_MAX_REDACTED_BYTES = 512 * 1024
# Job conclusions whose logs are failure evidence. Cancelled is deliberately
# absent: a cancellation is not a failure, and the roll-up's own status job
# treats it that way (checks.yml).
FAILED_JOB_CONCLUSIONS = frozenset({"failure", "timed_out"})
# Shared vocabulary for "the exact-head run failed": the automerge park
# (`merge_open_prs._failed_run`) and the gate's CI rerun both read this, so
# the two cannot drift.
TERMINAL_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "start_failure"}
)
# Step conclusions counted as failed within a failed job.
FAILED_STEP_CONCLUSIONS = frozenset({"failure", "timed_out"})
# Terminal conclusions an identity job may carry while still being a real
# profile observation (as opposed to skipped, which means it did not run).
_RAN_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "cancelled",
        "timed_out",
        "neutral",
        "action_required",
        "stale",
        "start_failure",
    }
)

# Bounded retry hints, seconds (``retry_after_seconds``).
RETRY_QUEUED_SECONDS = 60
RETRY_IN_PROGRESS_SECONDS = 30
# Identity mismatches against the SELECTED run (head/base stale, or an
# internal head inconsistency) are races, not verdicts: on a busy Forgejo the
# run list changes between polls, and the next poll legitimately settles. A
# bounded hint keeps a caller waiting instead of reading "no retry" as
# "permanent". Truly terminal causes (malformed envelope, unknown status,
# unparseable jobs) keep retry_after_seconds None.
RETRY_STALE_SECONDS = 30

# How long a terminal run may go on owing its identity job before "not here
# yet" becomes "not coming". The eventual-consistency window is
# real and is documented where it is applied below; it needs a bounded
# end. 300 seconds is a generous upper bound on that window, and
# deliberately so: closing the window
# early turns a slow but honest run into a spurious terminal answer, while
# closing it late only costs a waiter a few more polls.
PROFILE_MATERIALIZATION_WINDOW_SECONDS = 300

# The same end, for the jobs payload that carries no rows at all.
# The window above is measured from the job rows' own ``completed_at``, so a
# response listing zero jobs dates nothing and that window can never close on
# it: a terminal run whose `/actions/runs/{id}/jobs` stays empty would be advised
# to retry every 30 seconds forever. The run record's ``updated_at`` is the
# only clock left there. 600 seconds is twice the jobs bound because that
# clock is the weaker instrument -- it is the row's last modification rather
# than a completion -- and because an empty jobs list can appear beside a
# failed conclusion for a few minutes (``job_ids: []``).
# Whether an empty list can be permanent is unproven, which is
# precisely why the bound must not depend on job rows.
EMPTY_JOBS_WINDOW_SECONDS = 600

# Run conclusions the empty-jobs clock is allowed to age. Both are produced
# by the forge; every other conclusion keeps waiting. The restriction
# is not caution for its own sake:
# The Forgejo backend translates any Forgejo status
# outside its table to ``("completed", "action_required")``, so a status that
# means "still moving" and is merely absent from that table arrives here
# looking terminal. With no job rows to contradict it, this set is the only
# thing stopping a clock from declaring a live run's identity job permanently
# absent.
_SETTLED_RUN_CONCLUSIONS = frozenset({"success", "failure"})

# The closed reason codes, including CI_RUN_SKIPPED for the
# "skipped unexpectedly" UNAVAILABLE cause, and CI_LANE_HEAD_MISMATCH for
# another: the lane's own local head is not the head its remote branch
# carries, so there is no head here the forge could have tested. The other
# codes name causes a run can have; this one is a property of the LANE, which
# is why it needed a code of its own rather than a borrowed one. It is deliberately the same word the
# gate's own ``REMOTE_HEAD_MISMATCH`` uses for the identical condition, so a
# reader who meets it here already knows which refusal documents the recovery.
CI_REASON_CODES = frozenset(
    {
        "CI_NOT_ONBOARDED",
        "CI_LANE_HEAD_MISMATCH",
        "CI_RUN_MISSING",
        "CI_RUN_QUEUED",
        "CI_RUN_IN_PROGRESS",
        "CI_RUN_CANCELED",
        "CI_RUN_SKIPPED",
        "CI_RUN_TIMED_OUT",
        "CI_RUN_FAILED",
        "CI_BASE_STALE",
        "CI_HEAD_STALE",
        "CI_PULL_REQUEST_MISMATCH",
        "CI_WORKFLOW_UNTRUSTED",
        "CI_PROFILE_PENDING",
        "CI_PROFILE_MISSING",
        "CI_PROFILE_INSUFFICIENT",
        "CI_RESULT_AMBIGUOUS",
        "CI_API_UNAVAILABLE",
        "CI_RESPONSE_INVALID",
    }
)

# --- one CI-evidence answer ---------------------------------------
# The codes that say the CI read itself could not be completed, not what a run
# showed: the listing or a payload failed, or the run named no single pull
# request. An answer built on one of them cannot decide.
_UNDECIDED_EVIDENCE_CODES = frozenset(
    {"CI_API_UNAVAILABLE", "CI_RESPONSE_INVALID", "CI_RESULT_AMBIGUOUS"}
)
#: The closed reasons of a CI-evidence answer, keyed by its state. ``accepted``
#: is snapshot-review and train-admission evidence only; merge authority stays
#: the exact-pair full-profile check in the merge approval itself.
#: ``unknown`` is a read that cannot decide: it is never accepted, and it is
#: never reported as a refusal of the head.
CI_ANSWER_REASONS: dict[str, frozenset[str]] = {
    "accepted": frozenset({"GREEN", "NARROWED_RUN"}),
    "rejected": (CI_REASON_CODES - _UNDECIDED_EVIDENCE_CODES) | frozenset({
        "RUN_BASE_OUTSIDE_RANGE", "EVIDENCE_BINDING_MISMATCH", "NARROWED_POLICY_OFF",
        "PR_CI_NOT_GREEN",
    }),
    "unknown": frozenset({
        "CI_EVIDENCE_UNAVAILABLE", "RECORDED_BASE_MISSING", "MAIN_TIP_MISSING",
        "RANGE_UNCLASSIFIED",
    }),
}


class CiEvidenceAnswer(NamedTuple):
    """One CI-evidence answer: a key of ``CI_ANSWER_REASONS`` and one of its reasons."""

    state: str
    reason: str


def ci_evidence_answer(
    ci_status: Mapping[str, object] | None,
    *,
    pull_request: int | None,
    head_sha: str | None,
    main_tip: str | None,
    recorded_base: str | None,
    narrowed_policy: bool,
    range_known: bool,
) -> CiEvidenceAnswer:
    """Whether *ci_status* lets a snapshot review or a train admission act.

    Every inspection path asks this one function, so the same PR, head,
    recorded base, run base and main tip give the same answer on all of them.
    *ci_status* is one ``_automerge_ci_status`` answer. The keywords are the
    inspect record's own identity: its PR, head and main's tip (None where the
    record does not bind them), the forge's recorded base (the head's fork
    point from main), whether CI narrows every non-candidate head
    (``catalog.merge_candidate_enabled``), and whether the range's change
    class is known. Pure: no git, no forge. In order:

    1. Green is accepted ``GREEN``. The merge re-reads the exact pair, so
       green needs no binding here.
    2. A CI read that could not be completed is unknown
       ``CI_EVIDENCE_UNAVAILABLE``.
    3. A trusted narrowed run (``admits_narrowed_run``) needs the policy, a
       SHA recorded base (else unknown ``RECORDED_BASE_MISSING``), and the
       record's PR and head with a judged base in [recorded base, main's
       tip]: the recorded base, main's tip, or a run base the ancestry proof
       accepted (``accepted_run_base``). Without the policy it is
       ``NARROWED_POLICY_OFF``, or unknown ``RANGE_UNCLASSIFIED`` when the
       range is unknown, because the class behind its insufficient profile
       may need less.
    4. ``CI_BASE_STALE`` without a SHA recorded base is unknown
       ``RECORDED_BASE_MISSING``; a run-base proof that could not run is
       unknown ``MAIN_TIP_MISSING``; a refused proof is
       ``RUN_BASE_OUTSIDE_RANGE``.
    5. ``CI_PROFILE_INSUFFICIENT`` on a successful run of an unknown range is
       unknown ``RANGE_UNCLASSIFIED``: the range classified as C1 only
       because it is unknown.
    6. Anything else is rejected with the evaluator's reason code, or
       ``PR_CI_NOT_GREEN``.
    """
    if not isinstance(ci_status, Mapping):
        return CiEvidenceAnswer("unknown", "CI_EVIDENCE_UNAVAILABLE")
    verdict = ci_status.get("verdict")
    if verdict == "green":
        return CiEvidenceAnswer("accepted", "GREEN")
    evidence = ci_status.get("ci_evidence")
    if not isinstance(evidence, dict):
        return CiEvidenceAnswer("unknown", "CI_EVIDENCE_UNAVAILABLE")
    raw_reason = evidence.get("reason_code")
    reason = raw_reason if isinstance(raw_reason, str) and raw_reason in CI_REASON_CODES else None
    if reason in _UNDECIDED_EVIDENCE_CODES:
        return CiEvidenceAnswer("unknown", "CI_EVIDENCE_UNAVAILABLE")
    recorded = (recorded_base if isinstance(recorded_base, str) and SHA_RE.fullmatch(recorded_base)
                else None)
    if admits_narrowed_run(evidence) and verdict == "unavailable":
        if not narrowed_policy:
            return (CiEvidenceAnswer("rejected", "NARROWED_POLICY_OFF") if range_known
                    else CiEvidenceAnswer("unknown", "RANGE_UNCLASSIFIED"))
        if recorded is None:
            return CiEvidenceAnswer("unknown", "RECORDED_BASE_MISSING")
        bases = {base for base in (recorded, main_tip, ci_status.get("accepted_run_base"))
                 if isinstance(base, str) and SHA_RE.fullmatch(base)}
        bound = (type(pull_request) is int and evidence.get("pull_request") == pull_request
                 and isinstance(head_sha, str) and evidence.get("head_sha") == head_sha
                 and evidence.get("base_sha") in bases)
        return (CiEvidenceAnswer("accepted", "NARROWED_RUN") if bound
                else CiEvidenceAnswer("rejected", "EVIDENCE_BINDING_MISMATCH"))
    if reason == "CI_BASE_STALE":
        proof = ci_status.get("run_base_proof")
        if recorded is None:
            return CiEvidenceAnswer("unknown", "RECORDED_BASE_MISSING")
        if proof == "unknown":
            return CiEvidenceAnswer("unknown", "MAIN_TIP_MISSING")
        if proof == "refused":
            return CiEvidenceAnswer("rejected", "RUN_BASE_OUTSIDE_RANGE")
    if (reason == "CI_PROFILE_INSUFFICIENT" and not range_known
            and evidence.get("workflow_run_conclusion") == "success"):
        return CiEvidenceAnswer("unknown", "RANGE_UNCLASSIFIED")
    return CiEvidenceAnswer("rejected", reason or "PR_CI_NOT_GREEN")


# --- deterministic sanitization and redaction -------------------------------
# A closed pattern set, applied in this fixed order. Every pattern matches a
# concrete secret shape; nothing here is heuristic, because determinism is
# the contract: the same log must yield the same record every time.

# ANSI control sequences (CSI, OSC, and bare escape) and DEL — "untrusted
# terminal escapes".
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|[A-Za-z])"
)
# Remaining control characters (keeps tab, newline, carriage return).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# GitHub classic and fine-grained personal access tokens, plus org/user
# install tokens, as printed by tooling that echoes them.
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{82,})\b"
)
# Private key blocks in PEM form.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
# Userinfo credentials embedded in URLs (https://user:pass@host/...). The
# scheme is kept; only the credential is replaced.
_URL_CREDENTIALS_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")

_REDACTION_MARKER = "[REDACTED]"


def sanitize_and_redact(text: str) -> tuple[str, int]:
    """Strip terminal escapes/control characters, then redact secret shapes.

    Returns ``(safe_text, redaction_count)``. Fixed pattern order makes the
    output deterministic: same input, same bytes, same count. The count is
    the number of substituted matches across all patterns. Redaction happens
    before any truncation, so a secret that matched is gone from the output
    regardless of where the byte cap later cuts.
    """
    clean = _ANSI_ESCAPE_RE.sub("", text)
    clean = _CONTROL_RE.sub("", clean)

    def _counted(
        pattern: re.Pattern[str], value: str, replacement: str
    ) -> tuple[str, int]:
        hits = len(pattern.findall(value))
        return pattern.sub(replacement, value), hits

    count = 0
    for pattern, replacement in (
        (_GITHUB_TOKEN_RE, _REDACTION_MARKER),
        (_PRIVATE_KEY_RE, _REDACTION_MARKER),
        (_URL_CREDENTIALS_RE, r"\1" + _REDACTION_MARKER + "@"),
    ):
        clean, hits = _counted(pattern, clean, replacement)
        count += hits
    return clean, count


def redact_log(text: str) -> tuple[str, int]:
    """Sanitize + redact one job log and cap it to the in-memory bound.

    The cap keeps only the tail of the redacted text: failure detail is at
    the end of a log, and a cut after redaction can never recreate a match
    of the closed pattern set (a truncated token is shorter than every
    pattern requires).
    """
    clean, count = sanitize_and_redact(text)
    encoded = clean.encode("utf-8")
    if len(encoded) <= JOB_LOG_MAX_REDACTED_BYTES:
        return clean, count
    tail = encoded[-JOB_LOG_MAX_REDACTED_BYTES:]
    return tail.decode("utf-8", errors="replace"), count


# --- profile strength ------------------------------------------------------


def profile_meets_requirement(required: str, observed: str) -> bool:
    """``observed`` satisfies ``required`` when equal or stronger.

    Closed by the PROFILE_STRENGTH table; a value outside the table is a
    programming error, not an unknown. Asserting rather than returning False
    keeps a misspelled profile from silently weakening the check.
    """
    return PROFILE_STRENGTH[observed] >= PROFILE_STRENGTH[required]


# --- run selection ----------------------------------------------------------


def select_newest_run(
    workflow_runs: object,
    head_sha: str | None = None,
    *,
    event: str | None = None,
    path: str | None = None,
    pull_request: int | None = None,
) -> dict[str, object] | None:
    """The best run to evaluate for one exact head.

    Selection order: (1) the newest TERMINAL run (status ``completed`` with
    a ``success``/``failure`` conclusion) that matches *head_sha*; else (2)
    the newest run that is NOT ``completed`` (queued/in_progress/waiting — a
    verdict not yet reached); else (3) the newest run of any remaining state
    (``completed`` with a non-terminal conclusion, e.g. ``skipped`` or
    ``cancelled``); else None.

    ``id`` is monotonic, so "newest" is the deterministic total order the
    forge assigns; ties cannot occur. Returns None for an empty list or a
    list entry that is not a dict; the caller already validated the
    ``workflow_runs`` envelope shape.

    ``head_sha`` is the client-side narrowing the Forgejo backend cannot do
    server-side (``/actions/tasks`` accepts no head filter). Without it the
    selection would pick the newest run of the WHOLE repository, and on a
    busy repo the evaluator would be handed someone else's run — churning
    ``CI_HEAD_STALE``/``CI_BASE_STALE`` on a lane whose own run settled
    minutes ago. Product policy also supplies its exact workflow event, path,
    and pull-request number because that workflow legitimately handles push
    and manual events for the same commit. These identity filters run before
    terminal/newest precedence, so an unrelated newer run cannot hide the
    product pull-request run. A run whose ``head_sha`` field is missing or differs is
    never selected: the evaluator's head binding would refuse it
    anyway, and refusing at selection is what keeps the reason codes about
    the lane's own run.

    Terminal preference exists for the same reason: a re-triggered run for
    the same head is still the same commit, and a settled verdict for that
    head is proof regardless of how many newer runs are still in flight.

    Tier (2) sits ahead of tier (3) because ``checks.yml`` also
    fires ``pull_request_target`` on the PR's ``closed`` event so it can
    cancel an in-flight run through the workflow's concurrency group
   . After a merge, that close-time run's jobs are all
    skipped, and it completes — with a newer id — before the real PR run for
    the same head finishes. Falling straight to "newest of any state" would
    hand the evaluator that skipped run and answer CI_RUN_SKIPPED for a head
    whose real run is still in progress. Preferring a still-running run over
    a completed-but-non-terminal one closes that race; a head that is
    genuinely cancelled or skipped, with no in-progress run left, still
    falls through to tier (3) and reports correctly.
    """
    if not isinstance(workflow_runs, list):
        return None
    matched: list[dict[str, object]] = []
    for entry in workflow_runs:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("id"), int):
            continue
        if head_sha is not None and entry.get("head_sha") != head_sha:
            continue
        if event is not None and entry.get("event") != event:
            continue
        if path is not None and entry.get("path") != path:
            continue
        if pull_request is not None:
            pr_records = entry.get("pull_requests")
            if not isinstance(pr_records, list) or len(pr_records) != 1:
                continue
            pr_record = pr_records[0]
            if not isinstance(pr_record, dict):
                continue
            if pr_record.get("number") != pull_request:
                continue
        matched.append(entry)

    def _newest(candidates: list[dict[str, object]]) -> dict[str, object] | None:
        best: dict[str, object] | None = None
        best_id = -1
        for entry in candidates:
            raw_id = entry.get("id")
            if isinstance(raw_id, int) and raw_id > best_id:
                best = entry
                best_id = raw_id
        return best

    terminal = [
        entry
        for entry in matched
        if entry.get("status") == "completed"
        and entry.get("conclusion") in ("success", "failure")
    ]
    if terminal:
        return _newest(terminal)

    in_flight = [entry for entry in matched if entry.get("status") != "completed"]
    if in_flight:
        return _newest(in_flight)

    return _newest(matched)


# Run conclusions that end a run without a verdict on the head, so a newer
# run that ended so still supersedes an older settled one.
# `action_required` is also what the Forgejo backend reports for a Forgejo
# status it does not know. `skipped` is excluded: an unrelated metadata run
# skips every job and was never meant to judge the head.
_UNJUDGED_CONCLUSIONS = frozenset({"cancelled", "timed_out", "action_required"})


def newer_run_in_flight(
    workflow_runs: object,
    selected: dict[str, object] | None,
    head_sha: str | None,
    *,
    event: str | None = None,
    path: str | None = None,
    pull_request: int | None = None,
) -> dict[str, object] | None:
    """The newest run of *head_sha*, newer than *selected*, that supersedes it.

    None when there is none, or *selected* has no integer id. Applied only
    after *selected* settled with a profile too weak for the requirement
    (the narrowing rule): a newer in-flight run of the same head may still satisfy it, so it
    is the honest answer, and terminal preference must not hide it. With
    none in flight, the newest newer run that ended without judging the head
    (``_UNJUDGED_CONCLUSIONS``) is the answer instead: terminal
    preference ranks only success and failure, so a cancelled full run would
    stay hidden behind the narrowed success it was started to replace. The
    same identity filters as :func:`select_newest_run`.
    """
    selected_id = selected.get("id") if isinstance(selected, dict) else None
    if not isinstance(selected_id, int) or not isinstance(workflow_runs, list):
        return None
    newer = [
        entry for entry in workflow_runs
        if isinstance(entry, dict) and isinstance(entry.get("id"), int)
        and entry["id"] > selected_id
    ]
    in_flight = select_newest_run(
        [entry for entry in newer if entry.get("status") != "completed"],
        head_sha, event=event, path=path, pull_request=pull_request,
    )
    if in_flight is not None:
        return in_flight
    return select_newest_run(
        [entry for entry in newer if entry.get("status") == "completed"
         and entry.get("conclusion") in _UNJUDGED_CONCLUSIONS],
        head_sha, event=event, path=path, pull_request=pull_request,
    )


def selection_metadata(
    workflow_runs: object,
    head_sha: str | None,
    run: dict[str, object] | None,
    *,
    event: str | None = None,
    path: str | None = None,
    pull_request: int | None = None,
) -> dict[str, object]:
    """Selection metadata for a ci-evidence.v1 record, advisory only.

    Describes WHICH runs were considered for the exact head and which was
    chosen, so a caller can see re-triggers and superseded runs instead of a
    single ``workflow_run_id`` that jumps between polls. Never consumed by
    the gate: ``evaluate_evidence`` has already run when this is attached,
    so these keys cannot feed a decision. When a terminal run for the head
    exists but the evidence is UNAVAILABLE (e.g. the selected in-flight run
    has not settled, or the terminal run failed an identity check), the
    newest terminal candidate's id and conclusion are reported as the
    advisory ``terminal_candidate_*`` pair — "an earlier run for this exact
    head already went green" is the one fact a waiting caller most needs.
    """
    ids: list[int] = []
    terminal_id: int | None = None
    terminal_conclusion: str | None = None
    if isinstance(workflow_runs, list):
        newest_terminal_id = -1
        for entry in workflow_runs:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id")
            if not isinstance(raw_id, int):
                continue
            if head_sha is not None and entry.get("head_sha") != head_sha:
                continue
            if event is not None and entry.get("event") != event:
                continue
            if path is not None and entry.get("path") != path:
                continue
            if pull_request is not None:
                pr_records = entry.get("pull_requests")
                if not isinstance(pr_records, list) or len(pr_records) != 1:
                    continue
                pr_record = pr_records[0]
                if not isinstance(pr_record, dict):
                    continue
                if pr_record.get("number") != pull_request:
                    continue
            ids.append(raw_id)
            if (
                entry.get("status") == "completed"
                and entry.get("conclusion") in ("success", "failure")
                and raw_id > newest_terminal_id
            ):
                newest_terminal_id = raw_id
                terminal_conclusion = (
                    entry.get("conclusion")
                    if isinstance(entry.get("conclusion"), str)
                    else None
                )
        if newest_terminal_id != -1:
            terminal_id = newest_terminal_id
    meta: dict[str, object] = {
        "candidate_run_ids": sorted(ids),
        "selected_run_id": (
            run.get("id") if isinstance(run, dict) and isinstance(run.get("id"), int) else None
        ),
    }
    if terminal_id is not None:
        meta["terminal_candidate_run_id"] = terminal_id
        meta["terminal_candidate_conclusion"] = terminal_conclusion
    return meta


def needs_jobs_fetch(run: dict[str, object]) -> bool:
    """Whether the run's jobs must be fetched before evaluation can finish.

    A run that is still queued/in progress, or already ended cancelled/
    skipped/timed out, is decided by its own status/conclusion; jobs only
    matter once a terminal run reaches the profile-job stage.

    A run Forgejo reports ``blocked`` translates to ``queued``, but
    it may be stranded behind a failed job that nothing will ever promote to a
    conclusion. Its jobs are read once so ``with_job_lifecycle`` can tell; a
    plain ``queued`` or ``waiting`` run is still decided without a read.
    """
    if run.get("forge_status") == "blocked":
        return True
    return run.get("status") == "completed" and run.get("conclusion") not in (
        "cancelled",
        "skipped",
        "timed_out",
    )


def with_job_lifecycle(run: dict[str, object], jobs: dict[str, object]) -> dict[str, object]:
    """Do not mistake Forgejo's aggregate failure for workflow completion.

    A forge can return run=failure while a sibling job is
    running. The complete run-jobs endpoint owns that remaining-work fact;
    task pages and the run's stopped timestamp cannot prove completion.
    Keep conclusions authoritative only once all returned jobs are terminal.

    When every unfinished row is Forgejo-``blocked``, the run keeps
    its own status so ``evaluate_evidence`` can age a stranded aggregate;
    inside its window that still reads as pending.

    A run Forgejo itself reports ``blocked`` whose only unfinished
    rows are ``blocked`` behind a failed job has no work left that could
    change its outcome, so it reads as the failed run it is. Forgejo leaves
    such a run without a conclusion (a lost worker), and
    ``evaluate_evidence`` would otherwise answer ``CI_RUN_QUEUED`` for ever.
    """
    if _stranded_by_failed_job(run, jobs):
        return {**run, "status": "completed", "conclusion": "failure"}
    if run.get("status") != "completed":
        return run
    rows = jobs.get("jobs")
    if not isinstance(rows, list) or not rows:
        return run  # Existing evidence validation rejects absent profile jobs.
    if all(isinstance(row, dict) for row in rows):
        non_completed = [row for row in rows if row.get("status") != "completed"]
        if non_completed and len(non_completed) < len(rows) and all(
            row.get("forge_status") == "blocked" for row in non_completed
        ):
            return run
    statuses = [row.get("status") if isinstance(row, dict) else None for row in rows]
    if any(status not in ("queued", "in_progress", "completed") for status in statuses):
        return {**run, "status": "unknown", "conclusion": None}
    if any(status != "completed" for status in statuses):
        return {**run, "status": "in_progress", "conclusion": None}
    return run


def _stranded_by_failed_job(run: dict[str, object], jobs: dict[str, object]) -> bool:
    """True when a Forgejo-``blocked`` run waits only behind a failed job.

    True only when: the run's own raw status (``forge_status``) is ``blocked``;
    every row the forge lists for it is a dict; at least one row is
    ``completed`` with a conclusion in ``FAILED_JOB_CONCLUSIONS``; and every
    other row is ``completed`` or Forgejo-``blocked`` (a dependent that waits
    on the failed job). A ``waiting``, ``queued`` or ``running`` row is work
    that may still run, so it keeps the run pending. An empty or unreadable
    list is not a measurement, so every such case answers False. A run whose
    own status is not ``blocked`` never matches: the blocked-aggregate rule owns the shapes that
    arrive as ``completed``.
    """
    if run.get("forge_status") != "blocked":
        return False
    rows = jobs.get("jobs")
    if not isinstance(rows, list) or not rows:
        return False
    if not all(isinstance(row, dict) for row in rows):
        return False
    failed = [
        row
        for row in rows
        if row.get("status") == "completed"
        and isinstance(row.get("conclusion"), str)
        and row["conclusion"] in FAILED_JOB_CONCLUSIONS
    ]
    if not failed:
        return False
    return all(
        row.get("status") == "completed" or row.get("forge_status") == "blocked"
        for row in rows
    )


def run_status_supports_progress(run: dict[str, object]) -> bool:
    """Whether *run* can still produce failed jobs worth reporting early.

    Only an in-progress run: a queued run has not started a job, a completed
    run is decided by the terminal evidence path, and any other status is
    not this record's subject.
    """
    return run.get("status") == "in_progress"


def progress_trust_ok(
    run: dict[str, object],
    trust_profile: TrustProfile,
    base_sha: str | None,
    head_sha: str | None,
    pull_request: int | None = None,
) -> bool:
    """Whether an advisory run passes the checks that gate evidence release.

    ci-failure.v1 is released only after ci-evidence.v1 accepted the run
    identity. ci-progress.v1 is released earlier, while the run is still
    in_progress or failed against an older base, so this predicate applies
    the same checks itself: the run belongs to the
    trusted launcher's event and path, binds exactly one pull request, and
    that pull request's number/base/head tuple matches the caller's inputs.
    Any unreadable or mismatching field fails closed -- an unproven run
    carries no progress evidence and no log content.
    """
    if run.get("event") != trust_profile.event:
        return False
    if run.get("path") != trust_profile.launcher_path:
        return False
    pr_records = run.get("pull_requests")
    if not isinstance(pr_records, list) or len(pr_records) != 1:
        return False
    pr_record = pr_records[0]
    if not isinstance(pr_record, dict):
        return False
    if pull_request is not None and pr_record.get("number") != pull_request:
        return False
    head = pr_record.get("head")
    base = pr_record.get("base")
    run_head = head.get("sha") if isinstance(head, dict) else None
    run_base = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(run_head, str) or not isinstance(run_base, str):
        return False
    if run_head != run.get("head_sha"):
        return False
    if head_sha is not None and run.get("head_sha") != head_sha:
        return False
    if base_sha is not None and run_base != base_sha:
        return False
    return True


# --- evidence assembly ------------------------------------------------------


def evaluate_evidence(
    *,
    repository: str,
    trust_profile: TrustProfile = FORGEJO_TRUST_PROFILE,
    pull_request: int | None,
    base_sha: str | None,
    head_sha: str | None,
    required_profile: str | None,
    run: dict[str, object] | None,
    jobs: dict[str, object] | None,
    launcher_file_exists: bool | None,
    api_failure: bool,
    malformed_response: bool,
    observed_at: str,
    head_pushed: bool = True,
    awaiting_task_rows: object = False,
    job_profiles: dict[str, str] | None = None,
    require_successful_run: bool = False,
) -> dict[str, object]:
    """Evaluate one exact lane against one Actions run; return ci-evidence.v1.

    All identity inputs are gate-derived (closed repository, exact PR,
    exact current base and pushed head, policy's required profile). ``run``
    is the newest run of the trusted launcher for the exact pushed head
    (None when the forge reported none), ``jobs`` its parsed jobs envelope
    when fetched, ``launcher_file_exists`` the tri-state outcome of the
    launcher-at-the-trusted-commit file probe (None = not performed),
    ``api_failure`` whether a required fetch failed outright (UNAVAILABLE /
    ``CI_API_UNAVAILABLE``, never a guess about the run), and
    ``malformed_response`` whether a required payload was unparseable or
    outside the expected envelope (UNAVAILABLE / ``CI_RESPONSE_INVALID`` —
    the two are separate causes and stay distinguishable).

    ``head_pushed`` is whether the lane's local head is the head its own
    remote branch carries. False means no Actions call was made at all, so
    ``run is None`` here says nothing about the forge, and reporting it as a
    missing run told a waiter to poll for something that could not arrive
   . It defaults True because every caller that has a run to hand
    has one for a pushed head by construction.

    ``awaiting_task_rows`` is the backend's literal True observation that
    exact-head runs exist but their task rows are absent from its bounded
    listing. No run is selected in that state. An unrecognized marker cannot
    prove a temporary gap and receives no retry hint.

    The decision tree is ordered; the order is part of the contract and is
    table-tested. Stages never reached do no work: a queued run is decided
    before any workflow-trust or base/head check.

    Caller semantics of the outcome: ``PASS``/``FAIL`` are terminal; a
    bounded ``retry_after_seconds`` (run materialization, queued/in-progress,
    and the head/base stale races) means a waiter keeps polling, not that a
    caller re-reads it in a loop; a null hint on ``UNAVAILABLE`` means the
    cause is structural — read ``reason_code`` for which.
    """
    job_profiles = job_profiles or {name: f"checks/{name}" for name in PROFILE_JOBS}
    if api_failure:
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=required_profile,
            run=run,
            result="UNAVAILABLE",
            reason_code="CI_API_UNAVAILABLE",
            observed_profiles=[],
            accepted_profile=None,
            job_ids=[],
            job_timings=[],
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if malformed_response:
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=required_profile,
            run=run,
            result="UNAVAILABLE",
            reason_code="CI_RESPONSE_INVALID",
            observed_profiles=[],
            accepted_profile=None,
            job_ids=[],
            job_timings=[],
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if required_profile is None:
        # NOT_RUN: CI explicitly not required (not_onboarded or policy
        # requires no profile). No run was consulted and none is needed.
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=None,
            run=None,
            result="NOT_RUN",
            reason_code="CI_NOT_ONBOARDED",
            observed_profiles=[],
            accepted_profile=None,
            job_ids=[],
            job_timings=[],
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if not head_pushed:
        # The lane's local head is not the head its remote branch carries, so
        # the forge was never asked about it and there is no run to wait for.
        #
        # This is not ``run is None`` / ``CI_RUN_MISSING`` with a bounded
        # retry: that would tell a waiter to keep polling for a run that
        # cannot ever appear, because nothing has published this head. The
        # record also carries the open ``pull_request`` through unchanged,
        # because ``derive_verdict`` reads a null pull request first and
        # would answer the terminal ``no_pr`` ("no open pull request") about
        # a lane whose pull request is open. A lane behind its branch
        # reports a named condition, never ``no_pr``. A forge can merge main
        # into open PR branches when main advances, so this lane is common
        # after any merge.
        #
        # Naming the condition is what makes it actionable. The retry hint
        # stays null on purpose: waiting is precisely the wrong move, and a
        # bounded hint is how this record tells a waiter to keep going. The
        # lane has to be brought level with its branch first -- the same
        # condition, and the same recovery, as ``REMOTE_HEAD_MISMATCH``.
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=required_profile,
            run=None,
            result="UNAVAILABLE",
            reason_code="CI_LANE_HEAD_MISMATCH",
            observed_profiles=[],
            accepted_profile=None,
            job_ids=[],
            job_timings=[],
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if run is None:
        # The backend can observe an exact-head run before its tasks appear.
        # That incomplete join must not start the queue's missing-run clock.
        # Neither empty case grants a verdict; malformed markers cannot prove
        # the temporary gap that justifies a retry.
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=required_profile,
            run=None,
            result="UNAVAILABLE",
            reason_code=(
                "CI_RUN_MISSING" if awaiting_task_rows is False else "CI_RESULT_AMBIGUOUS"
            ),
            observed_profiles=[],
            accepted_profile=None,
            job_ids=[],
            job_timings=[],
            observed_at=observed_at,
            retry_after_seconds=(
                RETRY_IN_PROGRESS_SECONDS if isinstance(awaiting_task_rows, bool) else None
            ),
        )
    status = run.get("status")
    if status == "queued":
        return _unavailable(
            run,
            "CI_RUN_QUEUED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            RETRY_QUEUED_SECONDS,
        )
    if status == "in_progress":
        return _unavailable(
            run,
            "CI_RUN_IN_PROGRESS",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            RETRY_IN_PROGRESS_SECONDS,
        )
    if status != "completed":
        # A status outside queued/in_progress/completed is a response in a
        # shape the closed mapping does not know.
        return _unavailable(
            run,
            "CI_RESPONSE_INVALID",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    conclusion = run.get("conclusion")
    # Run-level early outs, before the trust checks: a cancelled, skipped or
    # timed-out run never executed the trusted workflow, and naming the
    # conclusion is the precise cause (each is UNAVAILABLE).
    if conclusion == "cancelled":
        return _unavailable(
            run,
            "CI_RUN_CANCELED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    if conclusion == "skipped":
        return _unavailable(
            run,
            "CI_RUN_SKIPPED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    if conclusion == "timed_out":
        return _unavailable(
            run,
            "CI_RUN_TIMED_OUT",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    if require_successful_run and conclusion != "success":
        if conclusion == "failure":
            return _record(
                repository=repository,
                pull_request=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                required_profile=required_profile,
                run=run,
                result="FAIL",
                reason_code="CI_RUN_FAILED",
                observed_profiles=[],
                accepted_profile=None,
                job_ids=[],
                job_timings=[],
                observed_at=observed_at,
                retry_after_seconds=None,
            )
        return _unavailable(
            run,
            "CI_RESPONSE_INVALID",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    # Trusted launcher identity (the run belongs to the authoritative
    # repository — true by construction of the query — and the event is the
    # approved PR event).
    if run.get("event") != trust_profile.event:
        return _unavailable(
            run,
            "CI_WORKFLOW_UNTRUSTED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    if run.get("path") != trust_profile.launcher_path:
        return _unavailable(
            run,
            "CI_WORKFLOW_UNTRUSTED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    # The launcher-at-the-trusted-commit file proof. `pull_request_target`
    # is defined to load the workflow file from the BASE ref, so the probe
    # targets the launcher at the base commit — that is what turns the
    # definition into evidence, and it replaces GitHub's resolved-reusable
    # proof (Forgejo emits no `referenced_workflows` on any endpoint, so
    # there is no second file to resolve).
    if launcher_file_exists is not True:
        # None (probe skipped) fails closed: trust cannot be proven.
        return _unavailable(
            run,
            "CI_WORKFLOW_UNTRUSTED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    # Exact PR/base/head binding: the run's own pull-request record is
    # the pull number and base/head pair the run actually tested.
    pr_records = run.get("pull_requests")
    if not isinstance(pr_records, list) or len(pr_records) != 1:
        return _unavailable(
            run,
            "CI_RESULT_AMBIGUOUS",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    pr_record = pr_records[0]
    if not isinstance(pr_record, dict):
        return _unavailable(
            run,
            "CI_RESPONSE_INVALID",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    if pull_request is not None and pr_record.get("number") != pull_request:
        return _unavailable(
            run,
            "CI_PULL_REQUEST_MISMATCH",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    head = pr_record.get("head")
    base = pr_record.get("base")
    run_head = head.get("sha") if isinstance(head, dict) else None
    run_base = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(run_head, str) or not isinstance(run_base, str):
        return _unavailable(
            run,
            "CI_RESPONSE_INVALID",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    if run_head != run.get("head_sha"):
        return _unavailable(
            run,
            "CI_RESPONSE_INVALID",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            RETRY_STALE_SECONDS,
        )
    if head_sha is not None and run.get("head_sha") != head_sha:
        return _unavailable(
            run,
            "CI_HEAD_STALE",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            RETRY_STALE_SECONDS,
        )
    if base_sha is not None and run_base != base_sha:
        return _unavailable(
            run,
            "CI_BASE_STALE",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            RETRY_STALE_SECONDS,
        )
    # Profile-job identity (required profile job exists with the
    # expected identity; no weaker or unrelated job is substituted).
    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
        return _unavailable(
            run,
            "CI_RESPONSE_INVALID",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            [],
            None,
            observed_at,
            None,
        )
    identity_jobs, observed, accepted = _profile_jobs(jobs, run, job_profiles)
    if observed is None:
        return _unavailable(
            run,
            "CI_RESULT_AMBIGUOUS",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            observed_profiles=[],
            accepted_profile=None,
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if not observed:
        raw_jobs = jobs["jobs"]
        profile_rows = [
            entry
            for entry in raw_jobs
            if isinstance(entry, dict)
            and entry.get("name") in job_profiles
            and _profile_satisfies(required_profile, job_profiles[str(entry["name"])])
        ]
        # Forgejo can mark the aggregate run terminal before its dependent
        # identity job appears in /actions/runs/{id}/jobs. A run can do this
        # for a short time: a terminal CI_PROFILE_MISSING would make a watcher
        # stop, although the same read later produces the real red c1 envelope.
        # The window can be longer: minutes before the identity job
        # materializes, so a short delay is a floor, not the window's size. No profile row, or a
        # matching row that is still non-terminal, is therefore an
        # eventual-consistency window. A profile row that is skipped or
        # belongs to another run is real missing evidence and remains
        # terminal.
        #
        # That window is bounded. An unbounded "it may still be coming"
        # is indistinguishable from "it is never coming", and the second
        # case is real: when the workflow revision that judged this head
        # named its only job something outside PROFILE_JOBS -- a checks
        # workflow that names its only job ``validate`` and concludes
        # failure -- no c1/c2/c3 row will ever appear, and every read would
        # answer CI_PROFILE_PENDING with a 30-second retry hint forever. Once the
        # aggregate run is terminal (proved above: this branch is only
        # reached with status ``completed``), every job the forge lists for
        # it is itself completed, and the newest of those completions is
        # older than PROFILE_MATERIALIZATION_WINDOW_SECONDS, there is
        # nothing left to wait for and the answer is the terminal
        # CI_PROFILE_MISSING. Inside the window the behaviour above is
        # unchanged.
        #
        # The empty-jobs bound closes the same hole from the other side: the first bound is
        # computed from the job rows, so a jobs payload with NO rows would keep the
        # unbounded retry hint the paragraph above removes. The empty payload
        # is a real state -- a run can answer ``job_ids: []`` beside
        # conclusion failure for minutes -- and nothing proves it cannot
        # be permanent, so it is dated from the run's own ``updated_at``
        # against the more generous EMPTY_JOBS_WINDOW_SECONDS instead.
        materializing = not profile_rows or (
            bool(identity_jobs)
            and any(job.get("status") != "completed" for job in identity_jobs)
        )
        # a forge interruption can leave the aggregate identity job
        # ``blocked`` after every dependency concluded. The window below can
        # never close on that row, so a stale blocked aggregate is terminal.
        #
        # a run Forgejo itself holds ``blocked`` behind a failed job
        # (``with_job_lifecycle`` normalized it to ``completed``/``failure``)
        # has an aggregate that is blocked on that job, not on a late sibling:
        # nothing is coming, so it is terminal at once, without the blocked-aggregate age.
        if materializing and (
            _stranded_by_failed_job(run, jobs)
            or _blocked_aggregate_terminal(jobs, run, observed_at, job_profiles)
        ):
            return _record(
                repository=repository,
                pull_request=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                required_profile=required_profile,
                run=run,
                result="FAIL",
                reason_code="CI_RUN_FAILED",
                observed_profiles=[],
                accepted_profile=None,
                job_ids=[job["id"] for job in identity_jobs],
                job_timings=[_job_timing(job) for job in identity_jobs],
                observed_at=observed_at,
                retry_after_seconds=None,
            )
        if materializing and _materialization_window_closed(
            jobs, run, observed_at
        ):
            materializing = False
        return _unavailable(
            run,
            "CI_PROFILE_PENDING" if materializing else "CI_PROFILE_MISSING",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            observed_profiles=[],
            accepted_profile=None,
            observed_at=observed_at,
            retry_after_seconds=(
                RETRY_IN_PROGRESS_SECONDS if materializing else None
            ),
        )
    observed_profile = observed[0]
    if not _profile_satisfies(required_profile, observed_profile):
        return _unavailable(
            run,
            "CI_PROFILE_INSUFFICIENT",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            observed_profiles=observed,
            accepted_profile=accepted,
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    # The required (or stronger) profile job ran. Its conclusion decides.
    job = _identity_job_for(identity_jobs, observed_profile, job_profiles)
    job_conclusion = job.get("conclusion") if job is not None else None
    if job_conclusion == "success":
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=required_profile,
            run=run,
            result="PASS",
            reason_code=None,
            observed_profiles=observed,
            accepted_profile=observed_profile,
            job_ids=[job["id"] for job in identity_jobs],
            job_timings=[_job_timing(job) for job in identity_jobs],
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if job_conclusion == "failure":
        return _record(
            repository=repository,
            pull_request=pull_request,
            base_sha=base_sha,
            head_sha=head_sha,
            required_profile=required_profile,
            run=run,
            result="FAIL",
            reason_code="CI_RUN_FAILED",
            observed_profiles=observed,
            accepted_profile=None,
            job_ids=[job["id"] for job in identity_jobs],
            job_timings=[_job_timing(job) for job in identity_jobs],
            observed_at=observed_at,
            retry_after_seconds=None,
        )
    if job_conclusion == "cancelled":
        return _unavailable(
            run,
            "CI_RUN_CANCELED",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            observed,
            accepted,
            observed_at,
            None,
        )
    if job_conclusion == "timed_out":
        return _unavailable(
            run,
            "CI_RUN_TIMED_OUT",
            repository,
            pull_request,
            base_sha,
            head_sha,
            required_profile,
            observed,
            accepted,
            observed_at,
            None,
        )
    # A terminal conclusion outside the closed mapping (neutral, stale,
    # action_required, start_failure, or anything new).
    return _unavailable(
        run,
        "CI_RESPONSE_INVALID",
        repository,
        pull_request,
        base_sha,
        head_sha,
        required_profile,
        observed,
        accepted,
        observed_at,
        None,
    )


def _profile_satisfies(required: str, observed: str) -> bool:
    if required.startswith("product/") or observed.startswith("product/"):
        return required == observed
    return profile_meets_requirement(required, observed)


def _profile_jobs(
    jobs: dict[str, object],
    run: dict[str, object],
    job_profiles: dict[str, str] | None = None,
) -> tuple[list[dict[str, object]], list[str] | None, str | None]:
    """(identity jobs, observed profiles or None when ambiguous, accepted).

    An identity job is one of the profile jobs that really ran as part of the
    run this provider has already proved trusted.

    ``run_id`` must be this run's id. The job is evidence about THIS pull
    request only if it belongs to the run whose launcher, event and base
    binding were just proved; a job named ``c1`` in some other run is a true
    statement about a different thing. The jobs envelope is fetched per run,
    so this normally holds by construction -- it is checked because "normally
    holds by construction" is the class of assumption that stops holding
    silently.

    Provenance beyond run membership is carried by the chain already proved:
    `pull_request_target` bound the launcher to the base ref, the
    file-existence probe proved that launcher present at the base commit, and
    the run binding proves the job is that launcher's. (GitHub's reusable
    workflow named itself in `workflow_name` as well; Forgejo has no reusable
    workflow, so there is no such check.)

    ``observed`` is the list of profiles whose identity job ran to a terminal
    non-skipped conclusion; None when more than one did (unambiguous identity
    is an acceptance requirement, and the checks workflow's
    mutually-exclusive jobs should never produce two). ``accepted`` is the
    observed profile whose job succeeded, else None.
    """
    job_profiles = job_profiles or {name: f"checks/{name}" for name in PROFILE_JOBS}
    raw_jobs = jobs.get("jobs")
    run_id = run.get("id")
    identity_jobs: list[dict[str, object]] = []
    # A run with no integer id cannot own a job, so nothing can be proved to
    # belong to it. Fail closed rather than let a None match a job's absent
    # ``run_id`` and read as membership.
    if isinstance(raw_jobs, list) and isinstance(run_id, int):
        for entry in raw_jobs:
            if not isinstance(entry, dict):
                continue
            if entry.get("run_id") != run_id:
                continue
            if entry.get("name") in job_profiles:
                identity_jobs.append(entry)
    identity_jobs.sort(key=lambda job: tuple(job_profiles).index(str(job.get("name"))))
    observed: list[str] = []
    accepted: str | None = None
    for job in identity_jobs:
        name = job.get("name")
        conclusion = job.get("conclusion")
        if not isinstance(name, str) or not isinstance(conclusion, str):
            continue
        if job.get("status") != "completed" or conclusion not in _RAN_CONCLUSIONS:
            continue
        profile = job_profiles[name]
        observed.append(profile)
        if conclusion == "success":
            accepted = profile
    if len(observed) > 1:
        return identity_jobs, None, None
    return identity_jobs, observed, accepted


def _identity_job_for(
    identity_jobs: list[dict[str, object]], profile: str,
    job_profiles: dict[str, str] | None = None,
) -> dict[str, object] | None:
    job_profiles = job_profiles or {name: f"checks/{name}" for name in PROFILE_JOBS}
    job_name = next((name for name, value in job_profiles.items() if value == profile), None)
    for job in identity_jobs:
        if job.get("name") == job_name:
            return job
    return None


def _job_timing(job: dict[str, object]) -> dict[str, object]:
    """One identity job's timing, straight from the Actions API's own
    ``started_at``/``completed_at`` -- already fetched for every job, never a
    second request. ``duration_seconds`` is derived only when both timestamps
    parse; a malformed or absent timestamp yields None rather than raising,
    matching this module's rule that nothing here explodes the record."""
    started_at = job.get("started_at")
    completed_at = job.get("completed_at")
    started_at = started_at if isinstance(started_at, str) else None
    completed_at = completed_at if isinstance(completed_at, str) else None
    duration_seconds: int | None = None
    if started_at is not None and completed_at is not None:
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError:
            duration_seconds = None
        else:
            duration_seconds = round((end - start).total_seconds())
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
    }


def _instant(value: object) -> datetime | None:
    """One Actions timestamp as an aware datetime, or None when it is not one.

    The same ``Z``-to-offset normalisation ``_job_timing`` performs, factored
    out because two callers need it. None for anything absent, malformed
    or naive: a timestamp with no offset cannot be compared against
    ``observed_at`` without inventing a timezone, and inventing one here
    would silently move a window boundary by hours.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _materialization_window_closed(
    jobs: dict[str, object], run: dict[str, object], observed_at: str
) -> bool:
    """Has the identity job had long enough to appear?

    True only when every job the forge lists for this run has completed and
    the newest of those completions is more than
    ``PROFILE_MATERIALIZATION_WINDOW_SECONDS`` before ``observed_at`` -- or,
    when the forge lists no job for this run at all, when
    ``_empty_jobs_window_closed`` says the run itself settled long enough
    ago.

    While there are job rows, the run's terminal instant is taken from them
    rather than from the run: ``completed_at`` is already fetched for every
    job and already read by ``_job_timing``, and it dates the last thing this
    run actually did. A still-running job keeps the window open by
    definition: the run has jobs in flight, so an identity job may yet be one
    of them.

    Every unmeasurable case answers False, the same as having no window bound.
    A jobs value that is not a list, an absent or malformed
    ``completed_at``, a naive or unparseable ``observed_at``: none of those
    is evidence that time has passed, and reading them as a closed window
    would turn an unreadable clock into a terminal verdict. Zero rows answer
    False here too; the empty-jobs bound closes that case -- see below.
    """
    raw_jobs = jobs.get("jobs")
    if not isinstance(raw_jobs, list):
        return False
    now = _instant(observed_at)
    if now is None:
        return False
    rows = [entry for entry in raw_jobs if isinstance(entry, dict)]
    if not rows:
        return _empty_jobs_window_closed(run, now)
    if any(row.get("status") != "completed" for row in rows):
        return False
    completions = [_instant(row.get("completed_at")) for row in rows]
    if any(instant is None for instant in completions):
        return False
    newest = max(instant for instant in completions if instant is not None)
    age = (now - newest).total_seconds()
    return age > PROFILE_MATERIALIZATION_WINDOW_SECONDS


def _blocked_aggregate_terminal(
    jobs: dict[str, object],
    run: dict[str, object],
    observed_at: str,
    job_profiles: dict[str, str],
) -> bool:
    """True when a blocked identity job is stranded.

    ``blocked`` means Forgejo is waiting on dependencies. When a leaf job is
    killed, ``c1`` can stay
    ``blocked`` after every dependency had concluded. Forgejo job rows carry
    no timestamps, so this ages the run's own ``updated_at``, the same clock
    ``_empty_jobs_window_closed`` uses.

    True only when: a blocked row is itself an identity row, its ``name`` in
    *job_profiles* (the same filter ``_profile_jobs`` applies -- a blocked
    row outside c1/c2/c3 is not this signal); every OTHER row the forge
    lists for this run is ``completed``; and the run's own ``updated_at`` is
    older than ``PROFILE_MATERIALIZATION_WINDOW_SECONDS``. Every
    unmeasurable case answers False.
    """
    raw_jobs = jobs.get("jobs")
    if not isinstance(raw_jobs, list):
        return False
    rows = [entry for entry in raw_jobs if isinstance(entry, dict)]
    blocked = [
        row
        for row in rows
        if row.get("forge_status") == "blocked" and row.get("name") in job_profiles
    ]
    if not blocked:
        return False
    blocked_ids = {id(row) for row in blocked}
    siblings = [row for row in rows if id(row) not in blocked_ids]
    if not siblings:
        return False
    if any(row.get("status") != "completed" for row in siblings):
        return False
    now = _instant(observed_at)
    if now is None:
        return False
    return _settled_age_exceeds(run, now, PROFILE_MATERIALIZATION_WINDOW_SECONDS)


def _settled_age_exceeds(
    run: dict[str, object], now: datetime, window_seconds: int
) -> bool:
    """Seconds since the run's own ``updated_at`` exceed *window_seconds*.

    Shared by ``_empty_jobs_window_closed`` and ``_blocked_aggregate_terminal``,
    both of which have no job-level timestamp to trust for their case and
    must age the RUN itself instead. An absent or unparseable ``updated_at``
    is "no age known" and answers False, never a guess.
    """
    settled = _instant(run.get("updated_at"))
    if settled is None:
        return False
    return (now - settled).total_seconds() > window_seconds


def _empty_jobs_window_closed(run: dict[str, object], now: datetime) -> bool:
    """Has a settled run with NO job rows waited long enough?

    The jobs clock above needs a job to read, so this is the case it cannot
    answer: `/actions/runs/{id}/jobs` listing nothing while the run is
    terminal. A run can hold that shape for minutes and then produce its
    c1 row, so the state is a real window; nothing shows it always ends,
    which is why the bound is taken from the run instead of waiting for a row
    that may never arrive.

    ``updated_at`` is the whole clock. The Forgejo translation
    (the Forgejo backend's run translation) emits ``created_at``,
    ``updated_at`` and ``run_started_at`` and no ``completed_at`` or
    ``stopped`` at all, and of those three only ``updated_at`` moves when the
    run settles -- ageing a run from its start time would close the window
    the moment any run longer than the bound finished. So there is no
    fallback field: an absent or malformed ``updated_at`` is "no age known"
    and answers False, leaving the pending reading in place.

    Two conditions guard the clock. ``status`` restates what the caller has
    already proved by control flow, so that this predicate stays honest when
    read on its own. ``conclusion`` is the one that fires: an unrecognised Forgejo
    status is translated to ``action_required`` on a ``completed`` run, so
    without it a live run in a status this provider has not met could be
    declared permanently profile-less by the clock alone.
    """
    if run.get("status") != "completed":
        return False
    if run.get("conclusion") not in _SETTLED_RUN_CONCLUSIONS:
        return False
    return _settled_age_exceeds(run, now, EMPTY_JOBS_WINDOW_SECONDS)


def _unavailable(
    run: dict[str, object],
    reason_code: str,
    repository: str,
    pull_request: int | None,
    base_sha: str | None,
    head_sha: str | None,
    required_profile: str | None,
    observed_profiles: list[str],
    accepted_profile: str | None,
    observed_at: str,
    retry_after_seconds: int | None,
) -> dict[str, object]:
    return _record(
        repository=repository,
        pull_request=pull_request,
        base_sha=base_sha,
        head_sha=head_sha,
        required_profile=required_profile,
        run=run,
        result="UNAVAILABLE",
        reason_code=reason_code,
        observed_profiles=observed_profiles,
        accepted_profile=accepted_profile,
        job_ids=[],
        job_timings=[],
        observed_at=observed_at,
        retry_after_seconds=retry_after_seconds,
    )


def _record(
    *,
    repository: str,
    pull_request: int | None,
    base_sha: str | None,
    head_sha: str | None,
    required_profile: str | None,
    run: dict[str, object] | None,
    result: str,
    reason_code: str | None,
    observed_profiles: list[str],
    accepted_profile: str | None,
    job_ids: list[object],
    job_timings: list[dict[str, object]],
    observed_at: str,
    retry_after_seconds: int | None,
) -> dict[str, object]:
    """Assemble the evidence record. Values come only from gate-derived inputs
    or from the run record itself; nothing is caller-supplied.

    ``trusted_workflow_ref`` is always None: it reports the SHA a run
    resolved its reusable workflow to, and Forgejo has no reusable workflow
    and emits no ``referenced_workflows`` on any endpoint — reporting a
    resolution that never happened would be a claim the forge never made.

    ``workflow_run_conclusion`` is the candidate run's own
    conclusion, copied verbatim from the run record exactly as the other
    ``workflow_*`` fields are. It is additive and optional to every reader:
    ``derive_verdict`` uses it so a terminal ``CI_PROFILE_MISSING`` can say
    that a run DID judge this head and merely carried no profile job, rather
    than reading as "CI never ran".
    """
    html_url = run.get("html_url") if run is not None else None
    return {
        "schema": CI_EVIDENCE_SCHEMA,
        "repository": repository,
        "pull_request": pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "required_profile": required_profile,
        "observed_profiles": observed_profiles,
        "accepted_profile": accepted_profile,
        "result": result,
        "reason_code": reason_code,
        "workflow_id": run.get("workflow_id") if run is not None else None,
        "workflow_run_id": run.get("id") if run is not None else None,
        "workflow_run_attempt": run.get("run_attempt") if run is not None else None,
        "workflow_run_conclusion": (
            run.get("conclusion") if run is not None else None
        ),
        "workflow_path": run.get("path") if run is not None else None,
        "trusted_workflow_ref": None,
        "job_ids": job_ids,
        "job_timings": job_timings,
        "target_url": html_url if isinstance(html_url, str) else None,
        "observed_at": observed_at,
        "retry_after_seconds": retry_after_seconds,
        "failure_summary_available": result == "FAIL",
    }


# --- bounded failure evidence -----------------------------------------------


def failed_jobs_of(jobs: dict[str, object]) -> list[dict[str, object]]:
    """The run's failed terminal jobs, ordered deterministically (id asc).

    Only jobs with a conclusion in FAILED_JOB_CONCLUSIONS qualify
    ("failed terminal jobs and failed steps only"); a cancelled job is not
    failure evidence. Entries that are not dicts or carry no integer id are
    skipped — a malformed row cannot shape the record.
    """
    raw_jobs = jobs.get("jobs")
    candidates: list[tuple[int, dict[str, object]]] = []
    if isinstance(raw_jobs, list):
        for entry in raw_jobs:
            if not isinstance(entry, dict):
                continue
            if entry.get("conclusion") not in FAILED_JOB_CONCLUSIONS:
                continue
            raw_id = entry.get("id")
            if isinstance(raw_id, int):
                candidates.append((raw_id, entry))
    candidates.sort(key=lambda pair: pair[0])
    return [entry for _, entry in candidates]


def build_failure_evidence(
    *,
    repository: str,
    pull_request: int | None,
    base_sha: str | None,
    head_sha: str | None,
    run: dict[str, object],
    jobs: dict[str, object],
    log_texts: dict[int, str | None],
    observed_at: str,
) -> dict[str, object]:
    """Assemble ci-failure.v1 for an accepted run.

    ``log_texts`` maps job id to the job's raw log (None when the fetch
    failed — that job's steps then carry no excerpt and ``log_available`` is
    false; log availability is best-effort and time-bounded by forge
    retention, so a missing log must not invalidate the whole record). Every excerpt is sanitized,
    redacted, and capped; the 20,000-byte budget is applied in deterministic
    order (job id asc, step number asc for step excerpts, then smallest log
    first across the job-level ones) and ``truncated`` reports whether
    anything was dropped. Only the redacted bytes are returned; no raw log
    text leaves this function.

    **Step granularity is a GitHub shape, not a universal one.**
    Forgejo's job rows carry no ``steps`` array at all, so keying the
    excerpt on a failed step discarded every log this function had already
    fetched and redacted: ``failed_steps: []`` with ``bytes_returned: 0``
    on every red Forgejo lane. Each job therefore also carries a job-level
    ``excerpt``, populated exactly when the job produced no failed steps to
    hang the log on. The two are mutually exclusive by construction, so a
    consumer reads whichever is non-null and never double-counts the budget.

    The alternative — one synthesised ``failed_steps`` entry per job — was
    rejected: it would assert that a failed step exists where the forge
    reported no steps whatsoever, which is the same fabrication
    the Forgejo backend's ``_translated_job`` is forbidden from making, moved
    one module downstream and into a versioned record.

    **Job-level excerpts are spent smallest log first**, because job-id order
    is not the order of usefulness. A large early log and a short later one,
    spent by id, give the whole budget to the first and nothing to the
    second - dropping the short summary log that actually said what failed. Smallest first maximises how
    many failing jobs are represented and lets a short log arrive whole; the
    large one takes what is left and ``truncated`` says so.
    """
    failed_jobs: list[dict[str, object]] = []
    truncated = False
    budget = FAILURE_EVIDENCE_MAX_BYTES
    returned = 0
    redaction_count = 0

    def take_tail(source: str) -> str:
        """Spend the remaining budget on the tail of one redacted log.

        The tail, not the head: the failure detail is at the end. Callers
        guard on ``budget > 0`` so the slice is never ``[-0:]``, which would
        return the whole log and overspend.
        """
        nonlocal truncated, returned, budget
        encoded = source.encode("utf-8")
        cut = encoded[-budget:]
        if len(cut) < len(encoded):
            truncated = True
        returned += len(cut)
        budget -= len(cut)
        return cut.decode("utf-8", errors="replace")

    #: (encoded log size, job id, redacted log, the record entry to fill)
    deferred: list[tuple[int, int, str, dict[str, object]]] = []

    for job in failed_jobs_of(jobs):
        raw_job_id = job.get("id")
        if not isinstance(raw_job_id, int):
            continue
        job_id = raw_job_id
        job_name = job.get("name")
        raw_log = log_texts.get(job_id)
        excerpt_source: str | None = None
        if raw_log is not None:
            excerpt_source, redactions = redact_log(raw_log)
            redaction_count += redactions
        failed_steps: list[dict[str, object]] = []
        steps = job.get("steps")
        if isinstance(steps, list):
            ordered: list[tuple[int, dict[str, object]]] = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                raw_number = step.get("number")
                if (
                    isinstance(raw_number, int)
                    and step.get("conclusion") in FAILED_STEP_CONCLUSIONS
                ):
                    ordered.append((raw_number, step))
            ordered.sort(key=lambda pair: pair[0])
            for _number, step in ordered:
                step_name = step.get("name")
                excerpt: str | None = None
                if excerpt_source is not None and budget > 0:
                    excerpt = take_tail(excerpt_source)
                failed_steps.append(
                    {
                        "step_name": step_name if isinstance(step_name, str) else None,
                        "excerpt": excerpt,
                    }
                )
        entry: dict[str, object] = {
            "job_id": job_id,
            "job_name": job_name if isinstance(job_name, str) else None,
            "log_available": raw_log is not None,
            "failed_steps": failed_steps,
            "excerpt": None,
        }
        if not failed_steps and excerpt_source is not None:
            # No failed step to attach the log to — either the forge reports
            # no `steps` array (Forgejo) or it reported one in which nothing
            # carries a failed conclusion. Both are the same condition for a
            # reader: the log is the job's, at job granularity. Spending is
            # deferred to the pass below so the order can be chosen.
            deferred.append((len(excerpt_source.encode("utf-8")), job_id, excerpt_source, entry))
        failed_jobs.append(entry)

    # Smallest log first, job id breaking a tie: see the docstring. The record
    # itself stays in job-id order — `failed_jobs` was appended above, and only
    # the excerpts are filled in here.
    deferred.sort(key=lambda item: (item[0], item[1]))
    for _size, _job_id, source, entry in deferred:
        if budget <= 0:
            break
        entry["excerpt"] = take_tail(source)

    return {
        "schema": CI_FAILURE_SCHEMA,
        "repository": repository,
        "pull_request": pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "workflow_run_id": run.get("id"),
        "failed_jobs": failed_jobs,
        "bytes_returned": returned,
        "truncated": truncated,
        "redaction_count": redaction_count,
        "observed_at": observed_at,
    }


def build_ci_progress(
    *,
    repository: str,
    pull_request: int | None,
    base_sha: str | None,
    head_sha: str | None,
    run: dict[str, object],
    jobs: dict[str, object],
    log_texts: dict[int, str | None],
    observed_at: str,
) -> dict[str, object]:
    """Assemble advisory failed-job evidence for a pending CI verdict.

    The record carries the same redacted, byte-capped, job-level excerpts as
    ci-failure.v1 -- this function is a thin envelope over
    build_failure_evidence, so the budget and redaction rules cannot drift
    between the two records -- enriched with each job's own terminal
    conclusion and marked non-terminal. The run may still be in progress or
    have failed against an older base. overall_state stays pending whatever
    the excerpts say: only the ci-status.v2 verdict is authority.

    No log content enters unless the caller already passed the progress
    release gate (progress_trust_ok plus the launcher-at-the-base file
    probe). This builder performs no trust check of its own.
    """
    failure = build_failure_evidence(
        repository=repository,
        pull_request=pull_request,
        base_sha=base_sha,
        head_sha=head_sha,
        run=run,
        jobs=jobs,
        log_texts=log_texts,
        observed_at=observed_at,
    )
    states: dict[int, str] = {}
    for job in failed_jobs_of(jobs):
        raw_job_id = job.get("id")
        conclusion = job.get("conclusion")
        if isinstance(raw_job_id, int) and isinstance(conclusion, str):
            states[raw_job_id] = conclusion
    failed_jobs: list[dict[str, object]] = []
    for entry in failure["failed_jobs"]:
        job_entry = dict(entry)
        conclusion = states.get(entry["job_id"])
        # failed_jobs_of admits exactly the FAILED_JOB_CONCLUSIONS, so the
        # closed vocabulary below is exhaustive; an entry missing from the
        # map cannot occur and reads as "failed" rather than as untyped.
        job_entry["job_state"] = (
            "failed" if conclusion != "timed_out" else "timed_out"
        )
        failed_jobs.append(job_entry)
    return {
        "schema": CI_PROGRESS_SCHEMA,
        "repository": repository,
        "pull_request": pull_request,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "workflow_run_id": run.get("id"),
        "run_status": run.get("status"),
        "overall_state": "pending",
        "terminal": False,
        "failed_jobs": failed_jobs,
        "bytes_returned": failure["bytes_returned"],
        "truncated": failure["truncated"],
        "redaction_count": failure["redaction_count"],
        "observed_at": observed_at,
    }


# --- consumer-facing status projection --------------------------------------
#
# One verdict, derived in one place. A consumer -- a footer, a status line,
# a watcher -- that maps ci-evidence.v1 onto "is CI green" for itself drifts:
# `NOT_RUN` means CI is *not required* for this lane, and a consumer reading
# it as "the run has not started yet" waits for a run nobody will ever
# schedule.
#
# Pure functions over an already-derived record: no forge call, no I/O, no
# clock. That is what lets a test drive every verdict directly instead of
# through a subprocess.
#
# This adds no policy and no authority. It restates evidence the gate already
# computed in the shape a consumer can render; the merge decision does not
# consult this diagnostic projection.

CI_STATUS_SCHEMA = "ci-status.v2"

#: The closed verdict vocabulary.
#:
#:   green       PASS for the exact head
#:   red         FAIL for the exact head -- the only actionable verdict
#:   pending     no verdict yet, and the evidence says ask again
#:   no_run      CI is not required for this lane
#:   no_pr       no open pull request, so there is nothing to report on
#:   unavailable no verdict, and asking again will not produce one
#:
#: `pending` and `unavailable` are both "no verdict"; conflating them is what
#: makes a caller either poll forever or give up on a queued run. The
#: discriminator is ci-evidence.v1's own `retry_after_seconds` hint -- a
#: bounded hint means wait, a null one means the cause is terminal or
#: structural -- never a guess made here or a clock read by the caller.
CI_VERDICTS = frozenset({"green", "red", "pending", "no_run", "no_pr", "unavailable"})


#: A run conclusion is a closed forge vocabulary in practice, but it arrives
#: from the API and a headline is rendered into status bars, hook messages and
#: terminal output. Only this shape is quoted; anything else is dropped rather
#: than escaped, because an omitted clause costs a reader one fact and a
#: relayed control character costs them a trustworthy line.
_CONCLUSION_RE = re.compile(r"[a-z_]{1,32}")


def _concluded_clause(evidence: dict[str, object]) -> str:
    """`` an earlier run for this head concluded <x>;`` or the empty string."""
    conclusion = evidence.get("workflow_run_conclusion")
    if not isinstance(conclusion, str) or not _CONCLUSION_RE.fullmatch(conclusion):
        return ""
    return f" an earlier run for this head concluded {conclusion};"


def derive_verdict(evidence: dict[str, object]) -> tuple[str, str]:
    """Map a ci-evidence.v1 record onto ``(verdict, headline)``.

    The headline is one line, safe to print in a status bar or a hook
    message, and never carries log text, a path, or a credential -- only the
    evidence's own closed reason code and the required profile's name.

    ``pull_request`` is read first and on purpose. A lane with no open pull
    request has nothing to report regardless of what any other field says:
    before the first publish there is no subject, and after a merge the
    branch is deleted, so a surface that renders anything here is
    rendering noise.

    That short-circuit is only as honest as the field: a null
    ``pull_request`` means that no pull request is open, so the caller must
    populate it for every lane that has one, including a lane whose local
    head has fallen behind its own branch. Which pull request exists is a
    question for the caller, not for how a record renders.
    """

    profile = evidence.get("required_profile")
    suffix = f" ({profile})" if isinstance(profile, str) and profile else ""
    if evidence.get("pull_request") is None:
        return "no_pr", "CI: no open pull request for this lane"
    result = evidence.get("result")
    raw_reason = evidence.get("reason_code")
    reason = raw_reason if isinstance(raw_reason, str) and raw_reason else None
    if result == "PASS":
        return "green", f"CI: green{suffix}"
    if result == "FAIL":
        return "red", f"CI: RED{suffix}"
    if result == "NOT_RUN":
        detail = f" ({reason})" if reason else ""
        return "no_run", f"CI: not required for this lane{detail}"
    if result == "UNAVAILABLE":
        retry = evidence.get("retry_after_seconds")
        named = reason or "unknown"
        if isinstance(retry, int) and not isinstance(retry, bool):
            # No "re-poll in Ns" here. The retry hint stays in the record as
            # `retry_after_seconds`, where a caller that needs it can read it;
            # as prose it was an instruction to a machine rendered into a
            # human-facing headline, telling a model to do the one thing a
            # rule against repeated polling exists to stop. A waiter already polls.
            if reason == "CI_RUN_MISSING":
                return "pending", "CI: waiting for exact-head run to start (CI_RUN_MISSING)"
            if reason == "CI_RUN_QUEUED":
                return "pending", "CI: exact-head run is queued (CI_RUN_QUEUED)"
            if reason == "CI_RUN_IN_PROGRESS":
                return "pending", "CI: exact-head run is running (CI_RUN_IN_PROGRESS)"
            return "pending", f"CI: no verdict yet ({named})"
        if reason == "CI_LANE_HEAD_MISMATCH":
            # The one unavailable cause a caller can clear by itself, so it
            # is the one worth spelling out rather than leaving as a code.
            return (
                "unavailable",
                "CI: this lane's head is not its branch's head, so no run "
                "answers for it (CI_LANE_HEAD_MISMATCH)",
            )
        if reason == "CI_PROFILE_MISSING":
            # the second unavailable cause with somewhere to go. A
            # run judged this head and carried no c1/c2/c3 identity job, so
            # no further read of THIS head can produce one -- a new head can.
            # Naming the run's own conclusion is what stops the line reading
            # as "CI never ran"; it did run, it just proved nothing the gate
            # accepts.
            return (
                "unavailable",
                f"CI:{_concluded_clause(evidence)} no accepted profile"
                f"{suffix}. Republish a new head; the workflow that judged "
                "this head has no c1/c2/c3 identity job "
                "(CI_PROFILE_MISSING)",
            )
        return "unavailable", f"CI: unavailable ({named})"
    # An unrecognised result is reported as unavailable rather than assumed
    # benign: a caller must never read a shape this function did not
    # understand as "nothing is wrong".
    return "unavailable", f"CI: unavailable (unrecognised evidence result {result!r})"


#: The two decisions that promise a follow-up instead of carrying it. Named
#: here rather than imported because this module is deliberately pure -- no
#: I/O, no policy, no evaluator -- and the two values are a closed vocabulary
#: another module owns and this module only reads.
#: ``test_ci_status_projection`` pins that the two lists agree.
DEFERRED_KNOWLEDGE_DECISIONS = ("kb_after_merge", "kb_after_runtime")


def knowledge_followup(knowledge_impact: str | None) -> dict[str, object] | None:
    """The owed follow-up a review claim records, or ``None``.

    *knowledge_impact* is the claim's own impact value, read from the lane
    head by the caller -- this function adds no second parser and performs
    no I/O.

    ``None`` for every non-deferred decision, for a claim that carries no
    follow-up reference (the field is additive, so every commit written before
    it is exactly that case), and for anything unreadable. A malformed claim is
    not a CI failure and must never turn a green verdict into an error: the
    worst outcome here is a reminder the reader does not get, the same as
    having no follow-up field at all.
    """
    if not knowledge_impact:
        return None
    try:
        decision = json.loads(knowledge_impact)
    except (ValueError, TypeError):
        return None
    if not isinstance(decision, dict):
        return None
    name = decision.get("decision")
    if name not in DEFERRED_KNOWLEDGE_DECISIONS:
        return None
    ticket = decision.get("followup_ticket")
    if not isinstance(ticket, str) or not ticket:
        return None
    return {"decision": name, "followup_ticket": ticket}


def build_ci_status(
    *,
    evidence: dict[str, object],
    failure: dict[str, object] | None,
    verdict: str,
    headline: str,
    knowledge_followup: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble ci-status.v2 from records the gate has already built.

    ``failure`` is the ci-failure.v1 record for a red run, or None for every
    other verdict. The complete accepted evidence and failure records are
    retained so a consumer loses nothing by using this one projection. Nothing
    is re-derived here and no log text is read: this function only selects.

    ``knowledge_followup`` is the deferred follow-up promise the
    lane head's own claim carries, or None. The schema version is NOT bumped
    for it: ``ci-status.v2`` succeeded v1 when the envelope was RESTRUCTURED --
    flat CI fields replaced by embedded ``ci-evidence.v1``/``ci-failure.v1``
    records -- and an added optional field breaks no reader. Bumping would be
    worse than inert: a status consumer refuses a record whose schema it
    does not recognise, so a bump would blind every deployed consumer to
    a status that had merely gained a hint.
    """

    return {
        "schema": CI_STATUS_SCHEMA,
        "verdict": verdict,
        "headline": headline,
        # The one field a caller may branch on to decide whether to act.
        # Deliberately not "is CI green": `no_run`, `no_pr` and `unavailable`
        # are all "nothing to do here", and only a red run asks for a fix.
        "actionable": verdict == "red",
        "ci_evidence": (
            dict(evidence) if evidence.get("schema") == CI_EVIDENCE_SCHEMA else None
        ),
        "failure_evidence": (
            dict(failure)
            if isinstance(failure, dict)
            and failure.get("schema") == CI_FAILURE_SCHEMA
            else None
        ),
        "knowledge_followup": (
            dict(knowledge_followup)
            if isinstance(knowledge_followup, dict)
            else None
        ),
    }
