"""The command line contract: modes, exit codes and the JSON envelope."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from merge_guard import cli, engine

POLICY = """
[repository]
name = "owner/repo"
base_branch = "main"
forge = "github"

[review]
reviewer_id = 1
reviewer_login = "reviewer[bot]"
required_checks = ["checks"]

[paths]
ordinary = ["docs/**"]
"""


@pytest.fixture()
def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "merge-guard.toml"
    path.write_text(POLICY, encoding="utf-8")
    return path


class Recorded:
    """Stands in for the forge client; records the apply flag it was built with."""

    def __init__(self) -> None:
        self.apply: bool | None = None

    def factory(self, policy, apply: bool = False) -> Recorded:
        self.apply = apply
        return self


def test_status_is_read_only(monkeypatch, policy_file, capsys):
    record = Recorded()
    calls: list[bool] = []

    def fake_review(api, policy, apply, event_head):
        calls.append(apply)
        return {"heads": {}}

    monkeypatch.setattr(cli, "GitHub", record.factory)
    monkeypatch.setattr(cli, "run_review", fake_review)
    code = cli.main(["status", "--policy", str(policy_file)])
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK
    assert record.apply is False
    assert calls == [False]
    assert out == {"mode": "status", "apply": False, "result": {"heads": {}}}


def test_status_refuses_apply(policy_file, capsys):
    code = cli.main(["status", "--apply", "--policy", str(policy_file)])
    assert code == cli.EXIT_UNUSABLE
    assert "status_is_read_only" in capsys.readouterr().err


def test_review_without_apply_is_a_dry_run(monkeypatch, policy_file, capsys):
    record = Recorded()
    monkeypatch.setattr(cli, "GitHub", record.factory)
    monkeypatch.setattr(cli, "run_review", lambda api, policy, apply, head: {"heads": {}})
    assert cli.main(["review", "--policy", str(policy_file)]) == cli.EXIT_OK
    assert record.apply is False


def test_apply_outside_a_trusted_context_is_refused(monkeypatch, policy_file, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    code = cli.main(["review", "--apply", "--policy", str(policy_file)])
    assert code == cli.EXIT_UNUSABLE
    assert "write_refused_outside_trusted_workflow" in capsys.readouterr().err


def test_an_operational_failure_exits_1(monkeypatch, policy_file, capsys):
    def broken(api, policy, apply, head):
        raise engine.ApiError("api_unavailable")

    monkeypatch.setattr(cli, "GitHub", Recorded().factory)
    monkeypatch.setattr(cli, "run_review", broken)
    code = cli.main(["status", "--policy", str(policy_file)])
    assert code == cli.EXIT_OPERATIONAL
    assert "merge_guard_refused: api_unavailable" in capsys.readouterr().err


def test_an_unknown_policy_field_is_refused(tmp_path, capsys):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY + "\n[review.extra]\nignored = true\n", encoding="utf-8")
    code = cli.main(["status", "--policy", str(path)])
    assert code == cli.EXIT_UNUSABLE
    assert "policy_unknown_field" in capsys.readouterr().err


def test_json_flag_changes_nothing(monkeypatch, policy_file, capsys):
    monkeypatch.setattr(cli, "GitHub", Recorded().factory)
    monkeypatch.setattr(cli, "run_review", lambda api, policy, apply, head: {"heads": {}})
    assert cli.main(["status", "--policy", str(policy_file)]) == cli.EXIT_OK
    plain = capsys.readouterr().out
    assert cli.main(["status", "--json", "--policy", str(policy_file)]) == cli.EXIT_OK
    assert capsys.readouterr().out == plain


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.startswith("merge-guard ")


def test_the_action_entry_point_still_runs():
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, str(root / "scripts" / "merge_guard.py"), "--version"],
        capture_output=True, text=True, cwd=root, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("merge-guard ")


def test_an_unreadable_policy_encoding_is_refused(tmp_path, capsys):
    path = tmp_path / "policy.toml"
    path.write_bytes(b'[repository]\nname = "b\xffd"\n')
    code = cli.main(["status", "--policy", str(path)])
    assert code == cli.EXIT_UNUSABLE
    assert "policy_not_utf8" in capsys.readouterr().err


def test_a_negative_review_verdict_still_exits_zero(monkeypatch, policy_file, capsys):
    head = "a" * 40
    monkeypatch.setattr(cli, "GitHub", Recorded().factory)
    monkeypatch.setattr(
        cli, "run_review",
        lambda api, policy, apply, event_head: {"heads": {head: ["pending", "current_head_approval_required"]}})
    assert cli.main(["status", "--policy", str(policy_file)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {
        "mode": "status", "apply": False,
        "result": {"heads": {head: ["pending", "current_head_approval_required"]}}}


def test_the_merge_envelope_lists_decisions(monkeypatch, policy_file, capsys):
    decision = {"number": 7, "head": "b" * 40, "eligible": False,
                "merged": False, "reason": "shared_head"}
    monkeypatch.setattr(cli, "GitHub", Recorded().factory)
    monkeypatch.setattr(cli, "run_merge", lambda api, policy, apply: [decision])
    assert cli.main(["merge", "--policy", str(policy_file)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {
        "mode": "merge", "apply": False, "result": [decision]}


def test_the_action_combinations_reach_the_trusted_context_check(monkeypatch, policy_file, capsys):
    # The action always passes --apply. review and merge must therefore be
    # accepted by the parser and refused by the trust check, not by argparse.
    for name in ("GITHUB_REPOSITORY", "GITHUB_REF", "GITHUB_REF_PROTECTED",
                 "GITHUB_EVENT_NAME", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(name, raising=False)
    for mode in ("review", "merge"):
        code = cli.main([mode, "--apply", "--policy", str(policy_file)])
        assert code == cli.EXIT_UNUSABLE
        assert "write_refused_outside_trusted_workflow" in capsys.readouterr().err
    # status is CLI-only, and the documented refusal is what the action gets.
    code = cli.main(["status", "--apply", "--policy", str(policy_file)])
    assert code == cli.EXIT_UNUSABLE
    assert "status_is_read_only" in capsys.readouterr().err
