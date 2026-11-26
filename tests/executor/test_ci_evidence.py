"""The read-only Actions evidence provider and projections.

Provider unit tests run against constructed API response shapes; the
real-git operation tests follow the policy-operation tests'
disposable-bare-origin pattern with the forge stubbed at the JSON boundary
-- the stub backend also serves the Actions reads (runs/jobs/logs/
contents), so ``ci-status`` runs end-to-end against fixture API shapes.

Fixture shapes are built from the documented Actions REST contract as
observed on the Forgejo server (the translated workflow-run object with
``pull_requests``, the jobs envelope, text job logs) and from the
GitHub-shaped contract the translator produces. The provider
evaluates the Forgejo trust profile only (``FORGEJO_TRUST_PROFILE``), and
the launcher-at-base-commit file probe is the trust proof.

The four-state vocabulary (PASS/FAIL/NOT_RUN/UNAVAILABLE), the closed
reason codes, and the equal-or-stronger profile table are the substance
this file pins: every row of the evidence-state mapping and of the profile
table is table-tested.
"""

from __future__ import annotations

import importlib.util as _iu
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools-config"))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tools-config" / "scripts")
)

SCRIPTS = Path(__file__).resolve().parents[3] / "tools-config" / "scripts"
GATE_PATH = SCRIPTS / "git_maintainer_gate.py"
_spec = _iu.spec_from_file_location("gate_ci_evidence", GATE_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load the gate shim")
gate = _iu.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

from git_gate import (  # noqa: E402
    ci,  # noqa: E402 -- after the by-path shim load, as in sibling gate tests
    gate_backend_forgejo,  # noqa: E402 -- the one backend that translates rather than fetches
)
from git_gate import (  # noqa: E402
    forge as gate_forge,  # noqa: E402 -- the same module instance the shim's gate uses
)

FORGEJO_LAUNCHER = ci.FORGEJO_LAUNCHER_PATH

# Negative fixtures: these GitHub launcher/reusable path strings are only
# the paths that must NOT satisfy the Forgejo trust profile.
TRUSTED_LAUNCHER = ".github/workflows/checks.yml"
TRUSTED_REUSABLE = ".github/workflows/reusable.yml"

# The instant ``evaluate`` observes every unit fixture at. The
# observation time is an input rather than a label -- the materialization
# window is measured against it -- so the fixtures that have to be dated
# relative to it share one value instead of spelling the literal twice.
OBSERVED_AT = "2020-01-01T00:00:00Z"


def _dict(value: object) -> dict[str, object]:
    """Narrow a fixture/result value to a dict, asserting the shape."""
    assert isinstance(value, dict)
    return value


def _list(value: object) -> list[object]:
    """Narrow a fixture/result value to a list, asserting the shape."""
    assert isinstance(value, list)
    return value


def _str(value: object) -> str:
    assert isinstance(value, str)
    return value


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value


# --- fixture builders: documented Actions REST shapes -----------------------


def run_record(
    *,
    run_id: int = 7001,
    head_sha: str = "a" * 40,
    base_sha: str = "b" * 40,
    pr_number: int = 1,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "pull_request_target",
    path: str = FORGEJO_LAUNCHER,
    workflow_id: int = 3001,
    pull_requests: list[dict[str, object]] | None = None,
    updated_at: str | None = OBSERVED_AT,
) -> dict[str, object]:
    """One workflow-run object in the shape the evaluator consumes.

    The evaluator's one run shape: the Forgejo route's trusted event and
    launcher, with the ``pull_requests`` record that the base/head binding
    check reads. No ``referenced_workflows`` field - it feeds a GitHub-only
    mechanism, and Forgejo emits it on no endpoint at all.

    ``updated_at`` is the run row's last modification, which for a terminal
    run is when it settled; ``gate_backend_forgejo._translated_run`` carries
    it on every Forgejo run record. It defaults to the instant ``evaluate``
    observes, so an unmoved fixture run is freshly settled — the state
    the empty-jobs bound is generous to, and the state that keeps every
    job-dated test above honest, since those close their window while this
    clock says the run settled a moment ago. ``None`` omits the field, which
    is the undated shape that fails open.
    """
    record: dict[str, object] = {
        "id": run_id,
        "name": "checks",
        "head_branch": "task/test",
        "head_sha": head_sha,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "workflow_id": workflow_id,
        "path": path,
        "run_attempt": 1,
        "run_number": run_id,
        "html_url": f"https://forge.example.invalid/test/repo/actions/runs/{run_id}",
        "pull_requests": (
            pull_requests
            if pull_requests is not None
            else [
                {
                    "id": 9,
                    "number": pr_number,
                    "url": "https://forge.example.invalid/repos/test/repo/pulls/1",
                    "head": {
                        "ref": "task/test",
                        "sha": head_sha,
                        "repo": {
                            "id": 1,
                            "url": "https://forge.example.invalid/repos/test/repo",
                            "name": "repo",
                        },
                    },
                    "base": {
                        "ref": "main",
                        "sha": base_sha,
                        "repo": {
                            "id": 1,
                            "url": "https://forge.example.invalid/repos/test/repo",
                            "name": "repo",
                        },
                    },
                }
            ]
        ),
    }
    if updated_at is not None:
        record["updated_at"] = updated_at
    return record


# --- raw Forgejo API shapes, for the backend readers ------------------------
#
# These three build what Forgejo really returns, before any translation.
# They are shared by the reader unit tests and by the real-git backend below,
# so the end-to-end path is exercised against the same shapes the unit tests
# pin rather than against a second, kinder copy of them.
#
# NO REAL PERSONAL DATA. `event_payload` on a live instance carries user
# objects with real email addresses; every address here is at the reserved
# `.invalid` TLD (RFC 2606), which cannot resolve or route, and every SHA is
# a repeated hex digit. The user objects exist ONLY so the containment test
# has something that must not escape.


def forgejo_task_row(
    *,
    head_sha: str = "a" * 40,
    event: str = "pull_request_target",
    status: str = "success",
    workflow_id: str = "checks.yml",
    run_id: int = 41,
    run_number: int = 7,
) -> dict[str, object]:
    """One `/actions/tasks` row: the task vocabulary (``head_sha``,
    ``run_number``, ``workflow_id`` as a bare basename, one ``status``)."""
    return {
        "id": run_id,
        "name": "checks",
        "head_branch": "task/test",
        "head_sha": head_sha,
        "run_number": run_number,
        "event": event,
        "display_title": "one change",
        "status": status,
        "workflow_id": workflow_id,
        "url": "https://forge.example.invalid/acme/core-repo/actions/runs/7",
        "created_at": "2020-01-02T09:00:00Z",
        "updated_at": "2020-01-02T09:04:00Z",
        "run_started_at": "2020-01-02T09:00:30Z",
    }


def forgejo_event_payload(
    *,
    head_sha: str = "a" * 40,
    base_sha: str = "b" * 40,
    number: int = 12,
    with_user_objects: bool = True,
) -> str:
    """One raw `pull_request_target` webhook payload, as a JSON STRING.

    Forgejo stores the webhook verbatim in `/actions/runs`'s ``event_payload``,
    so it is a string inside JSON rather than a nested object -- the reader has
    to parse it a second time, and that second parse is the containment
    boundary.

    ``with_user_objects`` adds the author and sender records a live payload
    carries. They are the reason this field is handled the way it is, and no
    test asserts their contents beyond proving they do not escape.
    """
    pull: dict[str, object] = {
        "id": 900,
        "number": number,
        "title": "a pull request title that must not travel",
        "head": {"sha": head_sha, "ref": "task/test", "label": "task/test"},
        "base": {"sha": base_sha, "ref": "main", "label": "main"},
    }
    payload: dict[str, object] = {
        "action": "synchronized",
        "number": number,
        "pull_request": pull,
    }
    if with_user_objects:
        author = {
            "id": 2,
            "login": "someone",
            "full_name": "Some One",
            "email": "pr-author@example.invalid",
        }
        pull["user"] = author
        payload["sender"] = {
            "id": 3,
            "login": "another",
            "full_name": "Ann Other",
            "email": "sender-address@example.invalid",
        }
        payload["repository"] = {
            "id": 4,
            "full_name": "acme/core-repo",
            "owner": author,
        }
    return json.dumps(payload)


def forgejo_run_row(
    *,
    commit_sha: str = "a" * 40,
    event_payload: str | None = None,
    run_id: int = 16,
    index_in_repo: int = 7,
    event: str = "pull_request",
    trigger_event: str = "pull_request_target",
    status: str = "success",
) -> dict[str, object]:
    """One `/actions/runs` row: the RUN vocabulary.

    ``commit_sha`` not ``head_sha``, ``index_in_repo`` not ``run_number``. The
    two endpoints share the ``workflow_runs`` envelope key and nothing else,
    which is exactly how a reader comes to correlate on a field the other side
    does not have and refuse every lane.

    ``id`` defaults to 16 against the task row's 41 ON PURPOSE. Forgejo
    reports the same run under different ids on `/actions/tasks` and
    `/actions/runs`, so the internal id is NOT a join key. Only
    ``index_in_repo`` == ``run_number`` is, and both default to 7 here.

    ``event`` defaults to the WRONG value and ``trigger_event`` to the right
    one, which is what Forgejo really returns for a
    ``pull_request_target`` run. Every test using this fixture therefore runs
    against the trap shape rather than a tidied one: a reader that ever sourced
    the run's event from here would refuse every Forgejo lane with a plausible
    ``pull_request``.

    ``status`` is THE RUN'S AGGREGATE, which lives here and nowhere else. A
    `/actions/tasks` row is one JOB of the run and carries that job's status,
    so a six-job run offers six of them and none is the run's. All
    six values Forgejo emits -- success, failure, cancelled, skipped,
    running, waiting -- are covered here.
    """
    row: dict[str, object] = {
        "id": run_id,
        "commit_sha": commit_sha,
        "index_in_repo": index_in_repo,
        "event": event,
        "trigger_event": trigger_event,
        "status": status,
        "trigger_user": {"id": 2, "login": "someone"},
        "repository": {"id": 4, "full_name": "acme/core-repo"},
    }
    if event_payload is not None:
        row["event_payload"] = event_payload
    return row


def forgejo_job_row(
    job_id: int,
    name: str,
    status: str,
    *,
    needs: list[str] | None = None,
    run_id: int = 16,
) -> dict[str, object]:
    """One `/actions/runs/{id}/jobs` row as Forgejo sends it, before translation.

    ``status`` is Forgejo's own word (``success``, ``failure``, ``skipped``,
    ``running``, ``waiting``, ``blocked``). ``run_id`` defaults to
    ``forgejo_run_row``'s id, the run the job belongs to. ``needs`` is the
    dependency list the jobs endpoint reports and the rerun policy reads.
    """
    return {
        "id": job_id,
        "run_id": run_id,
        "attempt": 1,
        "handle": name,
        "name": name,
        "needs": needs if needs is not None else [],
        "task_id": job_id + 424,
        "status": status,
    }


def evaluate(
    run: dict[str, object] | None,
    *,
    required_profile: str | None = "checks/c1",
    base_sha: str = "b" * 40,
    head_sha: str = "a" * 40,
    pull_request: int = 1,
    jobs: dict[str, object] | None = None,
    launcher_file_exists: bool | None = True,
    api_failure: bool = False,
    malformed_response: bool = False,
    trust_profile: ci.TrustProfile | None = None,
    job_profiles: dict[str, str] | None = None,
    require_successful_run: bool = False,
) -> dict[str, object]:
    """One evaluate_evidence call with the default trusted lane facts.

    ``trust_profile=None`` OMITS the argument rather than passing Forgejo's
    profile explicitly. That distinction is load-bearing: "a caller that
    forgets the profile still measures the right route" is one of the
    properties tested below, and a helper that always supplied one would make
    the parameter's own default untested by every call in this file.
    """
    profile_kwargs: dict[str, object] = (
        {} if trust_profile is None else {"trust_profile": trust_profile}
    )
    if job_profiles is not None:
        profile_kwargs["job_profiles"] = job_profiles
    if require_successful_run:
        profile_kwargs["require_successful_run"] = True
    return ci.evaluate_evidence(
        repository="test/repo",
        pull_request=pull_request,
        base_sha=base_sha,
        head_sha=head_sha,
        required_profile=required_profile,
        run=run,
        jobs=jobs if jobs is not None else jobs_payload(),
        launcher_file_exists=launcher_file_exists,
        api_failure=api_failure,
        malformed_response=malformed_response,
        observed_at=OBSERVED_AT,
        **profile_kwargs,
    )


def evaluate_product(
    run: dict[str, object] | None,
    *,
    jobs: dict[str, object] | None = None,
    base_sha: str = "b" * 40,
    api_failure: bool = False,
) -> dict[str, object]:
    return evaluate(
        run,
        required_profile="product/checks",
        base_sha=base_sha,
        jobs=jobs,
        api_failure=api_failure,
        trust_profile=ci.PRODUCT_TRUST_PROFILE,
        job_profiles=ci.PRODUCT_JOB_PROFILES,
        require_successful_run=True,
    )


class ProductProfileEvidenceTests(unittest.TestCase):
    def _run(self, **overrides: object) -> dict[str, object]:
        return run_record(
            event="pull_request",
            path=ci.PRODUCT_TRUST_PROFILE.launcher_path,
            **overrides,
        )

    def test_exact_product_pr_head_and_base_pass(self) -> None:
        evidence = evaluate_product(self._run(), jobs=jobs_payload(name="checks"))
        self.assertEqual((evidence["result"], evidence["accepted_profile"]),
                         ("PASS", "product/checks"))

    def test_stale_product_base_is_not_accepted(self) -> None:
        evidence = evaluate_product(self._run(base_sha="c" * 40),
                                    jobs=jobs_payload(name="checks"))
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")

    def test_wrong_event_is_not_accepted_as_product_ci(self) -> None:
        evidence = evaluate_product(
            run_record(path=ci.PRODUCT_TRUST_PROFILE.launcher_path),
            jobs=jobs_payload(name="checks"),
        )
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_same_head_and_base_from_wrong_pull_request_is_refused(self) -> None:
        evidence = evaluate_product(self._run(pr_number=99),
                                    jobs=jobs_payload(name="checks"))
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"],
                         "CI_PULL_REQUEST_MISMATCH")

    def test_missing_or_skipped_product_job_never_passes(self) -> None:
        missing = evaluate_product(
            self._run(updated_at="2019-12-31T23:49:59Z"), jobs=empty_jobs()
        )
        skipped = evaluate_product(
            self._run(), jobs=jobs_payload(name="checks", conclusion="skipped")
        )
        self.assertEqual(missing["reason_code"], "CI_PROFILE_MISSING")
        self.assertNotEqual(skipped["result"], "PASS")

    def test_red_product_job_fails_and_provider_failure_is_unavailable(self) -> None:
        red = evaluate_product(
            self._run(conclusion="failure"),
            jobs=jobs_payload(name="checks", conclusion="failure"),
        )
        unavailable = evaluate_product(None, api_failure=True)
        self.assertEqual(red["result"], "FAIL")
        self.assertEqual(unavailable["result"], "UNAVAILABLE")

    def test_two_product_identity_jobs_are_ambiguous(self) -> None:
        duplicate = dict(
            _dict(_list(jobs_payload(name="checks")["jobs"])[3]), id=201
        )
        evidence = evaluate_product(
            self._run(), jobs=jobs_payload(name="checks", extra_jobs=[duplicate])
        )
        self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")

    def test_product_run_itself_must_have_a_successful_closed_conclusion(self) -> None:
        jobs = jobs_payload(name="checks", conclusion="success")
        failed = evaluate_product(self._run(conclusion="failure"), jobs=jobs)
        unknown = evaluate_product(
            self._run(conclusion="action_required"), jobs=jobs
        )
        self.assertEqual((failed["result"], failed["reason_code"]),
                         ("FAIL", "CI_RUN_FAILED"))
        self.assertEqual((unknown["result"], unknown["reason_code"]),
                         ("UNAVAILABLE", "CI_RESPONSE_INVALID"))

    def test_primary_profile_job_still_decides_over_aggregate_failure(self) -> None:
        evidence = evaluate(
            run_record(conclusion="failure"),
            jobs=jobs_payload(identity_conclusion="success"),
        )
        self.assertEqual(evidence["result"], "PASS")

    def test_product_selection_filters_identity_before_run_precedence(self) -> None:
        selection = {
            "event": "pull_request",
            "path": ci.PRODUCT_TRUST_PROFILE.launcher_path,
            "pull_request": 1,
        }
        for product_id, other_id in ((7001, 7002), (7002, 7001)):
            expected = self._run(run_id=product_id)
            other = run_record(
                run_id=other_id,
                event="push",
                path=ci.PRODUCT_TRUST_PROFILE.launcher_path,
                pull_requests=[],
            )
            for candidates in ([expected, other], [other, expected]):
                with self.subTest(
                    product_id=product_id,
                    order=[run["id"] for run in candidates],
                ):
                    selected = ci.select_newest_run(
                        candidates, "a" * 40, **selection
                    )
                    self.assertIs(selected, expected)
                    metadata = ci.selection_metadata(
                        candidates, "a" * 40, selected, **selection
                    )
                    self.assertEqual(
                        metadata["candidate_run_ids"], [product_id]
                    )

    def test_product_identity_filter_survives_job_lifecycle_reselection(self) -> None:
        expected = self._run(run_id=7001, conclusion="failure")
        lifecycle_jobs = jobs_payload(name="checks", conclusion="failure")
        first_job = _dict(_list(lifecycle_jobs["jobs"])[0])
        first_job["status"] = "in_progress"
        normalized = ci.with_job_lifecycle(expected, lifecycle_jobs)
        unrelated = run_record(
            run_id=7002,
            event="workflow_dispatch",
            path=ci.PRODUCT_TRUST_PROFILE.launcher_path,
            pull_requests=[],
        )
        selected = ci.select_newest_run(
            [normalized, unrelated],
            "a" * 40,
            event="pull_request",
            path=ci.PRODUCT_TRUST_PROFILE.launcher_path,
            pull_request=1,
        )
        self.assertIs(selected, normalized)
        self.assertEqual(selected["status"], "in_progress")


def _set_pr_base_sha(run: dict[str, object], sha: str) -> None:
    """Mutate the fixture run's recorded base SHA (binding test helper)."""
    prs = _list(run["pull_requests"])
    pr_record = _dict(prs[0])
    base = _dict(pr_record["base"])
    base["sha"] = sha


def _set_pr_head_sha(run: dict[str, object], sha: str) -> None:
    prs = _list(run["pull_requests"])
    pr_record = _dict(prs[0])
    head = _dict(pr_record["head"])
    head["sha"] = sha


class _Completed(subprocess.CompletedProcess[str]):
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        super().__init__(
            args=[],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )


# --- provider unit tests ----------------------------------------------------


class ProfileStrengthTests(unittest.TestCase):
    """The equal-or-stronger table, pinned row by row."""

    def test_the_fixed_strength_order_is_closed(self) -> None:
        self.assertEqual(
            ci.PROFILE_STRENGTH,
            {"checks/c1": 3, "checks/c2": 2, "checks/c3": 1},
        )

    def test_equal_and_stronger_satisfy(self) -> None:
        for required, observed in (
            ("checks/c1", "checks/c1"),
            ("checks/c2", "checks/c2"),
            ("checks/c2", "checks/c1"),
            ("checks/c3", "checks/c3"),
            ("checks/c3", "checks/c1"),
            ("checks/c3", "checks/c2"),
        ):
            with self.subTest(required=required, observed=observed):
                self.assertTrue(ci.profile_meets_requirement(required, observed))

    def test_weaker_never_satisfies(self) -> None:
        for required, observed in (
            ("checks/c1", "checks/c2"),
            ("checks/c1", "checks/c3"),
            ("checks/c2", "checks/c3"),
        ):
            with self.subTest(required=required, observed=observed):
                self.assertFalse(ci.profile_meets_requirement(required, observed))

    def test_a_stronger_profile_is_bound_to_the_same_evidence(self) -> None:
        """Stronger-satisfies-weaker only after the base/head and
        trusted-workflow checks — which are unconditional in the decision
        tree, so a stronger profile with a stale base never passes."""
        run = run_record(base_sha="stale-base-sha")
        evidence = evaluate(run, required_profile="checks/c3")
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")


class NarrowedRunMergeTests(unittest.TestCase):
    """Merge-candidate narrowing: a narrowed run is `c2`.

    `checks.yml` names a non-candidate PR head's narrowed run `c2` and only
    the candidate's full run `c1`. A green narrowed run stays green feedback
    for a C2 lane, and can never satisfy a merge.
    """

    ENABLED = SimpleNamespace(policy=SimpleNamespace(merge_candidate_policy="label-v1"))

    def test_a_green_narrowed_run_is_green_feedback_for_a_c2_lane(self) -> None:
        evidence = evaluate(run_record(), required_profile="checks/c2",
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["accepted_profile"], "checks/c2")

    def test_a_green_narrowed_run_cannot_satisfy_the_merge(self) -> None:
        from git_gate.operations.landing import LandingOperationsMixin

        evidence = evaluate(run_record(), required_profile="checks/c2",
                            jobs=jobs_payload(name="c2"))
        with self.assertRaises(gate.GateError) as caught:
            LandingOperationsMixin._require_merge_profile(self.ENABLED, evidence)
        self.assertEqual(caught.exception.code, "PR_CI_NOT_GREEN")
        full = evaluate(run_record(), required_profile="checks/c2", jobs=jobs_payload())
        self.assertEqual(full["accepted_profile"], "checks/c1")
        LandingOperationsMixin._require_merge_profile(self.ENABLED, full)

    def test_the_candidate_projection_reads_a_narrowed_run_as_insufficient(self) -> None:
        evidence = evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")

    def test_the_strongest_profile_names_the_deploy_identity_job(self) -> None:
        self.assertEqual(ci.STRONGEST_PROFILE, "checks/c1")
        self.assertEqual(ci.STRONGEST_PROFILE_JOB, "c1")

    def test_only_a_newer_run_still_in_flight_supersedes(self) -> None:
        settled = run_record(run_id=10)
        newer = run_record(run_id=30, status="in_progress", conclusion=None)
        older = run_record(run_id=5, status="in_progress", conclusion=None)
        newer_settled = run_record(run_id=40, status="completed", conclusion="cancelled")
        other_head = run_record(run_id=50, head_sha="z" * 40, status="queued",
                                conclusion=None)
        runs = [older, settled, newer, newer_settled, other_head]
        successor = ci.newer_run_in_flight(runs, settled, "a" * 40)
        self.assertIsNotNone(successor)
        assert successor is not None
        self.assertEqual(successor["id"], 30)
        self.assertIsNone(ci.newer_run_in_flight([older, settled], settled, "a" * 40))
        self.assertIsNone(ci.newer_run_in_flight(runs, {"id": "10"}, "a" * 40))
        self.assertIsNone(ci.newer_run_in_flight(None, settled, "a" * 40))

    def test_a_newer_run_that_ended_without_a_verdict_supersedes_when_none_is_in_flight(
        self,
    ) -> None:
        """a cancelled promoted run must not hide behind the older one.

        Terminal preference ranks only success and failure, so a full run that
        was cancelled, timed out or ended `action_required` would leave the
        narrowed run selected and report a misleading CI_PROFILE_INSUFFICIENT.
        A run still in flight keeps priority; `skipped` (a stray label's
        `-meta-` run) and runs of other heads never supersede.
        """
        settled = run_record(run_id=10)
        older_ended = run_record(run_id=5, status="completed", conclusion="cancelled")
        for conclusion in ("cancelled", "timed_out", "action_required"):
            with self.subTest(conclusion=conclusion):
                ended = run_record(run_id=40, status="completed", conclusion=conclusion)
                earlier = run_record(run_id=30, status="completed", conclusion="cancelled")
                successor = ci.newer_run_in_flight(
                    [older_ended, settled, earlier, ended], settled, "a" * 40)
                self.assertIsNotNone(successor)
                assert successor is not None
                self.assertEqual(successor["id"], 40)
        running = run_record(run_id=30, status="in_progress", conclusion=None)
        cancelled = run_record(run_id=40, status="completed", conclusion="cancelled")
        successor = ci.newer_run_in_flight([settled, running, cancelled], settled, "a" * 40)
        assert successor is not None
        self.assertEqual(successor["id"], 30)
        skipped = run_record(run_id=40, status="completed", conclusion="skipped")
        other_head = run_record(run_id=50, head_sha="z" * 40, status="completed",
                                conclusion="cancelled")
        self.assertIsNone(ci.newer_run_in_flight(
            [older_ended, settled, skipped, other_head], settled, "a" * 40))


