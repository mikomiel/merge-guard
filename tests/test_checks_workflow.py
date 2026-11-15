"""A missed required run is recoverable by dispatching the CI workflow (#14).

The gate can name the condition and refuse the merge, but the head still has no
run. `workflow_dispatch` on `checks.yml` is the one command that creates a run
on the current head, so it is the documented recovery.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _triggers(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    # PyYAML resolves the YAML 1.1 boolean spelling of ``on`` to True.
    value = document.get("on", document.get(True))
    assert isinstance(value, dict), f"{path} must declare a mapping of triggers"
    return value


def test_the_checks_workflow_can_be_dispatched_for_a_branch():
    assert "workflow_dispatch" in _triggers(
        ROOT / ".github" / "workflows" / "checks.yml")


def test_the_documented_recovery_command_is_the_dispatch():
    text = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    assert "gh workflow run checks.yml --ref <pr-branch>" in text
