"""Pin the consumer contract that docs/cli.md documents."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from merge_guard import cli, policy

ROOT = Path(__file__).resolve().parents[1]


def test_action_inputs_are_pinned():
    action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    assert set(action["inputs"]) == {"mode", "policy", "token"}
    assert action["inputs"]["mode"]["required"] is True
    assert action["inputs"]["policy"]["default"] == ".github/merge-guard.toml"
    run = action["runs"]["steps"][-1]["run"]
    assert "$GITHUB_ACTION_PATH/scripts/merge_guard.py" in run
    assert "${{ inputs.mode }}" in run
    assert "--apply" in run
    assert "${{ inputs.policy }}" in run


def test_the_policy_schema_is_the_documented_set():
    example = tomllib.loads(
        (ROOT / "examples" / "merge-guard.toml").read_text(encoding="utf-8"))
    assert {section: frozenset(fields) for section, fields in example.items()} == policy.KNOWN_FIELDS
    assert policy.DEPRECATED_FIELDS == {"review.carry_unchanged_diff"}
    assert "carry_unchanged_diff" in policy.KNOWN_FIELDS["review"]


def test_this_repository_policy_uses_only_known_fields():
    data = tomllib.loads(
        (ROOT / ".github" / "merge-guard.toml").read_text(encoding="utf-8"))
    assert policy.unknown_fields(data) == []


def test_exit_codes_and_modes_are_pinned():
    assert (cli.EXIT_OK, cli.EXIT_OPERATIONAL, cli.EXIT_UNUSABLE) == (0, 1, 2)
    assert cli.MODES == ("status", "review", "merge")


def test_the_document_covers_every_mode_and_exit_code():
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    for mode in cli.MODES:
        assert f"`{mode}`" in text
    for code in (cli.EXIT_OK, cli.EXIT_OPERATIONAL, cli.EXIT_UNUSABLE):
        assert f"| {code} |" in text
