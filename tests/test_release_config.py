"""The release contract: Release Please owns the version, the tag, the changelog.

A release pull request is the release decision, and it always changes a path
outside the ordinary allowlist, so the gate never merges it. These assertions
pin what a release means, so a later edit cannot silently change the release
type, the tag shape, the version source or the identity that triggers checks.
"""

from __future__ import annotations

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
    assert "workflow_dispatch" in triggers