class NarrowedRunReviewTests(unittest.TestCase):
    """the snapshot review of a C1-class head accepts its narrowed run.

    The evaluator answers CI_PROFILE_INSUFFICIENT after the head and base
    binding but before the identity job's conclusion, so only a successful
    `c2` job in a successful run on the record's own head and base is
    accepted. Every record here comes from the real evaluator.
    """

    ENABLED = SimpleNamespace(merge_candidate_policy="label-v1")

    @staticmethod
    def _inspected(evidence: dict[str, object], *, head_sha: str = "a" * 40,
                   base_sha: str = "b" * 40, pull_request: int = 1,
                   main_sha: str = "b" * 40) -> dict[str, object]:
        """One inspect record; `_pull` carries the PR's verified main tip."""
        from git_gate.repository import PullRequest

        verdict, _headline = ci.derive_verdict(evidence)
        return {"head_sha": head_sha, "base_sha": base_sha, "pull_request": pull_request,
                "recorded_base_sha": "b" * 40, "ci_state": verdict,
                "_pull": PullRequest(1, "open", "main", main_sha, "task/test", "test/repo"),
                "_ci_status": {"verdict": verdict, "ci_evidence": evidence}}

    def _answer(self, inspected: dict[str, object], policy: object = None) -> tuple[str, str]:
        """The record's CI-evidence answer; every answer is a closed reason."""
        from git_gate.operations.landing import ci_evidence_answer

        answer = ci_evidence_answer(self.ENABLED if policy is None else policy, inspected)
        self.assertIn(answer.reason, ci.CI_ANSWER_REASONS[answer.state])
        return (answer.state, answer.reason)

    def test_a_successful_narrowed_run_of_a_c1_class_head_is_reviewable(self) -> None:
        evidence = evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(self._answer(self._inspected(evidence)), ("accepted", "NARROWED_RUN"))

    def test_green_ci_stays_reviewable_with_or_without_the_policy(self) -> None:
        evidence = evaluate(run_record(), required_profile="checks/c2",
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(self._answer(self._inspected(evidence)), ("accepted", "GREEN"))
        self.assertEqual(self._answer(self._inspected(evidence), SimpleNamespace()),
                         ("accepted", "GREEN"))

    def test_the_narrowed_run_needs_the_merge_candidate_policy(self) -> None:
        evidence = evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(self._answer(self._inspected(evidence), SimpleNamespace()),
                         ("rejected", "NARROWED_POLICY_OFF"))

    def test_a_failed_narrowed_job_is_insufficient_and_refused(self) -> None:
        """The profile check precedes the job's conclusion (ci.py)."""
        evidence = evaluate(run_record(conclusion="failure"),
                            required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2", conclusion="failure"))
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertIsNone(evidence["accepted_profile"])
        self.assertEqual(self._answer(self._inspected(evidence)),
                         ("rejected", "CI_PROFILE_INSUFFICIENT"))

    def test_a_failed_run_beside_a_successful_c2_job_is_refused(self) -> None:
        evidence = evaluate(run_record(conclusion="failure"),
                            required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(evidence["accepted_profile"], "checks/c2")
        self.assertEqual(self._answer(self._inspected(evidence)),
                         ("rejected", "CI_PROFILE_INSUFFICIENT"))

    def test_red_pending_cancelled_and_stale_evidence_is_refused(self) -> None:
        """An evaluator code the CI read decided is the rejection; one it could not is unknown."""
        cases = {
            "red": evaluate(run_record(conclusion="failure"), required_profile="checks/c2",
                            jobs=jobs_payload(name="c2", conclusion="failure")),
            "pending": evaluate(run_record(status="in_progress", conclusion=None),
                                required_profile=ci.STRONGEST_PROFILE),
            "cancelled": evaluate(run_record(conclusion="cancelled"),
                                  required_profile=ci.STRONGEST_PROFILE),
            "stale base": evaluate(run_record(base_sha="c" * 40),
                                   required_profile=ci.STRONGEST_PROFILE,
                                   jobs=jobs_payload(name="c2")),
            "stale head": evaluate(run_record(head_sha="d" * 40),
                                   required_profile=ci.STRONGEST_PROFILE,
                                   jobs=jobs_payload(name="c2")),
            "unavailable": evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                                    api_failure=True),
        }
        expected = {
            "red": ("CI_RUN_FAILED", ("rejected", "CI_RUN_FAILED")),
            "pending": ("CI_RUN_IN_PROGRESS", ("rejected", "CI_RUN_IN_PROGRESS")),
            "cancelled": ("CI_RUN_CANCELED", ("rejected", "CI_RUN_CANCELED")),
            "stale base": ("CI_BASE_STALE", ("rejected", "CI_BASE_STALE")),
            "stale head": ("CI_HEAD_STALE", ("rejected", "CI_HEAD_STALE")),
            "unavailable": ("CI_API_UNAVAILABLE", ("unknown", "CI_EVIDENCE_UNAVAILABLE")),
        }
        for name, evidence in cases.items():
            with self.subTest(name):
                code, answer = expected[name]
                self.assertEqual(evidence["reason_code"], code)
                self.assertEqual(self._answer(self._inspected(evidence)), answer)

    def test_only_a_c2_run_of_a_c1_class_head_counts(self) -> None:
        """A C2-class head's `c3` run is insufficient too, and proves less."""
        evidence = evaluate(run_record(), required_profile="checks/c2",
                            jobs=jobs_payload(name="c3"))
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(self._answer(self._inspected(evidence)),
                         ("rejected", "CI_PROFILE_INSUFFICIENT"))

    def test_evidence_for_another_pr_head_or_base_is_refused(self) -> None:
        evidence = evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        for binding in ({"head_sha": "e" * 40}, {"base_sha": "e" * 40},
                        {"pull_request": 2}):
            with self.subTest(binding):
                self.assertEqual(self._answer(self._inspected(evidence, **binding)),
                                 ("rejected", "EVIDENCE_BINDING_MISMATCH"))
        # Without the CI read the record keeps only its `ci_state`: nothing to
        # judge, so unknown. Without `_pull` the PR and main's tip bind nothing.
        for private, answer in (("_ci_status", ("unknown", "CI_EVIDENCE_UNAVAILABLE")),
                                ("_pull", ("rejected", "EVIDENCE_BINDING_MISMATCH"))):
            with self.subTest(missing=private):
                partial = {key: value for key, value in self._inspected(evidence).items()
                           if key != private}
                self.assertEqual(self._answer(partial), answer)

    def test_a_narrowed_run_on_the_recorded_base_is_reviewable(self) -> None:
        """the recorded-base projection need not equal main's tip."""
        evidence = evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        self.assertEqual(self._answer(self._inspected(evidence, main_sha="f" * 40)),
                         ("accepted", "NARROWED_RUN"))

    def test_a_narrowed_run_on_a_proven_later_base_is_reviewable(self) -> None:
        """the run judged a base between the fork point and main's tip."""
        run_base = "c" * 40
        evidence = evaluate(run_record(base_sha=run_base),
                            required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"), base_sha=run_base)
        self.assertEqual(evidence["base_sha"], run_base)
        inspected = self._inspected(evidence)
        self.assertEqual(self._answer(inspected), ("rejected", "EVIDENCE_BINDING_MISMATCH"))
        _dict(inspected["_ci_status"])["accepted_run_base"] = run_base
        self.assertEqual(self._answer(inspected), ("accepted", "NARROWED_RUN"))

    def test_a_narrowed_run_on_an_unproven_base_is_refused(self) -> None:
        run_base = "c" * 40
        evidence = evaluate(run_record(base_sha=run_base),
                            required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"), base_sha=run_base)
        for name, accepted in (("absent", None), ("another base", "d" * 40),
                               ("not a SHA", "not-a-sha"), ("not a string", 7)):
            with self.subTest(name):
                inspected = self._inspected(evidence)
                if accepted is not None:
                    _dict(inspected["_ci_status"])["accepted_run_base"] = accepted
                self.assertEqual(self._answer(inspected),
                                 ("rejected", "EVIDENCE_BINDING_MISMATCH"))

    def test_a_proven_run_base_does_not_loosen_the_other_bindings(self) -> None:
        run_base = "c" * 40
        evidence = evaluate(run_record(base_sha=run_base),
                            required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"), base_sha=run_base)
        for binding in ({"head_sha": "e" * 40}, {"pull_request": 2}, {"base_sha": "e" * 40}):
            with self.subTest(binding):
                inspected = self._inspected(evidence, **binding)
                _dict(inspected["_ci_status"])["accepted_run_base"] = run_base
                self.assertEqual(self._answer(inspected),
                                 ("rejected", "EVIDENCE_BINDING_MISMATCH"))
        failed = evaluate(run_record(base_sha=run_base, conclusion="failure"),
                          required_profile=ci.STRONGEST_PROFILE,
                          jobs=jobs_payload(name="c2", conclusion="failure"),
                          base_sha=run_base)
        inspected = self._inspected(failed)
        _dict(inspected["_ci_status"])["accepted_run_base"] = run_base
        self.assertEqual(self._answer(inspected), ("rejected", "CI_PROFILE_INSUFFICIENT"))

    def test_a_narrowed_run_needs_the_forge_proof_that_the_head_contains_its_base(self) -> None:
        """no narrowed run is accepted without a SHA recorded base.

        That answer is unknown, not a refusal. With a recorded base
        main's tip joins the accepted bases, so a run on the exact (head, tip)
        pair is accepted whichever fork point the forge records.
        """
        evidence = evaluate(run_record(), required_profile=ci.STRONGEST_PROFILE,
                            jobs=jobs_payload(name="c2"))
        for recorded in (None, "invalid"):
            with self.subTest(recorded=recorded):
                inspected = self._inspected(evidence)
                inspected["recorded_base_sha"] = recorded
                self.assertEqual(self._answer(inspected), ("unknown", "RECORDED_BASE_MISSING"))
        inspected = self._inspected(evidence)
        inspected["recorded_base_sha"] = "d" * 40
        self.assertEqual(self._answer(inspected), ("accepted", "NARROWED_RUN"))


class CiEvidenceAnswerTests(unittest.TestCase):
    """``ci.ci_evidence_answer``, the one rule every inspection path asks.

    Pure: the evidence comes from the real evaluator, and the keywords are an
    inspect record's own identity. Main's tip is ``b``, the forge's recorded
    base (the head's fork point) is ``d``, and ``c`` is a main commit between
    them. Every answer is checked against the closed reasons of its state.
    """

    HEAD, TIP, RECORDED, LATER = "a" * 40, "b" * 40, "d" * 40, "c" * 40

    def _answer(self, ci_status: object, **overrides: object) -> tuple[str, str]:
        keywords: dict[str, object] = {
            "pull_request": 1, "head_sha": self.HEAD, "main_tip": self.TIP,
            "recorded_base": self.RECORDED, "narrowed_policy": True, "range_known": True,
            **overrides,
        }
        answer = ci.ci_evidence_answer(ci_status, **keywords)  # type: ignore[arg-type]
        self.assertIn(answer.reason, ci.CI_ANSWER_REASONS[answer.state])
        return (answer.state, answer.reason)

    @staticmethod
    def _status(evidence: dict[str, object], **extra: object) -> dict[str, object]:
        verdict, _headline = ci.derive_verdict(evidence)
        return {"verdict": verdict, "ci_evidence": evidence, **extra}

    def _narrowed(self, run_base: str, *, judged: str | None = None,
                  **extra: object) -> dict[str, object]:
        """A trusted narrowed run on *run_base*, judged against *judged* (default: itself)."""
        return self._status(evaluate(run_record(base_sha=run_base),
                                     required_profile=ci.STRONGEST_PROFILE,
                                     jobs=jobs_payload(name="c2"),
                                     base_sha=judged or run_base), **extra)

    def test_the_states_and_their_reasons_are_closed_and_disjoint(self) -> None:
        reasons = ci.CI_ANSWER_REASONS
        self.assertEqual(set(reasons), {"accepted", "rejected", "unknown"})
        for one, other in (("accepted", "rejected"), ("accepted", "unknown"),
                           ("rejected", "unknown")):
            with self.subTest((one, other)):
                self.assertFalse(reasons[one] & reasons[other])
        # Every evaluator code is a rejection, but the three that say the CI
        # read itself failed: those answer unknown CI_EVIDENCE_UNAVAILABLE.
        undecided = {"CI_API_UNAVAILABLE", "CI_RESPONSE_INVALID", "CI_RESULT_AMBIGUOUS"}
        self.assertLessEqual(ci.CI_REASON_CODES - undecided, reasons["rejected"])
        self.assertFalse(undecided & reasons["rejected"])

    def test_green_is_accepted_without_a_binding(self) -> None:
        """The merge re-reads the exact pair, so green binds nothing here."""
        green = self._status(evaluate(run_record(), required_profile="checks/c2",
                                      jobs=jobs_payload(name="c2")))
        self.assertEqual(green["verdict"], "green")
        for overrides in ({}, {"narrowed_policy": False},
                          {"pull_request": None, "head_sha": None, "main_tip": None,
                           "recorded_base": None, "range_known": False}):
            with self.subTest(overrides):
                self.assertEqual(self._answer(green, **overrides), ("accepted", "GREEN"))
        self.assertEqual(self._answer({"verdict": "green"}), ("accepted", "GREEN"))

    def test_a_narrowed_run_on_a_base_between_the_fork_point_and_mains_tip_is_accepted(
            self) -> None:
        cases: dict[str, dict[str, object]] = {
            "main's tip": self._narrowed(self.TIP),
            "the recorded base": self._narrowed(self.RECORDED),
            "a proven later base": self._narrowed(self.LATER, accepted_run_base=self.LATER),
        }
        for name, status in cases.items():
            with self.subTest(name):
                self.assertEqual(self._answer(status), ("accepted", "NARROWED_RUN"))

    def test_a_narrowed_run_on_any_other_base_is_a_binding_mismatch(self) -> None:
        cases: dict[str, tuple[dict[str, object], dict[str, object]]] = {
            "an unproven later base": (self._narrowed(self.LATER), {}),
            "another proven base": (self._narrowed(self.LATER, accepted_run_base="e" * 40), {}),
            "a proven base that is no SHA": (
                self._narrowed(self.LATER, accepted_run_base="not-a-sha"), {}),
            "main's tip the record does not bind": (self._narrowed(self.TIP),
                                                    {"main_tip": None}),
            "another PR": (self._narrowed(self.TIP), {"pull_request": 2}),
            "no PR": (self._narrowed(self.TIP), {"pull_request": None}),
            "a bool PR": (self._narrowed(self.TIP), {"pull_request": True}),
            "another head": (self._narrowed(self.TIP), {"head_sha": "e" * 40}),
            "no head": (self._narrowed(self.TIP), {"head_sha": None}),
        }
        for name, (status, overrides) in cases.items():
            with self.subTest(name):
                self.assertEqual(self._answer(status, **overrides),
                                 ("rejected", "EVIDENCE_BINDING_MISMATCH"))

    def test_a_narrowed_run_without_a_recorded_base_is_unknown(self) -> None:
        """The fork-point guard holds: no fork point, no narrowed acceptance."""
        for recorded in (None, "invalid", "d" * 39):
            for status in (self._narrowed(self.TIP), self._narrowed(self.RECORDED)):
                with self.subTest(recorded=recorded, base=status["ci_evidence"]):
                    self.assertEqual(self._answer(status, recorded_base=recorded),
                                     ("unknown", "RECORDED_BASE_MISSING"))

    def test_without_the_policy_a_narrowed_run_is_refused_unless_its_range_is_unknown(
            self) -> None:
        status = self._narrowed(self.TIP)
        self.assertEqual(self._answer(status, narrowed_policy=False),
                         ("rejected", "NARROWED_POLICY_OFF"))
        self.assertEqual(self._answer(status, narrowed_policy=False, range_known=False),
                         ("unknown", "RANGE_UNCLASSIFIED"))
        # With the policy the narrowed run needs no class.
        self.assertEqual(self._answer(status, range_known=False), ("accepted", "NARROWED_RUN"))

    def test_a_stale_base_run_answers_with_its_run_base_proof(self) -> None:
        evidence = _dict(self._narrowed(self.LATER, judged=self.RECORDED)["ci_evidence"])
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        cases: dict[str, tuple[dict[str, object], tuple[str, str]]] = {
            "no proof asked": ({}, ("rejected", "CI_BASE_STALE")),
            "refused": ({"run_base_proof": "refused"}, ("rejected", "RUN_BASE_OUTSIDE_RANGE")),
            "could not run": ({"run_base_proof": "unknown"}, ("unknown", "MAIN_TIP_MISSING")),
        }
        for policy in (True, False):
            for name, (proof, expected) in cases.items():
                with self.subTest(name, policy=policy):
                    status = self._status(evidence, **proof)
                    self.assertEqual(self._answer(status, narrowed_policy=policy), expected)
                    self.assertEqual(self._answer(status, narrowed_policy=policy,
                                                  recorded_base=None),
                                     ("unknown", "RECORDED_BASE_MISSING"))

    def test_an_insufficient_success_of_an_unknown_range_is_unknown(self) -> None:
        """A range classified C1 only because it is unknown is no refusal."""
        success = self._status(evaluate(run_record(), required_profile="checks/c2",
                                        jobs=jobs_payload(name="c3")))
        failure = self._status(evaluate(run_record(conclusion="failure"),
                                        required_profile="checks/c2",
                                        jobs=jobs_payload(name="c3", conclusion="failure")))
        for status in (success, failure):
            self.assertEqual(_dict(status["ci_evidence"])["reason_code"],
                             "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(self._answer(success), ("rejected", "CI_PROFILE_INSUFFICIENT"))
        self.assertEqual(self._answer(success, range_known=False),
                         ("unknown", "RANGE_UNCLASSIFIED"))
        self.assertEqual(self._answer(failure, range_known=False),
                         ("rejected", "CI_PROFILE_INSUFFICIENT"))

    def test_a_ci_read_that_could_not_complete_is_unknown(self) -> None:
        record = run_record()
        prs = _list(record["pull_requests"])
        cases: dict[str, object] = {
            "listing failed": self._status(evaluate(run_record(), api_failure=True)),
            "payload invalid": self._status(evaluate(run_record(), malformed_response=True)),
            "two PR records": self._status(evaluate(
                run_record(pull_requests=[_dict(prs[0]), _dict(prs[0])]))),
            "no CI read": None,
            "not a mapping": [("verdict", "red")],
            "no evidence": {"verdict": "red"},
            "evidence not a mapping": {"verdict": "unavailable", "ci_evidence": "x"},
        }
        undecided = {"CI_API_UNAVAILABLE", "CI_RESPONSE_INVALID", "CI_RESULT_AMBIGUOUS"}
        for name, status in cases.items():
            with self.subTest(name):
                if isinstance(status, dict) and isinstance(status.get("ci_evidence"), dict):
                    self.assertIn(status["ci_evidence"]["reason_code"], undecided)
                self.assertEqual(self._answer(status), ("unknown", "CI_EVIDENCE_UNAVAILABLE"))

    def test_any_other_evidence_is_rejected_with_its_own_reason(self) -> None:
        cases: dict[str, tuple[dict[str, object], str]] = {
            "red": (self._status(evaluate(run_record(conclusion="failure"),
                                          required_profile="checks/c2",
                                          jobs=jobs_payload(name="c2", conclusion="failure"))),
                    "CI_RUN_FAILED"),
            "pending": (self._status(evaluate(run_record(status="in_progress", conclusion=None),
                                              required_profile=ci.STRONGEST_PROFILE)),
                        "CI_RUN_IN_PROGRESS"),
            "stale head": (self._status(evaluate(run_record(head_sha="e" * 40),
                                                 required_profile=ci.STRONGEST_PROFILE,
                                                 jobs=jobs_payload(name="c2"))),
                           "CI_HEAD_STALE"),
            "an unlisted code": ({"verdict": "red",
                                  "ci_evidence": {"reason_code": "SOMETHING_NEW"}},
                                 "PR_CI_NOT_GREEN"),
            "a code that is no string": ({"verdict": "red",
                                          "ci_evidence": {"reason_code": {"x": 1}}},
                                         "PR_CI_NOT_GREEN"),
        }
        for name, (status, reason) in cases.items():
            with self.subTest(name):
                self.assertEqual(self._answer(status), ("rejected", reason))


class EvidenceStateMappingTests(unittest.TestCase):
    """The four-state mapping, row by row. Only the four states may ever
    appear in ``result``; pending is not a fifth state."""

    ALLOWED_RESULTS = {"PASS", "FAIL", "NOT_RUN", "UNAVAILABLE"}

    def test_pending_is_not_a_fifth_state(self) -> None:
        queued = evaluate(run_record(status="queued", conclusion=None))
        in_flight = evaluate(run_record(status="in_progress", conclusion=None))
        for evidence in (queued, in_flight):
            self.assertIn(evidence["result"], self.ALLOWED_RESULTS)
            self.assertEqual(evidence["result"], "UNAVAILABLE")

    def test_queued_and_in_progress_carry_bounded_retry_hints(self) -> None:
        queued = evaluate(run_record(status="queued", conclusion=None))
        in_flight = evaluate(run_record(status="in_progress", conclusion=None))
        self.assertEqual(queued["reason_code"], "CI_RUN_QUEUED")
        self.assertEqual(queued["retry_after_seconds"], 60)
        self.assertEqual(in_flight["reason_code"], "CI_RUN_IN_PROGRESS")
        self.assertEqual(in_flight["retry_after_seconds"], 30)

    def test_green_required_profile_is_pass(self) -> None:
        evidence = evaluate(run_record())
        self.assertEqual(evidence["result"], "PASS")
        self.assertIsNone(evidence["reason_code"])
        self.assertEqual(evidence["accepted_profile"], "checks/c1")
        self.assertEqual(evidence["observed_profiles"], ["checks/c1"])
        self.assertEqual(evidence["required_profile"], "checks/c1")
        self.assertIs(evidence["failure_summary_available"], False)
        self.assertIsNone(evidence["trusted_workflow_ref"])
        self.assertEqual(evidence["workflow_run_id"], 7001)

    def test_pass_reports_real_per_job_timing(self) -> None:
        """job_timings is the SSH-less route to real CI duration -- straight
        from the same jobs-endpoint response the identity check already
        fetches, no second API call."""
        jobs = jobs_payload()
        for job in jobs["jobs"]:
            if job["name"] == "c1":
                job["started_at"] = "2020-01-01T00:00:00Z"
                job["completed_at"] = "2020-01-01T00:03:22Z"
        evidence = evaluate(run_record(), jobs=jobs)
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(
            [timing["id"] for timing in evidence["job_timings"]], evidence["job_ids"]
        )
        timings_by_name = {timing["name"]: timing for timing in evidence["job_timings"]}
        c1_timing = timings_by_name["c1"]
        self.assertEqual(c1_timing["id"], 200)
        self.assertEqual(c1_timing["started_at"], "2020-01-01T00:00:00Z")
        self.assertEqual(c1_timing["completed_at"], "2020-01-01T00:03:22Z")
        self.assertEqual(c1_timing["duration_seconds"], 202)

    def test_missing_timestamp_yields_none_duration_not_a_guess(self) -> None:
        jobs = jobs_payload()
        for job in jobs["jobs"]:
            if job["name"] == "c1":
                job["completed_at"] = None
        evidence = evaluate(run_record(), jobs=jobs)
        c1_timing = next(t for t in evidence["job_timings"] if t["name"] == "c1")
        self.assertIsNone(c1_timing["completed_at"])
        self.assertIsNone(c1_timing["duration_seconds"])

    def test_unparsable_timestamp_yields_none_duration_not_a_raise(self) -> None:
        jobs = jobs_payload()
        for job in jobs["jobs"]:
            if job["name"] == "c1":
                job["started_at"] = "not-a-timestamp"
        evidence = evaluate(run_record(), jobs=jobs)
        c1_timing = next(t for t in evidence["job_timings"] if t["name"] == "c1")
        self.assertEqual(c1_timing["started_at"], "not-a-timestamp")
        self.assertIsNone(c1_timing["duration_seconds"])

    def test_unavailable_carries_no_job_timings(self) -> None:
        """A run the gate never accepted has no jobs to time -- job_timings
        is empty for every UNAVAILABLE/NOT_RUN path, the same way job_ids
        already is."""
        evidence = evaluate(None, required_profile="checks/c1")
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RUN_MISSING")
        self.assertEqual(evidence["job_timings"], [])

    def test_stronger_c1_satisfies_c3_requirement(self) -> None:
        evidence = evaluate(run_record(), required_profile="checks/c3")
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["accepted_profile"], "checks/c1")

    def test_failed_required_profile_is_fail(self) -> None:
        run = run_record(conclusion="failure")
        evidence = evaluate(run, jobs=jobs_payload(identity_conclusion="failure"))
        self.assertEqual(evidence["result"], "FAIL")
        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")
        self.assertIsNone(evidence["accepted_profile"])
        self.assertIs(evidence["failure_summary_available"], True)

    def test_weaker_profile_is_unavailable_not_fail(self) -> None:
        """A weaker profile ran instead of the required one: the run did not
        fail the required profile, it never ran it."""
        run = run_record(conclusion="success")
        evidence = evaluate(
            run,
            required_profile="checks/c1",
            jobs=jobs_payload(identity_job="c2", identity_conclusion="success"),
        )
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(evidence["observed_profiles"], ["checks/c2"])

    def test_no_identity_job_ran_is_profile_missing(self) -> None:
        run = run_record(conclusion="success")
        evidence = evaluate(
            run,
            jobs={
                "total_count": 2,
                "jobs": [
                    {
                        "id": 1,
                        "run_id": 7001,
                        "name": "suites",
                        "workflow_name": "reusable",
                        "status": "completed",
                        "conclusion": "success",
                        "steps": [],
                    },
                    {
                        # `run_id` on both, so the reason this run reports no
                        # profile is the SKIPPED c1 below and nothing else. The
                        # identity proof also requires run membership, and
                        # without these the jobs would be excluded one stage
                        # earlier -- the same verdict for a different reason,
                        # which is how a test stops testing what it says.
                        "id": 2,
                        "run_id": 7001,
                        "name": "c1",
                        "workflow_name": "reusable",
                        "status": "completed",
                        "conclusion": "skipped",
                        "steps": [],
                    },
                ],
            },
        )
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")
        self.assertIsNone(evidence["retry_after_seconds"])

    def test_identity_job_not_yet_materialized_is_pending(self) -> None:
        """Forgejo may settle the run before listing its dependent c1 job."""
        jobs = jobs_payload()
        jobs["jobs"] = [
            job for job in _list(jobs["jobs"]) if _dict(job)["name"] != "c1"
        ]

        evidence = evaluate(run_record(conclusion="failure"), jobs=jobs)

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )
        verdict, _headline = ci.derive_verdict(evidence)
        self.assertEqual(verdict, "pending")

    def test_identity_job_still_running_is_pending(self) -> None:
        jobs = jobs_payload()
        c1 = next(
            _dict(job) for job in _list(jobs["jobs"]) if _dict(job)["name"] == "c1"
        )
        c1["status"] = "in_progress"
        c1["conclusion"] = None

        evidence = evaluate(run_record(conclusion="failure"), jobs=jobs)

        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    # --- the materialization window has an end ---------------------
    #
    # ``evaluate`` observes at 2020-01-01T00:00:00Z, so a job's own
    # ``completed_at`` is what places these runs before or after the bound.

    def test_a_run_with_no_profile_job_goes_missing_once_the_window_closes(
        self,
    ) -> None:
        """A run with conclusion failure.

        A workflow revision that named its only job ``validate`` has no
        c1/c2/c3 row and never will, so an unbounded provider would answer
        CI_PROFILE_PENDING with a 30-second retry hint on every read,
        forever. Ten minutes after the last job completed there
        is nothing left to wait for.
        """
        evidence = evaluate(
            run_record(conclusion="failure"),
            jobs=validate_only_jobs(completed_at="2019-12-31T23:50:00Z"),
        )

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")
        self.assertIsNone(evidence["retry_after_seconds"])
        verdict, headline = ci.derive_verdict(evidence)
        self.assertEqual(verdict, "unavailable")
        # The candidate run's own conclusion, so the line cannot be read as
        # "CI never ran": it ran, and proved nothing the gate accepts.
        self.assertIn("concluded failure", headline)
        self.assertIn("Republish a new head", headline)
        self.assertIn("c1/c2/c3 identity job", headline)
        self.assertIn("CI_PROFILE_MISSING", headline)

    def test_the_same_run_is_still_pending_inside_the_window(self) -> None:
        """Thirty seconds after the same run settled, nothing has changed
        about today's answer. The bound is the whole of the new behaviour."""
        evidence = evaluate(
            run_record(conclusion="failure"),
            jobs=validate_only_jobs(completed_at="2019-12-31T23:59:30Z"),
        )

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )
        self.assertEqual(ci.derive_verdict(evidence)[0], "pending")

    def test_a_job_still_running_holds_the_window_open_past_the_bound(
        self,
    ) -> None:
        """The bound closes on elapsed time AND on the run having no work
        left. A run with a job in flight may still produce an identity job,
        whatever its other completions say, so the clock alone must not be
        able to make this terminal."""
        jobs = validate_only_jobs(completed_at="2019-12-31T23:50:00Z")
        rows = _list(jobs["jobs"])
        rows.append(
            dict(
                _dict(rows[0]),
                id=901,
                name="late",
                status="in_progress",
                conclusion=None,
                completed_at=None,
            )
        )
        jobs["total_count"] = len(rows)

        evidence = evaluate(run_record(conclusion="failure"), jobs=jobs)

        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    # --- a Forgejo-blocked aggregate job never starts --------------
    #
    # Forgejo can kill a leaf job (conclusion ``failure``) while its
    # aggregate identity job sits in Forgejo's own ``blocked`` status
    # forever -- it will never be scheduled, because the dependency it
    # waited on already concluded. ``blocked`` translates to GitHub's
    # ``queued`` (``forge_status`` carries the untranslated word), so
    # ``_materialization_window_closed`` alone can never close here: it
    # requires every job row, including the blocked one, to read
    # ``completed``. ``evaluate`` observes at ``OBSERVED_AT``, so a job's
    # own value only ever set here is the identity job's ``forge_status``.

    @staticmethod
    def _blocked_c1_jobs(
        *, extra_jobs: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        """``jobs_payload()`` with ``c1`` Forgejo-``blocked`` and one
        sibling leaf (``leaf-test``) killed with conclusion
        ``failure`` -- the blocked-aggregate shape (one leaf
        killed, c1 blocked)."""
        jobs = jobs_payload(extra_jobs=extra_jobs)
        for job in _list(jobs["jobs"]):
            job = _dict(job)
            if job["name"] == "c1":
                job["status"] = "queued"
                job["conclusion"] = None
                job["forge_status"] = "blocked"
                job.pop("started_at", None)
                job.pop("completed_at", None)
            elif job["name"] == "leaf-test":
                job["conclusion"] = "failure"
        return jobs

    def test_blocked_aggregate_past_the_window_is_fail(self) -> None:
        """(a) c1 blocked, every sibling completed (one a failure), the
        run's own ``updated_at`` ten minutes stale: the aggregate never
        started after its dependencies concluded and never will."""
        jobs = self._blocked_c1_jobs()
        run = run_record(conclusion="failure", updated_at="2019-12-31T23:50:00Z")

        evidence = evaluate(run, jobs=jobs)

        self.assertEqual(evidence["result"], "FAIL")
        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")
        self.assertIsNone(evidence["accepted_profile"])
        self.assertIsNone(evidence["retry_after_seconds"])
        self.assertIs(evidence["failure_summary_available"], True)

    def test_blocked_aggregate_inside_the_window_is_pending(self) -> None:
        """(b) The same shape, thirty seconds after the run settled: nothing
        proves the aggregate is abandoned yet."""
        jobs = self._blocked_c1_jobs()
        run = run_record(conclusion="failure", updated_at="2019-12-31T23:59:30Z")

        evidence = evaluate(run, jobs=jobs)

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    def test_blocked_aggregate_with_a_sibling_still_running_is_pending(
        self,
    ) -> None:
        """(c) A sibling still ``in_progress`` past the window: the blocked
        job may yet be unblocked by work that has not finished, so the
        clock alone must not be able to make this terminal."""
        jobs = self._blocked_c1_jobs()
        for job in _list(jobs["jobs"]):
            job = _dict(job)
            if job["name"] == "leaf-test":
                job["status"] = "in_progress"
                job["conclusion"] = None
                job["completed_at"] = None
        run = run_record(conclusion="failure", updated_at="2019-12-31T23:50:00Z")

        evidence = evaluate(run, jobs=jobs)

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    def test_blocked_aggregate_with_no_run_updated_at_is_pending(self) -> None:
        """(d) Every sibling completed, but the run carries no ``updated_at``
        to age: no age known must never read as a closed window."""
        jobs = self._blocked_c1_jobs()
        run = run_record(conclusion="failure", updated_at=None)

        evidence = evaluate(run, jobs=jobs)

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    def test_merely_queued_aggregate_past_the_window_stays_pending(
        self,
    ) -> None:
        """(e) c1 genuinely ``queued`` (not Forgejo-``blocked``) past the
        window: only the word ``blocked`` is terminal -- an ordinary queued
        job may yet run, however stale the run's ``updated_at`` is."""
        jobs = jobs_payload()
        for job in _list(jobs["jobs"]):
            job = _dict(job)
            if job["name"] == "c1":
                job["status"] = "queued"
                job["conclusion"] = None
                job.pop("started_at", None)
                job.pop("completed_at", None)
            elif job["name"] == "leaf-test":
                job["conclusion"] = "failure"
        run = run_record(conclusion="failure", updated_at="2019-12-31T23:50:00Z")

        evidence = evaluate(run, jobs=jobs)

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    def test_production_sequence_blocked_aggregate_pending_then_fails(
        self,
    ) -> None:
        """(a) the forge module's own sequence: ``with_job_lifecycle`` normalizes
        the run before ``evaluate_evidence`` sees it. Without the blocked-aggregate
        rule in ``with_job_lifecycle`` a blocked c1 would flip the run to
        ``in_progress``, and CI_RUN_IN_PROGRESS would hide this branch
        entirely."""
        jobs = self._blocked_c1_jobs()

        stale_run = run_record(
            conclusion="failure", updated_at="2019-12-31T23:50:00Z"
        )
        normalized_stale = ci.with_job_lifecycle(stale_run, jobs)
        self.assertEqual(normalized_stale["status"], "completed")
        failed = evaluate(normalized_stale, jobs=jobs)
        self.assertEqual(failed["result"], "FAIL")
        self.assertEqual(failed["reason_code"], "CI_RUN_FAILED")

        fresh_run = run_record(
            conclusion="failure", updated_at="2019-12-31T23:59:30Z"
        )
        normalized_fresh = ci.with_job_lifecycle(fresh_run, jobs)
        self.assertEqual(normalized_fresh["status"], "completed")
        pending = evaluate(normalized_fresh, jobs=jobs)
        self.assertEqual(pending["result"], "UNAVAILABLE")
        self.assertEqual(pending["reason_code"], "CI_PROFILE_PENDING")

    def test_blocked_non_profile_row_without_a_profile_row_is_not_fail(
        self,
    ) -> None:
        """(c) A blocked row outside c1/c2/c3, with no profile row present
        at all: an unrelated blocked job must not read as a stranded
        identity job."""
        jobs = jobs_payload()
        jobs["jobs"] = [
            job
            for job in _list(jobs["jobs"])
            if _dict(job)["name"] not in ("c1", "c2", "c3")
        ]
        for job in _list(jobs["jobs"]):
            job = _dict(job)
            if job["name"] == "suites":
                job["status"] = "queued"
                job["conclusion"] = None
                job["forge_status"] = "blocked"
                job.pop("started_at", None)
                job.pop("completed_at", None)
        run = run_record(conclusion="failure", updated_at="2019-12-31T23:50:00Z")

        evidence = evaluate(run, jobs=jobs)

        self.assertNotEqual(evidence["result"], "FAIL")

    # --- a Forgejo-blocked RUN behind a failed job -----------------
    #
    # A lost CI worker leaves the run itself ``blocked`` with
    # no conclusion: one job ``failure``, its dependents ``blocked`` for ever.
    # ``blocked`` translates to ``queued``, so the run would read ``CI_RUN_QUEUED``
    # with no job read, never red, and a CI rerun would refuse it. The raw word
    # on the RUN (``forge_status``) is the signal the blocked-aggregate rule only had on jobs.
    # The blocked-aggregate shape (a ``completed`` run with a blocked aggregate)
    # carries no ``blocked`` run status and keeps its age window.

    @staticmethod
    def _blocked_run(forge_status: str = "blocked") -> dict[str, object]:
        """The run as ``_translated_run`` hands it over: the raw word kept
        beside the translated ``queued`` pair. ``run_record``'s default
        ``updated_at`` is the instant ``evaluate`` observes, so the run has
        just settled: a red read cannot lean on the blocked-aggregate age."""
        return {
            **run_record(status="queued", conclusion=None),
            "forge_status": forge_status,
        }

    @staticmethod
    def _stranded_jobs() -> dict[str, object]:
        """One job ``failure`` (``leaf-test``), its dependents ``c1``
        and ``status`` Forgejo-``blocked``, ``suites`` ``success``, and the
        two other profile jobs ``skipped``."""
        jobs = jobs_payload()
        for job in _list(jobs["jobs"]):
            job = _dict(job)
            if job["name"] == "leaf-test":
                job["conclusion"] = "failure"
                job["forge_status"] = "failure"
            elif job["name"] in ("c1", "status"):
                job["status"] = "queued"
                job["conclusion"] = None
                job["forge_status"] = "blocked"
                job.pop("started_at", None)
                job.pop("completed_at", None)
        return jobs

    @staticmethod
    def _set_job(
        jobs: dict[str, object], name: str, **change: object
    ) -> dict[str, object]:
        for job in _list(jobs["jobs"]):
            if _dict(job)["name"] == name:
                _dict(job).update(change)
        return jobs

    def test_a_blocked_run_behind_a_failed_job_is_red(self) -> None:
        """(a) The run reads as the failed run it is, the failed job named."""
        jobs = self._stranded_jobs()

        run = ci.with_job_lifecycle(self._blocked_run(), jobs)
        evidence = evaluate(run, jobs=jobs)

        self.assertEqual((run["status"], run["conclusion"]), ("completed", "failure"))
        self.assertEqual(run["forge_status"], "blocked")
        self.assertEqual(evidence["result"], "FAIL")
        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")
        self.assertIsNone(evidence["accepted_profile"])
        self.assertIsNone(evidence["retry_after_seconds"])
        self.assertIs(evidence["failure_summary_available"], True)
        self.assertEqual(ci.derive_verdict(evidence)[0], "red")
        self.assertEqual(
            [_str(_dict(job)["name"]) for job in ci.failed_jobs_of(jobs)],
            ["leaf-test"],
        )

    def test_a_blocked_run_with_a_timed_out_job_is_red(self) -> None:
        jobs = self._set_job(
            self._stranded_jobs(), "leaf-test", conclusion="timed_out"
        )

        run = ci.with_job_lifecycle(self._blocked_run(), jobs)

        self.assertEqual(evaluate(run, jobs=jobs)["result"], "FAIL")

    def test_a_blocked_run_that_is_not_stranded_by_a_failed_job_stays_queued(
        self,
    ) -> None:
        """(c) Negative controls: each variation must still read pending
        ``CI_RUN_QUEUED``, because work may yet run or nothing failed."""
        cases: dict[str, tuple[dict[str, object], dict[str, object]]] = {
            "no failed job": (
                self._blocked_run(),
                self._set_job(
                    self._stranded_jobs(),
                    "leaf-test",
                    conclusion="success",
                    forge_status="success",
                ),
            ),
            "a cancelled job is not a failure": (
                self._blocked_run(),
                self._set_job(
                    self._stranded_jobs(),
                    "leaf-test",
                    conclusion="cancelled",
                    forge_status="cancelled",
                ),
            ),
            "a waiting row beside the failed job": (
                self._blocked_run(),
                self._set_job(
                    self._stranded_jobs(),
                    "status",
                    status="queued",
                    forge_status="waiting",
                ),
            ),
            "a running row beside the failed job": (
                self._blocked_run(),
                self._set_job(
                    self._stranded_jobs(),
                    "status",
                    status="in_progress",
                    forge_status="running",
                ),
            ),
            "a waiting run, not a blocked one": (
                self._blocked_run("waiting"),
                self._stranded_jobs(),
            ),
            "a run with no raw word": (
                run_record(status="queued", conclusion=None),
                self._stranded_jobs(),
            ),
            "an empty job list": (self._blocked_run(), {"jobs": []}),
            "an unreadable job list": (self._blocked_run(), {"jobs": [None]}),
            "no job list": (self._blocked_run(), {"jobs": None}),
        }
        for label, (run, jobs) in cases.items():
            with self.subTest(label):
                normalized = ci.with_job_lifecycle(run, jobs)
                self.assertEqual(normalized, run)
                evidence = evaluate(normalized, jobs=jobs)
                self.assertEqual(evidence["result"], "UNAVAILABLE")
                self.assertEqual(evidence["reason_code"], "CI_RUN_QUEUED")
                self.assertEqual(ci.derive_verdict(evidence)[0], "pending")

    def test_only_a_blocked_run_reads_its_jobs_before_it_is_terminal(self) -> None:
        """A merely queued or waiting run is still decided without a read."""
        self.assertTrue(ci.needs_jobs_fetch(self._blocked_run()))
        for status, forge_status in (
            ("queued", "queued"),
            ("queued", "waiting"),
            ("in_progress", "running"),
        ):
            with self.subTest(forge_status):
                run = {
                    **run_record(status=status, conclusion=None),
                    "forge_status": forge_status,
                }
                self.assertFalse(ci.needs_jobs_fetch(run))
        self.assertFalse(ci.needs_jobs_fetch(run_record(status="queued", conclusion=None)))

    def test_a_running_run_with_an_early_failed_job_stays_in_progress(self) -> None:
        """(d) Unchanged by the blocked-run rule: a job is still running, so the run may
        yet change. The verdict stays pending ``CI_RUN_IN_PROGRESS``; the
        failed job reaches the reader only as advisory ``ci_progress``."""
        run = {
            **run_record(status="in_progress", conclusion=None),
            "forge_status": "running",
        }
        jobs = self._set_job(
            self._stranded_jobs(), "status", status="in_progress", forge_status="running"
        )

        normalized = ci.with_job_lifecycle(run, jobs)
        evidence = evaluate(normalized, jobs=jobs)

        self.assertFalse(ci.needs_jobs_fetch(run))
        self.assertEqual(normalized, run)
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RUN_IN_PROGRESS")
        self.assertEqual(ci.derive_verdict(evidence)[0], "pending")

    def test_a_completed_run_with_a_blocked_aggregate_keeps_its_age_window(self) -> None:
        """The blocked-aggregate shape has no ``blocked`` run status, so the blocked-run rule's
        immediate red does not touch it: a fresh run still reads pending."""
        jobs = self._stranded_jobs()
        run = {
            **run_record(conclusion="failure", updated_at="2019-12-31T23:59:30Z"),
            "forge_status": "failure",
        }

        evidence = evaluate(ci.with_job_lifecycle(run, jobs), jobs=jobs)

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")

    # --- the bound cannot depend on job rows ------------------------
    #
    # The window above is measured from job ``completed_at`` values, so a jobs
    # payload with no rows kept the unbounded retry hint. These date the run
    # itself instead: ``evaluate`` still observes at ``OBSERVED_AT``, and it
    # is the run's ``updated_at`` that places it before or after the bound.

    def test_an_empty_jobs_payload_is_pending_while_the_run_is_fresh(
        self,
    ) -> None:
        """A run can answer ``job_ids: []`` beside conclusion
        failure for a few minutes and then produce its c1 row. Thirty seconds
        after that run settled, an empty payload is that window and the
        answer is unchanged."""
        evidence = evaluate(
            run_record(
                conclusion="failure", updated_at="2019-12-31T23:59:30Z"
            ),
            jobs=empty_jobs(),
        )

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )
        self.assertEqual(ci.derive_verdict(evidence)[0], "pending")

        # The bound's own edge, paired with the 601-second case below: 600
        # seconds is still inside it. Together the two fix the constant at
        # exactly ten minutes, which a pair of loose measurements would not --
        # a bound quietly shortened to one minute would satisfy "30 seconds
        # pends, 601 seconds is missing" while calling live windows dead.
        at_the_bound = evaluate(
            run_record(
                conclusion="failure", updated_at="2019-12-31T23:50:00Z"
            ),
            jobs=empty_jobs(),
        )
        self.assertEqual(at_the_bound["reason_code"], "CI_PROFILE_PENDING")

    def test_an_empty_jobs_payload_goes_missing_once_the_run_settles_out(
        self,
    ) -> None:
        """the same payload, 601 seconds after the run settled.

        A terminal run whose jobs listing stays empty has no job row to date,
        so before this the window could never close and every read answered
        CI_PROFILE_PENDING with a 30-second hint forever. Past
        ``EMPTY_JOBS_WINDOW_SECONDS`` the run's own ``updated_at`` closes it,
        and the answer becomes the terminal one.
        """
        evidence = evaluate(
            run_record(
                conclusion="failure", updated_at="2019-12-31T23:49:59Z"
            ),
            jobs=empty_jobs(),
        )

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")
        self.assertIsNone(evidence["retry_after_seconds"])
        self.assertEqual(evidence["job_ids"], [])
        verdict, headline = ci.derive_verdict(evidence)
        self.assertEqual(verdict, "unavailable")
        # The one CI_PROFILE_MISSING headline serves this cause too: the
        # record carries no job count, so the line cannot honestly say
        # "the forge listed no jobs", and the remedy is the same either way.
        self.assertIn("concluded failure", headline)
        self.assertIn("Republish a new head", headline)
        self.assertIn("c1/c2/c3 identity job", headline)
        self.assertIn("CI_PROFILE_MISSING", headline)

    def test_an_empty_jobs_payload_on_a_live_run_is_never_closed_by_time(
        self,
    ) -> None:
        """Elapsed time alone must not be able to end a run that is moving.

        Both halves are asserted because only the second can fail here. A run
        short of ``completed`` is answered by the status stage long before the
        profile branch, whatever its age. The one this bound has to refuse for
        itself is the run that LOOKS terminal:
        ``gate_backend_forgejo._UNRECOGNISED_STATUS`` translates any Forgejo
        status outside its table to ``("completed", "action_required")``, so a
        status meaning "still moving" that the platform has not met yet
        arrives at this branch dressed as a settled run.
        """
        aged = "2019-12-31T23:49:59Z"
        live = evaluate(
            run_record(status="in_progress", conclusion=None, updated_at=aged),
            jobs=empty_jobs(),
        )
        self.assertEqual(live["reason_code"], "CI_RUN_IN_PROGRESS")
        self.assertEqual(
            live["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )
        self.assertEqual(ci.derive_verdict(live)[0], "pending")

        unrecognised = evaluate(
            run_record(conclusion="action_required", updated_at=aged),
            jobs=empty_jobs(),
        )
        self.assertEqual(unrecognised["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            unrecognised["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    def test_an_undated_run_with_an_empty_jobs_payload_stays_pending(
        self,
    ) -> None:
        """No clock is not an expired clock.

        ``updated_at`` is the only field the Forgejo translation carries that
        moves when a run settles, and a record without it cannot be aged. That
        must read as "unknown", never as "old enough": an unreadable timestamp
        turning into a terminal verdict is the failure this whole area exists
        to avoid.
        """
        evidence = evaluate(
            run_record(conclusion="failure", updated_at=None),
            jobs=empty_jobs(),
        )

        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )

    def test_job_rows_are_dated_by_the_jobs_and_not_by_the_run(self) -> None:
        """The two clocks do not leak into each other.

        The positive control for the materialization-window bound surviving the empty-jobs bound. With job
        rows present the 300-second job clock decides alone: a run that
        settled 30 seconds ago still goes missing when its last job completed
        600 seconds ago, and a run that settled 601 seconds ago stays pending
        while a job completed 30 seconds ago. Without this, dating the empty
        case from the run could quietly have redated the populated one too.
        """
        job_clock_expired = evaluate(
            run_record(
                conclusion="failure", updated_at="2019-12-31T23:59:30Z"
            ),
            jobs=validate_only_jobs(completed_at="2019-12-31T23:50:00Z"),
        )
        self.assertEqual(job_clock_expired["reason_code"], "CI_PROFILE_MISSING")

        job_clock_fresh = evaluate(
            run_record(
                conclusion="failure", updated_at="2019-12-31T23:49:59Z"
            ),
            jobs=validate_only_jobs(completed_at="2019-12-31T23:59:30Z"),
        )
        self.assertEqual(job_clock_fresh["reason_code"], "CI_PROFILE_PENDING")

    def test_not_required_is_not_run(self) -> None:
        evidence = evaluate(run_record(), required_profile=None)
        self.assertEqual(evidence["result"], "NOT_RUN")
        self.assertEqual(evidence["reason_code"], "CI_NOT_ONBOARDED")
        self.assertIsNone(evidence["workflow_run_id"])

    def test_missing_run_is_unavailable(self) -> None:
        evidence = evaluate(None)
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RUN_MISSING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )
        verdict, headline = ci.derive_verdict(evidence)
        self.assertEqual(verdict, "pending")
        self.assertIn("waiting for exact-head run", headline)

    def test_api_failure_is_unavailable(self) -> None:
        evidence = evaluate(None, api_failure=True)
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_API_UNAVAILABLE")

    def test_malformed_response_is_unavailable(self) -> None:
        evidence = evaluate(None, malformed_response=True)
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")

    def test_run_level_cancelled_skipped_timed_out(self) -> None:
        for conclusion, code in (
            ("cancelled", "CI_RUN_CANCELED"),
            ("skipped", "CI_RUN_SKIPPED"),
            ("timed_out", "CI_RUN_TIMED_OUT"),
        ):
            with self.subTest(conclusion=conclusion):
                evidence = evaluate(run_record(conclusion=conclusion))
                self.assertEqual(evidence["result"], "UNAVAILABLE")
                self.assertEqual(evidence["reason_code"], code)

    def test_identity_job_cancelled_and_timed_out(self) -> None:
        run = run_record(conclusion="success")
        cancelled = evaluate(run, jobs=jobs_payload(identity_conclusion="cancelled"))
        self.assertEqual(cancelled["reason_code"], "CI_RUN_CANCELED")
        timed_out = evaluate(run, jobs=jobs_payload(identity_conclusion="timed_out"))
        self.assertEqual(timed_out["reason_code"], "CI_RUN_TIMED_OUT")

    def test_unknown_identity_conclusion_is_response_invalid(self) -> None:
        run = run_record(conclusion="success")
        evidence = evaluate(
            run, jobs=jobs_payload(identity_conclusion="action_required")
        )
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")

    def test_two_identity_jobs_ran_is_ambiguous(self) -> None:
        run = run_record(conclusion="success")
        second = jobs_payload(identity_job="c2", identity_conclusion="success")
        second_jobs = _list(second["jobs"])
        extra = _dict(second_jobs[3])
        evidence = evaluate(run, jobs=jobs_payload(extra_jobs=[extra]))
        self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")

    def test_unexpected_run_status_is_response_invalid(self) -> None:
        evidence = evaluate(run_record(status="requested", conclusion=None))
        self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")


class TrustedWorkflowProofTests(unittest.TestCase):
    """The Forgejo route: each tamper vector fails the trust proof
    closed. (The reusable-workflow vectors -- wrong referenced ref, wrong
    reusable path, duplicate entries -- died with the GitHub profile;
    the file-existence probe is the surviving proof and it is pinned
    below and in ``ForgejoTrustProfileTests``.)"""

    def test_non_pull_request_event_is_untrusted(self) -> None:
        evidence = evaluate(run_record(event="push"))
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_launcher_path_swap_is_untrusted(self) -> None:
        evidence = evaluate(run_record(path=".github/workflows/evil.yml"))
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_launcher_file_missing_is_untrusted(self) -> None:
        evidence = evaluate(run_record(), launcher_file_exists=False)
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_probe_not_performed_fails_closed(self) -> None:
        evidence = evaluate(run_record(), launcher_file_exists=None)
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")


# --- the Forgejo trust profile ----------------------------------------------
#
# THE PROPERTY, and it is the same one on every forge: a pull request must
# not be able to weaken the CI that judges it. The GitHub
# route (a reusable workflow pinned to the base ref, reported in
# ``referenced_workflows``) does not apply to Forgejo. Forgejo
# emits that field on no endpoint, and its launcher runs on
# ``pull_request_target``, which is DEFINED to load the workflow file from
# the base ref before any job exists — so the surviving proof is the
# launcher's file-existence at the base commit, and the event/path
# comparisons. There is exactly one profile now; a caller that forgets to
# name it measures the Forgejo route by default, which is the route every
# operational lane uses.


class ForgejoTrustProfileTests(unittest.TestCase):
    """The Forgejo route: `pull_request_target` from the base ref.

    Each test states which single fact it moves away from the trusted Forgejo
    shape, because "untrusted" is the answer to almost every input and a test
    that does not name its one variable proves nothing about which check
    refused.
    """

    def test_a_forgejo_shaped_run_reaches_a_real_verdict(self) -> None:
        """The false negative the profile exists to remove.

        Before the profile, every Forgejo run refused ``CI_WORKFLOW_UNTRUSTED``
        on the event alone. This is the positive control for the whole class:
        without it, the eight refusals below would all still pass against an
        evaluator that refused everything, which is exactly the shape of a
        check that cannot fail.
        """
        evidence = evaluate(
            run_record(), trust_profile=ci.FORGEJO_TRUST_PROFILE
        )
        self.assertEqual(evidence["result"], "PASS")
        self.assertIsNone(evidence["reason_code"])
        self.assertEqual(evidence["workflow_path"], FORGEJO_LAUNCHER)
        # The GitHub-only field is empty rather than invented: there is no
        # resolved reusable SHA on Forgejo, and reporting one would be a
        # claim about a resolution that never happened.
        self.assertIsNone(evidence["trusted_workflow_ref"])

    def test_another_forges_event_is_untrusted_under_the_forgejo_profile(self) -> None:
        """The events are not interchangeable in EITHER direction.

        ``pull_request`` is GitHub's trusted event and is the natural mistake:
        a Forgejo run really can be triggered by it, and on Forgejo that event
        takes the workflow file from the pull request's own head -- the exact
        shape the rule exists to forbid. It has to refuse here even though the
        very same value is the trusted one one profile over.
        """
        for event in ("pull_request", "push", "workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                evidence = evaluate(
                    run_record(event=event),
                    trust_profile=ci.FORGEJO_TRUST_PROFILE,
                )
                self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_the_github_launcher_path_is_untrusted_under_the_forgejo_profile(
        self,
    ) -> None:
        """The paths are not interchangeable either. A run whose event is
        right but whose launcher lives in the other forge's directory is not
        the workflow this profile vouches for."""
        evidence = evaluate(
            run_record(path=TRUSTED_LAUNCHER),
            trust_profile=ci.FORGEJO_TRUST_PROFILE,
        )
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_a_sibling_forgejo_workflow_is_untrusted(self) -> None:
        """The check is the exact launcher, not the directory.

        A pull request can add any file it likes under ``.forgejo/workflows/``,
        so a prefix match would let it supply its own judge; ``checks.yml`` is
        the only file the profile names.
        """
        for path in (
            ".forgejo/workflows/evil.yml",
            ".forgejo/workflows/checks.yaml",
            ".forgejo/workflows/checks.yml.bak",
        ):
            with self.subTest(path=path):
                evidence = evaluate(
                    run_record(path=path),
                    trust_profile=ci.FORGEJO_TRUST_PROFILE,
                )
                self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_the_file_probe_gates_the_forgejo_profile(self) -> None:
        """Dropping the reusable-ref check does not drop the file proof.

        False means the probe answered "no such file"; None means no probe was
        made, and both refuse -- an unproven trust claim is not a weaker one,
        it is no claim at all.
        """
        for probe in (False, None):
            with self.subTest(launcher_file_exists=probe):
                evidence = evaluate(
                    run_record(),
                    trust_profile=ci.FORGEJO_TRUST_PROFILE,
                    launcher_file_exists=probe,
                )
                self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_the_default_profile_measures_the_forgejo_route(self) -> None:
        """The default value is safe because it measures the right route.

        ``evaluate_evidence``'s ``trust_profile`` defaults to the Forgejo
        profile (the one operational route now that the GitHub
        backend is removed). A caller that forgets to pass one therefore measures a
        Forgejo run against the Forgejo route -- and a GitHub-shaped run
        (the other forge's event or launcher directory) still refuses.
        Asserted with the argument OMITTED, because the omission is the
        mistake that will actually happen.
        """
        defaulted = evaluate(run_record())
        self.assertEqual(defaulted["result"], "PASS")
        defaulted_wrong_event = evaluate(run_record(event="pull_request"))
        self.assertEqual(defaulted_wrong_event["reason_code"], "CI_WORKFLOW_UNTRUSTED")
        defaulted_wrong_path = evaluate(run_record(path=TRUSTED_LAUNCHER))
        self.assertEqual(defaulted_wrong_path["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_the_profile_table_holds_exactly_the_forgejo_profile(self) -> None:
        """The table must stay in lock-step with the catalog contract.

        ``catalog``'s ``_FORGES_WITH_CI_PROVIDER_CONTRACT`` names the forges
        whose CI the gate can read; a profile present here but absent there
        (or the reverse) is two answers to one question. Gitea must not
        acquire a profile by someone copying a line: it has no Actions
        implementation at all, so it has no trust proof -- which is not the
        same as having a weak one.
        """
        self.assertEqual(set(ci.TRUST_PROFILES), {"forgejo"})
        self.assertNotIn("gitea", ci.TRUST_PROFILES)
        self.assertEqual(
            set(ci.TRUST_PROFILES),
            set(gate.catalog._FORGES_WITH_CI_PROVIDER_CONTRACT),
        )

    def test_each_profile_pins_its_own_route(self) -> None:
        """The two field values, restated rather than imported.

        A test that reads the values it checks passes for any values, and each
        of these is one edit away from silently disarming a check above: the
        event and the path decide what is accepted.
        """
        self.assertEqual(
            ci.FORGEJO_TRUST_PROFILE,
            ci.TrustProfile(
                event="pull_request_target",
                launcher_path=".forgejo/workflows/checks.yml",
            ),
        )
        self.assertEqual(ci.FORGEJO_LAUNCHER_PATH, ".forgejo/workflows/checks.yml")
        self.assertIs(ci.TRUST_PROFILES["forgejo"], ci.FORGEJO_TRUST_PROFILE)


# --- the profile-aware identity proof ---------------------------------------
#
# The last of the three trust proofs to become profile-aware. It asks: did
# the job that reports this profile's result really come from the trusted
# workflow, in the run just proved trusted?
#
# GitHub answers with `workflow_name == reusable`: the job names the
# reusable workflow it ran inside, so a tampered launcher's job cannot claim
# it. Forgejo has no reusable workflow -- there is no second file for a job to
# name -- so demanding that name would refuse every run on the forge, which is
# a false negative rather than a safe default. What answers there instead is
# the chain already proved: `pull_request_target` bound the launcher to the
# base ref, the probe proved that launcher present at the base commit, and run
# membership proves the job is that launcher's.
#
# One link shorter, one fewer FILE to bind to. The tests below exist to keep
# "shorter" from becoming "weaker": run membership is required, and there is
# no reusable-name check beside it -- Forgejo has no second file for a job
# to name, so the chain above is the whole proof.


def jobs_payload(
    *,
    run_id: int = 7001,
    name: str = "c1",
    conclusion: str = "success",
    workflow_name: str | None = None,
    suites_conclusion: str = "success",
    identity_job: str = "c1",
    identity_conclusion: str = "success",
    identity_failed_steps: list[dict[str, object]] | None = None,
    extra_jobs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """The jobs envelope in the documented REST shape (six jobs per run).

    No job carries ``workflow_name`` by default -- Forgejo's jobs
    endpoint reports none and ``_translated_job`` invents none. The identity
    job carries one only when a caller supplies it (the field-is-ignored
    tests); the GitHub reusable-name mechanism that gave every job
    ``workflow_name: reusable`` does not apply to Forgejo.

    ``run_id`` defaults to ``run_record``'s own id so the identity job
    belongs to the run by default, which is what makes "belongs to a
    DIFFERENT run" an explicit, visible argument at every call site that
    wants it. ``name`` and ``conclusion`` are the new-profile spellings of
    the identity job's name and conclusion; ``identity_job`` and
    ``identity_conclusion`` are the original spellings and mean the same
    fields.
    """
    skipped2: dict[str, object] = {
        "id": 100,
        "run_id": run_id,
        "name": "c2",
        "status": "completed",
        "conclusion": "skipped",
        "head_sha": "a" * 40,
        "started_at": "2020-01-01T00:00:00Z",
        "completed_at": "2020-01-01T00:00:00Z",
        "steps": [],
    }
    skipped3 = dict(skipped2, id=101, name="c3")
    identity: dict[str, object] = {
        "id": 200,
        "run_id": run_id,
        "name": identity_job if name == "c1" else name,
        "status": "completed",
        "conclusion": (
            identity_conclusion if conclusion == "success" else conclusion
        ),
        "head_sha": "a" * 40,
        "started_at": "2020-01-01T00:00:00Z",
        "completed_at": "2020-01-01T00:00:00Z",
        "steps": identity_failed_steps
        if identity_failed_steps is not None
        else [
            {
                "name": "Require both check jobs to have succeeded",
                "number": 1,
                "status": "completed",
                "conclusion": (
                    identity_conclusion if conclusion == "success" else conclusion
                ),
            }
        ],
    }
    if workflow_name is not None:
        identity["workflow_name"] = workflow_name
    suites: dict[str, object] = {
        "id": 300,
        "run_id": run_id,
        "name": "suites",
        "status": "completed",
        "conclusion": suites_conclusion,
        "head_sha": "a" * 40,
        "started_at": "2020-01-01T00:00:00Z",
        "completed_at": "2020-01-01T00:00:00Z",
        "steps": [
            {
                "name": "Checks (profile c1)",
                "number": 1,
                "status": "completed",
                "conclusion": suites_conclusion,
            }
        ],
    }
    leaf_job: dict[str, object] = {
        "id": 400,
        "run_id": run_id,
        "name": "leaf-test",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "a" * 40,
        "started_at": "2020-01-01T00:00:00Z",
        "completed_at": "2020-01-01T00:00:00Z",
        "steps": [
            {
                "name": "Full check (profile c1)",
                "number": 1,
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }
    status_job: dict[str, object] = {
        "id": 500,
        "run_id": run_id,
        "name": "status",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "a" * 40,
        "started_at": "2020-01-01T00:00:00Z",
        "completed_at": "2020-01-01T00:00:00Z",
        "steps": [
            {
                "name": "Publish combined status",
                "number": 1,
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }
    jobs: list[dict[str, object]] = [
        suites,
        leaf_job,
        status_job,
        identity,
        skipped2,
        skipped3,
    ]
    if extra_jobs:
        jobs.extend(extra_jobs)
    return {"total_count": len(jobs), "jobs": jobs}


def empty_jobs() -> dict[str, object]:
    """The jobs envelope the empty-jobs hole was found on: no rows at all.

    `/actions/runs/{id}/jobs` answers this while the forge has registered no
    job for the run yet. A run can hold it beside ``conclusion: failure`` for
    a few minutes and then produce its c1 row; whether it can be
    permanent is unproven, which is why the bound applied to it is taken from
    the run rather than from a job row that may never arrive.

    A function rather than a module constant: the evaluator only reads this
    envelope, but a shared mutable fixture is one careless test away from
    carrying state between cases.
    """
    return {"total_count": 0, "jobs": []}


def validate_only_jobs(
    *, completed_at: str, run_id: int = 7001
) -> dict[str, object]:
    """The jobs envelope for the materialization-window case: ONE job, and it is not a
    profile job.

    A run can be judged by a workflow revision whose
    only job is named ``validate``. No c1/c2/c3 row belongs to that run and
    none ever will, which is the case the materialization window had no end
    for.

    Built from ``jobs_payload``'s own row rather than a second literal, so
    the fields the provider actually reads -- ``run_id``, ``name``,
    ``status``, ``conclusion``, ``completed_at`` -- stay the ones every other
    test in this file exercises.
    """
    template = _dict(_list(jobs_payload(run_id=run_id)["jobs"])[0])
    return {
        "total_count": 1,
        "jobs": [
            dict(
                template,
                id=900,
                name="validate",
                conclusion="failure",
                started_at=completed_at,
                completed_at=completed_at,
            )
        ],
    }


class ProfileAwareIdentityProofTests(unittest.TestCase):
    """Which jobs count as identity evidence, per profile."""

    def test_a_forgejo_job_with_no_workflow_name_is_identity_evidence(self) -> None:
        """The positive control, and the gap this closed.

        Without it every refusal below would pass against an identity proof
        that accepted nothing -- the shape of a check that cannot succeed,
        which is how the Forgejo path sat on ``CI_PROFILE_MISSING``.
        """
        evidence = evaluate(
            run_record(),
            trust_profile=ci.FORGEJO_TRUST_PROFILE,
            jobs=jobs_payload(),
        )
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["accepted_profile"], "checks/c1")

    def test_a_job_from_another_run_is_not_identity_evidence(self) -> None:
        """THE ONE THE RUN BINDING CARRIES. A job named ``c1`` somewhere is not
        evidence about this pull request.

        With ``workflow_name`` gone on Forgejo, membership of the proved run is
        what remains, so a job belonging to a different run must not satisfy
        it. Otherwise any green ``c1`` anywhere in the repository -- a run of
        main, an unrelated pull request -- would answer for this one.
        """
        for foreign in (42, 41, 0, -1):
            with self.subTest(run_id=foreign):
                evidence = evaluate(
                    run_record(),
                    trust_profile=ci.FORGEJO_TRUST_PROFILE,
                    jobs=jobs_payload(run_id=foreign),
                )
                self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")

    def test_run_membership_is_required_even_with_a_workflow_name(self) -> None:
        """``workflow_name`` (when a payload carries one) proves which
        WORKFLOW a job came from; it says nothing about which RUN. A jobs
        envelope for the wrong run -- a backend bug, a cached response --
        would otherwise satisfy the proof with a perfectly correct name.
        """
        jobs = jobs_payload()
        for job in _list(jobs["jobs"]):
            _dict(job)["run_id"] = 999
        evidence = evaluate(run_record(), jobs=jobs)
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")

    def test_the_forgejo_profile_ignores_a_workflow_name_rather_than_requiring_it(
        self,
    ) -> None:
        """On a forge with no reusable workflow the field is not evidence.

        Forgejo reports none, so nothing should depend on its value either way:
        a job carrying some other workflow's name is still that run's job. This
        pins that the check is SKIPPED there rather than inverted into a
        different requirement nobody can satisfy.
        """
        evidence = evaluate(
            run_record(),
            trust_profile=ci.FORGEJO_TRUST_PROFILE,
            jobs=jobs_payload(workflow_name="something-else"),
        )
        self.assertEqual(evidence["result"], "PASS")

    def test_a_run_with_no_usable_id_proves_no_membership(self) -> None:
        """Fail closed when the run cannot own anything.

        ``run_id`` is compared for equality, so a run whose ``id`` is missing
        would match a job whose ``run_id`` is also missing -- None equalling
        None, read as membership. That is the fail-open direction and it is
        checked for explicitly.
        """
        run = run_record()
        del run["id"]
        jobs = jobs_payload()
        del _dict(_list(jobs["jobs"])[0])["run_id"]
        evidence = evaluate(
            run, trust_profile=ci.FORGEJO_TRUST_PROFILE, jobs=jobs
        )
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")

    def test_the_identity_proof_still_reads_the_job_conclusion(self) -> None:
        """Membership admits a job to the proof; it does not pass it.

        A failed ``c1`` belonging to the right run is still a failure, and a
        skipped one is still no observation. Without this, "belongs to the run"
        could be mistaken for the whole test.
        """
        failed = evaluate(
            run_record(conclusion="failure"),
            trust_profile=ci.FORGEJO_TRUST_PROFILE,
            jobs=jobs_payload(conclusion="failure"),
        )
        self.assertEqual(failed["result"], "FAIL")
        self.assertEqual(failed["reason_code"], "CI_RUN_FAILED")
        skipped = evaluate(
            run_record(),
            trust_profile=ci.FORGEJO_TRUST_PROFILE,
            jobs=jobs_payload(conclusion="skipped"),
        )
        self.assertEqual(skipped["reason_code"], "CI_PROFILE_MISSING")

    def test_an_unrelated_job_name_waits_for_the_identity_job(self) -> None:
        """Membership plus a name outside ``c1``/``c2``/``c3`` is not identity.

        The suites and status jobs belong to the run too; only the profile jobs
        report a profile's verdict. Forgejo may list those upstream jobs before
        the dependent profile row, so their presence is not terminal absence.
        """
        evidence = evaluate(
            run_record(),
            trust_profile=ci.FORGEJO_TRUST_PROFILE,
            jobs=jobs_payload(name="suites"),
        )
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_PENDING")
        self.assertEqual(
            evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS
        )


class LauncherIdentityJobTests(unittest.TestCase):
    """Do the launcher workflows actually DEFINE the jobs the proof reads?

    Every other test in this file feeds the evaluator a jobs envelope built by
    a fixture. That proves the provider handles the shapes correctly and proves
    nothing at all about whether the workflow files in this repository emit
    them. The two questions look alike and only one of them decides whether a
    real lane passes.
    """

    ROOT = Path(__file__).resolve().parents[3]

    def _jobs(self, workflow: str) -> dict[str, object]:
        import yaml

        document = yaml.safe_load((self.ROOT / workflow).read_text())
        return _dict(_dict(document)["jobs"])

    def _job_names(self, workflow: str) -> set[str]:
        return set(self._jobs(workflow))

    def test_the_forgejo_launcher_defines_one_identity_job_per_route(self) -> None:
        """`.forgejo/workflows/checks.yml` emits ``c1`` or ``c2``, never both.

        A file that emits NO profile job makes every real Forgejo lane answer
        UNAVAILABLE / ``CI_PROFILE_MISSING`` however green its check jobs are.
        A file that emits one identity job, ``c1``, for
        every route lets a narrowed run carry the strongest name, and nothing
        the gate reads could tell it from a full one. Merge-candidate narrowing
        narrows every non-candidate PR head, and the merge requires a full run, so the name
        carries the route: ``c1`` for a full run, ``c2`` for a narrowed
        one, each reporting the ``route`` job's verdict. Their ``if:``s are
        complementary, so exactly one reaches a terminal conclusion; a second
        terminal identity job makes ``_profile_jobs`` return AMBIGUOUS, which
        is refused. ``c3`` is never emitted: the classifier resolves ``c2``.
        """
        names = self._job_names(".forgejo/workflows/checks.yml")
        self.assertEqual(names & set(ci.PROFILE_JOBS), {"c1", "c2"})
        jobs = self._jobs(".forgejo/workflows/checks.yml")
        self.assertIn("outputs.profile != 'c2'", str(_dict(jobs["c1"]).get("if")))
        self.assertIn("outputs.profile == 'c2'", str(_dict(jobs["c2"]).get("if")))
        # Named explicitly, so this also records WHAT the file defines and
        # fails if that changes for any other reason. One job per
        # check group, and the group list is owned by the repository's CI
        # profile, so a mypy or node failure is named by the forge rather
        # than found inside the pytest job's log.
        self.assertEqual(
            names,
            {
                "leaf-01",
                "leaf-02",
                "leaf-03",
                "leaf-04",
                "leaf-05",
                "leaf-06",
                "leaf-07",
                "leaf-08",
                "leaf-09",
                "leaf-10",
                "leaf-11",
                "leaf-12",
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
                "leaf-21",
                "classify",
                "example-lane",
                "leaf-22",
                "leaf-23",
                "route",
                "c1",
                "c2",
            },
        )

    def test_the_forgejo_identity_job_reports_a_failure_rather_than_vanishing(
        self,
    ) -> None:
        """``route`` and both identity jobs must be `always()`; ``route`` gates
        on every check job and each identity job on ``route``.

        Without ``always()`` a failed check job skips the identity job, the
        provider finds no profile job in the run, and a plainly red CI run is
        reported as ``CI_PROFILE_MISSING`` -- a MISSING verdict rather than a
        failing one, which reads as "CI could not be evaluated" instead of
        "CI said no". The condition is the difference between those two, so it
        is pinned rather than left to a reviewer to notice.
        """
        jobs = self._jobs(".forgejo/workflows/checks.yml")
        job = _dict(jobs["route"])
        for identity in ("c1", "c2"):
            identity_job = _dict(jobs[identity])
            self.assertIn("always()", str(identity_job.get("if")))
            self.assertEqual(identity_job["needs"], [*_list(job["needs"]), "route"])
        self.assertIn("always()", str(job.get("if")))
        self.assertEqual(
            sorted(job["needs"]),
            [
                "classify",
                "example-lane",
                "leaf-01",
                "leaf-02",
                "leaf-03",
                "leaf-04",
                "leaf-05",
                "leaf-06",
                "leaf-07",
                "leaf-08",
                "leaf-09",
                "leaf-10",
                "leaf-11",
                "leaf-12",
                "leaf-13",
                "leaf-14",
                "leaf-15",
                "leaf-16",
                "leaf-17",
                "leaf-18",
                "leaf-19",
                "leaf-20",
                "leaf-21",
                "leaf-22",
                "leaf-23",
                "leaf-test-1",
                "leaf-test-2",
                "leaf-test-3",
            ],
        )

    def test_renovate_pull_requests_get_the_narrowed_manifest_plus_one_validation_lane(
        self,
    ) -> None:
        """The base workflow classifies once; c1 requires the focused lane too.

        The ordinary jobs no longer branch on the
        renovate signal -- a Renovate head is narrowed by the same
        affected-group resolution as any other PR. `example-lane`
        stays an ADDITIONAL exact-head lane on top of that, not a
        replacement for it.
        """
        jobs = self._jobs(".forgejo/workflows/checks.yml")
        classify = _dict(jobs["classify"])
        classify_steps = "\n".join(
            str(_dict(step).get("run", ""))
            + str(_dict(step).get("env", ""))
            for step in classify["steps"]
        )
        self.assertIn("startsWith(github.head_ref, 'renovate/')", classify_steps)
        self.assertIn(
            "contains(github.event.pull_request.labels.*.name, 'renovate')",
            classify_steps,
        )
        for name in (
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
            condition = str(_dict(jobs[name]).get("if"))
            self.assertNotIn("needs.classify.outputs.renovate", condition, name)
        self.assertIn(
            "needs.classify.outputs.renovate == 'true'",
            str(_dict(jobs["example-lane"]).get("if")),
        )
        self.assertIn("always()", str(_dict(jobs["route"]).get("if")))


class BaseHeadBindingTests(unittest.TestCase):
    """Both sides of the integration boundary are exact."""

    def test_base_movement_is_base_stale(self) -> None:
        run = run_record()
        _set_pr_base_sha(run, "old" + "b" * 37)
        evidence = evaluate(run)
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        # A selection race, not a verdict: the next poll legitimately
        # settles, so the caller gets a bounded retry hint.
        self.assertEqual(evidence["retry_after_seconds"], ci.RETRY_STALE_SECONDS)

    def test_head_movement_is_head_stale(self) -> None:
        run = run_record()
        _set_pr_head_sha(run, "a" * 40)
        evidence = evaluate(run_record(head_sha="new" + "a" * 37))
        self.assertEqual(evidence["reason_code"], "CI_HEAD_STALE")
        self.assertEqual(evidence["retry_after_seconds"], ci.RETRY_STALE_SECONDS)

    def test_missing_pr_record_is_ambiguous(self) -> None:
        evidence = evaluate(run_record(pull_requests=[]))
        self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")

    def test_two_pr_records_are_ambiguous(self) -> None:
        record = run_record()
        prs = _list(record["pull_requests"])
        evidence = evaluate(run_record(pull_requests=[_dict(prs[0]), _dict(prs[0])]))
        self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")

    def test_pr_record_head_sha_mismatch_is_response_invalid(self) -> None:
        run = run_record()
        _set_pr_head_sha(run, "z" * 40)
        evidence = evaluate(run)
        self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")
        self.assertEqual(evidence["retry_after_seconds"], ci.RETRY_STALE_SECONDS)

    def test_in_flight_with_missing_head_field_is_in_progress_not_stale(self) -> None:
        # An in-flight run whose head_sha field has not been populated yet
        # must be classified by its status (wait, bounded) — never by the
        # identity checks, which would misread the empty field as a stale or
        # internally-inconsistent run and refuse with no retry hint.
        run = run_record(status="in_progress", conclusion=None)
        del run["head_sha"]
        evidence = evaluate(run)
        self.assertEqual(evidence["reason_code"], "CI_RUN_IN_PROGRESS")
        self.assertEqual(evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS)

    def test_run_binds_the_exact_base_and_head_in_pass(self) -> None:
        evidence = evaluate(run_record())
        self.assertEqual(evidence["base_sha"], "b" * 40)
        self.assertEqual(evidence["head_sha"], "a" * 40)
        self.assertEqual(evidence["pull_request"], 1)


class RunSelectionTests(unittest.TestCase):
    def test_newest_run_wins(self) -> None:
        runs = [run_record(run_id=10), run_record(run_id=30), run_record(run_id=20)]
        newest = ci.select_newest_run(runs)
        self.assertIsNotNone(newest)
        assert newest is not None
        self.assertEqual(newest["id"], 30)

    def test_terminal_run_beats_newer_in_flight(self) -> None:
        # The churn this exists for: a re-trigger starts a newer run for the
        # SAME head while an older one is already settled. The settled
        # verdict is proof for that head; selection must not discard it for
        # a run that has not reached a verdict.
        runs = [
            run_record(run_id=10),
            run_record(run_id=30, status="in_progress", conclusion=None),
        ]
        selected = ci.select_newest_run(runs)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 10)

    def test_head_filter_excludes_other_heads(self) -> None:
        # Forgejo /actions/tasks serves the WHOLE repository. A run for a
        # different head must never be selected: the evaluator would refuse
        # it with CI_HEAD_STALE anyway, and refusing at selection keeps the
        # reason codes about the lane's own run.
        runs = [
            run_record(run_id=10),
            run_record(run_id=20, head_sha="z" * 40),
            run_record(run_id=30, head_sha="z" * 40, status="in_progress", conclusion=None),
        ]
        selected = ci.select_newest_run(runs, head_sha="a" * 40)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 10)

    def test_head_filter_with_no_match_returns_none(self) -> None:
        runs = [run_record(run_id=10, head_sha="z" * 40)]
        self.assertIsNone(ci.select_newest_run(runs, head_sha="a" * 40))

    def test_head_filter_still_prefers_terminal(self) -> None:
        runs = [
            run_record(run_id=10, status="in_progress", conclusion=None),
            run_record(run_id=20),
        ]
        selected = ci.select_newest_run(runs, head_sha="a" * 40)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 20)

    def test_non_dict_entries_are_skipped(self) -> None:
        runs = ["garbage", run_record(run_id=5)]
        newest = ci.select_newest_run(runs)
        self.assertIsNotNone(newest)
        assert newest is not None
        self.assertEqual(newest["id"], 5)

    def test_empty_and_non_list_answer_none(self) -> None:
        self.assertIsNone(ci.select_newest_run([]))
        self.assertIsNone(ci.select_newest_run("not a list"))

    def test_in_progress_beats_newer_completed_skipped(self) -> None:
        # the closed-event run for the same head completes (all
        # jobs skipped) with a newer id before the real PR run finishes.
        # The still-running run must win, not the newer skipped one.
        runs = [
            run_record(run_id=10, status="in_progress", conclusion=None),
            run_record(run_id=30, status="completed", conclusion="skipped"),
        ]
        selected = ci.select_newest_run(runs)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 10)

    def test_in_progress_beats_newer_completed_cancelled(self) -> None:
        runs = [
            run_record(run_id=10, status="in_progress", conclusion=None),
            run_record(run_id=30, status="completed", conclusion="cancelled"),
        ]
        selected = ci.select_newest_run(runs)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 10)

    def test_terminal_failure_still_beats_in_progress(self) -> None:
        runs = [
            run_record(run_id=10, status="in_progress", conclusion=None),
            run_record(run_id=30, status="completed", conclusion="failure"),
        ]
        selected = ci.select_newest_run(runs)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 30)

    def test_skipped_only_run_is_returned_as_fallback(self) -> None:
        runs = [run_record(run_id=10, status="completed", conclusion="skipped")]
        selected = ci.select_newest_run(runs)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["id"], 10)

    def test_selection_metadata_names_candidates_and_terminal(self) -> None:
        runs = [
            run_record(run_id=10),
            run_record(run_id=30, status="in_progress", conclusion=None),
        ]
        meta = ci.selection_metadata(runs, head_sha="a" * 40, run=run_record(run_id=10))
        self.assertEqual(meta["candidate_run_ids"], [10, 30])
        self.assertEqual(meta["selected_run_id"], 10)
        # Advisory for a waiting caller: an earlier run for this exact head
        # already reached a terminal conclusion.
        self.assertEqual(meta["terminal_candidate_run_id"], 10)
        self.assertEqual(meta["terminal_candidate_conclusion"], "success")

    def test_selection_metadata_head_filters_and_omits_terminal_when_none(self) -> None:
        runs = [
            run_record(run_id=10, head_sha="z" * 40),
            run_record(run_id=30, status="in_progress", conclusion=None),
        ]
        meta = ci.selection_metadata(runs, head_sha="a" * 40, run=None)
        self.assertEqual(meta["candidate_run_ids"], [30])
        self.assertIsNone(meta["selected_run_id"])
        self.assertNotIn("terminal_candidate_run_id", meta)


class RedactionTests(unittest.TestCase):
    """Deterministic secret-pattern redaction + escape stripping."""

    def test_redaction_is_deterministic(self) -> None:
        log = "token ghp_%s seen\n" % ("A" * 36)
        first = ci.sanitize_and_redact(log)
        second = ci.sanitize_and_redact(log)
        self.assertEqual(first, second)
        self.assertNotIn("ghp_", first[0])

    def test_github_token_shapes_are_redacted(self) -> None:
        for token in (
            "ghp_" + "A" * 36,
            "gho_" + "B" * 36,
            "ghu_" + "C" * 36,
            "ghs_" + "D" * 36,
            "ghr_" + "E" * 36,
            "github_pat_" + "F" * 82,
        ):
            with self.subTest(prefix=token[:4]):
                text, count = ci.sanitize_and_redact(f"prefix {token} suffix")
                self.assertNotIn(token, text)
                self.assertNotIn("ghp_", text)
                self.assertIn(ci._REDACTION_MARKER, text)
                self.assertEqual(count, 1)

    def test_pem_private_keys_are_redacted(self) -> None:
        # The header literal is split so repo-wide secret scanners (whose
        # patterns include PEM private-key headers) never see a contiguous
        # header; the redaction regex still matches the reconstructed string.
        key = (
            "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        text, count = ci.sanitize_and_redact(key)
        self.assertNotIn("PRIVATE KEY", text)
        self.assertEqual(count, 1)

    def test_url_credentials_are_redacted(self) -> None:
        text, count = ci.sanitize_and_redact(
            "clone https://user:secret@github.com/x.git"
        )
        self.assertIn("https://", text)
        self.assertNotIn("secret", text)
        self.assertEqual(count, 1)

    def test_ansi_escapes_and_control_characters_are_stripped(self) -> None:
        log = "\x1b[31mred\x1b[0m\x00\x01plain\tsummary\n"
        text, _count = ci.sanitize_and_redact(log)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x00", text)
        self.assertNotIn("\x01", text)
        self.assertIn("\t", text)
        self.assertIn("\n", text)
        self.assertIn("plain", text)

    def test_redaction_count_is_per_match(self) -> None:
        log = "a ghp_%s and ghp_%s\n" % ("A" * 36, "B" * 36)
        _text, count = ci.sanitize_and_redact(log)
        self.assertEqual(count, 2)


def _failed_job_names(record: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for entry in _list(record["failed_jobs"]):
        name = _dict(entry)["job_name"]
        names.add(_str(name))
    return names


def _all_excerpts(record: dict[str, object]) -> list[str]:
    excerpts: list[str] = []
    for entry in _list(record["failed_jobs"]):
        for step in _list(_dict(entry)["failed_steps"]):
            excerpt = _dict(step).get("excerpt")
            if isinstance(excerpt, str):
                excerpts.append(excerpt)
    return excerpts


class FailureEvidenceTests(unittest.TestCase):
    """Failed jobs/steps only, 20,000-byte cap, no raw text."""

    def _evidence(
        self,
        log_texts: dict[int, str | None] | None = None,
        jobs: dict[str, object] | None = None,
    ) -> dict[str, object]:
        run = run_record(conclusion="failure")
        return ci.build_failure_evidence(
            repository="test/repo",
            pull_request=1,
            base_sha="b" * 40,
            head_sha="a" * 40,
            run=run,
            # The real workflow's identity job fails whenever a needed job
            # failed (its step asserts both results), so the fixture models
            # the cascade: suites and the identity job both failed.
            jobs=jobs
            if jobs is not None
            else jobs_payload(
                suites_conclusion="failure", identity_conclusion="failure"
            ),
            log_texts=log_texts if log_texts is not None else {},
            observed_at="2020-01-01T00:00:00Z",
        )

    def test_failed_jobs_and_steps_only(self) -> None:
        record = self._evidence()
        names = _failed_job_names(record)
        # suites failed (identity conclusion failure cascades) and the
        # identity job failed; skipped and green jobs never appear.
        self.assertIn("suites", names)
        self.assertIn("c1", names)
        self.assertNotIn("status", names)
        self.assertNotIn("leaf-test", names)

    def test_cancelled_jobs_are_not_failure_evidence(self) -> None:
        jobs = jobs_payload(identity_conclusion="failure")
        _list(jobs["jobs"]).append(
            {
                "id": 600,
                "run_id": 7001,
                "name": "suites2",
                "workflow_name": "reusable",
                "status": "completed",
                "conclusion": "cancelled",
                "head_sha": "a" * 40,
                "started_at": "2020-01-01T00:00:00Z",
                "completed_at": "2020-01-01T00:00:00Z",
                "steps": [],
            }
        )
        record = self._evidence(jobs=jobs)
        self.assertNotIn("suites2", _failed_job_names(record))

    def test_deterministic_job_and_step_order(self) -> None:
        record = self._evidence()
        ids = [_int(_dict(job)["job_id"]) for job in _list(record["failed_jobs"])]
        self.assertEqual(ids, sorted(ids))
        for job in _list(record["failed_jobs"]):
            steps = [
                _str(_dict(step)["step_name"])
                for step in _list(_dict(job)["failed_steps"])
            ]
            self.assertEqual(steps, sorted(steps))

    def test_the_byte_cap_binds_the_response(self) -> None:
        huge = "x" * 100_000
        record = self._evidence(log_texts={300: huge, 200: huge})
        self.assertLessEqual(
            _int(record["bytes_returned"]), ci.FAILURE_EVIDENCE_MAX_BYTES
        )
        self.assertIs(record["truncated"], True)
        self.assertEqual(_int(record["bytes_returned"]), ci.FAILURE_EVIDENCE_MAX_BYTES)

    def test_small_logs_are_not_truncated(self) -> None:
        small = "y" * 100
        record = self._evidence(log_texts={300: small})
        self.assertIs(record["truncated"], False)
        self.assertEqual(_int(record["bytes_returned"]), 100)

    def test_excerpts_are_redacted(self) -> None:
        log = "step failed: token ghp_%s\n" % ("A" * 36)
        record = self._evidence(log_texts={300: log})
        excerpts = _all_excerpts(record)
        self.assertTrue(excerpts)
        self.assertNotIn("ghp_", "".join(excerpts))
        self.assertGreater(_int(record["redaction_count"]), 0)

    def test_missing_log_yields_no_excerpt_not_a_failure(self) -> None:
        record = self._evidence(log_texts={300: None})
        self.assertEqual(_all_excerpts(record), [])
        unavailable = [
            _dict(job)
            for job in _list(record["failed_jobs"])
            if _dict(job)["job_id"] == 300
        ]
        self.assertEqual(len(unavailable), 1)
        self.assertIs(unavailable[0]["log_available"], False)

    def test_no_failed_jobs_is_an_empty_truthful_record(self) -> None:
        green_jobs = jobs_payload(identity_conclusion="success")
        record = ci.build_failure_evidence(
            repository="test/repo",
            pull_request=1,
            base_sha="b" * 40,
            head_sha="a" * 40,
            run=run_record(),
            jobs=green_jobs,
            log_texts={},
            observed_at="2020-01-01T00:00:00Z",
        )
        self.assertEqual(record["failed_jobs"], [])
        self.assertIs(record["truncated"], False)
        self.assertEqual(_int(record["bytes_returned"]), 0)


# --- real-git CI-status operation tests -----------------------------------


class _ActionsBackend:
    """Forge stub: one pull + legacy status + fixture Actions reads.

    ``runs`` is the list of run records the runs endpoint returns for the
    head; ``jobs`` the jobs envelope for the selected run; ``logs`` maps
    job id to log text; ``file_at_ref`` returns 200 for the known reusable
    SHA and 404 otherwise; ``runs_api_failure`` makes the runs lookup fail.
    """

    state: dict[str, object] = {}

    @classmethod
    def list_pulls(cls, runner, cwd, forge_repo, page, page_size):
        if page > 1:
            return _Completed("[]")
        if cls.state.get("no_pull"):
            return _Completed("[]")
        record = {
            "index": 1,
            "state": "open",
            "base": "main",
            "base-commit": cls.state["base_commit"],
            "head": cls.state["branch"],
        }
        return _Completed(json.dumps([record]))

    @staticmethod
    def list_check_runs(runner, cwd, forge_repo, sha, page_size):
        return _Completed(
            json.dumps({"state": "success", "total_count": 0, "statuses": []})
        )

    @classmethod
    def branch_head_sha(cls, runner, cwd, forge_repo, branch):
        """The pushed branch's HEAD, or the forge's 404 for an absent one.

        The stub's pushed set is whatever ``_push_and_stub_pull`` last
        pushed: state["branch"] + state["pushed_sha"] answer 200 for that
        branch; everything else answers the 404 the real forge gives a
        branch that was never pushed (or was deleted with a merge).
        """
        if cls.state.get("branch") == branch and cls.state.get("pushed_sha"):
            return _Completed(
                json.dumps({"name": branch, "commit": {"id": cls.state["pushed_sha"]}})
            )
        return _Completed(
            '{"message": "Not Found"}', returncode=1, stderr="Not Found (HTTP 404)"
        )

    @classmethod
    def actions_runs_for_workflow(
        cls, runner, cwd, forge_repo, workflow_file, head_sha, page_size
    ):
        # Recorded before anything is decided, for the same reason
        # ``actions_file_at_ref`` records its probes: whether the Actions API
        # was consulted at all is not visible in the verdict, and the
        # no-call-for-an-unseen-head rule is a claim about exactly that.
        raw_reads = cls.state.setdefault("runs_reads", [])
        if isinstance(raw_reads, list):
            raw_reads.append(head_sha)
        if cls.state.get("runs_api_failure"):
            return _Completed("", returncode=1)
        raw_runs = cls.state.get("runs", [])
        runs = raw_runs if isinstance(raw_runs, list) else []
        matching = [
            r for r in runs if isinstance(r, dict) and r.get("head_sha") == head_sha
        ]
        return _Completed(
            json.dumps({"total_count": len(matching), "workflow_runs": matching})
        )

    @classmethod
    def actions_run_jobs(cls, runner, cwd, forge_repo, run_id, page_size):
        return _Completed(json.dumps(cls.state.get("jobs", jobs_payload())))

    @classmethod
    def actions_job_logs(cls, runner, cwd, forge_repo, job_id):
        raw_logs = cls.state.get("logs", {})
        logs = raw_logs if isinstance(raw_logs, dict) else {}
        log = logs.get(job_id)
        if log is None:
            return _Completed("", returncode=1)
        return _Completed(log)

    @classmethod
    def actions_file_at_ref(cls, runner, cwd, forge_repo, path, ref):
        """Record the probe, then answer 200 for a ref that holds the file.

        Every call is appended to ``state["file_probes"]`` before anything is
        decided, because WHICH FILE AT WHICH COMMIT is the whole of the trust
        proof on both forges and the return value cannot show either. A test
        that only reads the verdict cannot tell a probe of the base commit
        from a probe of the head.

        The known refs hold the file: ``reusable_sha`` (the SHA a GitHub run
        resolved its reusable workflow to, retained for the negative
        fixtures) and ``base_commit`` (what ``pull_request_target`` loaded
        the Forgejo launcher from), plus ``historical_base_commit`` when a
        completed run tested an earlier main. Anything else is a 404, so a probe aimed
        at the head fails the proof rather than passing it silently.
        ``file_probe_missing`` forces the 404 even for the two known refs --
        the "the trusted launcher was not there to be loaded" shape.
        """
        probes = cls.state.setdefault("file_probes", [])
        if isinstance(probes, list):
            probes.append({"path": path, "ref": ref})
        if cls.state.get("file_probe_missing"):
            # stderr carries the 404 marker: the gate's lookup-failure
            # classifier reads stderr, and a "not found" classification is
            # what turns the probe into launcher_file_exists=False (the
            # untrusted state) rather than api_failure.
            return _Completed(
                '{"message": "Not Found"}', returncode=1,
                stderr="Not Found (HTTP 404)",
            )
        if ref in (
            cls.state.get("reusable_sha"), cls.state.get("base_commit"),
            cls.state.get("historical_base_commit"),
        ):
            return _Completed(
                json.dumps(
                    {"type": "file", "sha": "blob-" + ref[:8], "path": path, "size": 10}
                )
            )
        return _Completed(
            '{"message": "Not Found"}', returncode=1,
            stderr="Not Found (HTTP 404)",
        )


class _NoActionsBackend:
    """The shape of a backend with the pull and legacy-status reads and none
    of the Actions read protocol (the tea engine's shape: it exposes no
    Actions surface for the gate to read).

    The two reads it does have are delegated to ``_ActionsBackend`` so the
    only difference between the two stubs is the absence under test.
    """

    list_pulls = _ActionsBackend.list_pulls
    list_check_runs = _ActionsBackend.list_check_runs
    branch_head_sha = _ActionsBackend.branch_head_sha


class _ForgejoRawBackend:
    """The REAL ``gate_backend_forgejo`` readers, over a fake ``tea``.

    Every other stub in this file hands the forge module an already-GitHub-shaped
    payload, so the translation layer -- the part that decides what a Forgejo
    run MEANS, and the part that reads the webhook -- is never on the path.
    This one serves RAW Forgejo bodies to the genuine reader functions, so the
    base that reaches ``evaluate_evidence`` travels the whole route it travels
    in production: `/actions/runs` -> ``event_payload`` -> the parse -> the
    synthesised ``pull_requests`` record -> the base/head comparison against
    the gate's own base.

    That is what makes the stale-base test below meaningful. A stub that
    emitted ``pull_requests`` directly would be asserting against a value the
    test itself wrote in ci.py's shape, and would pass just as happily if the
    reader were deleted.
    """

    state: dict[str, object] = {}

    # The two non-Actions reads are the ordinary ones; only the Actions
    # protocol is under test here.
    list_pulls = _ActionsBackend.list_pulls
    list_check_runs = _ActionsBackend.list_check_runs

    @classmethod
    def _tea(cls, argv, cwd, *_a, **_kw):
        """Stand in for the `tea` binary: dispatch on the REST endpoint."""
        endpoint = argv[4]
        if "/actions/tasks" in endpoint:
            rows = cls.state.get("tasks", [])
            rows = rows if isinstance(rows, list) else []
            return _Completed(
                json.dumps({"workflow_runs": rows, "total_count": len(rows)})
            )
        if "/actions/runs?" in endpoint:
            if cls.state.get("runs_api_failure"):
                return _Completed("", returncode=1)
            rows = cls.state.get("runs", [])
            rows = rows if isinstance(rows, list) else []
            return _Completed(json.dumps({"workflow_runs": rows}))
        if endpoint.endswith("/jobs"):
            reads = cls.state.setdefault("jobs_reads", [])
            if isinstance(reads, list):
                reads.append(endpoint)
            # ``jobs_by_run`` answers each run its own jobs (run id -> rows);
            # ``jobs_unreadable`` names the runs whose `/jobs` read fails.
            run_part = endpoint.rsplit("/", 2)[-2]
            run_key = int(run_part) if run_part.isdigit() else None
            unreadable = cls.state.get("jobs_unreadable", ())
            if isinstance(unreadable, (set, tuple, list)) and run_key in unreadable:
                return _Completed("", returncode=1)
            by_run = cls.state.get("jobs_by_run", {})
            rows = (
                by_run[run_key]
                if isinstance(by_run, dict) and run_key in by_run
                else cls.state.get("jobs", [])
            )
            return _Completed(json.dumps(rows if isinstance(rows, list) else []))
        if endpoint.endswith("/logs"):
            # One job's log, as plain text, from ``state["logs"]`` (job id ->
            # text); a job without an entry answers the forge's failure.
            job_id = endpoint.rsplit("/", 2)[-2]
            logs = cls.state.get("logs", {})
            log = logs.get(int(job_id)) if isinstance(logs, dict) and job_id.isdigit() else None
            return _Completed(log) if log is not None else _Completed("", returncode=1)
        if "/contents/" in endpoint:
            path, _, ref = endpoint.partition("?ref=")
            path = path.split("/contents/", 1)[1]
            probes = cls.state.setdefault("file_probes", [])
            if isinstance(probes, list):
                probes.append({"path": path, "ref": ref})
            if ref == cls.state.get("base_commit"):
                return _Completed(
                    json.dumps({"type": "file", "path": path, "size": 10})
                )
            return _Completed('{"message": "Not Found"}', returncode=1)
        if "/branches/" in endpoint:
            # The head-pushed read: the pushed branch answers 200 with its
            # HEAD; anything else is the forge's 404, with the stderr marker
            # the gate's lookup-failure classifier reads (stderr-only).
            branch = endpoint.split("/branches/", 1)[1]
            if cls.state.get("branch") == branch and cls.state.get("pushed_sha"):
                return _Completed(
                    json.dumps(
                        {"name": branch, "commit": {"id": cls.state["pushed_sha"]}}
                    )
                )
            return _Completed(
                '{"message": "Not Found"}', returncode=1, stderr="Not Found (HTTP 404)"
            )
        return _Completed('{"message": "unexpected endpoint"}', returncode=1)

    @classmethod
    def branch_head_sha(cls, runner, cwd, forge_repo, branch):
        """The real Forgejo reader over the fake tea binary."""
        return gate_backend_forgejo.branch_head_sha(cls._tea, cwd, forge_repo, branch)

    @classmethod
    def actions_runs_for_workflow(
        cls, runner, cwd, forge_repo, workflow_file, head_sha, page_size
    ):
        return gate_backend_forgejo.actions_runs_for_workflow(
            cls._tea, cwd, forge_repo, workflow_file, head_sha, page_size
        )

    @classmethod
    def actions_run_jobs(cls, runner, cwd, forge_repo, run_id, page_size):
        return gate_backend_forgejo.actions_run_jobs(
            cls._tea, cwd, forge_repo, run_id, page_size
        )

    @classmethod
    def actions_job_logs(cls, runner, cwd, forge_repo, job_id):
        return gate_backend_forgejo.actions_job_logs(cls._tea, cwd, forge_repo, job_id)

    @classmethod
    def actions_file_at_ref(cls, runner, cwd, forge_repo, path, ref):
        return gate_backend_forgejo.actions_file_at_ref(
            cls._tea, cwd, forge_repo, path, ref
        )


class ActionsReaderProtocolTests(unittest.TestCase):
    """``_backend_reads_actions`` decides whether CI evidence can be derived
    at all, so it must be able to answer both ways.

    A predicate that could only answer False would make the evidence
    permanently UNAVAILABLE on every forge and read as a policy decision
    rather than a bug -- the same class of silently-unfailable check this
    provider exists to avoid. The positive control is the real Forgejo
    backend, so renaming one of the four functions fails here instead of
    disabling the evidence path everywhere.
    """

    def test_the_forgejo_backend_provides_the_whole_protocol(self) -> None:
        """Positive control: the Forgejo backend owns the four Actions
        readers (the part of it that is not a delegation to the shared tea
        engine)."""
        self.assertTrue(gate_forge._backend_reads_actions(gate_backend_forgejo))

    def test_a_backend_with_none_of_it_does_not_qualify(self) -> None:
        self.assertFalse(gate_forge._backend_reads_actions(_NoActionsBackend))

    def test_a_partially_implemented_protocol_does_not_qualify(self) -> None:
        """Why all four names are checked rather than the first one: a
        backend that implements the runs lookup but not the jobs lookup
        would pass a single-name check and then raise at the second call,
        one stage deeper and no easier to read."""
        partial = SimpleNamespace(
            **{
                name: (lambda *args, **kwargs: None)
                for name in gate_forge._ACTIONS_READER_NAMES[:-1]
            }
        )
        self.assertFalse(gate_forge._backend_reads_actions(partial))


# --- the Forgejo status translation table -----------------------------------
#
# Forgejo carries one `status` where GitHub carries `status` plus `conclusion`,
# and this provider reads the GitHub pair. `gate_backend_forgejo` splits the
# single field on the way through, so the table it splits it with is the whole
# of what a Forgejo run means to every mapping tested above. A wrong row does
# not fail loudly: it presents one real CI outcome as another, and the row that
# matters most is the one for a status nobody has seen yet.
#
# Tested through the reader rather than by reading the dict, because the dict
# being right is not the claim -- the claim is that a `/actions/tasks` body
# leaves that function carrying the pair, and both call sites (runs and jobs)
# apply the same one.


class ForgejoStatusTranslationTests(unittest.TestCase):
    """The single Forgejo ``status`` split into GitHub's ``status``/
    ``conclusion`` pair, row by row, including the unrecognised-status
    default."""

    # The expected mapping, restated here rather than imported from the
    # backend: a test that reads the table it is checking passes for any table.
    EXPECTED: dict[str, tuple[str, str | None]] = {
        "success": ("completed", "success"),
        "failure": ("completed", "failure"),
        "cancelled": ("completed", "cancelled"),
        "canceled": ("completed", "cancelled"),
        "running": ("in_progress", None),
        "waiting": ("queued", None),
        "queued": ("queued", None),
        "blocked": ("queued", None),
        "skipped": ("completed", "skipped"),
    }

    @staticmethod
    def _forgejo_task(status: str) -> dict[str, object]:
        """One `/actions/tasks` row in the shape Forgejo returns.

        The keys are the observed ones, including the two that read as
        something else on GitHub: ``workflow_id`` is the bare file basename
        rather than a numeric id, and there is no ``conclusion`` field at all.
        """
        return {
            "id": 41,
            "name": "checks",
            "head_branch": "task/test",
            "head_sha": "a" * 40,
            "run_number": 7,
            "event": "pull_request",
            "display_title": "one change",
            "status": status,
            "workflow_id": "checks.yml",
            "url": "https://forge.example.invalid/acme/core-repo/actions/runs/7",
            "created_at": "2020-01-02T09:00:00Z",
            "updated_at": "2020-01-02T09:04:00Z",
            "run_started_at": "2020-01-02T09:00:30Z",
        }

    #: A `/actions/tasks` status that is in no mapping table, planted on the
    #: task row of every case below. The RUN's status is what these tests
    #: vary, and it lives on `/actions/runs`; the task row carries one JOB's
    #:. So a reader that read the status off the task row would
    #: answer ``action_required`` for every case here and every assertion
    #: would go red -- which makes each of these a proof of provenance as well
    #: as of translation, rather than a table read back to itself.
    JOB_STATUS_THAT_MUST_NOT_WIN = "a_job_status_no_table_maps"

    def _translated_run(self, status: str) -> dict[str, object]:
        """The one translated run record for a Forgejo RUN in *status*."""
        tasks_payload = {
            "total_count": 1,
            "workflow_runs": [
                self._forgejo_task(self.JOB_STATUS_THAT_MUST_NOT_WIN)
            ],
        }
        runs_payload = {
            "workflow_runs": [
                forgejo_run_row(
                    index_in_repo=7,
                    commit_sha="a" * 40,
                    status=status,
                    event_payload=forgejo_event_payload(),
                )
            ]
        }

        def runner(argv, cwd, *_a, **_kw):
            if "/actions/runs" in argv[4]:
                return _Completed(json.dumps(runs_payload))
            return _Completed(json.dumps(tasks_payload))

        result = gate_backend_forgejo.actions_runs_for_workflow(
            runner,
            Path("/nonexistent"),
            "acme/core-repo",
            FORGEJO_LAUNCHER,
            "a" * 40,
            20,
        )
        body = _dict(json.loads(_str(result.stdout)))
        runs = _list(body["workflow_runs"])
        self.assertEqual(len(runs), 1)
        return _dict(runs[0])

    def _translated_job(self, status: str) -> dict[str, object]:
        """The one translated job record for a Forgejo job in *status*.

        The jobs endpoint answers a BARE LIST, not an envelope, which is the
        other half of what this reader has to reconcile.
        """
        payload = [
            {
                "id": 88,
                "run_id": 41,
                "attempt": 1,
                "handle": "suites",
                "repo_id": 3,
                "owner_id": 2,
                "name": "suites",
                "needs": [],
                "runs_on": ["self-hosted"],
                "task_id": 512,
                "status": status,
            }
        ]

        def runner(argv, cwd, *_a, **_kw):
            return _Completed(json.dumps(payload))

        result = gate_backend_forgejo.actions_run_jobs(
            runner, Path("/nonexistent"), "acme/core-repo", 41, 20
        )
        body = _dict(json.loads(_str(result.stdout)))
        jobs = _list(body["jobs"])
        self.assertEqual(len(jobs), 1)
        return _dict(jobs[0])

    def test_the_table_is_closed(self) -> None:
        """A status added to the backend must be reviewed here, not absorbed.

        The default below is safe but blunt: a genuinely new Forgejo state
        mapped to ``action_required`` refuses a lane that may deserve better,
        and a new row added without a decision about which pair it deserves is
        how that goes unnoticed.
        """
        self.assertEqual(
            set(gate_backend_forgejo._STATUS_TRANSLATION),
            set(self.EXPECTED),
        )

    def test_every_mapped_status_yields_its_github_pair(self) -> None:
        for status, (expected_status, expected_conclusion) in self.EXPECTED.items():
            with self.subTest(status=status):
                run = self._translated_run(status)
                self.assertEqual(run["status"], expected_status)
                self.assertEqual(run["conclusion"], expected_conclusion)

    def test_both_cancellation_spellings_reach_the_conclusion_ci_reads(self) -> None:
        """``cancelled`` and ``canceled`` both map to the two-l spelling.

        ci.py's ``_RAN_CONCLUSIONS`` and its ``CI_RUN_CANCELED`` early-out both
        spell it ``cancelled``. Only ``success``, ``failure`` and ``running``
        are known to occur, so which spelling Forgejo emits
        for a cancellation is unconfirmed -- and the one-l spelling arriving
        unmapped would take the unrecognised default and report a cancelled run
        as ``action_required``.
        """
        for spelling in ("cancelled", "canceled"):
            with self.subTest(spelling=spelling):
                run = self._translated_run(spelling)
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["conclusion"], "cancelled")
                self.assertIn(run["conclusion"], ci._RAN_CONCLUSIONS)

    def test_an_unrecognised_status_is_terminal_but_never_a_pass(self) -> None:
        """The fail-closed default, and the property that makes it the right one.

        ``action_required`` is in ``_RAN_CONCLUSIONS``, so an unknown status is
        still reported as a real terminal observation rather than vanishing --
        but it is not ``success``, so no mapping above can read it as green.
        Defaulting to ``success`` would turn an unknown Forgejo state into a
        pass; defaulting to None would turn it into a completed run carrying no
        conclusion at all.
        """
        run = self._translated_run("some_status_forgejo_added_later")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["conclusion"], "action_required")
        self.assertIn(run["conclusion"], ci._RAN_CONCLUSIONS)
        self.assertNotEqual(run["conclusion"], "success")

    def test_the_default_is_reached_only_by_an_unrecognised_status(self) -> None:
        """The positive control for the test above: a mapped green status still
        arrives green, so ``action_required`` is the default rather than the
        answer for everything."""
        run = self._translated_run("success")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["conclusion"], "success")

    def test_the_browser_run_url_crosses_the_adapter(self) -> None:
        run = self._translated_run("failure")
        self.assertEqual(
            run["html_url"],
            "https://forge.example.invalid/acme/core-repo/actions/runs/7",
        )

    def test_a_missing_status_takes_the_default_rather_than_raising(self) -> None:
        """A row with no ``status`` at all, or a non-string one, must not raise.

        A ``TypeError`` out of a read-only evidence fetch reaches the caller as
        ``INTERNAL_ERROR``, which names no cause and reads as a broken gate
        rather than as an unreadable response.
        """
        for absent in (None, 7, ["running"]):
            with self.subTest(status=absent):
                self.assertEqual(
                    gate_backend_forgejo._translated_status(absent),
                    ("completed", "action_required"),
                )

    def test_the_jobs_reader_applies_the_same_table(self) -> None:
        """Both call sites split the status with one table.

        The jobs endpoint is where the pair actually decides a profile
        (ci.py reads each job's status and conclusion), so a second table here
        -- or a jobs reader that forgot to translate at all and passed
        Forgejo's ``status`` straight through -- would make a green run and a
        green job disagree about what the same word means.
        """
        for status, (expected_status, expected_conclusion) in self.EXPECTED.items():
            with self.subTest(status=status):
                job = self._translated_job(status)
                self.assertEqual(job["status"], expected_status)
                self.assertEqual(job["conclusion"], expected_conclusion)
        unknown = self._translated_job("some_status_forgejo_added_later")
        self.assertEqual(unknown["conclusion"], "action_required")

    def test_forge_status_carries_the_untranslated_word(self) -> None:
        """``forge_status`` is the raw Forgejo ``status`` string,
        untouched by ``_STATUS_TRANSLATION`` -- the signal ci.py needs to
        tell "blocked, will never run" apart from ordinary "queued, not run
        yet", both of which translate to the same GitHub ``status``."""
        job = self._translated_job("blocked")
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["conclusion"])
        self.assertEqual(job["forge_status"], "blocked")

    def test_forge_status_is_none_for_a_non_string_status(self) -> None:
        """A missing or non-string ``status`` must not raise, and must not
        fabricate a ``forge_status`` word Forgejo never sent."""
        payload = [
            {
                "id": 88,
                "run_id": 41,
                "attempt": 1,
                "name": "suites",
                "task_id": 512,
                "status": None,
            }
        ]

        def runner(argv, cwd, *_a, **_kw):
            return _Completed(json.dumps(payload))

        result = gate_backend_forgejo.actions_run_jobs(
            runner, Path("/nonexistent"), "acme/core-repo", 41, 20
        )
        body = _dict(json.loads(_str(result.stdout)))
        job = _dict(_list(body["jobs"])[0])
        self.assertIsNone(job["forge_status"])

    def test_a_run_carries_the_untranslated_word_too(self) -> None:
        """the RUN keeps Forgejo's own word beside the translated
        pair, from `/actions/runs` and never from the task row. ``blocked``,
        ``waiting`` and ``queued`` are one GitHub ``queued``; only the word
        tells a run stranded behind a failed job from one waiting for a
        runner."""
        for status in self.EXPECTED:
            with self.subTest(status=status):
                self.assertEqual(self._translated_run(status)["forge_status"], status)
        blocked = self._translated_run("blocked")
        self.assertEqual((blocked["status"], blocked["conclusion"]), ("queued", None))

    def test_a_run_without_a_matched_runs_row_has_no_forge_status(self) -> None:
        """No `/actions/runs` row matched: nothing fabricates a word, and the
        record keeps its fail-closed status pair."""
        run = gate_backend_forgejo._translated_run(forgejo_task_row(), None, None)
        self.assertIsNone(run["forge_status"])
        self.assertIsNone(run["id"])
        resolved = gate_backend_forgejo._ResolvedRun("a" * 40, 16, ["blocked"], None)
        odd = gate_backend_forgejo._translated_run(forgejo_task_row(), resolved, None)
        self.assertIsNone(odd["forge_status"])


# --- the Forgejo base/head binding ------------------------------------------
#
# The binding covers both sides of the integration boundary: the run's OWN record of
# what it tested, compared against the gate's current base and pushed head.
# `/actions/tasks` carries no such record and no base at all, so a Forgejo run
# reached the binding check with nothing to compare and answered CI_RESULT_AMBIGUOUS.
#
# THE CONSTRAINT THAT SHAPES ALL OF THIS: the base must come from the forge. A
# record synthesised out of values the gate already holds would put the same
# number on both sides of ci.py's comparison, and a check that cannot fail is
# not a check -- it is a check-shaped hole, which is worse than none because it
# reads as covered. The only place the base exists on this API is
# `/actions/runs`'s `event_payload`, the raw webhook Forgejo delivered at
# trigger time, so that is where it is read from.
#
# That payload also carries user email addresses, so the reader is a
# containment boundary as well as a translation, and both properties are
# tested here.


class ForgejoPullRequestBindingTests(unittest.TestCase):
    """The synthesised ``pull_requests`` record: where it comes from, when it
    refuses, and what must never travel with it."""

    def _call(
        self,
        *,
        tasks: list[dict[str, object]] | None = None,
        task_pages: dict[int, list[dict[str, object]]] | None = None,
        runs: list[dict[str, object]] | None = None,
        runs_rc: int = 0,
        runs_raw: str | None = None,
        task_source: Callable[[int], dict[str, object]] | None = None,
        jobs: dict[int, object] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        """Drive the REAL reader over a fake runner serving both endpoints.

        Returns the reader's result and every argv it issued, so a test can
        assert which endpoints were called as well as what came back.
        ``task_source`` serves a task page's body by page number; ``jobs``
        serves `/actions/runs/{id}/jobs` bodies by run id.
        """
        task_rows = tasks if tasks is not None else [forgejo_task_row()]
        run_rows = runs if runs is not None else [
            forgejo_run_row(event_payload=forgejo_event_payload())
        ]
        argvs: list[list[str]] = []

        def runner(argv, cwd, *_a, **_kw):
            argvs.append(list(argv))
            jobs_match = re.search(r"/actions/runs/(\d+)/jobs$", argv[4])
            if jobs_match is not None:
                body = (jobs or {}).get(int(jobs_match.group(1)), [])
                return _Completed(json.dumps(body))
            if "/actions/runs" in argv[4]:
                body = (
                    runs_raw
                    if runs_raw is not None
                    else json.dumps({"workflow_runs": run_rows})
                )
                return _Completed(body, returncode=runs_rc)
            if task_source is not None:
                match = re.search(r"(?:\?|&)page=(\d+)", argv[4])
                page = int(match.group(1)) if match is not None else 0
                return _Completed(json.dumps(task_source(page)))
            if task_pages is not None:
                match = re.search(r"(?:\?|&)page=(\d+)", argv[4])
                page = int(match.group(1)) if match is not None else 0
                page_rows = task_pages.get(page, [])
                return _Completed(
                    json.dumps(
                        {
                            "workflow_runs": page_rows,
                            "total_count": sum(len(rows) for rows in task_pages.values()),
                        }
                    )
                )
            return _Completed(
                json.dumps({"workflow_runs": task_rows, "total_count": len(task_rows)})
            )

        result = gate_backend_forgejo.actions_runs_for_workflow(
            runner,
            Path("/nonexistent"),
            "acme/core-repo",
            FORGEJO_LAUNCHER,
            "a" * 40,
            20,
        )
        return result, argvs

    def _one_run(self, **kwargs: object) -> dict[str, object]:
        result, _ = self._call(**kwargs)  # type: ignore[arg-type]
        body = _dict(json.loads(_str(result.stdout)))
        runs = _list(body["workflow_runs"])
        self.assertEqual(len(runs), 1)
        return _dict(runs[0])

    def test_the_binding_reaches_the_record_in_the_shape_ci_reads(self) -> None:
        """``evaluate_evidence`` reads ``pull_requests[0]["base"]["sha"]`` and
        ``["head"]["sha"]`` and refuses a list whose length is not one, so the
        record has to be that exact shape rather than merely carry the values.
        """
        run = self._one_run()
        records = _list(run["pull_requests"])
        self.assertEqual(len(records), 1)
        record = _dict(records[0])
        self.assertEqual(_dict(record["head"])["sha"], "a" * 40)
        self.assertEqual(_dict(record["base"])["sha"], "b" * 40)
        self.assertEqual(record["number"], 12)

    def test_the_base_comes_from_the_webhook_and_nowhere_else(self) -> None:
        """The property the whole design exists for.

        The task row carries no base at all and the gate is not consulted here,
        so a base in the output can only have come from ``event_payload``.
        Moving the payload's base moves the record's base with it; if it did
        not, the value would be coming from somewhere the pull request's own
        run did not report, and ci.py's comparison would be against itself.
        """
        for base in ("c" * 40, "d" * 40, "0123456789abcdef" + "0" * 24):
            with self.subTest(base=base):
                run = self._one_run(
                    runs=[
                        forgejo_run_row(
                            event_payload=forgejo_event_payload(base_sha=base)
                        )
                    ]
                )
                record = _dict(_list(run["pull_requests"])[0])
                self.assertEqual(_dict(record["base"])["sha"], base)
        # And the task row genuinely has no base to have supplied it.
        self.assertNotIn("base_sha", forgejo_task_row())
        self.assertNotIn("base", forgejo_task_row())

    def test_the_head_comes_from_the_webhook_too(self) -> None:
        """ci.py checks ``pull_requests[0]["head"]["sha"] == run["head_sha"]``.

        Taking the record's head from the task row would make that comparison a
        value against itself -- the same hole as the base, one field over. It
        comes from the payload, so the check compares two independent forge
        answers and can genuinely fail.
        """
        run = self._one_run(
            tasks=[forgejo_task_row(head_sha="a" * 40)],
            runs=[
                forgejo_run_row(
                    commit_sha="a" * 40,
                    event_payload=forgejo_event_payload(head_sha="e" * 40),
                )
            ],
        )
        record = _dict(_list(run["pull_requests"])[0])
        self.assertEqual(_dict(record["head"])["sha"], "e" * 40)
        self.assertNotEqual(_dict(record["head"])["sha"], run["head_sha"])

    def test_correlation_is_by_run_index(self) -> None:
        """A task row for another exact-head candidate is not emitted.

        ``index_in_repo`` here against ``run_number`` there is the join, and it
        is the whole join: without it the newest run's payload would be
        attached to whatever task row happened to be first, which is a base
        belonging to a different pull request.

        The mismatched row carries a PERFECTLY GOOD payload on purpose. Left
        without one it would bind nothing for the trivial reason that there was
        nothing to bind, and the test would pass against a reader that ignored
        correlation entirely -- which is how it was first written here, and
        what a reversion that ignored correlation exposed.
        """
        result, _ = self._call(
            tasks=[forgejo_task_row(run_number=7)],
            runs=[
                forgejo_run_row(
                    index_in_repo=9,
                    event_payload=forgejo_event_payload(base_sha="c" * 40),
                )
            ],
        )
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
        )

    def test_the_internal_id_is_not_the_join_key(self) -> None:
        """Matching on ``id`` would bind nothing on Forgejo.

        Forgejo reports one run under different ids on `/actions/tasks` and
        `/actions/runs`. The fixtures carry that difference, so a reader
        that joined on ``id`` would find no match here even though the indices
        agree -- and this test would go red rather than the whole forge going
        quietly unbindable.
        """
        task = forgejo_task_row(run_id=40, run_number=15)
        row = forgejo_run_row(
            run_id=16, index_in_repo=15, event_payload=forgejo_event_payload()
        )
        self.assertNotEqual(task["id"], row["id"])
        self.assertEqual(task["run_number"], row["index_in_repo"])
        run = self._one_run(tasks=[task], runs=[row])
        self.assertEqual(len(_list(run["pull_requests"])), 1)

    def test_the_emitted_run_id_is_the_runs_endpoints(self) -> None:
        """The record's ``id`` is `/actions/runs`'s, not the task's.

        A `/actions/tasks` row is one JOB and its ``id`` is a TASK id, from an
        id-space `/actions/runs/{id}/jobs` does not accept: a task id belongs
        to a run with a different id, and `/actions/runs/<task id>/jobs`
        answers 404 ("run with id <task id>: resource does not exist").
        The forge module folds that 404 into ``CI_RESPONSE_INVALID``, so every lane
        on Forgejo would answer UNAVAILABLE and none could reach a PASS
        or a FAIL.

        Both ids are asserted, not just the right one: an equality against
        the run id alone would still pass if the reader emitted the task id
        and the fixtures happened to agree.
        """
        run = self._one_run(
            tasks=[forgejo_task_row(run_id=90, run_number=53)],
            runs=[
                forgejo_run_row(
                    run_id=62,
                    index_in_repo=53,
                    event_payload=forgejo_event_payload(),
                )
            ],
        )
        self.assertEqual(run["id"], 62)
        self.assertNotEqual(run["id"], 90)

    def test_the_emitted_id_is_the_one_the_jobs_endpoint_is_asked_for(self) -> None:
        """The whole id path, end to end inside this backend.

        The test above pins what the record says; this one pins that the number
        the record carries is the number the jobs URL is built from. Without
        it, a later change could keep the record honest while some other id
        reached `actions_run_jobs`, and the 404 would come back wearing a
        passing test.
        """
        run = self._one_run(
            tasks=[forgejo_task_row(run_id=90, run_number=53)],
            runs=[
                forgejo_run_row(
                    run_id=62,
                    index_in_repo=53,
                    event_payload=forgejo_event_payload(),
                )
            ],
        )
        seen: list[str] = []

        def runner(argv, cwd, *_a, **_kw):
            seen.append(argv[4])
            return _Completed("[]")

        gate_backend_forgejo.actions_run_jobs(
            runner,
            Path("/nonexistent"),
            "acme/core-repo",
            _int(run["id"]),
            20,
        )
        self.assertEqual(
            seen, ["/repos/acme/core-repo/actions/runs/62/jobs"]
        )
        self.assertNotIn("90", seen[0])

    def test_one_runs_several_task_rows_collapse_to_one_record(self) -> None:
        """A `/actions/tasks` row is a JOB, so a run appears once per job.

        When ``checks.yml`` has six jobs, one run arrives
        as six rows sharing that ``run_number``, each with its own task id and
        its own status. Emitting six records would put the same run in
        ``candidate_run_ids`` six times and leave ``select_newest_run``
        choosing between copies that disagree about the run's state.
        """
        result, _ = self._call(
            tasks=[
                forgejo_task_row(run_id=90 + offset, run_number=53)
                for offset in range(6)
            ],
            runs=[
                forgejo_run_row(
                    run_id=62,
                    index_in_repo=53,
                    event_payload=forgejo_event_payload(),
                )
            ],
        )
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(_dict(runs[0])["id"], 62)

    def test_the_run_status_is_the_runs_rows_not_a_jobs(self) -> None:
        """A settled run whose jobs disagree still reports the run's own state.

        The six task rows below say the run is still ``running``; the run says
        it ``failure``d. Reading a job's status as the run's makes the run's
        state depend on which sibling row was selected, and a run that never
        reaches ``completed`` never triggers the jobs fetch
        ``ci.needs_jobs_fetch`` gates -- so a settled run stays pending for
        good.
        """
        result, _ = self._call(
            tasks=[
                forgejo_task_row(run_id=90 + offset, run_number=53, status="running")
                for offset in range(6)
            ],
            runs=[
                forgejo_run_row(
                    run_id=62,
                    index_in_repo=53,
                    status="failure",
                    event_payload=forgejo_event_payload(),
                )
            ],
        )
        run = _dict(_list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])[0])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["conclusion"], "failure")
        self.assertTrue(ci.needs_jobs_fetch(run))

    def test_a_task_row_that_resolves_to_no_run_is_not_emitted(self) -> None:
        """Fail closed without returning an unrelated repository task.

        The exact-head runs query identifies run 9 as the candidate. A task
        row for run 7 is not evidence about it, so the bounded join returns no
        candidate instead of manufacturing an unidentified run record.
        """
        result, _ = self._call(
            tasks=[forgejo_task_row(run_number=7)],
            runs=[
                forgejo_run_row(
                    index_in_repo=9, event_payload=forgejo_event_payload()
                )
            ],
        )
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
        )

    def test_one_of_several_workflows_on_a_commit_still_binds(self) -> None:
        """The live shape: several workflows share one commit.

        The exact runs query removes the other workflow files before this
        bounded join. The remaining run still binds by its repository run
        index, never by the commit it shares with those other workflows.
        """
        run = self._one_run(
            tasks=[forgejo_task_row(head_sha="a" * 40, run_number=8)],
            runs=[
                forgejo_run_row(
                    commit_sha="a" * 40,
                    index_in_repo=8,
                    event_payload=forgejo_event_payload(base_sha="c" * 40),
                )
            ],
        )
        record = _dict(_list(run["pull_requests"])[0])
        self.assertEqual(_dict(record["base"])["sha"], "c" * 40)

    def test_the_commit_must_agree_with_the_matched_index(self) -> None:
        """Two identifiers, and the binding needs both to say the same thing.

        An index that matches while the commit does not is a shape nobody
        should resolve by preferring one field -- it means the two endpoints
        disagree about which run this is, and either answer could be the wrong
        base. Refusing is the only honest response.
        """
        run = self._one_run(
            tasks=[forgejo_task_row(head_sha="a" * 40, run_number=7)],
            runs=[
                forgejo_run_row(
                    commit_sha="f" * 40,
                    index_in_repo=7,
                    event_payload=forgejo_event_payload(),
                )
            ],
        )
        self.assertEqual(run["pull_requests"], [])

    def test_a_duplicated_run_index_refuses(self) -> None:
        """The index is unique per repository in Forgejo.

        Two rows sharing one is a shape this reader does not understand, and
        picking either would state a base that may belong to the other.
        """
        run = self._one_run(
            runs=[
                forgejo_run_row(event_payload=forgejo_event_payload(base_sha="b" * 40)),
                forgejo_run_row(
                    run_id=17, event_payload=forgejo_event_payload(base_sha="c" * 40)
                ),
            ]
        )
        self.assertEqual(run["pull_requests"], [])

    def test_the_runs_endpoint_event_never_reaches_the_verdict(self) -> None:
        """THE `event` TRAP, pinned here.

        `/actions/runs` reports ``event: "pull_request"`` for a
        ``pull_request_target`` run -- the truth is in ``trigger_event`` -- and
        can report ``""`` outright. ``ci.evaluate_evidence`` compares the
        translated run's ``event`` against the profile's, so sourcing it from a
        runs row refuses EVERY Forgejo run with ``CI_WORKFLOW_UNTRUSTED``,
        carrying a value plausible enough to read as a real trust failure.

        The task row owns the field. Asserted against three hostile runs-row
        values, including the empty one, and the translated event must not move.
        """
        for wrong in ("pull_request", "", "push", "workflow_dispatch"):
            with self.subTest(runs_event=wrong):
                run = self._one_run(
                    tasks=[forgejo_task_row(event="pull_request_target")],
                    runs=[
                        forgejo_run_row(
                            event=wrong,
                            trigger_event="pull_request_target",
                            event_payload=forgejo_event_payload(),
                        )
                    ],
                )
                self.assertEqual(run["event"], "pull_request_target")
        # And a runs row carrying no event vocabulary at all changes nothing,
        # which is the positive control: the field is not merely overridden,
        # it is never read.
        bare = forgejo_run_row(event_payload=forgejo_event_payload())
        del bare["event"]
        del bare["trigger_event"]
        run = self._one_run(tasks=[forgejo_task_row()], runs=[bare])
        self.assertEqual(run["event"], "pull_request_target")
        self.assertEqual(len(_list(run["pull_requests"])), 1)

    def test_a_malformed_or_absent_payload_fails_closed(self) -> None:
        """Every unreadable payload answers "no binding", never a default.

        A default base would be a claim about what the run tested, invented
        here. ``CI_RESULT_AMBIGUOUS`` is the truthful answer and it refuses.
        """
        cases: dict[str, object] = {
            "absent": None,
            "not json": "{ this is not json",
            "json but not an object": "[1, 2, 3]",
            "no pull_request key": json.dumps({"action": "opened", "number": 1}),
            "pull_request not an object": json.dumps({"pull_request": "nope"}),
            "no base": json.dumps({"pull_request": {"head": {"sha": "a" * 40}}}),
            "no head": json.dumps({"pull_request": {"base": {"sha": "b" * 40}}}),
            "base not an object": json.dumps(
                {"pull_request": {"head": {"sha": "a" * 40}, "base": "main"}}
            ),
            "sha not a string": json.dumps(
                {"pull_request": {"head": {"sha": 1}, "base": {"sha": 2}}}
            ),
            "payload not a string": 12345,
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                row = forgejo_run_row()
                if payload is not None:
                    row["event_payload"] = payload
                run = self._one_run(runs=[row])
                self.assertEqual(run["pull_requests"], [])

    def test_only_a_full_hex_sha_can_travel(self) -> None:
        """The containment boundary, stated as a type.

        Every string leaving the payload is matched against a 40-character hex
        object name, so no free-text webhook field -- a branch name, a title,
        an address -- can reach the evidence record by sitting in a key this
        reader happens to read.
        """
        for bad in ("main", "", "a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 20 + "@"):
            with self.subTest(sha=bad):
                run = self._one_run(
                    runs=[
                        forgejo_run_row(
                            event_payload=json.dumps(
                                {
                                    "pull_request": {
                                        "number": 1,
                                        "head": {"sha": bad},
                                        "base": {"sha": "b" * 40},
                                    }
                                }
                            )
                        )
                    ]
                )
                self.assertEqual(run["pull_requests"], [])

    def test_no_part_of_the_webhook_payload_escapes_the_reader(self) -> None:
        """SECURITY. The payload carries email addresses; the record must carry
        two SHAs and an integer, plus the browser URL sourced independently
        from the trusted task row.

        Asserted as containment rather than as a spot check for ``@``: every
        string VALUE in the payload is required to be absent from the reader's
        entire serialised output, with the two SHAs the only exceptions. A
        future reader that started copying ``title``, ``ref``, ``login`` or a
        whole user object would fail here without anyone having predicted which
        field it would be.

        Values rather than keys, deliberately. Keys are schema and several are
        shared with the output by construction -- the record ci.py reads is
        itself spelled ``head``/``base``/``sha``. The payload's DATA is what
        must not travel.
        """
        payload = forgejo_event_payload(with_user_objects=True)
        result, _ = self._call(runs=[forgejo_run_row(event_payload=payload)])
        result_body = _dict(json.loads(_str(result.stdout)))
        emitted_run = _dict(_list(result_body["workflow_runs"])[0])
        # ``html_url`` comes from the task endpoint, not the webhook payload.
        # Its own adapter test pins the exact field. Remove only that
        # independently sourced value before checking that webhook free text
        # cannot escape through any other field.
        emitted_run.pop("html_url", None)
        emitted = json.dumps(emitted_run)

        def values(node: object) -> list[str]:
            if isinstance(node, str):
                return [node]
            if isinstance(node, dict):
                return [found for item in node.values() for found in values(item)]
            if isinstance(node, list):
                return [found for item in node for found in values(item)]
            return []

        carried = {"a" * 40, "b" * 40}
        checked = 0
        for value in values(json.loads(payload)):
            if value in carried or not value:
                continue
            checked += 1
            self.assertNotIn(
                value,
                emitted,
                f"the webhook value {value!r} reached the evidence record",
            )
        # The walk must have had something to reject, or it proves nothing.
        self.assertGreaterEqual(checked, 8)
        self.assertNotIn("@", emitted)
        self.assertNotIn("invalid", emitted)

    def test_the_two_endpoint_vocabularies_are_not_crossed(self) -> None:
        """``commit_sha`` on runs, ``head_sha`` on tasks, and never swapped.

        The endpoints share the ``workflow_runs`` envelope key and nothing
        else, so reading the wrong name on either side is a mistake that looks
        right. It would be invisible in the happy path of a single fixture that
        used both names, which is why each side is starved of the other's.
        """
        # A runs row that names its commit the way a TASK row would.
        run = self._one_run(
            runs=[
                {
                    "id": 41,
                    "index_in_repo": 7,
                    "head_sha": "a" * 40,
                    "event_payload": forgejo_event_payload(),
                }
            ]
        )
        self.assertEqual(
            run["pull_requests"],
            [],
            "the runs row must be read for commit_sha, not head_sha",
        )
        # A task row that names its commit the way a RUN row would.
        #
        # A candidate task with no head_sha contradicts the join contract;
        # it cannot be treated as a task row that has not arrived yet.
        task = forgejo_task_row()
        task["commit_sha"] = task.pop("head_sha")
        result, _ = self._call(tasks=[task])
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {},
            "the task row must be read for head_sha, not commit_sha",
        )

    def test_a_run_the_task_listing_ended_without_is_pending_not_invalid(
        self,
    ) -> None:
        """A run whose task rows have not registered yet is not a bad body.

        `/actions/runs` names run index 7 for this head; the task
        listing ends -- one row, far short of a full page, so there is no
        further page to ask for -- and 7 is not in it. The run exists and its
        jobs have not been registered, which is an eventual-consistency window
        and not a malformed response.

        The page-1 guard above this one already reports that honestly, but
        only while the missing index is NEWER than every task row seen. Here
        run 9 registered first, which is what happens the moment a second lane
        publishes, and that alone would send an ordinary race down the
        empty-body path -- `CI_RESPONSE_INVALID`, which the projection renders
        as terminal "asking again will not produce a verdict". On a forge
        several such reads can precede a green one minutes later, head
        unchanged throughout.
        """
        result, argvs = self._call(task_pages={1: [forgejo_task_row(run_number=9)]})
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
        )
        # One runs read and one tasks read: the short page ends the scan, so
        # nothing here is retried inside the call itself.
        self.assertEqual(len(argvs), 2)

    def test_a_failed_runs_read_is_an_api_failure_carrying_no_body(self) -> None:
        """A non-zero return keeps its code and loses its body.

        The forge module reads the return code first, so this lands on
        ``CI_API_UNAVAILABLE`` -- the truthful statement when the endpoint that
        holds the base could not be read. The body is emptied because a runs
        body answers to the same ``workflow_runs`` key while carrying a
        different vocabulary: handed back whole it would be accepted as a run
        list, and it holds the raw webhook payloads.
        """
        payload = forgejo_event_payload()
        result, _ = self._call(
            runs_rc=1,
            runs_raw=json.dumps({"workflow_runs": [forgejo_run_row(event_payload=payload)]}),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(_str(result.stdout)), {})
        self.assertNotIn("@", _str(result.stdout))
        self.assertNotIn("commit_sha", _str(result.stdout))

    def test_a_runs_body_in_the_wrong_shape_is_not_read_as_runs(self) -> None:
        """A parseable body without the envelope empties out at return code 0,
        which the forge module reports as ``CI_RESPONSE_INVALID`` rather than as a run
        list or as a missing run."""
        result, _ = self._call(runs_rc=0, runs_raw=json.dumps({"message": "Not Found"}))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(_str(result.stdout)), {})

    def test_runs_are_exact_filtered_and_tasks_are_explicitly_paged(self) -> None:
        """The small exact run query precedes one explicitly bounded task page."""
        _, argvs = self._call()
        endpoints = [argv[4] for argv in argvs]
        self.assertEqual(
            endpoints,
            [
                "/repos/acme/core-repo/actions/runs?page=1&limit=20&"
                f"head_sha={'a' * 40}&workflow_id=checks.yml",
                "/repos/acme/core-repo/actions/tasks?page=1&limit=50",
            ],
        )
        for argv in argvs:
            self.assertEqual(argv[1:4], ["api", "-X", "GET"])
            self.assertIn(gate_backend_forgejo.FORGEJO_READ_LOGIN, argv)

    def test_task_pages_continue_until_the_exact_run_is_found(self) -> None:
        """A run whose jobs start on page two is still translated."""
        unrelated = [
            forgejo_task_row(run_id=1000 + number, run_number=100 + number)
            for number in range(50)
        ]
        result, argvs = self._call(
            task_pages={1: unrelated, 2: [forgejo_task_row(run_number=7)]}
        )
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual([_dict(run)["id"] for run in runs], [16])
        self.assertEqual(
            [argv[4] for argv in argvs],
            [
                "/repos/acme/core-repo/actions/runs?page=1&limit=20&"
                f"head_sha={'a' * 40}&workflow_id=checks.yml",
                "/repos/acme/core-repo/actions/tasks?page=1&limit=50",
                "/repos/acme/core-repo/actions/tasks?page=2&limit=50",
            ],
        )

    def test_a_run_newer_than_the_task_surface_is_pending(self) -> None:
        """Fresh exact-head runs can appear before their task rows materialise."""
        task_rows = [
            forgejo_task_row(run_id=1000 + number, run_number=150 + number)
            for number in range(50)
        ]
        result, argvs = self._call(
            task_pages={1: task_rows},
            runs=[forgejo_run_row(index_in_repo=200)],
        )
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
        )
        self.assertEqual(len(argvs), 2)

    def test_a_newer_unresolved_candidate_suppresses_an_older_result(self) -> None:
        """Never report stale green while a newer exact-head run is unresolved."""
        result, _ = self._call(
            task_pages={1: [forgejo_task_row(run_number=7)]},
            runs=[
                forgejo_run_row(
                    run_id=16,
                    index_in_repo=7,
                    status="success",
                    event_payload=forgejo_event_payload(),
                ),
                forgejo_run_row(
                    run_id=17,
                    index_in_repo=8,
                    status="running",
                    event_payload=forgejo_event_payload(),
                ),
            ],
        )
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
        )

    # Forgejo writes a task row only when a runner picks a job up.
    # A pull request's first run was cancelled while still queued, by the
    # close event's run (skipped) in the same concurrency group. Both
    # carry `task_id: 0` on every job. The reopen run is live.

    @staticmethod
    def _never_ran_jobs(*run_ids: int) -> dict[int, object]:
        return {
            run_id: [
                {"id": run_id * 10 + n, "run_id": run_id, "attempt": 1,
                 "name": f"job-{n}", "task_id": 0, "status": "cancelled"}
                for n in range(3)
            ]
            for run_id in run_ids
        }

    @staticmethod
    def _full_pages(count: int, first_run_number: int) -> dict[int, list[dict[str, object]]]:
        """*count* full task pages of other runs, numbered from *first_run_number*."""
        return {
            page: [
                forgejo_task_row(
                    run_id=page * 1000 + offset,
                    run_number=first_run_number + page * 50 + offset,
                )
                for offset in range(50)
            ]
            for page in range(1, count + 1)
        }

    def test_a_close_and_reopen_leaves_the_live_run_readable(self) -> None:
        """The shape after a reopen: one live run, two that never ran.

        Before the never-run handling the two never-run candidates could not join, the scan
        ran out of pages, and the body was invalid for this head for good,
        even with run 44 running and after it went green.
        """
        pages = self._full_pages(5, 100)
        pages[1][0] = forgejo_task_row(run_id=4400, run_number=12, status="running")
        payload = forgejo_event_payload()
        result, argvs = self._call(
            task_pages=pages,
            runs=[
                forgejo_run_row(run_id=44, index_in_repo=12, status="running",
                                event_payload=payload),
                forgejo_run_row(run_id=43, index_in_repo=11, status="skipped",
                                event_payload=payload),
                forgejo_run_row(run_id=37, index_in_repo=8, status="cancelled",
                                event_payload=payload),
            ],
            jobs=self._never_ran_jobs(43, 37),
        )
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual(
            [(_dict(run)["id"], _dict(run)["status"]) for run in runs],
            [(44, "in_progress")],
        )
        # One runs read, one task page, one jobs read per never-run candidate.
        self.assertEqual(
            sorted(argv[4] for argv in argvs if argv[4].endswith("/jobs")),
            [
                "/repos/acme/core-repo/actions/runs/37/jobs",
                "/repos/acme/core-repo/actions/runs/43/jobs",
            ],
        )
        self.assertEqual(len(argvs), 4)

    def test_a_head_whose_only_run_never_ran_reads_as_no_run(self) -> None:
        """Cancelled or skipped before any job ran: the plain empty envelope.

        That envelope is what the forge module reports as ``CI_RUN_MISSING``, so the
        queue's missing-run clock starts. Without the never-run handling the same head would
        read as awaiting task rows (``CI_RESULT_AMBIGUOUS``, held for the full CI
        wait) or as an invalid body, by where the run sat in the task listing.
        """
        shapes = {
            "newer than the first task page": {1: [forgejo_task_row(run_number=3)]},
            "behind a short listing": self._full_pages(1, 100),
            "behind five full pages": self._full_pages(5, 100),
        }
        for status in ("cancelled", "skipped"):
            for shape, pages in shapes.items():
                with self.subTest(status=status, shape=shape):
                    result, argvs = self._call(
                        task_pages=pages,
                        runs=[forgejo_run_row(status=status,
                                              event_payload=forgejo_event_payload())],
                        jobs=self._never_ran_jobs(16),
                    )
                    self.assertEqual(
                        _dict(json.loads(_str(result.stdout))),
                        {"workflow_runs": [], "total_count": 0},
                    )
                    self.assertEqual(len(argvs), 3)

    def test_only_a_settled_run_with_no_job_that_ran_is_dropped(self) -> None:
        """Guards: every other unjoined candidate keeps its earlier answer.

        A queued run (a held one) is still awaiting task rows,
        with no jobs read. A cancelled run is kept if a job of it ran, if its
        jobs cannot be read, or if its jobs list is empty.
        """
        newer = {1: [forgejo_task_row(run_number=3)]}
        result, argvs = self._call(
            task_pages=newer,
            runs=[forgejo_run_row(status="waiting")],
            jobs=self._never_ran_jobs(16),
        )
        self.assertEqual(
            _dict(json.loads(_str(result.stdout))),
            {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
        )
        self.assertEqual(len(argvs), 2)

        ran = self._never_ran_jobs(16)
        _dict(_list(ran[16])[0])["task_id"] = 4180
        for label, jobs in (
            ("a job ran", ran),
            ("jobs unreadable", {16: {"message": "Not Found"}}),
            ("no jobs", {16: []}),
        ):
            with self.subTest(jobs=label):
                result, _ = self._call(
                    task_pages=newer,
                    runs=[forgejo_run_row(status="cancelled")],
                    jobs=jobs,
                )
                self.assertEqual(
                    _dict(json.loads(_str(result.stdout))),
                    {"workflow_runs": [], "total_count": 0, "awaiting_task_rows": True},
                )

    def test_task_scan_exhaustion_is_invalid_when_the_jobs_read_fails(self) -> None:
        """Five full pages, then a jobs read that answers no job list.

        Without a task id there is nothing to look the run up by, so the body
        stays invalid rather than becoming "no runs", and no older task page
        is read.
        """
        pages = {
            page: [
                forgejo_task_row(
                    run_id=page * 1000 + offset,
                    run_number=100 + page * 50 + offset,
                )
                for offset in range(50)
            ]
            for page in range(1, 6)
        }
        result, argvs = self._call(
            task_pages=pages, jobs={16: {"message": "Not Found"}}
        )
        self.assertEqual(_dict(json.loads(_str(result.stdout))), {})
        self.assertEqual(len(argvs), 7)
        self.assertEqual(argvs[6][4], "/repos/acme/core-repo/actions/runs/16/jobs")

    # A queued pull request's run falls behind the five-page scan
    # (16 jobs a run, so about fifteen runs). The fixtures below are the live
    # shape: a listing sorted by task id descending, 50 rows a page, with a
    # `total_count`, and a jobs list whose skipped job carries `task_id: 0`.

    @staticmethod
    def _listing(rows: list[dict[str, object]]) -> Callable[[int], dict[str, object]]:
        """Serve *rows*, newest task id first, as `/actions/tasks` pages them.

        The list is sliced at request time, so inserting rows at its head
        between two reads is tasks being created during the lookup.
        """

        def page(number: int) -> dict[str, object]:
            start = (number - 1) * 50
            return {"workflow_runs": rows[start : start + 50], "total_count": len(rows)}

        return page

    @staticmethod
    def _old_run_listing(newest: int, **run: object) -> list[dict[str, object]]:
        """1000 task rows, ids 5000 downward, run 7's 16 tasks from *newest* on.

        Every other row belongs to a run numbered 1000 or above, so nothing
        but run 7's own rows can join candidate index 7.
        """
        rows = [
            forgejo_task_row(run_id=5000 - position, run_number=1000 + position)
            for position in range(1000)
        ]
        for position in range(newest, newest + 16):
            fields: dict[str, object] = {"run_id": 5000 - position, "run_number": 7}
            fields.update(run)
            rows[position] = forgejo_task_row(**fields)  # type: ignore[arg-type]
        return rows

    @staticmethod
    def _run_jobs(newest_task: int, run_id: int = 16) -> dict[int, object]:
        """Run 16's jobs: 16 that ran, newest task first, and one skipped."""
        jobs: list[dict[str, object]] = [
            {
                "id": 900 + n,
                "run_id": run_id,
                "attempt": 1,
                "name": f"job-{n}",
                "needs": [],
                "task_id": newest_task - n,
                "status": "success",
            }
            for n in range(16)
        ]
        jobs.append(
            {
                "id": 950,
                "run_id": run_id,
                "attempt": 1,
                "name": "skipped",
                "needs": ["job-0"],
                "task_id": 0,
                "status": "skipped",
            }
        )
        return {run_id: jobs}

    @staticmethod
    def _task_pages_read(argvs: list[list[str]]) -> list[int]:
        return [
            int(match.group(1))
            for argv in argvs
            if (match := re.search(r"/actions/tasks\?page=(\d+)&", argv[4]))
        ]

    def test_an_old_run_is_found_through_its_newest_task_id(self) -> None:
        """Past the scan, the jobs' newest task id locates the run's row.

        Run 7's newest task is id 4180, at position 820: page 17 of 20. The
        search gallops up from page 6 -- 6, 7, 9 and 13 are all newer than
        the id -- to page 21, past the end, then bisects 14-21 and reads page
        17, which holds it. The record is the ordinary one -- the run's id and
        status, the task row's event, the webhook's binding.
        """
        result, argvs = self._call(
            task_source=self._listing(self._old_run_listing(820)),
            jobs=self._run_jobs(4180),
        )
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual(len(runs), 1)
        run = _dict(runs[0])
        self.assertEqual(run["id"], 16)
        self.assertEqual((run["status"], run["conclusion"]), ("completed", "success"))
        self.assertEqual(run["head_sha"], "a" * 40)
        self.assertEqual(run["event"], "pull_request_target")
        self.assertEqual(len(_list(run["pull_requests"])), 1)
        self.assertIn("/repos/acme/core-repo/actions/runs/16/jobs", [argv[4] for argv in argvs])
        self.assertEqual(self._task_pages_read(argvs), [1, 2, 3, 4, 5, 6, 7, 9, 13, 21, 17])

    def test_a_run_with_no_task_that_ran_is_invalid(self) -> None:
        """Only skipped jobs, `task_id: 0`: nothing to search for.

        The same listing yields a verdict in the test above, so what refuses
        here is the missing task id alone.
        """
        jobs = self._run_jobs(4180)
        for job in _list(jobs[16]):
            _dict(job)["task_id"] = 0
        result, argvs = self._call(
            task_source=self._listing(self._old_run_listing(820)), jobs=jobs
        )
        self.assertEqual(_dict(json.loads(_str(result.stdout))), {})
        self.assertEqual(self._task_pages_read(argvs), [1, 2, 3, 4, 5])

    def test_a_found_row_must_pass_the_same_join_checks(self) -> None:
        """The row at the task id is evidence only if it joins like a scanned one.

        The jobs endpoint names the task id; the task row still has to say it
        is run 7, at this head, from this workflow file. Otherwise a task id
        from another run's jobs would put that run's event and head on this
        one.
        """
        for field, value in (
            ("head_sha", "f" * 40),
            ("workflow_id", "other.yml"),
            ("run_number", 8),
        ):
            with self.subTest(field=field):
                result, argvs = self._call(
                    task_source=self._listing(self._old_run_listing(820, **{field: value})),
                    jobs=self._run_jobs(4180),
                )
                self.assertEqual(_dict(json.loads(_str(result.stdout))), {})
                # The row was reached; the join refused it.
                self.assertEqual(self._task_pages_read(argvs)[-1], 17)

    @staticmethod
    def _deep_listing(
        pages: int, run_page: int
    ) -> tuple[Callable[[int], dict[str, object]], dict[int, object]]:
        """A listing of *pages* pages, built per request, and run 16's jobs.

        Task ids run down from 10**7; run 7's 16 tasks start at the eleventh
        row of *run_page*. Too large to hold as a list at 100,000 pages.
        """
        top = 10**7
        run_positions = range((run_page - 1) * 50 + 10, (run_page - 1) * 50 + 26)

        def page(number: int) -> dict[str, object]:
            first = (number - 1) * 50
            return {
                "workflow_runs": [
                    forgejo_task_row(
                        run_id=top - position,
                        run_number=7 if position in run_positions else top + position,
                    )
                    for position in range(first, min(first + 50, pages * 50))
                ],
                "total_count": pages * 50,
            }

        return page, ForgejoPullRequestBindingTests._run_jobs(top - run_positions[0])

    def test_a_recent_run_costs_the_same_in_any_size_of_listing(self) -> None:
        """The search's cost follows the run's age, not the listing's size.

        100,000 pages -- a hundred times a typical listing -- with run 7's
        newest task on page 16, ten pages past the scan. The gallop reads 6,
        7, 9, 13 and 21, then 17, 15 and 16: one jobs read and eight pages,
        where a bisection over every page would take about seventeen.
        """
        page, jobs = self._deep_listing(100_000, 16)
        result, argvs = self._call(task_source=page, jobs=jobs)
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual([_dict(run)["id"] for run in runs], [16])
        self.assertEqual(
            self._task_pages_read(argvs), [1, 2, 3, 4, 5, 6, 7, 9, 13, 21, 17, 15, 16]
        )
        self.assertEqual(len(argvs), 1 + 5 + 9)

    def test_the_fallback_call_bound_fails_closed(self) -> None:
        """At most twenty calls past the scan for one run, then `CI_RESPONSE_INVALID`.

        Run 7's newest task is on page 100,000 of 200,000. The search needs
        34 page reads to reach it, and the per-run bound leaves nineteen after
        the jobs read. Raising only the total changes nothing, so the per-run
        bound is what refused. The same fixture with both bounds larger finds
        the run.
        """
        page, jobs = self._deep_listing(200_000, 100_000)
        result, argvs = self._call(task_source=page, jobs=jobs)
        self.assertEqual(_dict(json.loads(_str(result.stdout))), {})
        self.assertEqual(len(argvs), 1 + 5 + 20)

        with mock.patch.object(gate_backend_forgejo, "_FORGEJO_TASK_FALLBACK_TOTAL_CALLS", 40):
            result, argvs = self._call(task_source=page, jobs=jobs)
        self.assertEqual(_dict(json.loads(_str(result.stdout))), {})
        self.assertEqual(len(argvs), 1 + 5 + 20)

        with mock.patch.multiple(
            gate_backend_forgejo,
            _FORGEJO_TASK_FALLBACK_CALLS=40,
            _FORGEJO_TASK_FALLBACK_TOTAL_CALLS=40,
        ):
            result, _ = self._call(task_source=page, jobs=jobs)
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual([_dict(run)["id"] for run in runs], [16])

    # A head can carry several runs (a `labeled` event starts one), and
    # two that ran can both be past the scan. The fallback gives each its own
    # budget, so the second lookup is not starved wherever the first costs
    # more than half of a shared one.

    @staticmethod
    def _two_old_runs(
        newer_page: int, older_page: int
    ) -> tuple[
        Callable[[int], dict[str, object]],
        dict[int, object],
        list[dict[str, object]],
    ]:
        """One head with two runs past the scan: the listing, the jobs, the runs.

        Run 8 (id 17) is the newer, with its newest task on *newer_page*; run 7
        (id 16) is the older, on *older_page*. Task ids run down from 10**7,
        fifty to a page. Run 8's 16 tasks start at the eleventh row of its
        page and run 7's at the thirty-first row of its own, so both fit on one
        page. Every other row is a filler of run number above 10**7, so only
        the two runs' own rows join candidate indices 7 and 8. The runs list
        names the newer run first, as `/actions/runs` does.
        """
        top = 10**7
        starts = {8: (newer_page - 1) * 50 + 10, 7: (older_page - 1) * 50 + 30}

        def page(number: int) -> dict[str, object]:
            rows: list[dict[str, object]] = []
            for position in range((number - 1) * 50, number * 50):
                owner = next(
                    (run for run, start in starts.items() if start <= position < start + 16),
                    None,
                )
                rows.append(
                    {"id": top - position, "run_number": top + position}
                    if owner is None
                    else forgejo_task_row(run_id=top - position, run_number=owner)
                )
            return {"workflow_runs": rows, "total_count": 50 * 1000}

        jobs = {
            **ForgejoPullRequestBindingTests._run_jobs(top - starts[8], run_id=17),
            **ForgejoPullRequestBindingTests._run_jobs(top - starts[7], run_id=16),
        }
        runs = [
            forgejo_run_row(
                run_id=17, index_in_repo=8, event_payload=forgejo_event_payload()
            ),
            forgejo_run_row(
                run_id=16, index_in_repo=7, event_payload=forgejo_event_payload()
            ),
        ]
        return page, jobs, runs

    def _read_two_old_runs(
        self, newer_page: int, older_page: int
    ) -> tuple[list[object], list[list[str]]]:
        """The ids of the runs the real reader translated, and every argv."""
        page, jobs, runs = self._two_old_runs(newer_page, older_page)
        result, argvs = self._call(task_source=page, jobs=jobs, runs=runs)
        body = _dict(json.loads(_str(result.stdout)))
        return [_dict(run)["id"] for run in _list(body.get("workflow_runs", []))], argvs

    def test_each_old_run_of_a_head_has_a_lookup_budget_of_its_own(self) -> None:
        """Two reads that a shared budget would end as `CI_RESPONSE_INVALID`, and one it would not.

        Run 7's task is five pages older than run 8's. Pages 23 and 28 cost 10
        and 11 calls, 21 against a single shared budget of 20: the
        second search would probe pages 29, 25 and 27 and run out before page 28.
        Pages 24 and 29 cost 11 and 8, 19 in all, which always fit. With a
        budget for each, both reads translate both runs.
        """
        scan = [1, 2, 3, 4, 5]
        for newer, older, newer_search, older_search in (
            (23, 28, [6, 7, 9, 13, 21, 37, 29, 25, 23], [6, 7, 9, 13, 21, 37, 29, 25, 27, 28]),
            (24, 29, [6, 7, 9, 13, 21, 37, 29, 25, 23, 24], [6, 7, 9, 13, 21, 37, 29]),
        ):
            with self.subTest(newer=newer, older=older):
                ids, argvs = self._read_two_old_runs(newer, older)
                self.assertEqual(ids, [17, 16])
                self.assertEqual(
                    self._task_pages_read(argvs), scan + newer_search + older_search
                )
                self.assertEqual(
                    [argv[4].split("/actions/runs/")[1] for argv in argvs if "/jobs" in argv[4]],
                    ["17/jobs", "16/jobs"],
                )

    def test_two_old_runs_are_found_at_every_age_through_page_80(self) -> None:
        """The table behind the sizing: both runs, same page or five apart.

        Every page from the first past the scan to 80, a fixed number of days of
        history. Before this sizing the one budget of 20 refused a pair five pages
        apart whose newer run was on page 22, 23, 26, 27, 30, 31, 33 to 36, or
        38 and later, and a pair on one page from page 22 on wherever a lookup
        costs 11 or more.
        """
        total = gate_backend_forgejo._FORGEJO_TASK_FALLBACK_TOTAL_CALLS
        for newer in range(6, 81):
            for older in sorted({newer, min(newer + 5, 80)}):
                with self.subTest(newer=newer, older=older):
                    ids, argvs = self._read_two_old_runs(newer, older)
                    self.assertEqual(ids, [17, 16])
                    self.assertLessEqual(len(argvs), 1 + 5 + total)

    @staticmethod
    def _lookup_cost(task_page: int) -> int:
        """Calls one `_older_task_row` lookup of a task on *task_page* spends."""
        page, jobs, _ = ForgejoPullRequestBindingTests._two_old_runs(task_page, task_page)

        def runner(argv, cwd, *_a, **_kw):
            jobs_match = re.search(r"/actions/runs/(\d+)/jobs$", argv[4])
            if jobs_match is not None:
                return _Completed(json.dumps(jobs[int(jobs_match.group(1))]))
            page_match = re.search(r"(?:\?|&)page=(\d+)", argv[4])
            assert page_match is not None
            return _Completed(json.dumps(page(int(page_match.group(1)))))

        row, left = gate_backend_forgejo._older_task_row(
            runner, Path("/nonexistent"), "acme/core-repo", 17, 1000
        )
        assert row is not None, task_page
        return 1000 - left

    def test_two_lookups_fit_the_total_through_page_133(self) -> None:
        """The sizing the constants rest on, measured with the real search.

        The cost of one lookup is not monotonic in the page (page 28 costs 11,
        page 29 costs 8), so the worst case is taken over every page. It is 15
        through page 133, and page 134 costs 17. If a change to the search makes
        a lookup dearer, this fails and the constants are sized again, rather
        than the second lookup of a head going unread without notice.
        """
        per_run = gate_backend_forgejo._FORGEJO_TASK_FALLBACK_CALLS
        total = gate_backend_forgejo._FORGEJO_TASK_FALLBACK_TOTAL_CALLS
        costs = {page: self._lookup_cost(page) for page in range(6, 135)}
        worst = max(cost for page, cost in costs.items() if page <= 133)
        self.assertLessEqual(worst, per_run)
        self.assertLessEqual(2 * worst, total)
        self.assertGreater(costs[134], worst)

    def test_the_total_fallback_bound_fails_closed(self) -> None:
        """Two lookups that each fit their own budget but not the total.

        Run 8's task is on page 134 (17 calls) and run 7's on page 262 (19).
        The first leaves 13 of the total of 30, and the second needs 19, so
        the read ends as `CI_RESPONSE_INVALID` after exactly the total. The
        same fixture with a larger total translates both runs, so the total
        is what refused.
        """
        total = gate_backend_forgejo._FORGEJO_TASK_FALLBACK_TOTAL_CALLS
        # The refusal is the empty body `{}`. `_read_two_old_runs` would read a
        # valid empty envelope (`{"workflow_runs": []}`) as no runs too, so
        # this half reads the body itself.
        page, jobs, runs = self._two_old_runs(134, 262)
        result, argvs = self._call(task_source=page, jobs=jobs, runs=runs)
        self.assertEqual(_dict(json.loads(_str(result.stdout))), {})
        self.assertEqual(len(argvs), 1 + 5 + total)

        with mock.patch.object(
            gate_backend_forgejo, "_FORGEJO_TASK_FALLBACK_TOTAL_CALLS", total + 10
        ):
            ids, _ = self._read_two_old_runs(134, 262)
        self.assertEqual(ids, [17, 16])

    def test_tasks_created_during_the_search_are_tolerated(self) -> None:
        """New tasks push older rows to later pages mid-search.

        Run 7's newest task, id 4401, is the last row of page 12 when the
        gallop reads page 13, so page 13 answers "older than the id". Ten tasks
        are then created, which moves id 4401 onto page 13 itself. Page 13
        must stay in the bracket: the bisection of 10-13 reads 11 and 12, both
        newer than the id, and page 13 is read again and holds it.
        """
        listing = self._old_run_listing(599)
        serve = self._listing(listing)
        created: list[int] = []

        def shifting(number: int) -> dict[str, object]:
            body = serve(number)
            if number == 13 and not created:
                created.extend(range(5010, 5000, -1))
                listing[:0] = [
                    forgejo_task_row(run_id=task_id, run_number=2000 + task_id)
                    for task_id in created
                ]
            return body

        result, argvs = self._call(task_source=shifting, jobs=self._run_jobs(4401))
        runs = _list(_dict(json.loads(_str(result.stdout)))["workflow_runs"])
        self.assertEqual([_dict(run)["id"] for run in runs], [16])
        self.assertEqual(
            self._task_pages_read(argvs), [1, 2, 3, 4, 5, 6, 7, 9, 13, 11, 12, 13]
        )


class _RealGitEvidenceCase(unittest.TestCase):
    """Bare-origin/managed-checkout/linked-task-worktree fixture, plus a
    repository catalog fixture - the policy-operation harness, with the
    Actions backend state added."""

    ALIAS = "evidence-real-git"
    #: The lane's writable forge. Set in ONE place because two records name it
    #: -- the closed policy the gate dispatches on, and the catalog file the
    #: catalog parse validates -- and a subclass that moved only one would exercise a
    #: repository that cannot exist. The base is Forgejo, the one
    #: operational forge.
    FORGE = "forgejo"

    @staticmethod
    def _git(args, cwd, check=True):
        return subprocess.run(
            [gate.GIT_PATH, *args],
            cwd=str(cwd),
            check=check,
            capture_output=True,
            text=True,
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)

        self._orig_lock_dir = gate.contracts.GATE_LOCK_DIR
        self._orig_lock_parent = gate.contracts.GATE_LOCK_PARENT
        lock_parent = base / "run" / "lock"
        lock_parent.mkdir(parents=True, exist_ok=True)
        lock_dir = lock_parent / "merge-guard"
        lock_dir.mkdir(exist_ok=True)
        os.chmod(str(lock_dir), 0o700)
        gate.contracts.GATE_LOCK_DIR = lock_dir
        gate.contracts.GATE_LOCK_PARENT = lock_parent

        self._bare = base / "origin.git"
        self._bare.mkdir()
        self._git(["init", "--bare", "--initial-branch=main"], self._bare)

        self._checkout = base / "checkout"
        self._git(["clone", str(self._bare), str(self._checkout)], base)
        for k, v in [
            ("user.email", "t@example.invalid"),
            ("user.name", "Test"),
            ("commit.gpgsign", "false"),
        ]:
            self._git(["config", k, v], self._checkout)
        (self._checkout / "README.md").write_text("initial\n")
        self._git(["add", "README.md"], self._checkout)
        self._git(["commit", "-m", "initial"], self._checkout)
        self._git(["push", "origin", "main"], self._checkout)

        self._task_root = base / "worktrees"
        self._task_root.mkdir()
        self._git(["fetch", "origin", "main:refs/remotes/origin/main"], self._checkout)
        self._origin_main = self._git(
            ["rev-parse", "origin/main"], self._checkout
        ).stdout.strip()
        self._branch = "task/evidence-real-git"
        self._task_wt = self._task_root / "task-evidence-real-git"
        self._git(
            [
                "worktree",
                "add",
                "-b",
                self._branch,
                str(self._task_wt),
                self._origin_main,
            ],
            self._checkout,
        )
        for k, v in [
            ("user.email", "t@example.invalid"),
            ("user.name", "Test"),
            ("commit.gpgsign", "false"),
        ]:
            self._git(["config", k, v], self._task_wt)

        self._original_cr = gate.catalog.CLOSED_REPOSITORIES
        self._original_ca = gate.catalog.CLOSED_ALIASES
        self._policy = gate.RepositoryPolicy(
            name=f"acme/{self.ALIAS}",
            remote_url=str(self._bare),
            managed_checkout=str(self._checkout),
            task_root=str(self._task_root),
            forge=self.FORGE,
            forge_repository=f"test/{self.ALIAS}",
        )
        gate.catalog.CLOSED_REPOSITORIES = {f"acme/{self.ALIAS}": self._policy}
        gate.catalog.CLOSED_ALIASES = frozenset({self.ALIAS})
        canonical_aliases = mock.patch.object(
            gate.catalog, "_CANONICAL_GATE_ALIASES", frozenset({self.ALIAS})
        )
        canonical_aliases.start()
        self.addCleanup(canonical_aliases.stop)

        repos_yaml_text = f"""\
canonical_repos:
  - name: {self.ALIAS}
    remote: {self._bare}
    writable_forge: {self.FORGE}
    deploy_path: {self._checkout}
    repository_control_level: R1
    ci_applicability: required
"""
        repos_yaml_path = base / "repos.yaml"
        repos_yaml_path.write_text(repos_yaml_text)
        self._orig_repos_yaml = gate.catalog._REPOS_YAML
        gate.catalog._REPOS_YAML = repos_yaml_path

        _ActionsBackend.state = {}

    def tearDown(self):
        gate.catalog.CLOSED_REPOSITORIES = self._original_cr
        gate.catalog.CLOSED_ALIASES = self._original_ca
        gate.catalog._REPOS_YAML = self._orig_repos_yaml
        gate.contracts.GATE_LOCK_DIR = self._orig_lock_dir
        gate.contracts.GATE_LOCK_PARENT = self._orig_lock_parent
        _ActionsBackend.state = {}
        self._tmp.cleanup()

    def _gate(self, cwd):
        return gate.operations.GitMaintainerGate(runner=gate.run_command, cwd=cwd)

    def _push_and_stub_pull(self, change: str = "notes.txt") -> str:
        (self._task_wt / change).write_text("an ordinary change\n")
        self._git(["add", change], self._task_wt)
        self._git(["commit", "-m", "ordinary change"], self._task_wt)
        self._git(["push", "--set-upstream", "origin", self._branch], self._task_wt)
        head_sha = self._git(["rev-parse", "HEAD"], self._task_wt).stdout.strip()
        _ActionsBackend.state["base_commit"] = self._origin_main
        _ActionsBackend.state["branch"] = self._branch
        _ActionsBackend.state["pushed_sha"] = head_sha
        return head_sha

    def _green_run(self, head_sha: str) -> dict[str, object]:
        run = run_record(
            run_id=7001,
            head_sha=head_sha,
            base_sha=self._origin_main,
            pr_number=1,
        )
        _ActionsBackend.state["runs"] = [run]
        _ActionsBackend.state["jobs"] = jobs_payload()
        return run


class CiStatusProjectionRealGitTests(_RealGitEvidenceCase):
    """The full ``ci-status`` operation composition on a Forgejo lane.

    The Forgejo route has no resolved-reusable-workflow proof (a ``github``
    forge policy and the ``referenced_workflows`` mechanism), which Forgejo
    does not provide. The Forgejo route's file-existence probe is covered in depth by
    ``ForgejoEvidenceProbeRealGitTests``; this class pins the surrounding
    composition: the policy roll-up, the launcher path in the record, the
    no-reusable-ref record shape, and the failure states that are
    forge-agnostic.
    """

    FORGE = "forgejo"

    def test_ci_status_composes_green_evidence(self):
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "ci-status")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["schema"], "ci-evidence.v1")
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["base_sha"], self._origin_main)
        self.assertEqual(evidence["head_sha"], head_sha)
        self.assertEqual(evidence["workflow_path"], FORGEJO_LAUNCHER)
        self.assertIsNone(
            evidence["trusted_workflow_ref"],
            "Forgejo emits no referenced_workflows on any endpoint; the "
            "record must not claim a reusable resolution that never happened",
        )
        # The lane classifies as C2 (ordinary change), so the required
        # profile is checks/c2; the fixture run ran the stronger c1
        # profile, which the equal-or-stronger table accepts for the weaker
        # requirement.
        self.assertEqual(evidence["required_profile"], "checks/c2")
        self.assertEqual(evidence["accepted_profile"], "checks/c1")
        self.assertNotIn("combined_state", result)
        self.assertNotIn("checks_pending", result)

    def _narrowed_then_full(
        self, *, full_in_flight: bool = False, full_conclusion: str | None = None,
    ) -> dict[str, object]:
        """The `ci_evidence` of `_narrowed_then_full_result`."""
        result = self._narrowed_then_full_result(
            full_in_flight=full_in_flight, full_conclusion=full_conclusion)
        return _dict(result["ci_evidence"])

    def _narrowed_then_full_result(
        self, *, full_in_flight: bool = False, full_conclusion: str | None = None,
        change: str = "extra",
    ) -> dict[str, object]:
        """A `checks/c1` lane whose settled run is narrowed.

        A CI-trust path makes the lane C1, so it requires `checks/c1`. Run
        7001 settled green as a narrowed `c2` run; when ``full_in_flight``,
        run 7002 -- the full run that adding `gate/candidate` starts -- is
        still running for the same head. ``full_conclusion`` instead
        completes run 7002 with that conclusion. Returns the whole
        `ci-status` record. A caller that reads several heads from one lane
        passes a distinct ``change`` each time, so every head is a new commit.
        """
        (self._task_wt / ".forgejo" / "workflows").mkdir(parents=True, exist_ok=True)
        head_sha = self._push_and_stub_pull(f".forgejo/workflows/{change}.yml")
        narrowed = run_record(run_id=7001, head_sha=head_sha,
                              base_sha=self._origin_main, pr_number=1)
        runs = [narrowed]
        if full_in_flight:
            runs.append(run_record(run_id=7002, head_sha=head_sha, base_sha=self._origin_main,
                                   pr_number=1, status="in_progress", conclusion=None))
        elif full_conclusion is not None:
            runs.append(run_record(run_id=7002, head_sha=head_sha, base_sha=self._origin_main,
                                   pr_number=1, status="completed",
                                   conclusion=full_conclusion))
        _ActionsBackend.state["runs"] = runs
        _ActionsBackend.state["jobs"] = jobs_payload(name="c2")
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            return self._gate(self._task_wt).execute("ci-status")

    def _queue_owned_label_policy(self) -> None:
        """Make the fixture repository one whose queue promotes a candidate.

        `tearDown` restores `CLOSED_REPOSITORIES`, so the change cannot leak.
        """
        gate.catalog.CLOSED_REPOSITORIES = {self._policy.name: replace(
            self._policy, ci_applicability="required",
            automatic_review_workflow="automerge-review-v1",
            merge_candidate_policy="label-v1")}

    def test_a_narrowed_green_head_the_queue_owns_awaits_the_candidate_run(self):
        """(a) Through the real evaluator: the narrowed run is the first stage.

        Verdict and reason code stay `unavailable` / CI_PROFILE_INSUFFICIENT;
        the added field and the wait action say the queue acts next.
        """
        from git_gate.result_contract import decorate_gate_result

        self._queue_owned_label_policy()
        result = self._narrowed_then_full_result()
        evidence = _dict(result["ci_evidence"])
        self.assertEqual((result["verdict"], evidence["reason_code"]),
                         ("unavailable", "CI_PROFILE_INSUFFICIENT"))
        self.assertEqual(evidence["workflow_run_id"], 7001)
        self.assertIs(result["awaiting_candidate_run"], True)
        self.assertEqual(result["integration_owner"], "queue")
        verdict = _dict(decorate_gate_result(result)["gate_result"])
        self.assertEqual([_dict(action)["kind"] for action in _list(verdict["next_actions"])], ["wait"])
        self.assertIs(verdict["retryable"], True)

    def test_a_narrowed_green_head_the_queue_does_not_own_stays_a_stop(self):
        """(b) The same run in a repository without a queue is today's stop."""
        from git_gate.result_contract import decorate_gate_result

        result = self._narrowed_then_full_result()
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertNotIn("awaiting_candidate_run", result)
        self.assertNotIn("integration_owner", result)
        verdict = _dict(decorate_gate_result(result)["gate_result"])
        self.assertEqual([_dict(action)["kind"] for action in _list(verdict["next_actions"])], ["stop"])

    def test_a_candidate_run_that_ended_or_runs_is_never_hidden_behind_the_wait(self):
        """The queue's own full run, whatever its state, replaces the wait.

        A failed, cancelled or unreadable candidate run is selected over the
        older narrowed success, and a running one makes the verdict pending,
        so none of them can read as "the queue has not run it yet".
        """
        self._queue_owned_label_policy()
        for name, options in {
            "failed": {"full_conclusion": "failure"},
            "cancelled": {"full_conclusion": "cancelled"},
            "action_required": {"full_conclusion": "action_required"},
            "running": {"full_in_flight": True},
        }.items():
            with self.subTest(name):
                result = self._narrowed_then_full_result(change=name, **options)
                self.assertNotIn("awaiting_candidate_run", result)
                self.assertEqual(_dict(result["ci_evidence"])["workflow_run_id"], 7002)

    def test_a_narrowed_failure_in_a_queue_owned_repository_is_not_a_wait(self):
        """(c) A failed narrowed run keeps today's record, flag-free."""
        self._queue_owned_label_policy()
        (self._task_wt / ".forgejo" / "workflows").mkdir(parents=True, exist_ok=True)
        head_sha = self._push_and_stub_pull(".forgejo/workflows/extra.yml")
        _ActionsBackend.state["runs"] = [run_record(
            run_id=7001, head_sha=head_sha, base_sha=self._origin_main, pr_number=1,
            conclusion="failure")]
        _ActionsBackend.state["jobs"] = jobs_payload(name="c2", conclusion="failure")
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertNotIn("awaiting_candidate_run", result)
        self.assertEqual(result["headline"], "CI: unavailable (CI_PROFILE_INSUFFICIENT)")

    def test_an_insufficient_settled_run_yields_to_a_newer_run_in_flight(self):
        """The candidate's full run, not the narrowed one, is the answer."""
        evidence = self._narrowed_then_full(full_in_flight=True)
        self.assertEqual(evidence["required_profile"], "checks/c1")
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RUN_IN_PROGRESS")
        self.assertEqual(evidence["workflow_run_id"], 7002)

    def test_an_insufficient_settled_run_with_nothing_newer_stays_insufficient(self):
        evidence = self._narrowed_then_full(full_in_flight=False)
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(evidence["workflow_run_id"], 7001)

    def test_a_cancelled_full_run_is_reported_not_hidden_behind_the_narrowed_one(self):
        """the promoted run's own end is the answer, so the queue
        parks instead of retrying a merge the gate refuses until CI_WAIT."""
        evidence = self._narrowed_then_full(full_conclusion="cancelled")
        self.assertEqual(evidence["required_profile"], "checks/c1")
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RUN_CANCELED")
        self.assertEqual(evidence["workflow_run_id"], 7002)

    def test_an_action_required_full_run_is_reported_not_hidden(self):
        """Forgejo's unrecognised run status arrives as `action_required`.

        The successor's jobs are not read, so the evaluator cannot place the
        run and answers CI_RESPONSE_INVALID: still terminal, never the hidden
        narrowed run's CI_PROFILE_INSUFFICIENT."""
        evidence = self._narrowed_then_full(full_conclusion="action_required")
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["workflow_run_id"], 7002)
        self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")

    def test_a_skipped_newer_run_leaves_the_narrowed_run_insufficient(self):
        """A stray label's `-meta-` run is skipped and never the answer."""
        evidence = self._narrowed_then_full(full_conclusion="skipped")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_INSUFFICIENT")
        self.assertEqual(evidence["workflow_run_id"], 7001)

    def test_unfinished_failed_rerun_does_not_hide_older_settled_evidence(self):
        head_sha = self._push_and_stub_pull()
        settled = self._green_run(head_sha)
        rerun = run_record(run_id=7002, head_sha=head_sha,
                           base_sha=self._origin_main, pr_number=1)
        rerun["conclusion"] = "failure"
        _ActionsBackend.state["runs"] = [settled, rerun]
        unfinished = jobs_payload(run_id=7002, conclusion="failure", extra_jobs=[
            {"id": 99999, "run_id": 7002, "name": "still-running",
             "status": "in_progress", "conclusion": None},
        ])
        reads = []

        def read_jobs(runner, cwd, repository, run_id, page_size):
            reads.append(run_id)
            return _Completed(json.dumps(unfinished if run_id == 7002 else jobs_payload()))

        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            with mock.patch.object(_ActionsBackend, "actions_run_jobs", side_effect=read_jobs):
                result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "green")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["selected_run_id"], 7001)
        self.assertNotEqual(evidence.get("terminal_candidate_run_id"), 7002)
        self.assertEqual(reads, [7002, 7001])

    def test_aggregate_failure_is_progress_until_all_jobs_finish(self):
        head_sha = self._push_and_stub_pull()
        run = self._green_run(head_sha)
        run["conclusion"] = "failure"
        sibling = {"id": 99999, "run_id": 7001, "name": "still-running",
                   "status": "in_progress", "conclusion": None}
        jobs = jobs_payload(conclusion="failure", extra_jobs=[sibling])
        _ActionsBackend.state["jobs"] = jobs
        _ActionsBackend.state["logs"] = {
            job["id"]: "AssertionError: synthetic mid-run failure"
            for job in ci.failed_jobs_of(jobs)
        }
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
            self.assertEqual(result["verdict"], "pending")
            self.assertIsNone(result["failure_evidence"])
            progress = _dict(result["ci_progress"])
            self.assertFalse(progress["terminal"])
            self.assertEqual(progress["overall_state"], "pending")
            self.assertIn("synthetic mid-run failure", str(progress))
            sibling.update(status="completed", conclusion="success")
            settled = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(settled["verdict"], "red")
        self.assertNotIn("ci_progress", settled)

    def test_blocked_aggregate_past_the_window_is_red_end_to_end(self):
        """The blocked-aggregate rule, end to end through ``ci-status``: the forge module's own
        ``with_job_lifecycle`` + ``evaluate_evidence`` sequence for a
        Forgejo-blocked c1 whose siblings (one killed) already completed.
        The fixture run's default ``updated_at`` is already well past the
        materialization window relative to real wall-clock time, so no
        clock mocking is needed. ``with_job_lifecycle`` must not flip this
        run to ``in_progress``: CI_RUN_IN_PROGRESS would hide the FAIL
        entirely."""
        head_sha = self._push_and_stub_pull()
        run = self._green_run(head_sha)
        run["conclusion"] = "failure"
        jobs = jobs_payload(conclusion="failure")
        for job in jobs["jobs"]:
            if job["name"] == "c1":
                job["status"] = "queued"
                job["conclusion"] = None
                job["forge_status"] = "blocked"
                job.pop("started_at", None)
                job.pop("completed_at", None)
        _ActionsBackend.state["jobs"] = jobs
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "red")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")

    def test_a_launcher_missing_at_the_base_commit_is_untrusted(self):
        """The Forgejo route's untrusted state: the profile's proof is that
        the launcher exists at the pull request's BASE commit (what
        ``pull_request_target`` loads the workflow file from), so a probe
        that cannot find the file there must refuse. The stub backend
        answers 200 for the base commit and 404 for everything else;
        ``file_probe_missing`` forces the 404 on the base commit too, which
        is exactly the "the trusted launcher was not there to be loaded"
        shape the proof exists to catch.
        """
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        _ActionsBackend.state["file_probe_missing"] = True
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_stale_base_reports_base_stale(self):
        head_sha = self._push_and_stub_pull()
        run = self._green_run(head_sha)
        _set_pr_base_sha(run, "x" * 40)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_BASE_STALE")

    def test_unpushed_lane_has_no_evidence_and_no_forge_actions_call(self):
        """The evidence honors the legacy no-call-for-an-unseen-head rule.

        A lane that was never pushed has no remote branch and no pull
        request, so ``no_pr`` is the true verdict here and stays one. The
        reason code moved from ``CI_RUN_MISSING`` to
        ``CI_LANE_HEAD_MISMATCH``: no run was ever asked for, so
        calling one missing described a forge answer that was never
        collected, and the bounded retry it carried told a waiter to poll for
        a run that cannot arrive until something publishes this head.

        The no-call rule is now asserted rather than only named in the title.
        """
        (self._task_wt / "notes.txt").write_text("unpushed\n")
        self._git(["add", "notes.txt"], self._task_wt)
        self._git(["commit", "-m", "unpushed"], self._task_wt)
        _ActionsBackend.state["runs"] = []
        _ActionsBackend.state["no_pull"] = True
        _ActionsBackend.state["runs_reads"] = []
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertTrue(result["ok"])
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_LANE_HEAD_MISMATCH")
        self.assertIsNone(evidence["pull_request"])
        self.assertEqual(result["verdict"], "no_pr")
        self.assertEqual(_ActionsBackend.state["runs_reads"], [])

    def test_a_lane_behind_its_branch_still_reports_its_open_pull_request(self):
        """A lane behind its branch never reads ``no_pr``.

        The forge merges ``main`` into every open pull-request branch when
        main advances, so the branch head moves and the lane's local head
        does not. A lane in this state must not be answered ``no_pr`` -- a
        verdict whose own documentation reads "either not published yet, or
        merged and the branch deleted with it". Both are benign, and both
        would be false here: the check run after every publish would lose
        the ability to produce a positive result.

        The lane listing matches by branch name, so the pull request is
        resolved the same way, which is what this pins: an open pull request
        is reported as open even when the evidence for the lane's own head
        is, correctly, unavailable.

        The exact-head rule is untouched. No Actions call is made for a
        head the forge does not have, and the verdict does not become a
        borrowed green from the run that tested the branch.
        """
        published = self._push_and_stub_pull()
        # The forge's own merge commit, reproduced against the real origin
        # rather than stubbed: the branch moves on, the lane stays standing
        # on the commit it published. `_reported_remote_head` reads
        # `ls-remote`, so only a real push puts the two out of step.
        (self._task_wt / "merged.txt").write_text("main, merged in by the forge\n")
        self._git(["add", "merged.txt"], self._task_wt)
        self._git(["commit", "-m", "Merge branch 'main' into task/..."], self._task_wt)
        self._git(["push", "origin", "HEAD"], self._task_wt)
        self._git(["reset", "--hard", published], self._task_wt)
        self.assertNotEqual(
            self._git(["rev-parse", "HEAD"], self._task_wt).stdout.strip(),
            self._git(
                ["rev-parse", f"origin/{self._branch}"], self._task_wt
            ).stdout.strip(),
            "the fixture has to actually put the lane behind its branch",
        )
        _ActionsBackend.state["runs"] = []
        _ActionsBackend.state["runs_reads"] = []
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertTrue(result["ok"])
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(
            evidence["pull_request"],
            1,
            "the pull request is open; reporting it as absent is the defect",
        )
        self.assertEqual(result["verdict"], "unavailable")
        self.assertNotEqual(result["verdict"], "no_pr")
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_LANE_HEAD_MISMATCH")
        self.assertIsNone(
            evidence["retry_after_seconds"],
            "waiting cannot clear this; the lane has to be brought level first",
        )
        self.assertEqual(_ActionsBackend.state["runs_reads"], [])
        self.assertIn("CI_LANE_HEAD_MISMATCH", _str(result["headline"]))

    def test_pushed_head_without_pr_reports_missing_run(self):
        self._push_and_stub_pull()
        _ActionsBackend.state["no_pull"] = True
        _ActionsBackend.state["runs"] = []
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_RUN_MISSING")

    def test_runs_api_failure_is_unavailable_evidence(self):
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        _ActionsBackend.state["runs_api_failure"] = True
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_API_UNAVAILABLE")

    def test_a_backend_without_the_actions_reader_is_unavailable_evidence(self):
        """A backend without the four ``actions_*`` functions must report
        UNAVAILABLE / ``CI_API_UNAVAILABLE`` rather than raising
        ``AttributeError`` (which `cli`'s outermost handler would turn into
        ``INTERNAL_ERROR`` -- a fail-closed refusal that names no cause and
        reads as a broken gate). The answer for an unreadable Actions
        API is UNAVAILABLE, and it has to be reported rather than raised.

        A run IS stubbed for this head, so the assertion also pins that the
        record does not claim the forge reported no run: nothing asked it.
        """
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _NoActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertTrue(result["ok"])
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_API_UNAVAILABLE")
        self.assertEqual(evidence["head_sha"], head_sha)


