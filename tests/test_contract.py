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
    # One output, derived from the envelope: a consumer may gate a release on it.
    assert set(action["outputs"]) == {"merged"}
    assert action["outputs"]["merged"]["value"] == "${{ steps.envelope.outputs.merged }}"
    step = next(
        item for item in action["runs"]["steps"] if "scripts/merge_guard.py" in item.get("run", "")
    )
    run = step["run"]
    assert "$GITHUB_ACTION_PATH/scripts/merge_guard.py" in run
    assert "--apply" in run
    # Inputs travel through the environment: an expression spliced into the shell source is code injection.
    assert step["env"]["MERGE_GUARD_MODE"] == "${{ inputs.mode }}"
    assert step["env"]["MERGE_GUARD_POLICY"] == "${{ inputs.policy }}"
    assert '"$MERGE_GUARD_MODE"' in run
    assert '--policy "$MERGE_GUARD_POLICY"' in run
    assert "${{" not in run


FROZEN_SCHEMA = {
    "repository": {"name", "base_branch"},
    "review": {
        "reviewer_id", "reviewer_login", "reviewer_type", "required_checks",
        "gate_context", "review_workflow", "merge_workflow", "carry_unchanged_diff",
    },
    "paths": {"ordinary", "dependency_update_exemption"},
}


def test_the_policy_schema_is_frozen():
    assert {section: set(fields) for section, fields in policy.KNOWN_FIELDS.items()} == FROZEN_SCHEMA
    assert policy.DEPRECATED_FIELDS == {"review.carry_unchanged_diff"}


def test_the_example_uses_exactly_the_frozen_schema():
    example = tomllib.loads(
        (ROOT / "examples" / "merge-guard.toml").read_text(encoding="utf-8"))
    assert {section: set(fields) for section, fields in example.items()} == FROZEN_SCHEMA


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


def test_the_action_captures_the_envelope_and_reports_it_through_github_output():
    """The `merged` output exists only if the shell wiring between the steps survives.

    The engine's stdout is the envelope. One step tees it to a file and the next
    hands that file, plus GitHub's output file, to the helper. Both halves are
    shell source, so neither `tests/test_envelope.py` nor the release-config
    assertions can see a break in the connection between them.
    """
    action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    steps = {step.get("id"): step for step in action["runs"]["steps"] if step.get("id")}
    assert set(steps) == {"guard", "envelope"}
    guard, report = steps["guard"], steps["envelope"]
    envelope = "${{ runner.temp }}/merge-guard-envelope.json"
    assert guard["env"]["MERGE_GUARD_ENVELOPE"] == envelope
    assert report["env"]["MERGE_GUARD_ENVELOPE"] == envelope
    assert "set -o pipefail" in guard["run"]
    assert '| tee "$MERGE_GUARD_ENVELOPE"' in guard["run"]
    assert '"$MERGE_GUARD_ENVELOPE"' in report["run"]
    assert '"$GITHUB_OUTPUT"' in report["run"]
    # Path and argument order: a swap here would still pass the substring checks.
    assert (
        'python3 "$GITHUB_ACTION_PATH/scripts/merge_guard_envelope.py" '
        '"$MERGE_GUARD_ENVELOPE" "$GITHUB_OUTPUT"'
    ) in report["run"]
    # Nothing templated reaches shell source.
    assert "${" + "{" not in guard["run"]
    assert "${" + "{" not in report["run"]