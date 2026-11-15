"""The merge workflow runs once per gate publication, not once per prerequisite.

`checks` beside `merge-guard review` in the same `workflow_run` list evaluated
every head twice: the CI-triggered run always finished before the gate had
published, so it could not see a review that arrived after CI (issue #23). The
review workflow is itself triggered by CI and by the review relay, so the single
trigger keeps both paths and the "merge runs when the gate publishes" property
that `docs/trusted-relay.md` owns.
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


def _workflow(name: str) -> Path:
    return ROOT / name


def test_the_merge_workflow_has_one_prerequisite_trigger():
    triggers = _triggers(_workflow(".github/workflows/merge-guard-merge.yml"))
    assert triggers["workflow_run"] == {
        "workflows": ["merge-guard review"],
        "types": ["completed"],
    }
    assert triggers["schedule"] == [{"cron": "17 * * * *"}]


def test_the_example_teaches_the_same_single_trigger():
    triggers = _triggers(_workflow("examples/merge.yml"))
    assert triggers["workflow_run"] == {
        "workflows": ["merge-guard review"],
        "types": ["completed"],
    }


def test_the_review_workflow_still_fans_both_paths_into_the_gate():
    # The single merge trigger is safe only because the review workflow itself
    # runs after CI and after a relayed review.
    real = _triggers(_workflow(".github/workflows/merge-guard-review.yml"))
    assert real["workflow_run"]["workflows"] == ["checks", "review relay"]
    example = _triggers(_workflow("examples/review.yml"))
    assert example["workflow_run"]["workflows"] == ["CI", "review relay"]