class CiStatusPushedPathSingleListingTests(_RealGitEvidenceCase):
    """``ci-status`` on a pushed, exact-head lane lists the remote branch
    once, not twice.

    Before this, the diagnostic read (``_reported_remote_head``) and the
    policy projection it feeds when the head is pushed
    (``_policy_projection`` via ``_remote_head``) each ran their own
    ``git ls-remote`` for the identical ref. Once ``remote_sha ==
    local_head`` is established by a real listing, that already proves what
    ``_remote_head`` would re-derive, so the second listing is redundant --
    counted here by wrapping the real runner rather than by asserting on
    ``ci-status``'s own output, which cannot see how many subprocesses
    produced it.
    """

    FORGE = "forgejo"

    def _counting_gate(self, cwd, calls: list[list[str]]):
        def _counting_runner(argv, cwd=None, env=None):
            if "ls-remote" in argv:
                calls.append(list(argv))
            return gate.run_command(argv, cwd=cwd, env=env)

        return gate.operations.GitMaintainerGate(runner=_counting_runner, cwd=cwd)

    def test_one_listing_per_call_on_the_pushed_path(self):
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        calls: list[list[str]] = []
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._counting_gate(self._task_wt, calls).execute("ci-status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "green")
        self.assertEqual(
            len(calls), 1,
            f"expected exactly one ls-remote listing, got: {calls}",
        )


class ForgejoEvidenceProbeRealGitTests(_RealGitEvidenceCase):
    """WHICH FILE, AT WHICH COMMIT, asserted on the arguments the backend
    received rather than on the verdict they produced.

    The verdict cannot distinguish the two proofs -- probing the wrong file at
    the wrong commit and probing the right one both answer through a single
    boolean -- so a test that reads only ``reason_code`` would pass against a
    probe aimed at the pull request's own head, which is the one commit the
    proof exists to exclude.

    The lane here is the ordinary one from ``_RealGitEvidenceCase``, declared
    on Forgejo in both records that name a forge. The stub backend is reused
    unchanged: what is under test is the forge module's choice of path and commit, not
    a backend's argv.
    """

    FORGE = "forgejo"

    def _forgejo_run(
        self, head_sha: str, *, referenced: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        """Stub one Forgejo run for *head_sha* and return it.

        ``referenced=None`` leaves ``referenced_workflows`` absent, which is
        the shape Forgejo really produces --
        ``gate_backend_forgejo._translated_run`` emits ten fixed keys and that
        is not among them (the fixture run_record omits it by default).
        ``referenced=[...]`` adds the field explicitly for the tests
        that probe how a payload carrying it is treated.

        ``reusable_sha`` is deliberately NOT stubbed, so the base commit is the
        only ref the contents probe answers 200 for. A probe aimed anywhere
        else 404s and the lane refuses, which means the verdict assertions
        below are not merely decorative.
        """
        run = run_record(
            run_id=7001,
            head_sha=head_sha,
            base_sha=self._origin_main,
            pr_number=1,
            event="pull_request_target",
            path=FORGEJO_LAUNCHER,
        )
        if referenced is not None:
            run["referenced_workflows"] = referenced
        _ActionsBackend.state["runs"] = [run]
        _ActionsBackend.state["jobs"] = jobs_payload()
        return run

    def test_the_probe_targets_the_launcher_at_the_base_commit(self):
        """The Forgejo arm of the branch: ``.forgejo/workflows/checks.yml`` at
        the pull request's BASE commit.

        Both halves matter and each excludes a different mistake. The path
        excludes GitHub's reusable workflow, which does not exist on Forgejo
        at all. The commit excludes the head, and that is the security half:
        ``pull_request_target`` is defined to load the workflow file from the
        base ref, so probing the base is what turns that definition into
        evidence -- probing the head would prove only that the pull request
        contains a file of that name, which the pull request author chooses.

        Reaching this branch needs a run carrying a ``referenced_workflows``
        entry, and Forgejo emits none, so the shape is supplied here. See
        ``test_a_run_in_the_shape_forgejo_emits_never_reaches_the_probe``
        below, which pins that gap as the defect it is rather than leaving
        this test to imply a reachability it does not have.
        """
        head_sha = self._push_and_stub_pull()
        self._forgejo_run(
            head_sha,
            referenced=[
                {"path": TRUSTED_REUSABLE, "sha": "c" * 40, "ref": "refs/heads/main"}
            ],
        )
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        probes = _list(_ActionsBackend.state["file_probes"])
        self.assertEqual(
            probes, [{"path": FORGEJO_LAUNCHER, "ref": self._origin_main}]
        )
        probe = _dict(probes[0])
        self.assertNotEqual(probe["path"], TRUSTED_REUSABLE)
        self.assertNotEqual(probe["ref"], head_sha)
        # The ref that WAS available to be used by mistake: the run's own
        # resolved reusable SHA. Naming it makes the assertion above a choice
        # between three candidates rather than a match against the only value
        # in scope.
        self.assertNotEqual(probe["ref"], "c" * 40)
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["workflow_path"], FORGEJO_LAUNCHER)

    def test_the_run_shape_forgejo_actually_emits_still_reaches_the_probe(self):
        """The probe fires on a run carrying NO ``referenced_workflows`` at all.

        This is the shape Forgejo really produces, and it is the one a guard
        written for GitHub would let slip through. Gating the probe on
        ``ci.needs_referenced_file_check``, which is
        ``referenced_workflow_of(run) is not None``, asks a GitHub question.
        Forgejo emits that field on no endpoint, so such a guard is false for
        every real Forgejo run: no probe runs, the file-existence tri-state
        stays None, and ``evaluate_evidence`` fails closed. The refusal is
        safe, and it is still the same false negative the trust profile
        exists to remove, arriving one stage later. The probe therefore
        fires unconditionally.

        A profile with no reusable resolution has nothing to gate on, so it
        always probes. Asserting the RECORDED PROBE rather than only the verdict
        is deliberate: a verdict alone cannot distinguish "probed and proved"
        from "never probed and refused", which is exactly how the gap survived
        its first round of tests.
        """
        head_sha = self._push_and_stub_pull()
        self._forgejo_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        probes = _ActionsBackend.state.get("file_probes", [])
        self.assertEqual(
            [probe["path"] for probe in probes],
            [FORGEJO_LAUNCHER],
            "a Forgejo run with no referenced_workflows must still be probed, "
            "and at its own launcher rather than GitHub's reusable path",
        )
        evidence = _dict(result["ci_evidence"])
        self.assertNotEqual(
            evidence["reason_code"],
            "CI_WORKFLOW_UNTRUSTED",
            "the trusted launcher was proved present at the base commit, so "
            "the workflow must not be reported untrusted",
        )

    def test_a_forgejo_lane_resolves_a_trust_profile_at_all(self):
        """The forge reached ``ci.TRUST_PROFILES`` and found itself there.

        The forge module sets ``api_failure`` when a backend exists but the forge
        has no profile, so a Forgejo lane that had been left out of the table
        would report ``CI_API_UNAVAILABLE`` -- a different refusal, about the
        API rather than the workflow. Distinguishing the two is what stops the
        two tests above from passing for the wrong reason.
        """
        head_sha = self._push_and_stub_pull()
        self._forgejo_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        evidence = _dict(result["ci_evidence"])
        self.assertNotEqual(evidence["reason_code"], "CI_API_UNAVAILABLE")
        self.assertEqual(evidence["required_profile"], "checks/c2")

    def test_the_github_event_is_untrusted_on_a_forgejo_lane(self):
        """End to end, the cross-profile refusal the unit tests assert.

        The lane is Forgejo, so the forge module resolves the Forgejo profile; the
        run claims GitHub's trusted event. It has to refuse, or a
        ``pull_request`` run -- which on Forgejo takes its workflow from the
        pull request's own head -- would judge its own pull request.
        """
        head_sha = self._push_and_stub_pull()
        run = self._forgejo_run(
            head_sha,
            referenced=[
                {"path": TRUSTED_REUSABLE, "sha": "c" * 40, "ref": "refs/heads/main"}
            ],
        )
        run["event"] = "pull_request"
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")


class ForgejoBaseBindingRealGitTests(_RealGitEvidenceCase):
    """The base/head binding end to end on Forgejo, through the real translation.

    The gate's base is a real commit in a real repository; the run's base is a
    SHA inside a webhook payload the fake forge returns. Nothing copies one
    into the other, so the comparison ci.py performs is between two independent
    values and can genuinely go either way -- which is the only thing that
    makes a passing base check mean anything.
    """

    FORGE = "forgejo"

    def setUp(self):
        super().setUp()
        _ForgejoRawBackend.state = {}

    def tearDown(self):
        _ForgejoRawBackend.state = {}
        super().tearDown()

    def _stub_forgejo(
        self,
        head_sha: str,
        *,
        payload_base: str,
        payload_head: str | None = None,
        run_commit: str | None = None,
        job_run_id: int = 16,
        runs_event: str = "pull_request",
    ) -> None:
        """Serve one task row and one runs row for *head_sha*.

        ``payload_base`` is the ONLY place a base exists in this fixture. The
        task row has none, the gate is never asked, and the two are compared
        by ci.py -- so pointing this at ``self._origin_main`` and pointing it
        elsewhere must produce different verdicts.

        ``job_run_id`` defaults to 16, ``forgejo_run_row``'s id, because a job
        belongs to a RUN, never to a task: it must not default to 41,
        ``forgejo_task_row``'s id, which is a TASK id. A reader that emitted
        the task row's id as the run's would match a job stubbed against the
        task id, and the whole chain would go green on a premise the forge
        does not hold. A job
        carries ``run_id`` (the run) and ``task_id`` (the task), and
        `/actions/runs/<task id>/jobs` answers 404. The stub models both
        ids -- ``task_id`` is 512 below -- and ``run_id`` must point at the run.
        """
        _ForgejoRawBackend.state["base_commit"] = self._origin_main
        _ForgejoRawBackend.state["branch"] = self._branch
        _ForgejoRawBackend.state["pushed_sha"] = head_sha
        _ForgejoRawBackend.state["tasks"] = [forgejo_task_row(head_sha=head_sha)]
        _ForgejoRawBackend.state["runs"] = [
            forgejo_run_row(
                commit_sha=run_commit if run_commit is not None else head_sha,
                event=runs_event,
                event_payload=forgejo_event_payload(
                    head_sha=payload_head if payload_head is not None else head_sha,
                    base_sha=payload_base,
                    number=1,
                ),
            )
        ]
        # The jobs Forgejo really reports: no `workflow_name` on any of them.
        _ForgejoRawBackend.state["jobs"] = [
            {
                "id": 88,
                "run_id": job_run_id,
                "attempt": 1,
                "handle": "c1",
                "name": "c1",
                "task_id": 512,
                "status": "success",
            }
        ]

    def _ci_evidence(self) -> dict[str, object]:
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ForgejoRawBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        return _dict(result["ci_evidence"])

    def test_unjoined_newer_run_is_retryable_incomplete_evidence(self) -> None:
        """Both task-list gaps suppress older green until the join completes."""
        head_sha = self._push_and_stub_pull()
        for surface in ("ahead", "short"):
            with self.subTest(surface=surface):
                self._stub_forgejo(head_sha, payload_base=self._origin_main)
                _ForgejoRawBackend.state["file_probes"] = []
                _list(_ForgejoRawBackend.state["runs"]).append(
                    forgejo_run_row(
                        run_id=17,
                        index_in_repo=8,
                        commit_sha=head_sha,
                        event_payload=forgejo_event_payload(
                            head_sha=head_sha, base_sha=self._origin_main, number=1
                        ),
                    )
                )
                tasks = _list(_ForgejoRawBackend.state["tasks"])
                if surface == "short":
                    tasks.append(forgejo_task_row(run_number=9, head_sha="f" * 40))
                with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ForgejoRawBackend}):
                    result = self._gate(self._task_wt).execute("ci-status")
                    evidence = _dict(result["ci_evidence"])
                    self.assertEqual(result["verdict"], "pending")
                    self.assertEqual(evidence["result"], "UNAVAILABLE")
                    self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")
                    self.assertEqual(evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS)
                    self.assertIsNone(evidence["accepted_profile"])
                    self.assertIsNone(evidence["selected_run_id"])
                    self.assertIsNone(evidence["workflow_run_id"])
                    self.assertEqual(evidence["candidate_run_ids"], [])
                    self.assertEqual(evidence["head_sha"], head_sha)
                    self.assertEqual(_ForgejoRawBackend.state.get("file_probes", []), [])

                    tasks.append(forgejo_task_row(run_number=8, head_sha=head_sha))
                    _dict(_list(_ForgejoRawBackend.state["jobs"])[0])["run_id"] = 17
                    recovered = self._gate(self._task_wt).execute("ci-status")
                recovered_evidence = _dict(recovered["ci_evidence"])
                self.assertEqual(recovered["verdict"], "green")
                self.assertEqual(recovered_evidence["result"], "PASS")
                self.assertEqual(recovered_evidence["accepted_profile"], "checks/c1")
                self.assertEqual(recovered_evidence["selected_run_id"], 17)
                self.assertEqual(recovered_evidence["candidate_run_ids"], [16, 17])

    def test_empty_exact_head_runs_remain_missing(self) -> None:
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        _ForgejoRawBackend.state["runs"] = []
        evidence = self._ci_evidence()
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_RUN_MISSING")
        self.assertEqual(evidence["retry_after_seconds"], ci.RETRY_IN_PROGRESS_SECONDS)
        self.assertIsNone(evidence["selected_run_id"])
        self.assertIsNone(evidence["accepted_profile"])

    def test_unjoinable_foreign_tasks_do_not_block_valid_candidate(self) -> None:
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        tasks = _list(_ForgejoRawBackend.state["tasks"])
        tasks[:0] = [None, {"run_number": True, "head_sha": "f" * 40,
                           "workflow_id": "other.yml"}]
        evidence = self._ci_evidence()
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["accepted_profile"], "checks/c1")
        self.assertEqual(evidence["selected_run_id"], 16)

    def test_conflicting_unjoined_identity_has_no_retry(self) -> None:
        head_sha = self._push_and_stub_pull()
        for conflict in ("task_head", "task_workflow", "run_head", "run_id", "duplicate_index"):
            with self.subTest(conflict=conflict):
                self._stub_forgejo(head_sha, payload_base=self._origin_main)
                tasks = _list(_ForgejoRawBackend.state["tasks"])
                runs = _list(_ForgejoRawBackend.state["runs"])
                if conflict == "task_head":
                    _dict(tasks[0])["head_sha"] = "f" * 40
                elif conflict == "task_workflow":
                    _dict(tasks[0])["workflow_id"] = "other.yml"
                else:
                    tasks.clear()
                    if conflict == "run_head":
                        _dict(runs[0])["commit_sha"] = "f" * 40
                    elif conflict == "run_id":
                        del _dict(runs[0])["id"]
                    else:
                        runs.append({**_dict(runs[0]), "id": 17})
                evidence = self._ci_evidence()
                self.assertEqual(evidence["result"], "UNAVAILABLE")
                self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")
                self.assertIsNone(evidence["retry_after_seconds"])
                self.assertIsNone(evidence["selected_run_id"])
                self.assertIsNone(evidence["accepted_profile"])

    def test_unknown_incomplete_observation_has_no_retry(self) -> None:
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        for marker in (None, 0, 1, "true", [], {}):
            with self.subTest(marker=marker):
                result = _Completed(json.dumps({"workflow_runs": [], "awaiting_task_rows": marker}))
                with mock.patch.object(_ForgejoRawBackend, "actions_runs_for_workflow", return_value=result):
                    evidence = self._ci_evidence()
                self.assertEqual(evidence["result"], "UNAVAILABLE")
                self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")
                self.assertIsNone(evidence["retry_after_seconds"])
                self.assertIsNone(evidence["selected_run_id"])
                self.assertIsNone(evidence["accepted_profile"])

    def test_a_base_the_run_did_not_test_is_base_stale(self):
        """THE TEST. A run whose webhook base is not the lane's current base
        must refuse ``CI_BASE_STALE``.

        This is what proves the base is genuinely independent rather than the
        gate's own value round-tripped through the reader. If the record were
        synthesised from ``pull.base_commit``, both sides of ci.py's comparison
        would be the same number, this test could not be made to fail, and the
        base/head binding would be decoration.

        Its control is ``test_the_gate_base_is_accepted`` below: the identical
        lane with the payload base moved onto the gate's base stops reporting
        stale. One variable, two verdicts.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base="9" * 40)
        evidence = self._ci_evidence()
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        self.assertEqual(evidence["base_sha"], self._origin_main)

    def test_a_whole_forgejo_lane_reaches_pass(self):
        """THE END-TO-END VERDICT, and the control for the stale test above.

        One variable moves -- the payload base onto the lane's real base -- and
        the verdict changes, so the stale test was measuring something.

        Every link in the chain is asserted rather than summarised by the
        verdict, because PASS is the one result that means every stage was
        satisfied and so the one that hides which stage did the work:

          launcher   the run's own event and path, against the Forgejo profile
          file       `.forgejo/workflows/checks.yml` proved present at the BASE
                     commit, which is what `pull_request_target` binds
          binding    base and head from the webhook, matching the gate's view
          identity   a `c1` job belonging to THIS run
          profile    c1 accepted for the lane's required c2

        The run carries no ``referenced_workflows`` and the jobs carry no
        ``workflow_name`` -- the shapes Forgejo really emits -- so this passes
        by the Forgejo route rather than by accidentally looking like GitHub.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        evidence = self._ci_evidence()
        self.assertEqual(evidence["result"], "PASS")
        self.assertIsNone(evidence["reason_code"])
        self.assertEqual(evidence["workflow_path"], FORGEJO_LAUNCHER)
        self.assertEqual(evidence["head_sha"], head_sha)
        self.assertEqual(evidence["base_sha"], self._origin_main)
        self.assertEqual(evidence["accepted_profile"], "checks/c1")
        self.assertEqual(evidence["required_profile"], "checks/c2")
        # Selection projection metadata: one candidate, the accepted run.
        # 16 is `forgejo_run_row`'s id, from `/actions/runs`. It must not be
        # 41 -- `forgejo_task_row`'s id, a TASK id, which no run-scoped
        # endpoint accepts. A projection naming an id the forge cannot be asked
        # about is a wrong answer that reads like a right one, so pin the id
        # the jobs fetch actually uses.
        self.assertEqual(evidence["candidate_run_ids"], [16])
        self.assertEqual(evidence["selected_run_id"], 16)
        # No resolved reusable SHA exists on Forgejo, and none is invented.
        self.assertIsNone(evidence["trusted_workflow_ref"])
        # The file proof really ran, at the base commit rather than the head.
        probes = _list(_ForgejoRawBackend.state["file_probes"])
        self.assertEqual(
            probes, [{"path": FORGEJO_LAUNCHER, "ref": self._origin_main}]
        )

    def test_a_job_from_another_run_does_not_pass_the_lane(self):
        """End to end: the identity proof's run binding, on the whole chain.

        Everything else about this lane is the passing one above -- the same
        launcher, the same base, the same green ``c1``. Only the job's owning
        run moves, and the lane must stop passing. A green ``c1`` from a
        different run is a true statement about a different pull request.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main, job_run_id=999)
        evidence = self._ci_evidence()
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_PROFILE_MISSING")

    def test_the_runs_endpoint_event_disagreement_does_not_change_the_verdict(self):
        """The `event` trap, on the whole chain rather than at the reader.

        `/actions/runs` reports ``event: "pull_request"`` for these runs and
        ``""`` for some -- as Forgejo reports. The lane must still PASS,
        because the event that decides trust comes from `/actions/tasks`. A
        reader that took it from the runs row would refuse every Forgejo lane
        with ``CI_WORKFLOW_UNTRUSTED``, and ``pull_request`` is plausible
        enough that the refusal would be read as real.
        """
        head_sha = self._push_and_stub_pull()
        for wrong in ("pull_request", "", "push", "workflow_dispatch"):
            with self.subTest(runs_event=wrong):
                # One push for the lane; only the runs row's event moves. The
                # commit is what makes the lane, so re-pushing per subtest
                # would fail on an empty commit rather than test anything.
                self._stub_forgejo(
                    head_sha, payload_base=self._origin_main, runs_event=wrong
                )
                evidence = self._ci_evidence()
                self.assertEqual(evidence["result"], "PASS")
                self.assertNotEqual(evidence["reason_code"], "CI_WORKFLOW_UNTRUSTED")

    def test_a_payload_head_that_disagrees_with_the_run_is_response_invalid(self):
        """ci.py cross-checks the record's head against the run's own.

        Both values are Forgejo's -- the task row's ``head_sha`` and the
        webhook's ``pull_request.head.sha`` -- so this is one forge answer
        against another. Taking the record's head from the task row would make
        it a value against itself and this test could not fail.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(
            head_sha, payload_base=self._origin_main, payload_head="e" * 40
        )
        evidence = self._ci_evidence()
        self.assertEqual(evidence["reason_code"], "CI_RESPONSE_INVALID")

    def test_a_run_that_cannot_be_bound_is_ambiguous(self):
        """No `/actions/runs` row for this commit means no base at all.

        The evidence must say the result is ambiguous rather than pass the
        stage on an absent record, and it must not invent a base to compare.

        AND AMBIGUOUS IS NOT MISSING. The commit gate costs the base, never the
        identity: the run index still matched, so the record keeps its id and
        reaches this answer. Made to cost the identity too, the record would
        carry no int id, `ci.select_newest_run` would drop it, and the lane
        would report ``CI_RUN_MISSING``, which claims the
        forge has no run for a head it plainly has one for. Both are refusals,
        so both look safe; only one of them is true.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(
            head_sha, payload_base=self._origin_main, run_commit="f" * 40
        )
        evidence = self._ci_evidence()
        self.assertEqual(evidence["reason_code"], "CI_RESULT_AMBIGUOUS")

    def test_an_unreadable_runs_endpoint_is_an_api_failure(self):
        """The endpoint holding the base is required, so losing it is an API
        failure rather than a quietly unbound run.

        ``CI_RESULT_AMBIGUOUS`` here would say the forge gave an unclear
        answer; it gave none.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        _ForgejoRawBackend.state["runs_api_failure"] = True
        evidence = self._ci_evidence()
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_API_UNAVAILABLE")

    def test_the_probe_argv_reaches_the_launcher_at_the_base_commit(self):
        """The trust probe, asserted on the endpoint the real backend built.

        The earlier probe tests assert the arguments the forge module passed to a stub;
        this one asserts the REST path ``gate_backend_forgejo`` actually
        constructed from them, so a backend that built the wrong contents URL
        would be caught rather than hidden behind a stub that never used it.
        """
        head_sha = self._push_and_stub_pull()
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        self._ci_evidence()
        probes = _list(_ForgejoRawBackend.state["file_probes"])
        self.assertEqual(
            probes, [{"path": FORGEJO_LAUNCHER, "ref": self._origin_main}]
        )
        self.assertNotEqual(_dict(probes[0])["ref"], head_sha)

    # --- a run Forgejo holds `blocked` behind a failed job ---------
    #
    # Raw Forgejo bodies through the real reader: only this path can tell a
    # run that kept no ``forge_status`` or a ``needs_jobs_fetch`` that never
    # asks for the jobs, because a stub that already speaks GitHub's shape
    # would hand the evaluator both.

    def _stub_run_in(
        self, head_sha: str, status: str, jobs: list[dict[str, object]]
    ) -> None:
        """One raw run in Forgejo *status* whose `/jobs` answer is *jobs*."""
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        _dict(_list(_ForgejoRawBackend.state["runs"])[0])["status"] = status
        _ForgejoRawBackend.state["jobs"] = jobs

    @staticmethod
    def _stranded_rows(run_id: int = 16) -> list[dict[str, object]]:
        """Forgejo's run behind a failed job: one ``failure``, its dependents
        ``blocked``, one ``success``."""
        return [
            forgejo_job_row(80, "classify", "success", run_id=run_id),
            forgejo_job_row(
                81, "leaf-test-3", "failure", needs=["classify"], run_id=run_id
            ),
            forgejo_job_row(
                82, "route", "blocked", needs=["classify", "leaf-test-3"],
                run_id=run_id,
            ),
            forgejo_job_row(
                88, "c1", "blocked", needs=["classify", "leaf-test-3", "route"],
                run_id=run_id,
            ),
        ]

    def _ci_status(self) -> dict[str, object]:
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ForgejoRawBackend}):
            return self._gate(self._task_wt).execute("ci-status")

    def test_a_blocked_run_behind_a_failed_job_is_red_end_to_end(self) -> None:
        """(a) The stranded-run shape reads red, with the existing bounded,
        redacted ``ci-failure.v1`` record naming the failed job."""
        head_sha = self._push_and_stub_pull()
        self._stub_run_in(head_sha, "blocked", self._stranded_rows())
        _ForgejoRawBackend.state["logs"] = {
            81: "compiling\ntoken ghp_%s leaked\nboom: the real failure\n" % ("A" * 36)
        }

        result = self._ci_status()

        self.assertEqual(result["verdict"], "red")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "FAIL")
        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")
        self.assertIsNone(evidence["retry_after_seconds"])
        self.assertEqual(evidence["job_ids"], [88])
        failure = _dict(result["failure_evidence"])
        self.assertEqual(failure["schema"], ci.CI_FAILURE_SCHEMA)
        self.assertEqual(_failed_job_names(failure), {"leaf-test-3"})
        entry = _dict(_list(failure["failed_jobs"])[0])
        self.assertIs(entry["log_available"], True)
        excerpt = _str(entry["excerpt"])
        self.assertIn("boom: the real failure", excerpt)
        self.assertNotIn("ghp_", excerpt)
        self.assertIn("[REDACTED]", excerpt)
        self.assertEqual(len(_list(_ForgejoRawBackend.state["jobs_reads"])), 1)

    def test_a_blocked_run_with_no_failed_job_stays_queued(self) -> None:
        """(c) Negative control: ``blocked`` with nothing failed is still
        waiting work. Its jobs are read once, and the verdict is unchanged."""
        head_sha = self._push_and_stub_pull()
        rows = self._stranded_rows()
        rows[1] = forgejo_job_row(81, "leaf-test-3", "success", needs=["classify"])
        self._stub_run_in(head_sha, "blocked", rows)

        result = self._ci_status()

        self.assertEqual(result["verdict"], "pending")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["reason_code"], "CI_RUN_QUEUED")
        self.assertEqual(evidence["job_ids"], [])
        self.assertIsNone(result["failure_evidence"])
        self.assertEqual(len(_list(_ForgejoRawBackend.state["jobs_reads"])), 1)

    def test_a_blocked_run_with_a_waiting_job_beside_the_failure_stays_queued(
        self,
    ) -> None:
        """(c) A ``waiting`` job is work a runner may still pick up, so the
        failed job beside it does not make the run red yet."""
        head_sha = self._push_and_stub_pull()
        rows = self._stranded_rows()
        rows[2] = forgejo_job_row(
            82, "route", "waiting", needs=["classify", "leaf-test-3"]
        )
        self._stub_run_in(head_sha, "blocked", rows)

        result = self._ci_status()

        self.assertEqual(result["verdict"], "pending")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_RUN_QUEUED")
        self.assertIsNone(result["failure_evidence"])

    def test_a_waiting_run_reads_no_jobs(self) -> None:
        """(c) Only ``blocked`` asks for the jobs: a plain ``waiting`` run is
        decided by its own status, as before, even beside a failed row."""
        head_sha = self._push_and_stub_pull()
        self._stub_run_in(head_sha, "waiting", self._stranded_rows())

        result = self._ci_status()

        self.assertEqual(result["verdict"], "pending")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_RUN_QUEUED")
        self.assertEqual(_list(_ForgejoRawBackend.state.get("jobs_reads", [])), [])

    def test_a_running_run_with_an_early_failed_job_stays_in_progress_end_to_end(
        self,
    ) -> None:
        """(d) Unchanged: Forgejo still runs a job, so the verdict stays
        pending ``CI_RUN_IN_PROGRESS`` and the failure is advisory only."""
        head_sha = self._push_and_stub_pull()
        rows = self._stranded_rows()
        rows[2] = forgejo_job_row(
            82, "route", "running", needs=["classify", "leaf-test-3"]
        )
        self._stub_run_in(head_sha, "running", rows)
        _ForgejoRawBackend.state["logs"] = {81: "boom: the early failure\n"}

        result = self._ci_status()

        self.assertEqual(result["verdict"], "pending")
        self.assertEqual(
            _dict(result["ci_evidence"])["reason_code"], "CI_RUN_IN_PROGRESS"
        )
        self.assertIsNone(result["failure_evidence"])
        progress = _dict(result["ci_progress"])
        self.assertFalse(progress["terminal"])
        self.assertEqual(progress["overall_state"], "pending")
        self.assertIn("boom: the early failure", str(progress))

    # --- the narrowed-to-full successor ----------
    #
    # A lane that requires `checks/c1` and settled green as a narrowed `c2` run
    # is answered by the full run that `gate/candidate` starts. That run
    # is chosen AFTER the settled one was evaluated, so the selected run's
    # jobs read never saw it: it has to be settled the same way, or a full run
    # Forgejo holds `blocked` behind a failed job reads queued for ever.

    def _push_c1_lane(self) -> str:
        """A lane that changes a CI-trust path, so it requires `checks/c1`."""
        (self._task_wt / ".forgejo" / "workflows").mkdir(parents=True, exist_ok=True)
        return self._push_and_stub_pull(".forgejo/workflows/extra.yml")

    def _stub_narrowed_then_full(
        self, head_sha: str, full_status: str, full_rows: list[dict[str, object]]
    ) -> None:
        """Run 16 settled green as a narrowed `c2`; run 17, the full run, is
        Forgejo-*full_status* and its jobs are *full_rows*."""
        self._stub_forgejo(head_sha, payload_base=self._origin_main)
        _list(_ForgejoRawBackend.state["runs"]).append(
            forgejo_run_row(
                run_id=17,
                index_in_repo=8,
                commit_sha=head_sha,
                status=full_status,
                event_payload=forgejo_event_payload(
                    head_sha=head_sha, base_sha=self._origin_main, number=1
                ),
            )
        )
        _list(_ForgejoRawBackend.state["tasks"]).append(
            forgejo_task_row(run_number=8, head_sha=head_sha)
        )
        _ForgejoRawBackend.state["jobs_by_run"] = {
            16: [forgejo_job_row(79, "c2", "success", run_id=16)],
            17: full_rows,
        }

    @staticmethod
    def _full_run_reads() -> list[object]:
        """The `/jobs` reads the gate made for run 17, the full run."""
        reads = _list(_ForgejoRawBackend.state.get("jobs_reads", []))
        return [read for read in reads if _str(read).endswith("/runs/17/jobs")]

    def test_a_blocked_full_run_behind_a_failed_job_is_red_behind_a_narrowed_green_run(
        self,
    ) -> None:
        """The full `c1` run is Forgejo-`blocked` behind a
        failed leaf. Red, the leaf named, its jobs read once."""
        head_sha = self._push_c1_lane()
        self._stub_narrowed_then_full(
            head_sha, "blocked", self._stranded_rows(run_id=17)
        )
        _ForgejoRawBackend.state["logs"] = {81: "compiling\nboom: the real failure\n"}

        result = self._ci_status()

        self.assertEqual(result["verdict"], "red")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["required_profile"], "checks/c1")
        self.assertEqual(evidence["result"], "FAIL")
        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")
        self.assertEqual(evidence["workflow_run_id"], 17)
        self.assertEqual(evidence["job_ids"], [88])
        # The advisory selection facts read the settled run, not the queued
        # record: the newest terminal candidate is the red run, not run 16.
        self.assertEqual(evidence["terminal_candidate_run_id"], 17)
        self.assertEqual(evidence["terminal_candidate_conclusion"], "failure")
        failure = _dict(result["failure_evidence"])
        self.assertEqual(_failed_job_names(failure), {"leaf-test-3"})
        entry = _dict(_list(failure["failed_jobs"])[0])
        self.assertIn("boom: the real failure", _str(entry["excerpt"]))
        self.assertEqual(len(self._full_run_reads()), 1)

    def test_rerun_ci_is_handed_the_settled_successor_and_its_jobs(self) -> None:
        """What a CI rerun reads: the run is completed/failure with the raw
        word kept, the jobs are the fetched payload, and the failed leaf is
        the one root-cause job to re-run (the blocked rows are not active)."""
        head_sha = self._push_c1_lane()
        self._stub_narrowed_then_full(
            head_sha, "blocked", self._stranded_rows(run_id=17)
        )
        checker = self._gate(self._task_wt)

        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ForgejoRawBackend}):
            _policy, evidence, run, jobs = checker._policy_evidence_run(
                checker.context()
            )

        self.assertEqual(evidence["reason_code"], "CI_RUN_FAILED")
        settled = _dict(run)
        self.assertEqual(settled["id"], 17)
        self.assertEqual(settled["status"], "completed")
        self.assertEqual(settled["conclusion"], "failure")
        self.assertEqual(settled["forge_status"], "blocked")
        self.assertEqual(
            checker._rerun_candidates(
                None, 17, jobs, blocked_run=settled["forge_status"] == "blocked"
            ),
            # task_id binds the code proof to this attempt.
            [{"id": 81, "name": "leaf-test-3", "attempt": 1, "task_id": 505}],
        )
        self.assertEqual(len(self._full_run_reads()), 1)

    def test_a_blocked_full_run_with_no_failed_job_stays_queued_behind_a_narrowed_green_run(
        self,
    ) -> None:
        """Negative control: nothing failed, so nothing is stranded. The jobs
        are read once and the verdict is the queued full run's, as before."""
        head_sha = self._push_c1_lane()
        rows = self._stranded_rows(run_id=17)
        rows[1] = forgejo_job_row(
            81, "leaf-test-3", "success", needs=["classify"], run_id=17
        )
        self._stub_narrowed_then_full(head_sha, "blocked", rows)

        result = self._ci_status()

        self.assertEqual(result["verdict"], "pending")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["reason_code"], "CI_RUN_QUEUED")
        self.assertEqual(evidence["workflow_run_id"], 17)
        self.assertIsNone(result["failure_evidence"])
        self.assertEqual(len(self._full_run_reads()), 1)

    def test_an_unreadable_successor_jobs_read_is_unavailable_not_queued(self) -> None:
        """The successor's read fails as any selected run's does: the evidence
        is UNAVAILABLE ``CI_API_UNAVAILABLE`` (retryable), not a quiet queued
        verdict that hides the failed leaf."""
        head_sha = self._push_c1_lane()
        self._stub_narrowed_then_full(
            head_sha, "blocked", self._stranded_rows(run_id=17)
        )
        _ForgejoRawBackend.state["jobs_unreadable"] = {17}

        evidence = _dict(self._ci_status()["ci_evidence"])

        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_API_UNAVAILABLE")
        self.assertEqual(len(self._full_run_reads()), 1)

    def test_a_waiting_full_run_behind_a_narrowed_green_run_reads_no_jobs(self) -> None:
        """Only a Forgejo-`blocked` successor asks for its jobs: a run that is
        merely `waiting` for a runner is decided by its own status, as before,
        even beside a failed row."""
        head_sha = self._push_c1_lane()
        self._stub_narrowed_then_full(
            head_sha, "waiting", self._stranded_rows(run_id=17)
        )

        result = self._ci_status()

        self.assertEqual(result["verdict"], "pending")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_RUN_QUEUED")
        self.assertEqual(self._full_run_reads(), [])


