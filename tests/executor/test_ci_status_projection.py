"""ci-status.v2: the one lossless CI verdict every consumer reads.

Consumers must not each map ci-evidence.v1 onto "is CI green" for
themselves: separate mappings drift, and a consumer that reads ``NOT_RUN`` as
"the run has not started yet" (it means *CI is not required for this lane*)
waits for a run nobody will ever schedule. This file pins the one mapping,
and the composition that attaches a red run's reasons to its verdict.

Everything here runs without git, without a forge and without a subprocess:
``derive_verdict`` and ``build_ci_status`` are pure over an
already-derived record, and the operation tests drive the two methods
directly with the evidence step stubbed.
"""

from __future__ import annotations

import importlib.util as _iu
import json
import sys
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools-config"))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tools-config" / "scripts")
)

SCRIPTS = Path(__file__).resolve().parents[3] / "tools-config" / "scripts"
GATE_PATH = SCRIPTS / "git_maintainer_gate.py"
_spec = _iu.spec_from_file_location("gate_ci_status_projection", GATE_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load the gate module")
gate = _iu.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

from git_gate import ci  # noqa: E402 -- after the by-path shim load, as in sibling gate tests
from git_gate import forge as gate_forge  # noqa: E402 -- the same module instance the shim uses
from git_gate import kb_docs_enforce as kb_enforce  # noqa: E402 -- owns the decision vocabulary
from git_gate.contracts import CI_STATUS, GateError  # noqa: E402


def evidence(**overrides: object) -> dict[str, object]:
    """A ci-evidence.v1-shaped record with only the fields the mapping reads."""

    record: dict[str, object] = {
        "schema": "ci-evidence.v1",
        "repository": "acme/core-repo",
        "pull_request": 14,
        "head_sha": "a" * 40,
        "required_profile": "c1",
        "result": "PASS",
        "reason_code": None,
        "retry_after_seconds": None,
        "target_url": "https://forge.example.org/run/7",
        "workflow_run_id": 7,
        "observed_at": "2020-01-01T10:00:00+00:00",
    }
    record.update(overrides)
    # The reason code is checked against the producer's own closed set rather
    # than trusted as a string. A row spelled "CI_NOT_REQUIRED" -- a spelling
    # no code path emits and no set contains -- would pin the NOT_RUN mapping
    # by a name that could never arrive, and the test would keep passing had
    # the real route broken. Checking here rather than in one dedicated test
    # closes the class: every call in this file is covered, including the
    # ones written after this comment.
    reason = record.get("reason_code")
    if reason is not None and reason not in ci.CI_REASON_CODES:
        raise AssertionError(
            f"reason_code {reason!r} is not in CI_REASON_CODES; "
            "this test would pin a shape the producer never emits"
        )
    return record


class DeriveVerdictTests(unittest.TestCase):
    """Every row of the evidence-to-verdict mapping."""

    def test_pass_is_green_and_names_the_required_profile(self) -> None:
        verdict, headline = ci.derive_verdict(evidence())
        self.assertEqual(verdict, "green")
        self.assertEqual(headline, "CI: green (c1)")

    def test_fail_is_red(self) -> None:
        verdict, headline = ci.derive_verdict(evidence(result="FAIL"))
        self.assertEqual(verdict, "red")
        self.assertIn("RED", headline)

    def test_not_run_means_not_required_never_pending(self) -> None:
        """The drift this projection exists to remove.

        ``NOT_RUN`` is CI *not required for this lane*. A consumer that reads
        it as "no run yet" polls for a run that will never be scheduled, so
        this row is pinned by name rather than by shape.
        """

        verdict, headline = ci.derive_verdict(
            evidence(result="NOT_RUN", reason_code="CI_NOT_ONBOARDED")
        )
        self.assertEqual(verdict, "no_run")
        self.assertNotEqual(verdict, "pending")
        self.assertIn("not required", headline)

    def test_unavailable_with_a_retry_hint_is_pending(self) -> None:
        """The hint decides the verdict; it is not advice to repeat in prose.

        ``retry_after_seconds`` rides in the record for any caller that needs
        it, and it is the sole discriminator between ``pending`` and
        ``unavailable``. It is not rendered into the headline: a headline
        ending "re-poll in 60s" would be an instruction to a machine printed
        into human-facing text, and it would instruct exactly the repeated
        polling that a caller should never be told to do.
        """

        record = evidence(
            result="UNAVAILABLE",
            reason_code="CI_RUN_QUEUED",
            retry_after_seconds=60,
        )
        verdict, headline = ci.derive_verdict(record)
        self.assertEqual(verdict, "pending")
        self.assertIn("CI_RUN_QUEUED", headline)
        self.assertNotIn("re-poll", headline)
        self.assertNotIn("60s", headline)
        # The hint itself is untouched — only how it is worded changed.
        self.assertEqual(record["retry_after_seconds"], 60)

    def test_missing_run_with_retry_hint_is_starting(self) -> None:
        """A pushed PR head can precede its Forgejo run-list row."""

        verdict, headline = ci.derive_verdict(
            evidence(
                result="UNAVAILABLE",
                reason_code="CI_RUN_MISSING",
                retry_after_seconds=30,
            )
        )
        self.assertEqual(verdict, "pending")
        self.assertIn("waiting for exact-head run", headline)

    def test_unavailable_without_a_retry_hint_is_terminal(self) -> None:
        """A null hint means asking again will not produce an answer."""

        verdict, headline = ci.derive_verdict(
            evidence(result="UNAVAILABLE", reason_code="CI_API_UNAVAILABLE")
        )
        self.assertEqual(verdict, "unavailable")
        self.assertIn("CI_API_UNAVAILABLE", headline)

    def test_no_pull_request_wins_over_every_other_field(self) -> None:
        """Before the first publish, and after a merge, there is no subject."""

        verdict, headline = ci.derive_verdict(
            evidence(pull_request=None, result="PASS")
        )
        self.assertEqual(verdict, "no_pr")
        self.assertIn("no open pull request", headline)

    def test_missing_run_without_a_pull_request_never_starts_polling(self) -> None:
        verdict, headline = ci.derive_verdict(
            evidence(
                pull_request=None,
                result="UNAVAILABLE",
                reason_code="CI_RUN_MISSING",
                retry_after_seconds=30,
            )
        )
        self.assertEqual(verdict, "no_pr")
        self.assertIn("no open pull request", headline)

    def test_an_unrecognised_result_is_never_read_as_benign(self) -> None:
        verdict, _headline = ci.derive_verdict(evidence(result="SOMETHING_NEW"))
        self.assertEqual(verdict, "unavailable")

    def test_every_verdict_is_in_the_closed_vocabulary(self) -> None:
        results = ["PASS", "FAIL", "NOT_RUN", "UNAVAILABLE", "?"]
        seen = {ci.derive_verdict(evidence(result=r))[0] for r in results}
        seen.add(ci.derive_verdict(evidence(pull_request=None))[0])
        self.assertTrue(seen <= ci.CI_VERDICTS, seen - ci.CI_VERDICTS)


class BuildCiStatusTests(unittest.TestCase):
    def test_red_carries_the_failed_jobs_unchanged(self) -> None:
        failure = {
            "schema": "ci-failure.v1",
            "failed_jobs": [{"job_id": 3, "job_name": "checks", "failed_steps": []}],
            "bytes_returned": 20000,
            "truncated": True,
            "redaction_count": 2,
        }
        record = ci.build_ci_status(
            evidence=evidence(result="FAIL"),
            failure=failure,
            verdict="red",
            headline="CI: RED (c1)",
        )
        self.assertTrue(record["actionable"])
        self.assertEqual(record["schema"], "ci-status.v2")
        self.assertEqual(record["ci_evidence"], evidence(result="FAIL"))
        self.assertEqual(record["failure_evidence"], failure)
        self.assertNotIn("failures", record)
        self.assertNotIn("failures_truncated", record)

    def test_green_keeps_accepted_evidence_without_failure_envelope(self) -> None:
        accepted = evidence()
        record = ci.build_ci_status(
            evidence=accepted, failure=None, verdict="green", headline="CI: green"
        )
        self.assertEqual(record["ci_evidence"], accepted)
        self.assertIsNone(record["failure_evidence"])

    def test_only_red_is_actionable(self) -> None:
        for verdict in sorted(ci.CI_VERDICTS):
            record = ci.build_ci_status(
                evidence=evidence(), failure=None, verdict=verdict, headline="x"
            )
            self.assertEqual(
                record["actionable"], verdict == "red", f"verdict={verdict}"
            )
            self.assertIsNone(record["failure_evidence"])


class CiStatusOperationTests(unittest.TestCase):
    """The operation composes; it does not re-derive."""

    def _context(self) -> SimpleNamespace:
        return SimpleNamespace(
            cwd=Path("/work/core-repo-worktrees/lane"),
            branch="task/example",
            policy=SimpleNamespace(forge_repository="acme/core-repo"),
        )

    def _call(self, gate_self: SimpleNamespace, **options: bool) -> dict[str, object]:
        # The claim read is stubbed away here and pinned for real in
        # `DocsFollowupTests` below. Every test in this class is about the
        # verdict composition, and a stub is only safe because exactly one
        # test owns the real method's contract.
        if not hasattr(gate_self, "_lane_knowledge_followup"):
            gate_self._lane_knowledge_followup = lambda *_a, **_k: None
        with mock.patch.object(gate_forge, "lane_ref_for", return_value=None):
            return gate_forge.GateForgeMixin.ci_status(gate_self, self._context(), **options)

    def test_a_green_run_never_fetches_logs(self) -> None:
        failure_evidence = mock.Mock()
        record = self._call(
            SimpleNamespace(
                _policy_evidence_run=lambda _ctx, **_kw: (None, evidence(), None, None),
                _failure_evidence=failure_evidence,
            )
        )
        self.assertEqual(record["verdict"], "green")
        self.assertTrue(record["ok"])
        self.assertEqual(record["operation"], CI_STATUS)
        failure_evidence.assert_not_called()

    def test_the_default_status_read_does_not_ask_for_the_failure_proof(self) -> None:
        """The merge step calls ``ci_status`` for its verdict, so the default
        spends no read on the advisory failure proof; merge, train, rerun and
        the status projection all leave it off."""
        evidence_run = mock.Mock(return_value=(None, evidence(), None, None))
        self._call(SimpleNamespace(_policy_evidence_run=evidence_run, _failure_evidence=mock.Mock()))
        evidence_run.assert_called_once()
        self.assertEqual(evidence_run.call_args.kwargs, {"classify_failure": False})

    def test_an_explicit_request_asks_for_the_advisory_failure_proof(self) -> None:
        evidence_run = mock.Mock(return_value=(None, evidence(), None, None))
        self._call(SimpleNamespace(_policy_evidence_run=evidence_run, _failure_evidence=mock.Mock()),
                   classify_failure=True)
        evidence_run.assert_called_once()
        self.assertEqual(evidence_run.call_args.kwargs, {"classify_failure": True})

    def test_a_materializing_exact_head_is_projected_as_pending(self) -> None:
        record = self._call(
            SimpleNamespace(
                _policy_evidence_run=lambda _ctx, **_kw: (
                    None,
                    evidence(
                        result="UNAVAILABLE",
                        reason_code="CI_RUN_MISSING",
                        retry_after_seconds=30,
                    ),
                    None,
                    None,
                ),
                _failure_evidence=mock.Mock(),
                _ci_progress=lambda *_a, **_k: None,
            )
        )
        self.assertEqual(record["verdict"], "pending")
        self.assertEqual(record["ci_evidence"]["retry_after_seconds"], 30)
        self.assertIn("waiting for exact-head run", str(record["headline"]))

    def test_a_red_run_arrives_with_its_reasons(self) -> None:
        """The whole point: one call answers "is it red" and "why"."""

        failed_jobs = [{"job_id": 9, "job_name": "component pytest", "failed_steps": []}]
        record = self._call(
            SimpleNamespace(
                _policy_evidence_run=lambda _ctx, **_kw: (
                    None,
                    evidence(result="FAIL"),
                    {"id": 9},
                    {"jobs": []},
                ),
                _failure_evidence=lambda *_a, **_k: {
                    "schema": "ci-failure.v1",
                    "failed_jobs": failed_jobs,
                    "truncated": False,
                    "bytes_returned": 80,
                    "redaction_count": 0,
                },
            )
        )
        self.assertEqual(record["verdict"], "red")
        self.assertTrue(record["actionable"])
        self.assertEqual(record["failure_evidence"]["failed_jobs"], failed_jobs)

    def _forge_unreachable(self, code: str = "REMOTE_HEAD_FAILED") -> SimpleNamespace:
        """A stand-in whose evidence read raises, with the REAL fallback bound.

        These tests are about two things at once -- that ``ci_status`` routes
        a credential-free failure to the fallback rather than propagating it,
        and what the fallback then reports -- so the fallback is the actual
        method rather than another stub.
        """

        gate_self = SimpleNamespace(
            _policy_evidence_run=mock.Mock(side_effect=GateError(code))
        )
        gate_self._ci_status_without_forge = partial(
            gate_forge.GateForgeMixin._ci_status_without_forge, gate_self
        )
        return gate_self

    def test_forge_loss_never_infers_no_pull_request_from_a_missing_ref(self) -> None:
        """Gate pushes do not create tracking refs, so absence proves nothing."""

        gate_self = self._forge_unreachable()
        with mock.patch.object(
            gate_forge,
            "run_command",
            return_value=SimpleNamespace(returncode=1),
            create=True,
        ) as local_git:
            record = self._call(gate_self)
        self.assertEqual(record["verdict"], "unavailable")
        self.assertEqual(
            record["ci_evidence"]["reason_code"], "REMOTE_HEAD_FAILED"
        )
        self.assertTrue(record["forge_unreachable"])
        local_git.assert_not_called()
        # Nothing was read, so nothing is claimed.
        self.assertNotIn("result", record["ci_evidence"])

    def test_forge_loss_does_not_promote_a_present_local_ref_either(self) -> None:
        gate_self = self._forge_unreachable()
        with mock.patch.object(
            gate_forge,
            "run_command",
            return_value=SimpleNamespace(returncode=0),
            create=True,
        ) as local_git:
            record = self._call(gate_self)
        self.assertEqual(record["verdict"], "unavailable")
        self.assertIn("cannot reach the forge", str(record["headline"]))
        local_git.assert_not_called()

    def test_an_unrelated_gate_failure_still_raises(self) -> None:
        """The fallback is for the credential-free read, not a catch-all."""

        gate_self = self._forge_unreachable("HEAD_INVALID")
        with self.assertRaises(GateError) as caught:
            self._call(gate_self)
        self.assertEqual(caught.exception.code, "HEAD_INVALID")


class CiStatusByPullRequestTests(unittest.TestCase):
    """The CI status read for one pull request by alias and number: the lane verdict for one open PR, no lane.

    Head and base come from the forge's PR record; the evidence call is the
    one workflow-status read makes, so the two reads cannot disagree.
    """

    HEAD = "c" * 40

    def _gate(self, *, state: str = "open", base: str = "main",
              evidence_record: dict[str, object] | None = None) -> SimpleNamespace:
        pull = SimpleNamespace(index=7, state=state, base=base, base_commit="b" * 40,
                               head="task/example")
        gate_self = SimpleNamespace(
            pull_by_index=mock.Mock(return_value=pull),
            _read_pr_state=mock.Mock(return_value={"head": {"sha": self.HEAD.upper()}}),
            _range_required_profile=mock.Mock(return_value="c2"),
            _policy_evidence_run=mock.Mock(
                return_value=(None, evidence_record or evidence(), None, None)),
            _failure_evidence=mock.Mock(),
            _ci_progress=mock.Mock(return_value=None),
            _lane_knowledge_followup=mock.Mock(),
        )
        gate_self.pull = pull
        return gate_self

    def _call(self, gate_self: SimpleNamespace, **options: bool) -> dict[str, object]:
        context = gate_forge.WorktreeContext(
            cwd=Path("/work/core-repo"), top_level=Path("/work/core-repo"), branch="main",
            origin="origin", policy=SimpleNamespace(forge_repository="acme/core-repo"),
            registered_worktrees=frozenset(),
        )
        with mock.patch.object(gate_forge.catalog, "queue_owns_integration", return_value=True):
            return gate_forge.GateForgeMixin.ci_status_by_index(gate_self, context, 7, **options)

    def test_the_prs_remote_head_and_base_are_read_without_a_lane(self) -> None:
        gate_self = self._gate()
        record = self._call(gate_self)

        self.assertEqual((record["verdict"], record["ok"], record["operation"]),
                         ("green", True, CI_STATUS))
        self.assertEqual(record["integration_owner"], "queue")
        self.assertIsNone(record["knowledge_followup"])
        self.assertNotIn("lane_ref", record)
        context, base, head = gate_self._range_required_profile.call_args.args
        self.assertEqual((context.branch, base, head), ("task/example", "b" * 40, self.HEAD))
        args, kwargs = gate_self._policy_evidence_run.call_args
        self.assertEqual(args[0].branch, "task/example")
        self.assertEqual(kwargs, {"pull_override": gate_self.pull, "head_override": self.HEAD,
                                  "required_profile_override": "c2", "classify_failure": False})
        gate_self._lane_knowledge_followup.assert_not_called()
        gate_self._failure_evidence.assert_not_called()

    def test_an_explicit_request_asks_for_the_advisory_failure_proof(self) -> None:
        gate_self = self._gate()
        self._call(gate_self, classify_failure=True)
        self.assertIs(gate_self._policy_evidence_run.call_args.kwargs["classify_failure"], True)

    def test_a_red_pr_arrives_with_its_reasons(self) -> None:
        gate_self = self._gate(evidence_record=evidence(result="FAIL"))
        gate_self._failure_evidence.return_value = {
            "schema": "ci-failure.v1", "failed_jobs": [{"job_id": 9}], "truncated": False,
            "bytes_returned": 10, "redaction_count": 0}
        record = self._call(gate_self)
        self.assertEqual(record["verdict"], "red")
        self.assertEqual(record["failure_evidence"]["failed_jobs"], [{"job_id": 9}])

    def test_a_pr_that_is_not_open_on_main_is_refused(self) -> None:
        for gate_self in (self._gate(state="closed"), self._gate(base="release")):
            with self.assertRaises(GateError) as caught:
                self._call(gate_self)
            self.assertEqual(caught.exception.code, "PR_EXACT_MATCH_REQUIRED")
            gate_self._policy_evidence_run.assert_not_called()

    def test_a_malformed_head_is_refused_before_any_evidence_read(self) -> None:
        gate_self = self._gate()
        gate_self._read_pr_state.return_value = {"head": {"sha": "not-a-sha"}}
        with self.assertRaises(GateError) as caught:
            self._call(gate_self)
        self.assertEqual(caught.exception.code, "REMOTE_HEAD_INVALID")
        gate_self._policy_evidence_run.assert_not_called()


class AwaitingCandidateRunTests(unittest.TestCase):
    """a narrowed green run on a queue-owned head awaits the queue.

    In a `label-v1` repository every non-candidate head runs the narrowed
    profile, so a green `checks/c2` run on a head that needs `checks/c1` is the
    normal first stage: the queue reviews the head, labels it `gate/candidate`
    and runs the required profile on the candidate. The record keeps verdict
    `unavailable` and reason `CI_PROFILE_INSUFFICIENT`, because the queue
    executor keys on both; `awaiting_candidate_run` is the one added fact.
    Both entry points go through the same code, so every test runs both.
    """

    FORMS = ("lane", "pull_request")

    @staticmethod
    def narrowed_run(**overrides: object) -> dict[str, object]:
        fields: dict[str, object] = {
            "result": "UNAVAILABLE",
            "reason_code": "CI_PROFILE_INSUFFICIENT",
            "required_profile": "checks/c1",
            "accepted_profile": "checks/c2",
            "observed_profiles": ["checks/c2"],
            "workflow_run_conclusion": "success",
        }
        fields.update(overrides)
        return evidence(**fields)

    @staticmethod
    def policy(**overrides: object) -> SimpleNamespace:
        fields: dict[str, object] = {
            "forge_repository": "acme/core-repo",
            "ci_applicability": "required",
            "automatic_review_workflow": "automerge-review-v1",
            "merge_candidate_policy": "label-v1",
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    @staticmethod
    def read(form: str, run_evidence: dict[str, object], policy: SimpleNamespace) -> dict[str, object]:
        """One ci-status read through the lane form or the pull-request form."""
        gate_self = SimpleNamespace(
            _policy_evidence_run=mock.Mock(return_value=(None, run_evidence, None, None)),
            _failure_evidence=mock.Mock(),
            _ci_progress=mock.Mock(return_value=None),
            _lane_knowledge_followup=mock.Mock(return_value=None),
        )
        if form == "lane":
            context = SimpleNamespace(
                cwd=Path("/work/core-repo-worktrees/lane"), branch="task/example", policy=policy
            )
            with mock.patch.object(gate_forge, "lane_ref_for", return_value=None):
                return gate_forge.GateForgeMixin.ci_status(gate_self, context)
        gate_self.pull_by_index = mock.Mock(return_value=SimpleNamespace(
            index=7, state="open", base="main", base_commit="b" * 40, head="task/example"))
        gate_self._read_pr_state = mock.Mock(return_value={"head": {"sha": "c" * 40}})
        gate_self._range_required_profile = mock.Mock(return_value="c1")
        context = gate_forge.WorktreeContext(
            cwd=Path("/work/core-repo"), top_level=Path("/work/core-repo"), branch="main",
            origin="origin", policy=policy, registered_worktrees=frozenset(),
        )
        return gate_forge.GateForgeMixin.ci_status_by_index(gate_self, context, 7)

    def test_a_green_narrowed_run_on_a_queue_owned_head_awaits_the_candidate_run(self) -> None:
        for form in self.FORMS:
            with self.subTest(form=form):
                record = self.read(form, self.narrowed_run(), self.policy())
                self.assertEqual(record["verdict"], "unavailable")
                self.assertEqual(record["ci_evidence"]["reason_code"], "CI_PROFILE_INSUFFICIENT")
                self.assertIs(record["awaiting_candidate_run"], True)
                self.assertEqual(record["integration_owner"], "queue")
                self.assertIn("waiting for the queue", str(record["headline"]))
                self.assertIn("CI_PROFILE_INSUFFICIENT", str(record["headline"]))
                self.assertFalse(record["actionable"])

    def test_every_other_state_keeps_the_record_it_had(self) -> None:
        """Not queue-owned, no candidate policy, a red or full run: no wait.

        Each case must read exactly as `ci.derive_verdict` says, with no added
        field, so the gate cannot start promising a queue that is not coming.
        """
        cases = {
            "the queue does not own integration": (
                self.narrowed_run(), self.policy(automatic_review_workflow="")),
            "the queue does not promote a candidate": (
                self.narrowed_run(), self.policy(merge_candidate_policy="")),
            "the narrowed run failed": (
                self.narrowed_run(workflow_run_conclusion="failure"), self.policy()),
            "the accepted run is the narrowed one but not alone": (
                self.narrowed_run(observed_profiles=["checks/c2", "checks/c3"]), self.policy()),
            "the full profile passed": (
                evidence(required_profile="checks/c1", accepted_profile="checks/c1",
                         observed_profiles=["checks/c1"]), self.policy()),
            "the lane has no pull request": (
                self.narrowed_run(pull_request=None), self.policy()),
            "no run exists yet": (
                evidence(result="UNAVAILABLE", reason_code="CI_RUN_MISSING",
                         retry_after_seconds=30), self.policy()),
            "the forge answer is untrusted": (
                evidence(result="UNAVAILABLE", reason_code="CI_API_UNAVAILABLE"), self.policy()),
        }
        for form in self.FORMS:
            for name, (run_evidence, policy) in cases.items():
                with self.subTest(form=form, case=name):
                    record = self.read(form, run_evidence, policy)
                    self.assertNotIn("awaiting_candidate_run", record)
                    self.assertEqual(
                        (record["verdict"], record["headline"]), ci.derive_verdict(run_evidence)
                    )

    def test_a_not_queue_owned_narrowed_run_is_still_the_plain_unavailable_stop(self) -> None:
        for form in self.FORMS:
            with self.subTest(form=form):
                record = self.read(
                    form, self.narrowed_run(), self.policy(ci_applicability="advisory"))
                self.assertEqual(record["headline"], "CI: unavailable (CI_PROFILE_INSUFFICIENT)")
                self.assertNotIn("integration_owner", record)


class CiStatusDispatchTests(unittest.TestCase):
    """The lane form is unchanged; the alias form needs a positive PR number."""

    def _execute(self, alias, slug):
        gate_self = mock.Mock(spec=["ci_status", "ci_status_by_index", "context",
                                    "managed_checkout_context"])
        result = gate.operations.GitMaintainerGate.execute(gate_self, CI_STATUS, alias, slug)
        return gate_self, result

    def test_the_lane_form_reads_the_lane(self) -> None:
        """The public read is the one caller that asks for the advisory failure proof."""
        gate_self, result = self._execute(None, None)
        gate_self.ci_status.assert_called_once_with(
            gate_self.context.return_value, classify_failure=True)
        gate_self.ci_status_by_index.assert_not_called()
        self.assertIs(result, gate_self.ci_status.return_value)

    def test_the_alias_form_reads_the_managed_checkout(self) -> None:
        gate_self, result = self._execute("core-repo", "7")
        gate_self.managed_checkout_context.assert_called_once_with("core-repo")
        gate_self.ci_status_by_index.assert_called_once_with(
            gate_self.managed_checkout_context.return_value, 7, classify_failure=True)
        gate_self.context.assert_not_called()
        self.assertIs(result, gate_self.ci_status_by_index.return_value)

    def test_a_malformed_alias_form_is_usage(self) -> None:
        for alias, slug in (("core-repo", None), ("core-repo", "0"), ("core-repo", "x"),
                            ("not-a-repository", "7")):
            with self.assertRaises(GateError) as caught:
                self._execute(alias, slug)
            self.assertEqual(caught.exception.code, "USAGE", (alias, slug))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DocsFollowupTests(unittest.TestCase):
    """the envelope carries the promise the lane head made.

    THE CONTRACT-OWNING TEST for `_lane_knowledge_followup`. Every other test
    in this file stubs it, so this is the one place its real behaviour is
    pinned -- including that it reuses the claim reader rather than parsing
    a commit body a second way.
    """

    # The deferred decision names come from `ci` itself, so the data cannot
    # drift from the vocabulary the reader matches.
    DEFERRED = ci.DEFERRED_KNOWLEDGE_DECISIONS[0]
    IMPACT_DEFERRED = json.dumps(
        {"decision": DEFERRED, "followup_ticket": "T-1", "reason": "deferred"}
    )
    IMPACT_NOT_DEFERRED = json.dumps(
        {"decision": "docs_in_branch", "reason": "written here"}
    )
    # Exactly what a claim written before the field looks like: no key at all.
    IMPACT_LEGACY_DEFERRED = json.dumps(
        {"decision": DEFERRED, "reason": "deferred"}
    )

    @staticmethod
    def _body(impact: str) -> str:
        """A claim body built from the claim contract's own constants."""
        contracts = gate.contracts
        fields = dict.fromkeys(contracts.CAPSULE_V2_FIELDS, "t")
        fields[contracts.CAPSULE_V2_FIELDS[-1]] = impact  # the documentation-impact field
        lines = [contracts.CAPSULE_V2_MARKER]
        lines += [f"{name}: {value}" for name, value in fields.items()]
        origin = contracts.HARNESS_ORIGIN_TRAILER_PREFIX + contracts.HARNESS_ORIGINS[0]
        return "\n".join(lines) + f"\n\n{origin}\n"

    def _followup(self, body: str | Exception) -> object:
        def _run(_argv, _cwd, _code):
            if isinstance(body, Exception):
                raise body
            return body

        gate_self = SimpleNamespace(_run=_run)
        return gate_forge.GateForgeMixin._lane_knowledge_followup(
            gate_self, SimpleNamespace(cwd=Path("/work/lane"))
        )

    def test_a_deferred_head_names_its_decision_and_ticket(self) -> None:
        self.assertEqual(
            self._followup(self._body(self.IMPACT_DEFERRED)),
            {"decision": self.DEFERRED, "followup_ticket": "T-1"},
        )

    def test_a_not_deferred_head_owes_nothing(self) -> None:
        self.assertIsNone(self._followup(self._body(self.IMPACT_NOT_DEFERRED)))

    def test_a_claim_written_before_the_field_owes_nothing(self) -> None:
        """Additive means additive: an older deferred claim reports no
        follow-up rather than a broken one."""
        self.assertIsNone(
            self._followup(self._body(self.IMPACT_LEGACY_DEFERRED))
        )

    def test_an_unreadable_head_is_silent_rather_than_fatal(self) -> None:
        """A hint must never be able to change the verdict it rides on."""
        for body in (
            "not a claim at all",
            self._body("not json"),
            GateError("GIT_CONTEXT_FAILED"),
        ):
            with self.subTest(body=str(body)[:40]):
                self.assertIsNone(self._followup(body))

    def test_the_envelope_carries_it_and_defaults_to_null(self) -> None:
        owed = {"decision": ci.DEFERRED_KNOWLEDGE_DECISIONS[1], "followup_ticket": "T-1"}
        record = ci.build_ci_status(
            evidence=evidence(), failure=None, verdict="green",
            headline="CI: green", knowledge_followup=owed,
        )
        self.assertEqual(record["knowledge_followup"], owed)
        plain = ci.build_ci_status(
            evidence=evidence(), failure=None, verdict="green",
            headline="CI: green",
        )
        self.assertIsNone(plain["knowledge_followup"])

    def test_the_schema_version_is_not_bumped_for_an_added_field(self) -> None:
        """ci-status.v2 succeeded v1 on a RESTRUCTURE, not on an addition.

        A status consumer refuses a record whose schema it does not
        recognise, so bumping for an optional field would blind every deployed
        consumer to a gate that had merely gained a hint.
        """
        record = ci.build_ci_status(
            evidence=evidence(), failure=None, verdict="green",
            headline="CI: green",
            knowledge_followup={"decision": ci.DEFERRED_KNOWLEDGE_DECISIONS[0],
                                "followup_ticket": "T-1"},
        )
        self.assertEqual(record["schema"], "ci-status.v2")
        self.assertEqual(gate.contracts.CI_STATUS_SCHEMA_VERSION, "ci-status.v2")

    def test_the_deferred_vocabulary_matches_the_evaluator(self) -> None:
        """`ci.py` is pure and names the two decisions itself; the evaluator
        owns them. A divergence would make the reminder silently stop firing
        for one of them."""
        self.assertEqual(
            sorted(ci.DEFERRED_KNOWLEDGE_DECISIONS),
            sorted((kb_enforce.DECISION_KB_AFTER_MERGE,
                    kb_enforce.DECISION_KB_AFTER_RUNTIME)),
        )


class CiProgressTests(unittest.TestCase):
    """Job-level progress rides inside a pending ci-status.v2 record.

    ci_progress is the same redacted job-level evidence as ci-failure.v1
    under a non-terminal envelope. These tests pin the composition: the
    operation attaches the block only for a pending verdict, never changes
    the verdict to do so, and every failure in the advisory path means
    "no block".
    """

    def _call(self, gate_self: SimpleNamespace) -> dict[str, object]:
        if not hasattr(gate_self, "_lane_knowledge_followup"):
            gate_self._lane_knowledge_followup = lambda *_a, **_k: None
        context = SimpleNamespace(
            cwd=Path("/work/core-repo-worktrees/lane"),
            branch="task/example",
            policy=SimpleNamespace(
                forge="forgejo", forge_repository="acme/core-repo"
            ),
        )
        with mock.patch.object(gate_forge, "lane_ref_for", return_value=None):
            return gate_forge.GateForgeMixin.ci_status(gate_self, context)

    def _pending_in_progress_gate(
        self, progress: dict[str, object] | None
    ) -> SimpleNamespace:
        run = {
            "id": 57,
            "status": "in_progress",
            "head_sha": "c" * 40,
            "pull_requests": [{"head": {"sha": "c" * 40}, "base": {"sha": "b" * 40}}],
        }
        return SimpleNamespace(
            _policy_evidence_run=lambda _ctx, **_kw: (
                None,
                evidence(
                    result="UNAVAILABLE",
                    reason_code="CI_RUN_IN_PROGRESS",
                    retry_after_seconds=30,
                    base_sha="b" * 40,
                    head_sha="c" * 40,
                ),
                run,
                None,
            ),
            _failure_evidence=mock.Mock(),
            _ci_progress=lambda *_a, **_k: progress,
        )

    def test_a_pending_in_progress_run_carries_the_progress_block(self) -> None:
        progress = {
            "schema": "ci-progress.v1",
            "failed_jobs": [
                {"job_id": 4, "job_name": "c1", "job_state": "failed"}
            ],
            "overall_state": "pending",
            "terminal": False,
        }
        record = self._call(self._pending_in_progress_gate(progress))
        self.assertEqual(record["verdict"], "pending")
        self.assertEqual(record["ci_progress"], progress)
        self.assertIsNone(record["failure_evidence"])
        self.assertFalse(record["actionable"])
        self.assertTrue(record["ok"])

    def test_a_pending_run_without_progress_carries_no_block(self) -> None:
        record = self._call(self._pending_in_progress_gate(None))
        self.assertEqual(record["verdict"], "pending")
        self.assertNotIn("ci_progress", record)

    def test_non_pending_verdicts_never_consult_progress(self) -> None:
        ci_progress = mock.Mock()
        record = self._call(
            SimpleNamespace(
                _policy_evidence_run=lambda _ctx, **_kw: (None, evidence(), None, None),
                _ci_progress=ci_progress,
            )
        )
        self.assertEqual(record["verdict"], "green")
        ci_progress.assert_not_called()

    def test_the_progress_block_does_not_bump_the_schema(self) -> None:
        """Same pin as knowledge_followup: an added optional field is not a
        restructure, and a bump would blind every deployed helper."""
        record = ci.build_ci_status(
            evidence=evidence(),
            failure=None,
            verdict="pending",
            headline="CI: exact-head run is running (CI_RUN_IN_PROGRESS)",
        )
        record["ci_progress"] = {"schema": "ci-progress.v1"}
        self.assertEqual(record["schema"], "ci-status.v2")


class CiProgressReleaseGateTests(unittest.TestCase):
    """The advisory fetch path: release rules and fail-closed behaviour.

    _ci_progress runs inside every pending ci-status read of an in-progress
    run. Each test drives the real method with a backend double so the
    release order is pinned: trust binding first, then the jobs listing,
    then the launcher probe, then the failed jobs' logs.
    """

    def _run(self, **overrides: object) -> dict[str, object]:
        run: dict[str, object] = {
            "id": 57,
            "status": "in_progress",
            "event": "pull_request_target",
            "path": ".forgejo/workflows/checks.yml",
            "head_sha": "c" * 40,
            "pull_requests": [
                {
                    "number": 14,
                    "head": {"sha": "c" * 40},
                    "base": {"sha": "b" * 40},
                }
            ],
        }
        run.update(overrides)
        return run

    def _evidence(self) -> dict[str, object]:
        return evidence(
            result="UNAVAILABLE",
            reason_code="CI_RUN_IN_PROGRESS",
            retry_after_seconds=30,
            base_sha="b" * 40,
            head_sha="c" * 40,
        )

    def _gate(self, backend: object) -> SimpleNamespace:
        gate_self = SimpleNamespace(
            _backend_for=lambda _policy: backend,
            _runner=object(),
        )
        gate_self._failed_job_log_texts = partial(
            gate_forge.GateForgeMixin._failed_job_log_texts, gate_self
        )
        return gate_self

    def _call(
        self, backend: object, run: dict[str, object]
    ) -> dict[str, object] | None:
        bound = partial(gate_forge.GateForgeMixin._ci_progress, self._gate(backend))
        return bound(
            SimpleNamespace(
                cwd=Path("/work/core-repo-worktrees/lane"),
                policy=SimpleNamespace(
                    forge="forgejo",
                    forge_repository="acme/core-repo",
                    gate_profile="default",
                ),
            ),
            self._evidence(),
            run,
            "2020-01-01T10:00:00+00:00",
        )

    def test_releases_job_level_evidence_for_a_bound_in_progress_run(self) -> None:
        backend = _FakeProgressBackend(
            jobs_payload=(
                '{"jobs": [{"id": 4, "name": "c1", "status": "completed", '
                '"conclusion": "failure"}]}'
            ),
            file_payload="{}",
            log_payload="FAILED tests/test_example.py::test_x\n",
        )
        record = self._call(backend, self._run())
        assert record is not None
        self.assertEqual(record["schema"], "ci-progress.v1")
        self.assertEqual(record["terminal"], False)
        self.assertEqual(record["overall_state"], "pending")
        job = record["failed_jobs"][0]
        self.assertEqual(job["job_name"], "c1")
        self.assertEqual(job["job_state"], "failed")
        self.assertIn("test_example", str(job["excerpt"]))
        self.assertEqual(backend.calls, ["jobs", "file", "log"])

    def test_a_run_that_is_not_in_progress_releases_nothing(self) -> None:
        backend = _FakeProgressBackend()
        record = self._call(backend, self._run(status="queued"))
        self.assertIsNone(record)
        self.assertEqual(backend.calls, [])

    def test_a_run_that_fails_the_binding_releases_nothing(self) -> None:
        backend = _FakeProgressBackend()
        record = self._call(backend, self._run(event="push"))
        self.assertIsNone(record)
        self.assertEqual(backend.calls, [])

    def test_a_failed_jobs_listing_releases_nothing(self) -> None:
        backend = _FakeProgressBackend()
        record = self._call(backend, self._run())
        self.assertIsNone(record)
        self.assertEqual(backend.calls, ["jobs"])

    def test_a_run_without_failed_jobs_never_probes_or_fetches_logs(self) -> None:
        backend = _FakeProgressBackend(
            jobs_payload=(
                '{"jobs": [{"id": 3, "name": "c1", "status": "in_progress", '
                '"conclusion": null}]}'
            ),
        )
        record = self._call(backend, self._run())
        self.assertIsNone(record)
        self.assertEqual(backend.calls, ["jobs"])

    def test_a_failed_launcher_probe_releases_nothing(self) -> None:
        backend = _FakeProgressBackend(
            jobs_payload=(
                '{"jobs": [{"id": 4, "name": "c1", "status": "completed", '
                '"conclusion": "failure"}]}'
            ),
            file_payload=None,
        )
        record = self._call(backend, self._run())
        self.assertIsNone(record)
        self.assertEqual(backend.calls, ["jobs", "file"])

    def test_a_missing_job_log_still_releases_the_job_identity(self) -> None:
        backend = _FakeProgressBackend(
            jobs_payload=(
                '{"jobs": [{"id": 4, "name": "c1", "status": "completed", '
                '"conclusion": "failure"}]}'
            ),
            file_payload="{}",
            log_payload=None,
        )
        record = self._call(backend, self._run())
        assert record is not None
        job = record["failed_jobs"][0]
        self.assertIsNone(job["excerpt"])
        self.assertFalse(job["log_available"])

    def test_a_forge_error_body_is_a_missing_log_not_an_excerpt(self) -> None:
        """tea api exits 0 on a forge 500; the body is no log."""
        backend = _FakeProgressBackend(
            jobs_payload=(
                '{"jobs": [{"id": 4, "name": "c1", "status": "completed", '
                '"conclusion": "failure"}]}'
            ),
            file_payload="{}",
            log_payload=(
                '{"message": "job log missing", '
                '"url": "https://forge.example.org/api/swagger"}'
            ),
        )
        record = self._call(backend, self._run())
        assert record is not None
        job = record["failed_jobs"][0]
        self.assertIsNone(job["excerpt"])
        self.assertFalse(job["log_available"])
        self.assertNotIn("job log missing", json.dumps(record))


class _FakeProgressBackend:
    """Backend double whose methods fail when their payload is None."""

    def __init__(
        self,
        jobs_payload: str | None = None,
        file_payload: str | None = None,
        log_payload: str | None = None,
    ) -> None:
        self.jobs_payload = jobs_payload
        self.file_payload = file_payload
        self.log_payload = log_payload
        self.calls: list[str] = []

    def _result(self, payload: str | None) -> SimpleNamespace:
        if payload is None:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=payload)

    def actions_run_jobs(self, *_args: object) -> SimpleNamespace:
        self.calls.append("jobs")
        return self._result(self.jobs_payload)

    def actions_file_at_ref(self, *_args: object) -> SimpleNamespace:
        self.calls.append("file")
        return self._result(self.file_payload)

    def actions_job_logs(self, *_args: object) -> SimpleNamespace:
        self.calls.append("log")
        return self._result(self.log_payload)
