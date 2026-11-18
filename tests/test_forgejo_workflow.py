"""The live Forgejo job runs pinned code, and it runs the live module.

The job cannot run outside GitHub Actions, so this module checks what can be
checked without it: every action and the image are pinned to an immutable
reference, the job keeps the read-only token, it sets the variables that
``tests/test_forgejo_live.py`` reads, so the live tests cannot skip there, and
the live tests read only the accounts that the module fixture creates.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

import test_forgejo_live as live
import yaml

from merge_guard import forgejo

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "forgejo.yml"
ACTION = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}")
IMAGE = re.compile(r"[a-z0-9.-]+(/[a-z0-9._-]+)+:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}")


def _document() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert document["permissions"] == {"contents": "read"}
    return document


def _job() -> dict:
    [job] = _document()["jobs"].values()
    return job


def test_a_pull_request_runs_the_job_when_it_changes_what_the_job_exercises():
    # PyYAML reads the bare key `on` as True.
    triggers = _document()[True]
    assert triggers["push"] == {"branches": ["main"]}
    assert "workflow_dispatch" in triggers
    paths = triggers["pull_request"]["paths"]
    assert set(paths) == {"merge_guard/**", "tests/test_forgejo_live.py",
                          "requirements-dev.txt", ".github/workflows/forgejo.yml"}
    # The live module imports the package only; a new local import elsewhere
    # needs its path added to the filter.
    source = (ROOT / "tests" / "test_forgejo_live.py").read_text(encoding="utf-8")
    local = {name for name in re.findall(r"^(?:from|import) ([A-Za-z_][\w]*)", source, re.MULTILINE)
             if (ROOT / name).is_dir() or (ROOT / f"{name}.py").exists()
             or (ROOT / "tests" / f"{name}.py").exists()}
    assert local == {"merge_guard"}, local


def test_every_action_is_pinned_by_full_commit():
    for step in _job()["steps"]:
        if "uses" in step:
            assert ACTION.fullmatch(step["uses"]), step["uses"]


def test_the_forgejo_image_is_pinned_by_exact_version_and_digest():
    [service] = _job()["services"].values()
    assert IMAGE.fullmatch(service["image"]), service["image"]


def test_the_instance_runs_forgejo_actions():
    # The adapter reads CI evidence from the Actions runs and jobs.
    [service] = _job()["services"].values()
    assert service["env"]["FORGEJO__actions__ENABLED"] == "true"


def test_the_checkout_does_not_keep_a_credential():
    [checkout] = [step for step in _job()["steps"]
                  if step.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False


def test_the_live_tests_read_only_the_accounts_that_the_fixture_creates():
    # Elsewhere the live tests are skipped, so a read of an account that the
    # module fixture never creates would fail only in the live job.
    source = (ROOT / "tests" / "test_forgejo_live.py").read_text(encoding="utf-8")
    read = set(re.findall(r"""live\.(?:client|user_id|review)\(\s*["']([^"']+)["']""", source))
    assert read, "the pattern no longer finds the account reads"
    assert read <= set(live.SHARED_ROLES)
    assert set(live.SHARED_ROLES) <= set(live.PERMISSIONS)


def test_the_job_runs_the_live_module_against_this_machine():
    [step] = [step for step in _job()["steps"] if "test_forgejo_live.py" in step.get("run", "")]
    url = step["env"][live.URL]
    assert urllib.parse.urlsplit(url).hostname in forgejo.LOOPBACK_HOSTS
    assert step["env"][live.ADMIN]
    assert live.PASSWORD in step["run"]