class RecordedBaseReviewEvidenceRealGitTests(_RealGitEvidenceCase):
    """``_automerge_ci_status``'s ``accept_run_base``, through the Actions path.

    Only the recorded-base reads of the inspect projections pass the
    predicate (the gate's own; ``test_merge_train_gate.py`` proves the
    ancestry predicate on real git). Here the predicate is a test double and
    the subject is what the evidence path does with its answer: an accepted
    run base is re-read with every trust, binding, job and profile check
    bound to that base, and nothing else changes.
    """

    TESTED_BASE = "9" * 40

    def _status(self, *, accept=None, conclusion="success", undecided=False):
        """*undecided*: the predicate answers None, a proof that could not run."""
        head_sha = self._push_and_stub_pull()
        run = run_record(head_sha=head_sha, base_sha=self.TESTED_BASE,
                         conclusion=conclusion)
        _ActionsBackend.state["runs"] = [run]
        _ActionsBackend.state["jobs"] = (
            jobs_payload() if conclusion == "success" else
            jobs_payload(suites_conclusion="failure", identity_conclusion="failure"))
        _ActionsBackend.state["historical_base_commit"] = self.TESTED_BASE
        checker = self._gate(self._checkout)
        context = replace(checker.managed_checkout_context(self.ALIAS),
                          branch=self._branch)
        pull = gate.PullRequest(1, "open", "main", self._origin_main, self._branch,
                                f"test/{self.ALIAS}", "fix: change")
        asked: list[str] = []

        def predicate(run_base: str) -> bool | None:
            asked.append(run_base)
            return None if undecided else bool(accept)

        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            status = (checker._automerge_ci_status(context, pull, head_sha)
                      if accept is None and not undecided else
                      checker._automerge_ci_status(context, pull, head_sha,
                                                   accept_run_base=predicate))
        return status, _dict(status["ci_evidence"]), asked

    def test_an_accepted_run_base_is_green_bound_to_that_base(self):
        status, evidence, asked = self._status(accept=True)
        self.assertEqual(asked, [self.TESTED_BASE])
        self.assertEqual(status["verdict"], "green")
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["base_sha"], self.TESTED_BASE)
        self.assertEqual(evidence["required_profile"], "checks/c2")
        # The launcher is proved at the base the run loaded it from.
        self.assertEqual(_list(_ActionsBackend.state["file_probes"])[-1],
                         {"path": FORGEJO_LAUNCHER, "ref": self.TESTED_BASE})

    def test_a_refused_run_base_stays_base_stale(self):
        status, evidence, asked = self._status(accept=False)
        self.assertEqual(asked, [self.TESTED_BASE])
        self.assertEqual(status["verdict"], "pending")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        self.assertEqual(evidence["base_sha"], self._origin_main)
        self.assertEqual(len(_list(_ActionsBackend.state["runs_reads"])), 1)

    def test_an_accepted_run_base_is_reported(self):
        """``ci.ci_evidence_answer`` reads it; nothing else sets it."""
        status, _evidence, _asked = self._status(accept=True)
        self.assertEqual(status["accepted_run_base"], self.TESTED_BASE)

    def test_a_refused_run_base_is_not_reported(self):
        status, _evidence, _asked = self._status(accept=False)
        self.assertNotIn("accepted_run_base", status)

    def test_no_run_base_is_reported_without_the_predicate(self):
        status, _evidence, _asked = self._status()
        self.assertNotIn("accepted_run_base", status)

    def test_a_failed_run_on_an_accepted_base_is_not_green(self):
        status, evidence, _asked = self._status(accept=True, conclusion="failure")
        self.assertNotEqual(status["verdict"], "green")
        self.assertNotEqual(evidence["result"], "PASS")

    def test_without_the_predicate_ci_stays_bound_to_the_pull_base(self):
        """Every merge, refresh, train and ci-status caller: no predicate."""
        status, evidence, asked = self._status()
        self.assertEqual(asked, [])
        self.assertEqual(status["verdict"], "pending")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        self.assertEqual(evidence["base_sha"], self._origin_main)

    def test_a_proven_run_base_is_reported_proven(self):
        """``ci.ci_evidence_answer`` reads the proof's own outcome."""
        status, _evidence, asked = self._status(accept=True)
        self.assertEqual(asked, [self.TESTED_BASE])
        self.assertEqual(status["run_base_proof"], "proven")

    def test_a_refused_run_base_is_reported_refused(self):
        status, _evidence, asked = self._status(accept=False)
        self.assertEqual(asked, [self.TESTED_BASE])
        self.assertEqual(status["run_base_proof"], "refused")

    def test_a_proof_that_could_not_run_is_reported_unknown_not_refused(self):
        status, _evidence, asked = self._status(undecided=True)
        self.assertEqual(asked, [self.TESTED_BASE])
        self.assertEqual(status["run_base_proof"], "unknown")

    def test_an_undecided_proof_is_neither_accepted_nor_read_again(self):
        status, evidence, _asked = self._status(undecided=True)
        self.assertNotIn("accepted_run_base", status)
        self.assertEqual(status["verdict"], "pending")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        self.assertEqual(evidence["base_sha"], self._origin_main)
        self.assertEqual(len(_list(_ActionsBackend.state["runs_reads"])), 1)

    def test_no_run_base_proof_is_reported_without_the_predicate(self):
        status, _evidence, _asked = self._status()
        self.assertNotIn("run_base_proof", status)


