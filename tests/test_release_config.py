"""The release contract: Release Please owns the version, the tag, the changelog.

A release pull request is the release decision, and it always changes a path
outside the ordinary allowlist, so the gate never merges it. These assertions
pin what a release means, so a later edit cannot silently change the release
type, the tag shape, the version source or the identity that triggers checks.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "release-please-config.json"
MANIFEST = ROOT / ".release-please-manifest.json"
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"

PINNED_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _workflow() -> dict:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _triggers(document: dict) -> dict:
    # PyYAML resolves the YAML 1.1 boolean spelling of ``on`` to True.
    value = document.get("on", document.get(True))
    assert isinstance(value, dict), "the workflow must declare a mapping of triggers"
    return value


def _yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _release_step(job: dict) -> dict:
    """The release-please action step: the first of the job's two steps.

    The job runs the action and then looks up the open release pull request. It
    runs nothing else. The regeneration of the release manifest is in two other
    jobs (tests/test_release_please_workflow.py), so this job holds the token and
    runs no repository code. Unpacking exactly two steps keeps that true here
    too: a third step fails this test.
    """
    action, lookup = job["steps"]
    assert (action.get("id"), lookup.get("id")) == ("release", "target")
    return action


def test_release_type_and_tag_shape():
    config = _json(CONFIG)
    assert config["release-type"] == "python"
    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
    # Before 1.0.0 a breaking change raises the minor version, not the major one.
    assert config["bump-minor-pre-major"] is True
    assert list(config["packages"]) == ["."]
    assert config["packages"]["."]["package-name"] == "merge-guard"


def test_manifest_starts_from_the_last_released_version():
    manifest = _json(MANIFEST)
    assert list(manifest) == ["."]
    assert SEMVER.fullmatch(manifest["."])


def test_the_manifest_version_and_the_project_version_move_together():
    manifest = _json(MANIFEST)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == manifest["."]


def test_the_workflow_uses_the_token_that_can_trigger_checks():
    workflow = _workflow()
    # The personal access token makes every write, so the default token has no
    # write grant. It has none at the workflow level, and the one job that
    # lists the open pull requests has that read.
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["release-please"]["permissions"] == {"pull-requests": "read"}
    step = _release_step(workflow["jobs"]["release-please"])
    assert PINNED_ACTION.fullmatch(step["uses"])
    assert step["with"]["token"] == "${{ secrets.RELEASE_PLEASE_TOKEN }}"
    assert step["with"]["config-file"] == "release-please-config.json"
    assert step["with"]["manifest-file"] == ".release-please-manifest.json"


def test_the_merge_workflow_calls_the_release_workflow_only_when_it_merged():
    """`workflow_run` fires on every completion of the merge workflow, merged or not.

    That workflow completes hourly and after every review pass, so observing the
    completion cost one release run per completion rather than per merge. The
    merge job now reports whether it landed a pull request, and the release
    workflow is called from that result.
    """
    merge = _yaml(ROOT / ".github" / "workflows" / "merge-guard-merge.yml")
    job = merge["jobs"]["merge"]
    assert job["outputs"] == {"merged": "${{ steps.gate.outputs.merged }}"}
    gate = next(step for step in job["steps"] if step.get("id") == "gate")
    assert gate["with"]["mode"] == "merge"
    release = merge["jobs"]["release"]
    assert release["needs"] == "merge"
    assert release["if"] == "needs.merge.outputs.merged == 'true'"
    assert release["uses"] == "./.github/workflows/release-please.yml"
    assert release["secrets"] == "inherit"
    assert release["permissions"] == {"contents": "write", "pull-requests": "write"}
    # The called workflow must not share the caller's group: with
    # cancel-in-progress that cancels the workflow that called it.
    assert merge["concurrency"]["group"] == "merge-guard-merge"
    called = _workflow()["concurrency"]
    assert called["group"] == "release-please"
    assert called["group"] != merge["concurrency"]["group"]
    assert called["cancel-in-progress"] is False


def test_the_product_dogfoods_the_default_branch_and_consumers_pin_a_release():
    """The released tag has no `merged` output yet, so the product runs its own tree."""
    real = _yaml(ROOT / ".github" / "workflows" / "merge-guard-merge.yml")
    real_gate = next(step for step in real["jobs"]["merge"]["steps"] if step.get("id") == "gate")
    assert real_gate["uses"] == "./"
    example = _yaml(ROOT / "examples" / "merge.yml")
    example_gate = next(step for step in example["jobs"]["merge"]["steps"] if step.get("id") == "gate")
    owner, _, ref = example_gate["uses"].partition("@")
    assert owner == "mikomiel/merge-guard"
    assert ref == "0" * 40, "the example teaches a pinned release, never a branch"


def test_the_example_teaches_the_same_release_gate():
    example = _yaml(ROOT / "examples" / "merge.yml")
    assert example["jobs"]["merge"]["outputs"] == {"merged": "${{ steps.gate.outputs.merged }}"}
    release = example["jobs"]["release"]
    assert release["needs"] == "merge"
    assert release["if"] == "needs.merge.outputs.merged == 'true'"
    assert release["uses"] == "./.github/workflows/release-please.yml"


def test_the_release_workflow_is_called_or_pushed_and_never_observes_a_completion():
    triggers = _triggers(_workflow())
    assert triggers["push"] == {"branches": ["main"]}
    assert "workflow_call" in triggers
    assert "workflow_dispatch" in triggers
    assert "workflow_run" not in triggers


def test_the_release_job_pins_the_action_and_needs_no_branch_guard():
    job = _workflow()["jobs"]["release-please"]
    assert "if" not in job
    step = _release_step(job)
    assert step["uses"].startswith("googleapis/release-please-action@")
    assert step["with"]["token"] == "${{ secrets.RELEASE_PLEASE_TOKEN }}"
    assert step["with"]["manifest-file"] == ".release-please-manifest.json"


def test_the_python_updater_targets_exist_in_the_shape_it_rewrites():
    # release-please's python strategy rewrites a literal [project].version and
    # a literal ``__version__ = "x.y.z"`` in <name>/__init__.py. A computed value
    # or a moved attribute would silently stop being updated.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "merge-guard"
    assert SEMVER.fullmatch(project["project"]["version"])
    init_text = (ROOT / "merge_guard" / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r'^__version__ = "\d+\.\d+\.\d+"$', init_text, re.MULTILINE)


def test_the_project_is_metadata_only():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "build-system" not in project


def test_the_release_metadata_stays_outside_the_ordinary_allowlist():
    # The release pull request always changes a version-bearing path, so the gate
    # must never merge it: that is what makes merging it the release decision.
    policy = tomllib.loads(
        (ROOT / ".github" / "merge-guard.toml").read_text(encoding="utf-8")
    )
    ordinary = policy["paths"]["ordinary"]
    for path in (
        "pyproject.toml",
        "merge_guard/__init__.py",
        "release-please-config.json",
        ".release-please-manifest.json",
    ):
        assert not any(fnmatch.fnmatch(path, pattern) for pattern in ordinary), path


_REPO_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


def _own_pins(workflows: Path) -> list[tuple[str, str]]:
    """Every step in *workflows* that uses this action, as (file name, uses)."""
    pins = []
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        for job in (_yaml(path).get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("mikomiel/merge-guard@"):
                    pins.append((path.name, step["uses"]))
    return pins


def test_a_second_pin_under_either_workflow_suffix_is_found(tmp_path):
    step = "jobs:\n  j:\n    steps:\n      - uses: mikomiel/merge-guard@" + "0" * 40 + "\n"
    (tmp_path / "a.yml").write_text(step, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(step, encoding="utf-8")
    assert [name for name, _ in _own_pins(tmp_path)] == ["a.yml", "b.yaml"]


def test_the_repository_pins_its_own_release_in_one_place_and_the_tag_names_it():
    """The review workflow holds the only real pin, and its comment tag resolves to it."""
    workflows = ROOT / ".github" / "workflows"
    pins = _own_pins(workflows)
    assert [name for name, _ in pins] == ["merge-guard-review.yml"], pins
    ref = pins[0][1].partition("@")[2]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), ref
    text = (workflows / pins[0][0]).read_text(encoding="utf-8")
    (line,) = [line for line in text.splitlines() if "mikomiel/merge-guard@" in line]
    comment = line.partition("#")[2].split()
    assert comment, "the pin line must carry its release tag in a trailing comment"
    tag = comment[0].rstrip(":")
    env = {k: v for k, v in os.environ.items() if k not in _REPO_GIT_ENV}
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"tag {tag} is not in this clone: run `git fetch --tags`"
    assert result.stdout.strip() == ref, f"the pin {ref} is not the commit of {tag}"
