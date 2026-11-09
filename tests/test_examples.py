"""The example workflows must load: GitHub allows no `env` in a job-level if."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_no_env_context_in_a_job_level_if():
    for path in sorted((ROOT / "examples").glob("*.yml")):
        data = yaml.safe_load(path.read_text())
        jobs = data.get("jobs") or {}
        for name, job in jobs.items():
            condition = job.get("if") if isinstance(job, dict) else None
            if isinstance(condition, str):
                assert "env." not in condition, f"{path.name}:{name} uses env in a job-level if"