class FailureEvidenceRealGitTests(_RealGitEvidenceCase):
    def _fail_run(self, head_sha: str) -> None:
        run = run_record(
            run_id=7001,
            head_sha=head_sha,
            base_sha=self._origin_main,
            pr_number=1,
            conclusion="failure",
        )
        _ActionsBackend.state["runs"] = [run]
        _ActionsBackend.state["jobs"] = jobs_payload(
            suites_conclusion="failure", identity_conclusion="failure"
        )
        _ActionsBackend.state["logs"] = {
            300: "some output\ntoken ghp_%s leaked\nfailure details\n" % ("A" * 36),
            200: "identity job failed\n",
        }

    def test_ci_status_returns_bounded_redacted_failure_evidence(self):
        head_sha = self._push_and_stub_pull()
        self._fail_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "ci-status")
        self.assertEqual(result["schema"], "ci-status.v2")
        failure = _dict(result["failure_evidence"])
        names = _failed_job_names(failure)
        self.assertIn("suites", names)
        self.assertIn("c1", names)
        excerpts = "".join(_all_excerpts(failure))
        self.assertNotIn("ghp_", excerpts)
        self.assertIn("[REDACTED]", excerpts)
        self.assertLessEqual(
            _int(failure["bytes_returned"]), ci.FAILURE_EVIDENCE_MAX_BYTES
        )
        self.assertGreater(_int(failure["redaction_count"]), 0)

    def test_green_ci_status_has_no_failure_record(self):
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "green")
        self.assertIsNone(result["failure_evidence"])

    def test_stale_base_failed_run_keeps_bound_redacted_diagnostics(self):
        head_sha = self._push_and_stub_pull()
        self._fail_run(head_sha)
        tested_base = "9" * 40
        run = _dict(_list(_ActionsBackend.state["runs"])[0])
        _set_pr_base_sha(run, tested_base)
        _ActionsBackend.state["historical_base_commit"] = tested_base
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "pending")
        evidence = _dict(result["ci_evidence"])
        self.assertEqual(evidence["result"], "UNAVAILABLE")
        self.assertEqual(evidence["reason_code"], "CI_BASE_STALE")
        self.assertEqual(evidence["base_sha"], self._origin_main)
        self.assertIsNone(evidence["accepted_profile"])
        self.assertIsNone(result["failure_evidence"])
        progress = _dict(result["ci_progress"])
        self.assertEqual(progress["schema"], "ci-progress.v1")
        self.assertEqual(progress["head_sha"], head_sha)
        self.assertEqual(progress["pull_request"], 1)
        self.assertEqual(progress["workflow_run_id"], 7001)
        self.assertEqual(progress["base_sha"], tested_base)
        self.assertEqual(progress["current_base_sha"], self._origin_main)
        self.assertTrue(progress["base_stale"])
        self.assertEqual(progress["run_status"], "completed")
        self.assertEqual(progress["overall_state"], "pending")
        self.assertFalse(progress["terminal"])
        self.assertEqual(_failed_job_names(progress), {"suites", "c1"})
        excerpts = "".join(_all_excerpts(progress))
        self.assertIn("failure details", excerpts)
        self.assertNotIn("ghp_", excerpts)
        self.assertIn("[REDACTED]", excerpts)
        self.assertLessEqual(
            _int(progress["bytes_returned"]), ci.FAILURE_EVIDENCE_MAX_BYTES
        )
        self.assertGreater(_int(progress["redaction_count"]), 0)
        self.assertEqual(_ActionsBackend.state["file_probes"], [
            {"path": FORGEJO_LAUNCHER, "ref": self._origin_main},
            {"path": FORGEJO_LAUNCHER, "ref": tested_base},
        ])

    def test_stale_base_diagnostics_reject_untrusted_run_identity(self):
        head_sha = self._push_and_stub_pull()
        tested_base = "9" * 40
        cases = {
            "wrong workflow": {"path": ".forgejo/workflows/other.yml"},
            "wrong event": {"event": "push"},
            "wrong pull request": {
                "pull_requests": [{"number": 2, "head": {"sha": head_sha},
                                   "base": {"sha": tested_base}}],
            },
            "different run head": {"head_sha": "f" * 40},
            "inconsistent recorded head": {
                "pull_requests": [{"number": 1, "head": {"sha": "f" * 40},
                                   "base": {"sha": tested_base}}],
            },
        }
        _ActionsBackend.state["historical_base_commit"] = tested_base
        for name, overrides in cases.items():
            with self.subTest(name=name):
                self._fail_run(head_sha)
                run = _dict(_list(_ActionsBackend.state["runs"])[0])
                _set_pr_base_sha(run, tested_base)
                run.update(overrides)
                with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
                    with mock.patch.object(_ActionsBackend, "actions_job_logs") as logs:
                        result = self._gate(self._task_wt).execute("ci-status")
                self.assertNotIn("ci_progress", result)
                self.assertIsNone(result["failure_evidence"])
                self.assertNotIn(result["verdict"], {"green", "red"})
                logs.assert_not_called()

    def test_stale_base_diagnostics_reject_malformed_tested_base(self):
        head_sha = self._push_and_stub_pull()
        for tested_base in ("not-a-commit", "9" * 8, "9" * 41):
            with self.subTest(tested_base=tested_base):
                self._fail_run(head_sha)
                run = _dict(_list(_ActionsBackend.state["runs"])[0])
                _set_pr_base_sha(run, tested_base)
                # The API can resolve a non-SHA ref; that must not turn it
                # into a proven immutable base or release any log content.
                _ActionsBackend.state["historical_base_commit"] = tested_base
                _ActionsBackend.state["file_probes"] = []
                with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
                    with mock.patch.object(_ActionsBackend, "actions_job_logs") as logs:
                        result = self._gate(self._task_wt).execute("ci-status")
                self.assertEqual(result["verdict"], "pending")
                self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_BASE_STALE")
                self.assertNotIn("ci_progress", result)
                self.assertIsNone(result["failure_evidence"])
                self.assertEqual(_ActionsBackend.state["file_probes"], [
                    {"path": FORGEJO_LAUNCHER, "ref": self._origin_main},
                ])
                logs.assert_not_called()

    def test_stale_base_diagnostics_require_launcher_at_tested_base(self):
        head_sha = self._push_and_stub_pull()
        self._fail_run(head_sha)
        tested_base = "9" * 40
        run = _dict(_list(_ActionsBackend.state["runs"])[0])
        _set_pr_base_sha(run, tested_base)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            with mock.patch.object(_ActionsBackend, "actions_job_logs") as logs:
                result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "pending")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_BASE_STALE")
        self.assertNotIn("ci_progress", result)
        self.assertIsNone(result["failure_evidence"])
        self.assertEqual(_ActionsBackend.state["file_probes"], [
            {"path": FORGEJO_LAUNCHER, "ref": self._origin_main},
            {"path": FORGEJO_LAUNCHER, "ref": tested_base},
        ])
        logs.assert_not_called()

    def test_stale_base_successful_run_has_no_failure_diagnostics(self):
        head_sha = self._push_and_stub_pull()
        run = self._green_run(head_sha)
        tested_base = "9" * 40
        _set_pr_base_sha(run, tested_base)
        _ActionsBackend.state["historical_base_commit"] = tested_base
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            with mock.patch.object(_ActionsBackend, "actions_job_logs") as logs:
                result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "pending")
        self.assertEqual(_dict(result["ci_evidence"])["reason_code"], "CI_BASE_STALE")
        self.assertNotIn("ci_progress", result)
        self.assertIsNone(result["failure_evidence"])
        logs.assert_not_called()

    def test_ci_status_reports_unaccepted_run_with_no_logs(self):
        """UNAVAILABLE evidence is explicit, never an empty failure record."""
        head_sha = self._push_and_stub_pull()
        self._green_run(head_sha)
        _ActionsBackend.state["runs_api_failure"] = True
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "unavailable")
        self.assertIsNone(result["failure_evidence"])
        self.assertEqual(
            _dict(result["ci_evidence"])["reason_code"], "CI_API_UNAVAILABLE"
        )

    def test_ci_status_needs_no_log_reader_guard_of_its_own(self):
        """``actions_job_logs`` is the fourth function of the protocol and
        `ci_status` calls it only for red evidence, but it never reaches that
        call on a backend without the reader: the evidence it derives first is
        UNAVAILABLE, which is already a closed refusal. The evidence reader's
        one protocol guard therefore covers both the run and log reads."""
        head_sha = self._push_and_stub_pull()
        self._fail_run(head_sha)
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _NoActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "unavailable")
        self.assertIsNone(result["failure_evidence"])

    def test_ci_status_returns_no_run_when_ci_is_not_required(self):
        repos_yaml_text = f"""\
canonical_repos:
  - name: {self.ALIAS}
    remote: {self._bare}
    writable_forge: forgejo
    deploy_path: {self._checkout}
    repository_control_level: R2
    ci_applicability: not_onboarded
"""
        gate.catalog._REPOS_YAML.write_text(repos_yaml_text)
        self._push_and_stub_pull()
        _ActionsBackend.state["runs"] = []
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._gate(self._task_wt).execute("ci-status")
        self.assertEqual(result["verdict"], "no_run")
        self.assertIsNone(result["failure_evidence"])
        self.assertEqual(_dict(result["ci_evidence"])["result"], "NOT_RUN")

    def test_legacy_ci_routes_are_removed(self):
        for operation in ("checks", "check-logs"):
            with self.subTest(operation=operation):
                with self.assertRaises(gate.GateError) as raised:
                    self._gate(self._task_wt).execute(operation)
                self.assertEqual(raised.exception.code, "USAGE")


