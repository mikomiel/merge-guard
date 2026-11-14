"""The release contract: Release Please owns the version, the tag, the changelog.

A release pull request is the release decision, and it always changes a path
outside the ordinary allowlist, so the gate never merges it. These assertions
pin what a release means, so a later edit cannot silently change the release
type, the tag shape, the version source or the identity that triggers checks.
"""

from __future__ import annotations

import fnmatch
import json
import re
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


def test_release_type_and_tag_shape():
    config = _json(CONFIG)
    assert config["release-type"] == "python"
    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
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
    assert workflow["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    (step,) = workflow["jobs"]["release-please"]["steps"]
    assert PINNED_ACTION.fullmatch(step["uses"])
    assert step["with"]["token"] == "${{ secrets.RELEASE_PLEASE_TOKEN }}"
    assert step["with"]["config-file"] == "release-please-config.json"
    assert step["with"]["manifest-file"] == ".release-please-manifest.json"


def test_the_workflow_observes_the_merges_that_suppress_push_events():
    triggers = _triggers(_workflow())
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["workflow_run"]["workflows"] == ["merge-guard merge"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert "workflow_dispatch" in triggers


def test_the_workflow_pins_the_release_please_action_and_the_default_branch_guard():
    workflow = _workflow()
    job = workflow["jobs"]["release-please"]
    (step,) = job["steps"]
    assert step["uses"].startswith("googleapis/release-please-action@")
    guard = job["if"]
    assert "github.event_name != 'workflow_run'" in guard
    assert "head_branch == 'main'" in guard


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
