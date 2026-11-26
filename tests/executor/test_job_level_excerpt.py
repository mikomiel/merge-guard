"""A fetched job log must survive a forge that reports no steps.

``ci-failure.v1`` attaches an excerpt to a failed *step*, which is a GitHub
shape. A Forgejo job row carries no ``steps`` array at all, and its fetched,
redacted log must still reach the verdict, so a red lane says *why* it is
red as well as which jobs failed.

These tests are written against the Forgejo job shape (``attempt, handle,
id, name, needs, owner_id, repo_id, run_id, runs_on, status, task_id``): no
``steps``, no ``conclusion`` beyond what the translator supplies. The
GitHub-shaped cases are here too, because they must not change.
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


def _forgejo_job(job_id: int, name: str) -> dict[str, object]:
    """A job row with no ``steps`` key, as Forgejo actually reports one."""
    return {
        "id": job_id,
        "name": name,
        "conclusion": "failure",
        "status": "completed",
        "run_id": 62,
        "attempt": 1,
    }


def _github_job(job_id: int, name: str, *, step_name: str) -> dict[str, object]:
    return {
        "id": job_id,
        "name": name,
        "conclusion": "failure",
        "steps": [
            {"number": 1, "name": "checkout", "conclusion": "success"},
            {"number": 2, "name": step_name, "conclusion": "failure"},
        ],
    }


def _record(
    jobs: list[dict[str, object]], log_texts: dict[int, str | None]
) -> dict[str, object]:
    return ci.build_failure_evidence(
        repository="acme/core-repo",
        pull_request=14,
        base_sha="b" * 40,
        head_sha="c" * 40,
        run={"id": 62},
        jobs={"jobs": jobs},
        log_texts=log_texts,
        observed_at="2020-01-01T00:00:00+00:00",
    )


def _jobs(record: dict[str, object]) -> list[dict[str, object]]:
    entries = record["failed_jobs"]
    assert isinstance(entries, list)
    return [entry for entry in entries if isinstance(entry, dict)]


class JobLevelExcerpt(unittest.TestCase):
    def test_a_forgejo_job_with_no_steps_still_carries_its_log(self) -> None:
        """The regression pin: a fetched log must not reach the caller as zero bytes."""
        record = _record(
            [_forgejo_job(32, "c1")],
            {32: "FAILED tests/test_example.py::test_example_case\n"},
        )
        job = _jobs(record)[0]
        self.assertEqual(job["failed_steps"], [])
        self.assertIn("test_example_case", str(job["excerpt"]))
        self.assertGreater(int(str(record["bytes_returned"])), 0)

    def test_a_github_job_keeps_its_excerpt_on_the_step(self) -> None:
        """The pre-existing path is unchanged: steps carry it, the job does not."""
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

    def test_the_two_levels_never_both_spend_the_budget(self) -> None:
        """A job's log is quoted once, so the byte count cannot double."""
        log = "y" * 500
        record = _record([_github_job(300, "unit-h", step_name="pytest")], {300: log})
        self.assertEqual(int(str(record["bytes_returned"])), len(log))

    def test_a_steps_array_where_nothing_failed_falls_back_to_the_job(self) -> None:
        """Same condition for a reader as no array at all: no step to name."""
        job_row = _github_job(300, "unit-h", step_name="pytest")
        steps = job_row["steps"]
        assert isinstance(steps, list)
        for step in steps:
            assert isinstance(step, dict)
            step["conclusion"] = "success"
        record = _record([job_row], {300: "the runner died after the last step\n"})
        job = _jobs(record)[0]
        self.assertEqual(job["failed_steps"], [])
        self.assertIn("the runner died", str(job["excerpt"]))

    def test_a_log_that_could_not_be_fetched_carries_nothing(self) -> None:
        """Log availability is best-effort; a missing log is not a malformed record."""
        record = _record([_forgejo_job(32, "c1")], {32: None})
        job = _jobs(record)[0]
        self.assertIsNone(job["excerpt"])
        self.assertEqual(job["failed_steps"], [])
        self.assertEqual(int(str(record["bytes_returned"])), 0)
        self.assertFalse(record["truncated"])

    def test_the_job_level_excerpt_is_redacted_like_any_other(self) -> None:
        """The excerpt is the redacted text, never the raw log."""
        token = "ghp_" + "A" * 36
        record = _record([_forgejo_job(32, "c1")], {32: f"token {token}\n"})
        excerpt = str(_jobs(record)[0]["excerpt"])
        self.assertNotIn(token, excerpt)
        self.assertIn("[REDACTED]", excerpt)
        self.assertGreaterEqual(int(str(record["redaction_count"])), 1)

    def test_a_short_log_is_not_crowded_out_by_a_long_one(self) -> None:
        """The informative log can be the small one.

        A large test job can log tens of thousands of bytes while ``c1``
        logs a few hundred. Spending the budget in job-id order gave the
        first job all 20,000 and the second nothing, dropping the short
        summary log that named the failure.
        """
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
        # The short log arrives whole; the long one takes what is left.
        self.assertIsNotNone(big["excerpt"])
        self.assertTrue(record["truncated"])
        self.assertLessEqual(
            int(str(record["bytes_returned"])), ci.FAILURE_EVIDENCE_MAX_BYTES
        )

    def test_the_record_stays_in_job_id_order(self) -> None:
        """Spending order is not record order — a reader still sees ids ascending."""
        record = _record(
            [_forgejo_job(31, "unit-config"), _forgejo_job(32, "c1")],
            {31: "x" * 5_000, 32: "y" * 10},
        )
        ids = [int(str(job["job_id"])) for job in _jobs(record)]
        self.assertEqual(ids, sorted(ids))

    def test_the_byte_cap_binds_across_several_stepless_jobs(self) -> None:
        """The 20,000-byte budget is one budget, whichever level spends it."""
        huge = "z" * 100_000
        record = _record(
            [_forgejo_job(31, "unit-config"), _forgejo_job(32, "c1")],
            {31: huge, 32: huge},
        )
        self.assertLessEqual(
            int(str(record["bytes_returned"])), ci.FAILURE_EVIDENCE_MAX_BYTES
        )
        self.assertTrue(record["truncated"])
        # The first job by id spends the budget; the second finds none left
        # and carries no excerpt rather than an empty-string one.
        first, second = _jobs(record)
        self.assertIsNotNone(first["excerpt"])
        self.assertIsNone(second["excerpt"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