class ExecutorStatusContextsTests(unittest.TestCase):
    """Legacy statuses posted by a review pipeline are not CI proof."""

    def test_example_review_contexts_stay_excluded_for_historical_heads(self):
        self.assertEqual(
            gate_forge.EXECUTOR_STATUS_CONTEXTS,
            {"ci/example-review/cron", "ci/example-review/manual"},
        )
        self.assertFalse((Path(__file__).resolve().parents[3] / ".example-review/review.yaml").exists())


class RefChecksRealGitTests(_RealGitEvidenceCase):
    """ref-checks: the roll-up-only projection for an explicit ref, not
    necessarily the caller's own lane's branch -- deliberately carries no
    ci_evidence/policy (no PR to anchor a trust proof to for a ref the
    caller does not own)."""

    def _execute_ref_checks(self, ref):
        stdin_payload = json.dumps({"ref": ref})
        with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
            return self._gate(self._task_wt).execute("ref-checks")

    def test_resolves_a_ref_other_than_the_callers_own_branch(self):
        """The whole point: from a lane on ``self._branch``, ask about
        ``main`` -- a ref this lane does not own -- and get a real answer."""
        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._execute_ref_checks("main")
        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "ref-checks")
        self.assertEqual(result["ref"], "main")
        self.assertTrue(result["ref_resolved"])
        self.assertEqual(result["head_sha"], self._origin_main)
        self.assertTrue(result["checks_available"])
        self.assertEqual(result["combined_state"], "success")
        self.assertFalse(result["checks_pending"])
        self.assertFalse(result["trust_verified"])
        self.assertTrue(result["advisory_only"])
        self.assertNotIn("ci_evidence", result)
        self.assertNotIn("policy", result)

    def test_a_bare_list_forge_derives_pending_from_the_rows(self):
        """Forgejo answers with a bare list and publishes no roll-up.

        ``combined_state`` is then None, and reading only
        ``combined_state == "pending"`` would render a ref whose checks were
        still running as *not* pending -- so a caller gating a deploy on
        this field would fail OPEN.  With several rows of which some are
        pending, ``checks_pending`` must be true.
        """

        class _BareListBackend(_ActionsBackend):
            @staticmethod
            def list_check_runs(runner, cwd, forge_repo, sha, page_size):
                return _Completed(
                    json.dumps(
                        [
                            {"context": "checks / c1", "status": "success"},
                            {"context": "checks / c1", "status": "pending"},
                            {"context": "checks / leaf-15", "status": "success"},
                        ]
                    )
                )

        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _BareListBackend}):
            result = self._execute_ref_checks("main")
        self.assertTrue(result["ok"])
        self.assertTrue(result["checks_available"])
        self.assertIsNone(result["combined_state"])
        self.assertEqual(result["check_runs_total"], 3)
        self.assertTrue(result["checks_pending"])

    def test_a_bare_list_of_terminal_rows_is_not_pending(self):
        """The other direction: no roll-up and every row finished means the
        ref is not pending.  An unrecognised conclusion counts as terminal
        rather than pending, so a new forge word cannot wedge the field."""

        class _TerminalBackend(_ActionsBackend):
            @staticmethod
            def list_check_runs(runner, cwd, forge_repo, sha, page_size):
                return _Completed(
                    json.dumps(
                        [
                            {"context": "checks / c1", "status": "success"},
                            {"context": "checks / leaf-15", "status": "failure"},
                            {"context": "checks / new", "status": "neutral"},
                        ]
                    )
                )

        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _TerminalBackend}):
            result = self._execute_ref_checks("main")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["combined_state"])
        self.assertFalse(result["checks_pending"])

    def test_the_queue_status_does_not_change_the_verdict(self):
        """The automerge executor keeps a ``gate/queue`` status permanently
        ``pending`` on every open PR head, and Forgejo rolls it into the
        combined ``state``. Unfiltered, a green commit would read as pending
        to a deploy gate forever; filtered, the other rows decide, and a real
        pending check still reads as pending."""

        def backend(rows):
            class _QueueStatusBackend(_ActionsBackend):
                @staticmethod
                def list_check_runs(runner, cwd, forge_repo, sha, page_size):
                    return _Completed(json.dumps({"state": "pending", "statuses": rows}))
            return _QueueStatusBackend

        queue = {"context": "gate/queue", "status": "pending",
                 "description": "queue=eligible reason=- owner=queue pos=2"}
        green = [{"context": "checks / c1 (push)", "status": "success"},
                 {"context": "checks / leaf-15", "status": "success"}]
        running = [{"context": "checks / c1 (push)", "status": "pending"}]
        spec = _iu.spec_from_file_location(
            "deploy_tool", Path(__file__).resolve().parents[3] / "tools-config" / "scripts" / "oc_deploy.py")
        deploy_tool = sys.modules.get("deploy_tool") or _iu.module_from_spec(spec)
        if "deploy_tool" not in sys.modules:
            sys.modules["deploy_tool"] = deploy_tool
            spec.loader.exec_module(deploy_tool)

        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": backend([*green, queue])}):
            result = self._execute_ref_checks("main")
        self.assertEqual([run["name"] for run in result["check_runs"]],
                         ["checks / c1 (push)", "checks / leaf-15"])
        self.assertIsNone(result["combined_state"])
        self.assertFalse(result["checks_pending"])
        # The fixture repository is not CI-gated; the classifier only
        # weighs the rows of a gated one.
        gated = {**result, "ci_applicability": "required"}
        self.assertEqual(deploy_tool.classify_ci(gated)[0], "green")
        # Control: the same answer with the queue row left in reads pending.
        unfiltered = {**gated, "combined_state": "pending", "check_runs": [
            *gated["check_runs"], {"name": "gate/queue", "conclusion": "pending"}]}
        self.assertEqual(deploy_tool.classify_ci(unfiltered)[0], "pending")

        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": backend([*running, queue])}):
            result = self._execute_ref_checks("main")
        self.assertTrue(result["checks_pending"])
        self.assertEqual(
            deploy_tool.classify_ci({**result, "ci_applicability": "required"})[0], "pending")

        # A PR that left the queue carries a final `success` row. With no
        # other row, the roll-up it caused must not read as green CI.
        done = {"context": "gate/queue", "status": "success",
                "description": "queue=done reason=- owner=- pos=-"}

        def success_backend(rows):
            class _DoneStatusBackend(_ActionsBackend):
                @staticmethod
                def list_check_runs(runner, cwd, forge_repo, sha, page_size):
                    return _Completed(json.dumps({"state": "success", "statuses": rows}))
            return _DoneStatusBackend

        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": success_backend([done])}):
            result = self._execute_ref_checks("main")
        self.assertEqual(result["check_runs"], [])
        self.assertIsNone(result["combined_state"])
        with mock.patch.dict(gate._FORGE_BACKENDS,
                             {"forgejo": success_backend([*green, done])}):
            result = self._execute_ref_checks("main")
        self.assertEqual(result["combined_state"], "success")
        self.assertEqual(
            deploy_tool.classify_ci({**result, "ci_applicability": "required"})[0], "green")

    def test_the_executor_pipeline_status_does_not_change_the_verdict(self):
        """A review pipeline posted its own ``ci/example-review/cron``
        status on the ``main`` head for the automerge pass. It is ``pending`` while a
        model review runs and ``failure`` after a failed pass; neither is a
        check of the commit. Another context of that system still counts."""

        def backend(state, rows):
            class _ExecutorStatusBackend(_ActionsBackend):
                @staticmethod
                def list_check_runs(runner, cwd, forge_repo, sha, page_size):
                    return _Completed(json.dumps({"state": state, "statuses": rows}))
            return _ExecutorStatusBackend

        green = {"context": "checks / c1 (push)", "status": "success"}
        spec = _iu.spec_from_file_location(
            "deploy_tool", Path(__file__).resolve().parents[3] / "tools-config" / "scripts" / "oc_deploy.py")
        deploy_tool = sys.modules.get("deploy_tool") or _iu.module_from_spec(spec)
        if "deploy_tool" not in sys.modules:
            sys.modules["deploy_tool"] = deploy_tool
            spec.loader.exec_module(deploy_tool)

        def verdict(state, rows):
            with mock.patch.dict(gate._FORGE_BACKENDS,
                                 {"forgejo": backend(state, rows)}):
                result = self._execute_ref_checks("main")
            return result, deploy_tool.classify_ci({**result, "ci_applicability": "required"})[0]

        self._push_and_stub_pull()
        for context in sorted(gate_forge.EXECUTOR_STATUS_CONTEXTS):
            with self.subTest(context=context, status="pending"):
                result, classified = verdict(
                    "pending", [green, {"context": context, "status": "pending"}])
                self.assertEqual([run["name"] for run in result["check_runs"]],
                                 ["checks / c1 (push)"])
                self.assertIsNone(result["combined_state"])
                self.assertFalse(result["checks_pending"])
                self.assertEqual(classified, "green")
            with self.subTest(context=context, status="failure"):
                result, classified = verdict(
                    "failure", [green, {"context": context, "status": "failure"}])
                self.assertEqual([run["name"] for run in result["check_runs"]],
                                 ["checks / c1 (push)"])
                self.assertIsNone(result["combined_state"])
                self.assertEqual(classified, "green")

        executor = {"context": "ci/example-review/cron", "status": "failure"}
        # Controls: a genuine failing or pending context is still reported,
        # including another pipeline of the same system, so the match is exact.
        other = {"context": "ci/example-review/push", "status": "failure"}
        result, classified = verdict("failure", [green, executor, other])
        self.assertEqual([run["name"] for run in result["check_runs"]],
                         ["checks / c1 (push)", "ci/example-review/push"])
        self.assertEqual(classified, "red")
        failing = {"context": "checks / c1 (push)", "status": "failure"}
        result, classified = verdict("failure", [failing, executor])
        self.assertEqual(classified, "red")
        running = {"context": "ci/example-review/push", "status": "pending"}
        result, classified = verdict(
            "pending", [green, {**executor, "status": "pending"}, running])
        self.assertTrue(result["checks_pending"])
        self.assertEqual(classified, "pending")
        # A full page may have cut off an older failing context the forge
        # still rolled up, so the roll-up stands instead of the rows.
        page = [executor] + [
            {"context": f"checks / c{n} (push)", "status": "success"}
            for n in range(gate_forge.CHECK_RUNS_MAX - 1)]
        result, classified = verdict("failure", page)
        self.assertEqual(result["combined_state"], "failure")
        self.assertEqual(classified, "red")

    def test_the_identity_job_is_read_past_twenty_contexts(self):
        """`main` can carry 20 or more status contexts, a full page at
        a window of 20. A deploy gate requires `c1` by name, so
        a window that cuts it off would refuse every deploy as unknown."""

        rows = [{"context": f"checks / job-{n} (push)", "status": "success"}
                for n in range(24)]
        rows.append({"context": "checks / c1 (push)", "status": "success"})

        class _WideBackend(_ActionsBackend):
            @staticmethod
            def list_check_runs(runner, cwd, forge_repo, sha, page_size):
                return _Completed(json.dumps(
                    {"state": "success", "statuses": rows[:page_size]}))

        spec = _iu.spec_from_file_location(
            "deploy_tool", Path(__file__).resolve().parents[3] / "tools-config" / "scripts" / "oc_deploy.py")
        deploy_tool = sys.modules.get("deploy_tool") or _iu.module_from_spec(spec)
        if "deploy_tool" not in sys.modules:
            sys.modules["deploy_tool"] = deploy_tool
            spec.loader.exec_module(deploy_tool)

        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _WideBackend}):
            result = self._execute_ref_checks("main")
        self.assertIn("checks / c1 (push)", [run["name"] for run in result["check_runs"]])
        self.assertEqual(
            deploy_tool.classify_ci({**result, "ci_applicability": "required"})[0], "green")

    def test_an_exact_sha_is_used_directly(self):
        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._execute_ref_checks(self._origin_main)
        self.assertTrue(result["ok"])
        self.assertTrue(result["ref_resolved"])
        self.assertEqual(result["head_sha"], self._origin_main)

    def test_an_unresolvable_branch_name_is_reported_not_raised(self):
        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._execute_ref_checks("no-such-branch-anywhere")
        self.assertTrue(result["ok"])
        self.assertFalse(result["ref_resolved"])
        self.assertIsNone(result["head_sha"])
        self.assertFalse(result["checks_available"])
        self.assertEqual(result["check_runs"], [])

    def test_empty_ref_fails_closed(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"ref": "   "}))):
            with self.assertRaises(gate.GateError) as raised:
                self._gate(self._task_wt).execute("ref-checks")
        self.assertEqual(raised.exception.code, "REF_CHECKS_REF_REQUIRED")

    def test_malformed_stdin_fails_closed(self):
        with mock.patch("sys.stdin", io.StringIO("not json")):
            with self.assertRaises(gate.GateError) as raised:
                self._gate(self._task_wt).execute("ref-checks")
        self.assertEqual(raised.exception.code, "REF_CHECKS_STDIN_INVALID")

    def test_oversized_stdin_fails_closed_before_it_is_parsed(self):
        """The ref-checks stdin ceiling, at the boundary that enforces it.

        Sibling of ``test_malformed_stdin_fails_closed`` above: both refuse
        before the payload means anything, one for shape and one for size.
        The ceiling exists so an unbounded read cannot be used to push the
        gate into an allocation it never budgeted for, which is why it is
        checked against the raw bytes rather than the parsed object.

        The size is derived from the gate's own constant, not restated: a
        literal here would keep passing if the ceiling moved, and would be
        testing this file's memory of it rather than the contract.
        """
        oversized = "x" * (gate.REF_CHECKS_INPUT_MAX_BYTES + 1)
        payload = json.dumps({"ref": self._origin_main, "padding": oversized})
        self.assertGreater(
            len(payload.encode("utf-8")), gate.REF_CHECKS_INPUT_MAX_BYTES,
            "the fixture must actually exceed the ceiling, or this test "
            "asserts nothing",
        )
        with mock.patch("sys.stdin", io.StringIO(payload)):
            with self.assertRaises(gate.GateError) as raised:
                self._gate(self._task_wt).execute("ref-checks")
        self.assertEqual(raised.exception.code, "REF_CHECKS_STDIN_TOO_LARGE")

    def test_stdin_at_the_ceiling_is_still_accepted(self):
        """CONTROL. The ceiling is a bound, not an off-by-one: a payload
        that exactly fits must get past the size check and be judged on its
        content, so the refusal above is about size and not about padding.
        """
        base = json.dumps({"ref": self._origin_main, "padding": ""})
        room = gate.REF_CHECKS_INPUT_MAX_BYTES - len(base.encode("utf-8"))
        self.assertGreater(room, 0, "no room to pad up to the ceiling")
        payload = json.dumps({"ref": self._origin_main, "padding": "x" * room})
        self.assertLessEqual(
            len(payload.encode("utf-8")), gate.REF_CHECKS_INPUT_MAX_BYTES
        )
        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
                result = self._gate(self._task_wt).execute("ref-checks")
        self.assertTrue(result["ok"])

    def test_a_ref_starting_with_a_dash_fails_closed(self):
        """Defense in depth: never let a ref value be mistaken for a git
        flag, even though the actual git invocation never uses shell=True
        and always passes an explicit argv list."""
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"ref": "--upload-pack=x"}))):
            with self.assertRaises(gate.GateError) as raised:
                self._gate(self._task_wt).execute("ref-checks")
        self.assertEqual(raised.exception.code, "REF_CHECKS_REF_INVALID")

    def test_an_alias_reads_the_ref_without_any_lane(self):
        """The alias form, for a caller that owns no lane at all.

        The deploy chain runs from the managed checkout, so the lane-scoped
        form is unreachable to it -- and the commit it is about to advance
        onto is exactly the thing it most needs a verdict on. ``cwd`` here is
        the managed checkout, which ``context()`` rejects by construction
        (not a registered task worktree, and on ``main``), so a pass proves
        the answer came from the alias rather than from an accidental lane.
        """

        self._push_and_stub_pull()
        stdin_payload = json.dumps({"ref": "main"})
        with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
            with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
                result = self._gate(self._checkout).execute("ref-checks", self.ALIAS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "ref-checks")
        self.assertEqual(result["head_sha"], self._origin_main)
        self.assertTrue(result["checks_available"])
        # Still the smaller claim: the alias form widens who may ASK, never
        # what the answer is worth.
        self.assertFalse(result["trust_verified"])
        self.assertTrue(result["advisory_only"])

    def test_the_lane_form_still_refuses_a_cwd_that_owns_no_lane(self):
        """Widening the alias form must not widen the lane form with it."""

        with mock.patch("sys.stdin", io.StringIO(json.dumps({"ref": "main"}))):
            with self.assertRaises(gate.GateError) as raised:
                self._gate(self._checkout).execute("ref-checks")
        self.assertEqual(raised.exception.code, "WORKTREE_CONTEXT_MISMATCH")

    def test_an_unknown_alias_is_still_a_usage_error(self):
        with self.assertRaises(gate.GateError) as raised:
            self._gate(self._task_wt).execute("ref-checks", "not-a-closed-alias")
        self.assertEqual(raised.exception.code, "USAGE")

    def test_ci_applicability_is_reported_so_a_caller_can_weigh_the_answer(self):
        """A verdict from a repository that publishes no checks means
        something different from one that publishes them and passed. A
        caller that cannot tell the two apart either blocks forever on the
        first or trusts an empty answer -- so the governance field travels
        with the roll-up."""

        self._push_and_stub_pull()
        with mock.patch.dict(gate._FORGE_BACKENDS, {"forgejo": _ActionsBackend}):
            result = self._execute_ref_checks("main")
        self.assertIn("ci_applicability", result)
        self.assertEqual(result["ci_applicability"], self._policy.ci_applicability)


if __name__ == "__main__":
    unittest.main()
