"""A missed required run is recoverable by an event; a dispatch only when it can
produce every required context (#14).

The gate can name the condition and refuse the merge, but the head still has no
run. A dispatch restores only the checks the dispatched workflow produces; this
repository's `dependency-review` runs only on `pull_request`, so close/reopen is
its recovery, and the dispatch stays the consumer recommendation under that
condition.
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


def _troubleshooting() -> str:
    text = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    return " ".join(text.split())


def test_the_checks_workflow_can_be_dispatched_for_a_branch():
    assert "workflow_dispatch" in _triggers(
        ROOT / ".github" / "workflows" / "checks.yml")


def test_the_documented_command_is_conditioned_on_every_required_check():
    assert "gh workflow run <ci-workflow>.yml --ref <pr-branch>" in _troubleshooting()


def test_the_docs_limit_a_dispatch_to_the_checks_it_produces():
    text = _troubleshooting()
    assert "restores only the required checks that the dispatched workflow produces" in text
    assert "dependency-review" in text
    assert "reopening or pushing" in text
