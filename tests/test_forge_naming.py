"""The policy names its forge; nothing defaults one.

The composite action names GitHub on its run line, so a policy file written
before the key existed keeps working there. The command line and the library
refuse when nothing names the forge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from merge_guard import cli, forgejo
from merge_guard import policy as mg

ROOT = Path(__file__).resolve().parents[1]

POLICY = """
[repository]
name = "owner/repo"
base_branch = "main"
{forge}
[review]
reviewer_id = 1
reviewer_login = "reviewer[bot]"
required_checks = ["checks"]
"""

TRUSTED = {
    "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": "schedule",
    "GITHUB_REF": "refs/heads/main", "GITHUB_REF_PROTECTED": "true",
    "GITHUB_WORKFLOW_REF": "owner/repo/.github/workflows/merge-guard-review.yml@refs/heads/main",
}


def write(tmp_path: Path, forge: str | None) -> str:
    path = tmp_path / "merge-guard.toml"
    path.write_text(POLICY.format(forge=f'forge = "{forge}"' if forge else ""), encoding="utf-8")
    return str(path)


class Built:
    def __init__(self):
        self.forges: list[str] = []

    def github(self, policy, apply=False):
        self.forges.append(policy.forge)
        return self


def test_a_policy_file_without_a_forge_still_loads():
    data = {"repository": {"name": "o/r", "base_branch": "main"},
            "review": {"reviewer_id": 1, "reviewer_login": "r[bot]", "required_checks": ["c"]}}
    assert mg.load_policy(data).forge is None


def test_an_unknown_forge_is_refused():
    data = {"repository": {"name": "o/r", "base_branch": "main", "forge": "gitlab"},
            "review": {"reviewer_id": 1, "reviewer_login": "r[bot]", "required_checks": ["c"]}}
    with pytest.raises(mg.Unusable, match="policy_forge_invalid"):
        mg.load_policy(data)


def test_require_forge_has_no_default():
    unnamed = mg.load_policy({"repository": {"name": "o/r", "base_branch": "main"},
                              "review": {"reviewer_id": 1, "reviewer_login": "r[bot]",
                                         "required_checks": ["c"]}})
    with pytest.raises(mg.Unusable, match="policy_forge_missing"):
        mg.require_forge(unnamed)
    assert mg.require_forge(unnamed, "github").forge == "github"
    named = mg.require_forge(unnamed, "forgejo")
    assert mg.require_forge(named, "forgejo").forge == "forgejo"
    with pytest.raises(mg.Unusable, match="policy_forge_conflict"):
        mg.require_forge(named, "github")


def test_the_command_line_refuses_a_policy_that_names_no_forge(tmp_path, capsys):
    code = cli.main(["status", "--policy", write(tmp_path, None)])
    assert code == cli.EXIT_UNUSABLE
    assert "merge_guard_refused: policy_forge_missing" in capsys.readouterr().err


def test_the_forge_flag_names_it_for_an_older_policy(tmp_path, monkeypatch, capsys):
    built = Built()
    monkeypatch.setattr(cli, "GitHub", built.github)
    monkeypatch.setattr(cli, "run_review", lambda api, policy, apply, head: {"heads": {}})
    assert cli.main(["status", "--forge", "github", "--policy", write(tmp_path, None)]) == cli.EXIT_OK
    assert built.forges == ["github"]


def test_a_flag_that_contradicts_the_policy_is_refused(tmp_path, capsys):
    code = cli.main(["status", "--forge", "github", "--policy", write(tmp_path, "forgejo")])
    assert code == cli.EXIT_UNUSABLE
    assert "merge_guard_refused: policy_forge_conflict" in capsys.readouterr().err


def test_a_forgejo_policy_runs_against_the_configured_instance(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("FORGEJO_URL", "https://forgejo.example")
    monkeypatch.setenv("FORGEJO_TOKEN", "t")
    monkeypatch.setattr(cli, "run_review",
                        lambda api, policy, apply, head: seen.append(api) or {"heads": {}})
    assert cli.main(["status", "--policy", write(tmp_path, "forgejo")]) == cli.EXIT_OK
    assert isinstance(seen[0], forgejo.Forgejo)
    assert seen[0].root == "https://forgejo.example/api/v1"


def test_a_forgejo_run_without_an_instance_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    code = cli.main(["status", "--policy", write(tmp_path, "forgejo")])
    assert code == cli.EXIT_UNUSABLE
    assert "merge_guard_refused: forgejo_url_missing" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["https://[", "https://forgejo.example:99999",
                                 "https://forgejo.example:0", "http://@localhost:3000",
                                 "http://:@127.0.0.1:3000", "https://forgejo.example?",
                                 "https://forgejo.example#", "http://local\thost:3000",
                                 "\x01http://localhost:3000", "https://forgejo.example%3A0"])
def test_a_malformed_forgejo_url_is_an_unusable_input(tmp_path, monkeypatch, capsys, url):
    # The refusal comes before any request, as an input refusal and not a traceback.
    monkeypatch.setenv("FORGEJO_URL", url)
    monkeypatch.setenv("FORGEJO_TOKEN", "t")
    monkeypatch.setattr(cli, "run_review", None)
    code = cli.main(["status", "--policy", write(tmp_path, "forgejo")])
    assert code == cli.EXIT_UNUSABLE
    assert capsys.readouterr().err == "merge_guard_refused: forgejo_url_invalid\n"


@pytest.mark.parametrize("mode", ["review", "merge"])
@pytest.mark.parametrize("trusted", [True, False])
def test_the_command_line_writes_to_github_only(tmp_path, monkeypatch, capsys, mode, trusted):
    # Even a context that GitHub's trust check accepts does not authorise a
    # Forgejo write: that trust argument is about GitHub workflows. The refusal
    # has its own code, because no context can make this write.
    for name, value in TRUSTED.items():
        if trusted:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    if mode == "merge" and trusted:
        monkeypatch.setenv("GITHUB_WORKFLOW_REF",
                           "owner/repo/.github/workflows/merge-guard-merge.yml@refs/heads/main")
    monkeypatch.setenv("FORGEJO_URL", "https://forgejo.example")
    monkeypatch.setenv("FORGEJO_TOKEN", "t")
    monkeypatch.setattr(cli, "Forgejo", None)  # the refusal comes before any adapter
    code = cli.main([mode, "--apply", "--policy", write(tmp_path, "forgejo")])
    assert code == cli.EXIT_UNUSABLE
    err = capsys.readouterr().err
    assert err == "merge_guard_refused: forgejo_write_unsupported\n"
    assert "write_refused_outside_trusted_workflow" not in err


def test_a_github_write_outside_the_trusted_context_keeps_its_code(tmp_path, monkeypatch, capsys):
    for name in TRUSTED:
        monkeypatch.delenv(name, raising=False)
    code = cli.main(["review", "--apply", "--policy", write(tmp_path, "github")])
    assert code == cli.EXIT_UNUSABLE
    assert capsys.readouterr().err == "write_refused_outside_trusted_workflow\n"


def test_the_action_names_github_on_its_run_line():
    action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    runs = [step["run"] for step in action["runs"]["steps"] if "scripts/merge_guard.py" in step.get("run", "")]
    assert len(runs) == 1
    assert "--forge github" in runs[0]


def test_the_document_covers_the_forge_key_and_its_refusals():
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    for needle in ("`forge`", "--forge", "policy_forge_missing", "policy_forge_conflict",
                   "policy_forge_invalid", "forgejo_write_unsupported", "FORGEJO_URL",
                   "FORGEJO_TOKEN"):
        assert needle in text, needle
