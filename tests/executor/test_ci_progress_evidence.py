"""ci-progress.v1: job-level excerpts for an in-progress run's failed jobs.

The terminal failure envelope (ci-failure.v1) established that a fetched
log must survive a forge that reports no steps. Progress evidence carries
the same job-level excerpts earlier, while the run is still in progress,
and must never read as the exact-head verdict: the envelope is
non-terminal by construction and the verdict stays pending.

These tests are written against the Forgejo job shape: no steps array, no
conclusion beyond what the translator supplies. The GitHub-shaped cases
are here too, because the point of the fix is that they do not change.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools-config"))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tools-config" / "scripts")
)

from git_gate import ci  # noqa: E402


class RunLifecycleTests(unittest.TestCase):
    def test_failed_run_with_unfinished_sibling_stays_pending(self):
        # A failed job beside an unfinished sibling.
        for status in ("queued", "in_progress"):
            for conclusion in ("failure", "success"):
                with self.subTest(status=status, conclusion=conclusion):
                    run = {"id": 91, "status": "completed", "conclusion": conclusion}
                    jobs = {"jobs": [
                        {"id": 921, "status": "completed", "conclusion": "failure"},
                        {"id": 922, "status": status, "conclusion": None},
                    ]}
                    normalized = ci.with_job_lifecycle(run, jobs)
                    self.assertEqual(normalized["status"], "in_progress")
                    self.assertIsNone(normalized["conclusion"])
                    self.assertTrue(ci.run_status_supports_progress(normalized))
                    self.assertEqual(run["status"], "completed")

    def test_all_jobs_terminal_preserves_authoritative_conclusion(self):
        run = {"status": "completed", "conclusion": "failure"}
        self.assertIs(ci.with_job_lifecycle(run, {"jobs": [
            {"status": "completed", "conclusion": "failure"},
            {"status": "completed", "conclusion": "success"},
        ]}), run)

    def test_unknown_job_state_cannot_prove_completion(self):
        for row in ({"status": "unexpected"}, {}, None):
            with self.subTest(row=row):
                run = ci.with_job_lifecycle(
                    {"status": "completed", "conclusion": "success"}, {"jobs": [row]}
                )
                self.assertEqual(run["status"], "unknown")
                self.assertIsNone(run["conclusion"])

    def test_blocked_row_beside_a_completed_row_stays_completed(self):
        """a Forgejo-``blocked`` row is not remaining work -- it
        will never turn terminal -- so it must not push the run back to
        ``in_progress`` and hide it from ``evaluate_evidence``."""
        run = {"status": "completed", "conclusion": "failure"}
        jobs = {"jobs": [
            {"status": "completed", "conclusion": "failure"},
            {"status": "queued", "conclusion": None, "forge_status": "blocked"},
        ]}
        self.assertIs(ci.with_job_lifecycle(run, jobs), run)

    def test_queued_without_forge_status_still_becomes_in_progress(self):
        """An ordinary queued row (no ``forge_status: blocked``) is real
        remaining work, so the behaviour from before the blocked-aggregate rule is unchanged."""
        run = {"status": "completed", "conclusion": "failure"}
        jobs = {"jobs": [
            {"status": "completed", "conclusion": "failure"},
            {"status": "queued", "conclusion": None},
        ]}
        normalized = ci.with_job_lifecycle(run, jobs)
        self.assertEqual(normalized["status"], "in_progress")
        self.assertIsNone(normalized["conclusion"])

    def test_blocked_beside_in_progress_still_becomes_in_progress(self):
        """A blocked row does not exempt a genuinely still-running sibling."""
        run = {"status": "completed", "conclusion": "failure"}
        jobs = {"jobs": [
            {"status": "in_progress", "conclusion": None},
            {"status": "queued", "conclusion": None, "forge_status": "blocked"},
        ]}
        normalized = ci.with_job_lifecycle(run, jobs)
        self.assertEqual(normalized["status"], "in_progress")
        self.assertIsNone(normalized["conclusion"])


def _forgejo_job(
    job_id: int, name: str, *, conclusion: str = "failure"
) -> dict[str, object]:
    """A job row with no steps key, as Forgejo actually reports one."""
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "status": "completed",
        "run_id": 62,
        "attempt": 1,
    }


def _github_job(job_id: int, name: str, *, step_name: str) -> dict[str, object]:
    return {
        "id": job_id,
        "name": name,
        "conclusion": "failure",
        "status": "completed",
        "steps": [
            {"number": 1, "name": "checkout", "conclusion": "success"},
            {"number": 2, "name": step_name, "conclusion": "failure"},
        ],
    }


def _record(
    jobs: list[dict[str, object]], log_texts: dict[int, str | None]
) -> dict[str, object]:
    return ci.build_ci_progress(
        repository="acme/core-repo",
        pull_request=14,
        base_sha="b" * 40,
        head_sha="c" * 40,
        run={"id": 62, "status": "in_progress"},
        jobs={"jobs": jobs},
        log_texts=log_texts,
        observed_at="2020-01-01T00:00:00+00:00",
    )


def _jobs(record: dict[str, object]) -> list[dict[str, object]]:
    entries = record["failed_jobs"]
    assert isinstance(entries, list)
    return [entry for entry in entries if isinstance(entry, dict)]


class ProgressEnvelopeTests(unittest.TestCase):
    def test_the_envelope_is_non_terminal_and_names_the_run(self) -> None:
        record = _record([_forgejo_job(32, "c1")], {32: "FAILED\n"})
        self.assertEqual(record["schema"], "ci-progress.v1")
        self.assertEqual(record["overall_state"], "pending")
        self.assertIs(record["terminal"], False)
        self.assertEqual(record["run_status"], "in_progress")
        self.assertEqual(record["workflow_run_id"], 62)
        self.assertEqual(record["head_sha"], "c" * 40)

    def test_a_forgejo_job_with_no_steps_still_carries_its_log(self) -> None:
        record = _record(
            [_forgejo_job(32, "c1")],
            {32: "FAILED tests/test_example.py::test_example_case\n"},
        )
        job = _jobs(record)[0]
        self.assertEqual(job["failed_steps"], [])
        self.assertEqual(job["job_state"], "failed")
        self.assertIn("test_example_case", str(job["excerpt"]))
        self.assertGreater(int(str(record["bytes_returned"])), 0)

    def test_a_timed_out_job_is_named_timed_out(self) -> None:
        record = _record(
            [_forgejo_job(32, "c1", conclusion="timed_out")],
            {32: "the runner gave up\n"},
        )
        job = _jobs(record)[0]
        self.assertEqual(job["job_state"], "timed_out")

    def test_a_github_job_keeps_its_excerpt_on_the_step(self) -> None:
        record = _record(
            [_github_job(300, "unit-h", step_name="pytest")],
            {300: "E   assert 22 == 23\n"},
        )
        job = _jobs(record)[0]
        steps = job["failed_steps"]
        assert isinstance(steps, list)
        self.assertEqual(len(steps), 1)
        step = steps[0]
        assert isinstance(step, dict)
        self.assertEqual(step["step_name"], "pytest")
        self.assertIn("assert 22 == 23", str(step["excerpt"]))
        self.assertIsNone(job["excerpt"])
        self.assertEqual(job["job_state"], "failed")

    def test_the_two_levels_never_both_spend_the_budget(self) -> None:
        log = "y" * 500
        record = _record([_github_job(300, "unit-h", step_name="pytest")], {300: log})
        self.assertEqual(int(str(record["bytes_returned"])), len(log))

    def test_a_short_log_is_not_crowded_out_by_a_long_one(self) -> None:
        record = _record(
            [_forgejo_job(31, "unit-config"), _forgejo_job(32, "c1")],
            {
                31: "noise\n" * 20_000,
                32: "FAILED tests/test_example.py::test_example_case\n",
            },
        )
        big, small = _jobs(record)
        self.assertEqual(small["job_name"], "c1")
        self.assertIn("test_example_case", str(small["excerpt"]))
        self.assertIsNotNone(big["excerpt"])
        self.assertTrue(record["truncated"])
        self.assertLessEqual(
            int(str(record["bytes_returned"])), ci.FAILURE_EVIDENCE_MAX_BYTES
        )

    def test_the_record_stays_in_job_id_order(self) -> None:
        record = _record(
            [_forgejo_job(31, "unit-config"), _forgejo_job(32, "c1")],
            {31: "x" * 5_000, 32: "y" * 10},
        )
        ids = [int(str(job["job_id"])) for job in _jobs(record)]
        self.assertEqual(ids, sorted(ids))

    def test_the_byte_cap_binds_across_several_stepless_jobs(self) -> None:
        huge = "z" * 100_000
        record = _record(
            [_forgejo_job(31, "a"), _forgejo_job(32, "b")],
            {31: huge, 32: huge},
        )
        self.assertLessEqual(
            int(str(record["bytes_returned"])), ci.FAILURE_EVIDENCE_MAX_BYTES
        )
        self.assertTrue(record["truncated"])
        first, second = _jobs(record)
        self.assertIsNotNone(first["excerpt"])
        self.assertIsNone(second["excerpt"])

    def test_a_log_that_could_not_be_fetched_carries_nothing(self) -> None:
        record = _record([_forgejo_job(32, "c1")], {32: None})
        job = _jobs(record)[0]
        self.assertIsNone(job["excerpt"])
        self.assertEqual(job["failed_steps"], [])
        self.assertFalse(job["log_available"])
        self.assertEqual(int(str(record["bytes_returned"])), 0)
        self.assertFalse(record["truncated"])

    def test_the_job_level_excerpt_is_redacted_like_any_other(self) -> None:
        token = "ghp_" + "A" * 36
        record = _record([_forgejo_job(32, "c1")], {32: f"token {token}\n"})
        excerpt = str(_jobs(record)[0]["excerpt"])
        self.assertNotIn(token, excerpt)
        self.assertIn("[REDACTED]", excerpt)
        self.assertGreaterEqual(int(str(record["redaction_count"])), 1)


class ProgressReleasePredicateTests(unittest.TestCase):
    def test_only_an_in_progress_run_supports_progress(self) -> None:
        cases = {
            "queued": False,
            "in_progress": True,
            "completed": False,
            "waiting": False,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(
                    ci.run_status_supports_progress({"status": status}), expected
                )
        self.assertFalse(ci.run_status_supports_progress({}))

    def _bound_run(self, **overrides: object) -> dict[str, object]:
        run: dict[str, object] = {
            "id": 62,
            "status": "in_progress",
            "event": "pull_request_target",
            "path": ".forgejo/workflows/checks.yml",
            "head_sha": "c" * 40,
            "pull_requests": [
                {"head": {"sha": "c" * 40}, "base": {"sha": "b" * 40}}
            ],
        }
        run.update(overrides)
        return run

    def test_a_bound_run_passes(self) -> None:
        self.assertTrue(
            ci.progress_trust_ok(
                self._bound_run(), ci.FORGEJO_TRUST_PROFILE, "b" * 40, "c" * 40
            )
        )

    def test_every_mismatch_fails_closed(self) -> None:
        cases = {
            "wrong event": {"event": "push"},
            "wrong path": {"path": "somewhere-else.yml"},
            "no pull records": {"pull_requests": []},
            "two pull records": {
                "pull_requests": [
                    {"head": {"sha": "c" * 40}, "base": {"sha": "b" * 40}},
                    {"head": {"sha": "c" * 40}, "base": {"sha": "b" * 40}},
                ]
            },
            "run head inconsistent with its own record": {
                "head_sha": "d" * 40,
                "pull_requests": [
                    {"head": {"sha": "c" * 40}, "base": {"sha": "b" * 40}}
                ],
            },
            "recorded head differs from the lane head": {
                "pull_requests": [
                    {"head": {"sha": "d" * 40}, "base": {"sha": "b" * 40}}
                ]
            },
            "recorded base differs from the lane base": {
                "pull_requests": [
                    {"head": {"sha": "c" * 40}, "base": {"sha": "d" * 40}}
                ]
            },
            "malformed pull record": {"pull_requests": [{"head": "c" * 40}]},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                self.assertFalse(
                    ci.progress_trust_ok(
                        self._bound_run(**overrides),
                        ci.FORGEJO_TRUST_PROFILE,
                        "b" * 40,
                        "c" * 40,
                    ),
                    name,
                )

    def test_a_null_lane_side_of_the_binding_is_not_required(self) -> None:
        """A caller without its own base/head still verifies self-consistency."""
        self.assertTrue(
            ci.progress_trust_ok(
                self._bound_run(), ci.FORGEJO_TRUST_PROFILE, None, None
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
