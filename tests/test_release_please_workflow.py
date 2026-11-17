"""The release workflow regenerates the release manifest on the release branch.

release-please's release pull request always edits ``merge_guard/__init__.py``
(``__version__``), a file of the release closure. ``release-manifest.json`` is
therefore stale on that branch, and ``checks`` fails until somebody regenerates
it. The workflow does that after the action, so no person has to.

It uses three jobs, because the steps of one job share one runner: repository
code and the personal access token must never run on the same runner. These
tests pin:

* the exact jobs, the exact steps of each, how the jobs are chained, and what
  decides whether each one runs;
* where the personal access token appears: the action, and the step that pushes.
  Never the job that runs repository code;
* that the job that holds the token runs no file of its checkout;
* that every action is pinned by full SHA;
* what each step script does, run in bash against local repositories and a stub
  ``gh``. The handoff between the jobs is done as GitHub does it: the second job
  reports strings, and the third job starts from a fresh clone and those strings
  alone.

A real run on GitHub needs an open release pull request, so none of this proves
that the ``needs``, ``outputs`` and ``if`` expressions evaluate there. The first
release pull request is the first real run.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from scripts import release_manifest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"

PINNED_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")
RELEASE_BRANCH = "release-please--branches--main--components--merge-guard"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
SUBJECT = "chore(release): regenerate the release manifest"
# Not a credential: a marker that must appear in no argument and no config file.
FAKE_TOKEN = "synthetic-secret-value"
HASH = "0123456789abcdef" * 2 + "01234567"
# The jobs in order, and every step of each in order. A step that is not in this
# table, or one that is missing from the workflow, fails the first test below.
JOBS = {
    "release-please": ["release", "target"],
    "regenerate": ["checkout", "regenerate"],
    "push": ["checkout", "commit", "push"],
}
# The outputs that the pinned release-please action sets (v5.0.0, src/index.ts):
# `pr` is set only in a run that created or updated the pull request.
ACTION_OUTPUTS = {"pr"}


def _requires(*tools: str):
    missing = [tool for tool in tools if shutil.which(tool) is None]
    return pytest.mark.skipif(
        bool(missing), reason="needs " + ", ".join(missing) + " on the PATH"
    )


NEEDS = _requires("bash", "git", "jq", "base64")


def _document() -> dict:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _job(name: str) -> dict:
    return _document()["jobs"][name]


def _step(job: str, step_id: str) -> dict:
    found = [step for step in _job(job)["steps"] if step.get("id") == step_id]
    assert len(found) == 1, f"exactly one step of {job!r} must carry the id {step_id!r}"
    return found[0]


def _all_steps() -> list[tuple[str, dict]]:
    return [(name, step) for name, job in _document()["jobs"].items() for step in job["steps"]]


def _leaves(node: object, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Every string of the parsed workflow, with the keys that lead to it.

    A step is named by its id, so a path stays readable and does not depend on
    where the step sits.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            label = str(item["id"]) if isinstance(item, dict) and "id" in item else str(index)
            yield from _leaves(item, (*path, label))
    elif isinstance(node, str):
        yield path, node


def _holders(needle: str) -> set[tuple[str, str]]:
    """The (job, step id) pairs that contain *needle* anywhere: key, value or script."""
    found = set()
    for path, value in _leaves(_document()):
        if needle in value or any(needle in part for part in path):
            assert path[0] == "jobs", f"{needle!r} must appear only inside a job: {path}"
            found.add((path[1], path[3]) if path[2] == "steps" else (path[1], ""))
    return found


# -- structure ---------------------------------------------------------------


def test_the_workflow_has_exactly_these_jobs_and_these_steps():
    """Pin every step, not only the ones that matter.

    A step inserted between two named steps shares the runner with them. A
    token-free step before the push could point git at another server, and the
    credential helper would answer for it. Pinning the whole list makes any new
    step a reviewed change to this table.
    """
    assert list(_document()["jobs"]) == list(JOBS)
    for name, ids in JOBS.items():
        assert [step.get("id") for step in _job(name)["steps"]] == ids, name


def test_the_first_job_runs_the_action_then_reports_the_release_branch():
    job = _job("release-please")
    assert job["steps"][0]["uses"].startswith("googleapis/release-please-action@")
    assert "needs" not in job and "if" not in job
    assert job["outputs"] == {"branch": "${{ steps.target.outputs.branch }}"}


def test_the_jobs_are_chained_and_each_runs_only_when_there_is_work():
    regenerate = _job("regenerate")
    assert regenerate["needs"] == "release-please"
    assert regenerate["if"] == "needs.release-please.outputs.branch != ''"
    assert regenerate["outputs"] == {
        "changed": "${{ steps.regenerate.outputs.changed }}",
        "head": "${{ steps.regenerate.outputs.head }}",
        "manifest": "${{ steps.regenerate.outputs.manifest }}",
    }
    push = _job("push")
    # It reads the branch from the first job, so both are named.
    assert push["needs"] == ["release-please", "regenerate"]
    assert push["if"] == "needs.regenerate.outputs.changed == 'true'"
    assert "outputs" not in push


def test_only_the_push_step_has_a_condition_of_its_own():
    conditions = {
        (name, step["id"]): step["if"] for name, step in _all_steps() if "if" in step
    }
    assert conditions == {("push", "push"): "steps.commit.outputs.committed == 'true'"}


def test_every_output_that_an_expression_reads_is_declared_and_set():
    """GitHub reads an undeclared output as an empty string and skips the job.

    Nothing then fails, and the release pull request quietly stays stale.
    """
    document = _document()
    for name, job in document["jobs"].items():
        needed = job.get("needs", [])
        needed = [needed] if isinstance(needed, str) else needed
        text = json.dumps(job)
        for dependency, output in re.findall(r"needs\.([\w-]+)\.outputs\.(\w+)", text):
            assert dependency in needed, (name, dependency)
            assert output in document["jobs"][dependency].get("outputs", {}), (name, output)
        for output, expression in job.get("outputs", {}).items():
            match = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outputs\.(\w+) \}\}", expression)
            assert match, (name, output, expression)
            step = _step(name, match[1])
            assert re.search(rf'echo "{match[2]}=', step["run"]), (name, output)
        for step_id, output in re.findall(r"steps\.([\w-]+)\.outputs\.(\w+)", text):
            step = _step(name, step_id)
            if "uses" in step:
                assert output in ACTION_OUTPUTS, (name, step_id, output)
            else:
                assert re.search(rf'echo "{output}=', step["run"]), (name, step_id, output)


def test_the_target_step_reads_only_the_pr_output_of_the_action():
    """The action sets `pr` only in a run that created or updated the pull request.

    The pinned action (v5.0.0, src/index.ts, outputPRs) sets `pr`, the first
    pull request as JSON, only when its run created or updated a pull request.
    release-please rewrites the branch in exactly that case. A run that finds the
    pull request unchanged sets nothing, so a re-run after a failed push, and a
    manual dispatch, would repair nothing. The step therefore falls back to the
    list of open pull requests, and runs on every workflow run.
    """
    assert set(re.findall(r"steps\.release\.outputs\.(\w+)", WORKFLOW.read_text())) == {"pr"}
    target = _step("release-please", "target")
    assert "if" not in target
    assert target["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "REPORTED_PR": "${{ steps.release.outputs.pr }}",
    }


def test_every_checkout_takes_the_release_branch_and_stores_no_credential():
    checkouts = [(name, step) for name, step in _all_steps() if "uses" in step and step["uses"].startswith("actions/checkout@")]
    assert [name for name, _ in checkouts] == ["regenerate", "push"]
    for _, step in checkouts:
        assert step["with"] == {
            "ref": "${{ needs.release-please.outputs.branch }}",
            "persist-credentials": False,
        }


def test_the_job_token_only_reads_and_each_job_asks_for_one_read():
    document = _document()
    # Nothing by default: a job added later has to ask.
    assert document["permissions"] == {}
    assert {name: job["permissions"] for name, job in document["jobs"].items()} == {
        "release-please": {"pull-requests": "read"},
        "regenerate": {"contents": "read"},
        "push": {"contents": "read"},
    }


def test_the_personal_access_token_is_in_the_action_and_the_push_step_only():
    assert _holders("secrets") == {("release-please", "release"), ("push", "push")}
    assert _holders("RELEASE_PLEASE_TOKEN") == {("release-please", "release"), ("push", "push")}
    # Exactly two references, one per holder.
    references = [path for path, value in _leaves(_document()) if "secrets." in value]
    assert references == [
        ("jobs", "release-please", "steps", "release", "with", "token"),
        ("jobs", "push", "steps", "push", "env", "RELEASE_PLEASE_TOKEN"),
    ]
    assert _step("push", "push")["env"] == {
        "RELEASE_PLEASE_TOKEN": "${{ secrets.RELEASE_PLEASE_TOKEN }}",
        "BRANCH": "${{ needs.release-please.outputs.branch }}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    # The commit step comes before the push step and has no token of its own.
    assert "RELEASE_PLEASE_TOKEN" not in json.dumps(_step("push", "commit"))


def test_the_job_token_appears_only_in_the_lookup_of_the_open_pull_request():
    assert _holders("github.token") == {("release-please", "target")}
    assert _holders("GITHUB_TOKEN") == set()
    assert _holders("GH_TOKEN") == {("release-please", "target")}


def test_the_job_that_runs_repository_code_references_no_secret_and_no_token():
    job = _job("regenerate")
    text = json.dumps(job).lower()
    for forbidden in ("secret", "token"):
        assert forbidden not in text, forbidden
    step = _step("regenerate", "regenerate")
    assert "credential" not in step["run"]
    assert "git config" not in step["run"]
    assert set(step["env"]) == {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    }
    assert {step["env"][name] for name in step["env"] if name.endswith("_NAME")} == {BOT_NAME}
    assert {step["env"][name] for name in step["env"] if name.endswith("_EMAIL")} == {BOT_EMAIL}


def test_the_job_that_holds_the_token_runs_no_file_of_its_checkout():
    """It runs only commands that are written in the workflow file.

    The regenerate job already ran repository code, so everything it reports is
    data here: checked, written to one path, never run.
    """
    for step in _job("push")["steps"]:
        script = step.get("run", "")
        for forbidden in ("python", "scripts/", "./", "source ", "pytest", "pip ", "make ", "uv "):
            assert forbidden not in script, (step["id"], forbidden)
    for name in ("REGENERATED_FROM", "MANIFEST"):
        assert _step("push", "commit")["env"][name].startswith("${{ needs.regenerate.outputs.")


def test_no_workflow_or_job_environment_carries_anything():
    document = _document()
    # A workflow or job `env` reaches every step of the job.
    assert "env" not in document
    for name, job in document["jobs"].items():
        assert "env" not in job, name
        assert "defaults" not in job, name


def test_no_script_expands_a_workflow_expression():
    """A value reaches a script through `env`, never through `${{ }}` in its text."""
    for name, step in _all_steps():
        assert "${{" not in step.get("run", ""), (name, step["id"])


def test_every_script_runs_in_bash_with_pipefail_and_no_step_may_fail_silently():
    for name, step in _all_steps():
        assert "continue-on-error" not in step, (name, step["id"])
        if "run" in step:
            # `shell: bash` is `bash -eo pipefail`; the target step relies on pipefail.
            assert step["shell"] == "bash", (name, step["id"])


def test_every_action_is_pinned_by_full_sha_with_its_version_in_a_comment():
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if re.match(r"\s*(?:- )?uses:", line)]
    assert len(lines) == 3
    for line in lines:
        assert re.fullmatch(
            r"\s*(?:- )?uses: [\w./-]+@[0-9a-f]{40}\s+# v\d+\.\d+\.\d+", line
        ), line
    used = [step["uses"] for _, step in _all_steps() if "uses" in step]
    assert all(PINNED_ACTION.fullmatch(value) for value in used), used
    # The runner image carries Python, git, jq and gh, so no setup action is needed.
    assert sorted(value.partition("@")[0] for value in used) == [
        "actions/checkout",
        "actions/checkout",
        "googleapis/release-please-action",
    ]


# -- running the step scripts ------------------------------------------------

_RUNS = itertools.count()


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _bin(tmp_path: Path) -> Path:
    """A directory for stubs that comes first on the PATH, with `python3` in it."""
    directory = tmp_path / "bin"
    if not directory.exists():
        directory.mkdir()
        _executable(
            directory / "python3",
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        )
    return directory


def _runner_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return {
        "PATH": os.pathsep.join([str(_bin(tmp_path)), os.environ.get("PATH", os.defpath)]),
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def _read_outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def _run(
    job: str,
    step_id: str,
    cwd: Path,
    tmp_path: Path,
    supplied: dict[str, str] | None = None,
    runner: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run a step's script as GitHub Actions runs `shell: bash`.

    The step's literal `env` entries come from the workflow itself. The values of
    its expression entries are *supplied*, and the test must supply exactly those.
    """
    step = _step(job, step_id)
    declared = step.get("env", {})
    expressions = {name for name, value in declared.items() if "${{" in str(value)}
    assert set(supplied or {}) == expressions
    run_id = next(_RUNS)
    script = tmp_path / f"{job}-{step_id}-{run_id}.sh"
    script.write_text(step["run"], encoding="utf-8")
    output = tmp_path / f"{job}-{step_id}-{run_id}.output"
    environment = {
        **_runner_env(tmp_path),
        **(runner or {}),
        **{name: str(value) for name, value in declared.items() if name not in expressions},
        **(supplied or {}),
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, _read_outputs(output)


# -- the target step ---------------------------------------------------------


def _gh(tmp_path: Path, listing: object, status: int = 0) -> Path:
    """A stub `gh` that logs its arguments and prints *listing*."""
    log = tmp_path / "gh.log"
    body = tmp_path / "gh-body.json"
    body.write_text(listing if isinstance(listing, str) else json.dumps(listing), encoding="utf-8")
    _executable(
        _bin(tmp_path) / "gh",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        f"cat {shlex.quote(str(body))}\n"
        f"exit {status}\n",
    )
    return log


def _pull(ref: str, repo: str | None = "owner/repo") -> dict:
    return {"head": {"ref": ref, "repo": None if repo is None else {"full_name": repo}}}


def _reported(branch: str = RELEASE_BRANCH, number: int = 12) -> str:
    return json.dumps(
        {
            "headBranchName": branch,
            "baseBranchName": "main",
            "number": number,
            "title": "chore(main): release 0.2.2",
            "body": ":robot: synthetic",
            "labels": ["autorelease: pending"],
            "files": [],
        }
    )


def _target(tmp_path: Path, reported: str, listing: object = (), status: int = 0):
    log = _gh(tmp_path, list(listing) if not isinstance(listing, str) else listing, status)
    result, outputs = _run(
        "release-please",
        "target",
        tmp_path,
        tmp_path,
        supplied={"GH_TOKEN": "unused", "REPORTED_PR": reported},
        runner={"GITHUB_REPOSITORY": "owner/repo"},
    )
    return result, outputs, log


@_requires("bash", "jq")
def test_target_takes_the_branch_the_action_reported_without_a_lookup(tmp_path):
    result, outputs, log = _target(tmp_path, _reported())
    assert result.returncode == 0, result.stderr
    assert outputs == {"branch": RELEASE_BRANCH}
    assert not log.exists(), "the action's own report needs no lookup"


@_requires("bash", "jq")
def test_target_ignores_a_reported_pull_request_that_was_never_created(tmp_path):
    """release-please can report `number: 0` when it detected no code change."""
    listing = [_pull(RELEASE_BRANCH)]
    result, outputs, log = _target(tmp_path, _reported(branch="release-please--branches--ghost", number=0), listing)
    assert result.returncode == 0, result.stderr
    assert outputs == {"branch": RELEASE_BRANCH}
    assert log.read_text(encoding="utf-8").count("\n") == 1


@_requires("bash", "jq")
def test_target_finds_the_open_release_pull_request_when_the_action_reports_none(tmp_path):
    listing = [
        _pull("feature/unrelated"),
        _pull(RELEASE_BRANCH, repo="someone-else/repo"),
        _pull(RELEASE_BRANCH, repo=None),
        _pull(RELEASE_BRANCH),
    ]
    result, outputs, log = _target(tmp_path, "", listing)
    assert result.returncode == 0, result.stderr
    assert outputs == {"branch": RELEASE_BRANCH}
    assert "repos/owner/repo/pulls?state=open" in log.read_text(encoding="utf-8")


@_requires("bash", "jq")
def test_target_has_no_branch_when_no_release_pull_request_is_open(tmp_path):
    for listing in ([], [_pull("feature/unrelated")]):
        result, outputs, _ = _target(tmp_path, "", listing)
        assert result.returncode == 0, result.stderr
        assert outputs == {"branch": ""}


@_requires("bash", "jq")
def test_target_refuses_two_open_release_pull_requests(tmp_path):
    listing = [_pull(RELEASE_BRANCH), _pull("release-please--branches--main--components--other")]
    result, outputs, _ = _target(tmp_path, "", listing)
    assert result.returncode != 0
    assert "more than one open release pull request" in result.stderr
    assert "branch" not in outputs


@_requires("bash", "jq")
def test_target_fails_when_the_lookup_fails_instead_of_finding_nothing(tmp_path):
    result, outputs, _ = _target(tmp_path, "", "", status=1)
    assert result.returncode != 0
    assert "branch" not in outputs


@_requires("bash", "jq")
@pytest.mark.parametrize(
    "name",
    [
        "main",
        "release-please--branches",
        "release-please--branches--",
        "release-please--branches--main;id",
        "release-please--branches--$(id)",
        "release-please--branches--main y",
        "release-please--branches--a/../b",
        "refs/heads/release-please--branches--main",
    ],
)
def test_target_refuses_a_branch_that_is_not_a_release_branch(tmp_path, name):
    result, outputs, _ = _target(tmp_path, _reported(branch=name))
    assert result.returncode != 0
    assert "not a release-please branch name" in result.stdout
    assert "branch" not in outputs


# -- the regenerate job ------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", message,
    )  # fmt: skip


def _release_repo(tmp_path: Path, *, bumped: bool) -> Path:
    """A repository shaped like a release branch: a closure, its manifest, a version.

    With *bumped* the last commit changes ``__version__``, as the release pull
    request does, so the manifest no longer describes the tree.
    """
    root = tmp_path / "work"
    (root / "scripts").mkdir(parents=True)
    (root / "merge_guard").mkdir()
    shutil.copy(ROOT / "scripts" / "release_manifest.py", root / "scripts" / "release_manifest.py")
    (root / "scripts" / "assurance.py").write_text("print('classify')\n", encoding="utf-8")
    init = root / "merge_guard" / "__init__.py"
    init.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    _git(root, "init", "-q")
    _commit_all(root, "closure")
    files = {}
    for relative in ("merge_guard/__init__.py", "scripts/assurance.py"):
        sha256, size = release_manifest.digest(root / relative)
        files[relative] = {"sha256": sha256, "bytes": size}
    manifest = {
        "schema": release_manifest.SCHEMA,
        "product": release_manifest.PRODUCT,
        "entrypoints": ["scripts/assurance.py"],
        "files": files,
    }
    (root / release_manifest.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    _commit_all(root, "manifest")
    if bumped:
        init.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
        _commit_all(root, "chore(main): release 0.2.0")
    return root


def _regenerate(root: Path, tmp_path: Path):
    return _run("regenerate", "regenerate", root, tmp_path)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@NEEDS
def test_regenerate_reports_the_head_it_read_and_the_new_manifest(tmp_path):
    root = _release_repo(tmp_path, bumped=True)
    findings = release_manifest.check(root)
    assert any(finding.startswith("merge_guard/__init__.py:") for finding in findings), findings
    release_head = _head(root)

    result, outputs = _regenerate(root, tmp_path)
    assert result.returncode == 0, result.stderr
    assert set(outputs) == {"changed", "head", "manifest"}
    assert outputs["changed"] == "true"
    assert outputs["head"] == release_head
    # One line of base64, as a job output carries it: the bytes that passed `check`.
    assert re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", outputs["manifest"])
    reported = base64.b64decode(outputs["manifest"], validate=True)
    assert reported == (root / "release-manifest.json").read_bytes()
    assert release_manifest.check(root) == []
    assert json.loads(reported)["files"]["merge_guard/__init__.py"]["bytes"] == len(
        (root / "merge_guard" / "__init__.py").read_bytes()
    )


@NEEDS
def test_regenerate_reports_nothing_to_do_when_the_manifest_is_current(tmp_path):
    root = _release_repo(tmp_path, bumped=False)
    before = _head(root)
    result, outputs = _regenerate(root, tmp_path)
    assert result.returncode == 0, result.stderr
    assert outputs == {"changed": "false"}
    assert _head(root) == before


@NEEDS
def test_regenerate_fails_without_reporting_when_the_closure_is_broken(tmp_path):
    root = _release_repo(tmp_path, bumped=True)
    _git(root, "rm", "-q", "scripts/assurance.py")
    _commit_all(root, "remove a closure file")
    before = _head(root)
    result, outputs = _regenerate(root, tmp_path)
    assert result.returncode != 0
    assert "scripts/assurance.py" in result.stderr
    assert "changed" not in outputs, "the push job must not run"
    assert _head(root) == before


# -- the push job ------------------------------------------------------------


class Stage(NamedTuple):
    remote: Path  # a bare repository that holds the release branch at the release head
    clone: Path  # the push job's checkout of that branch
    outputs: dict[str, str]  # what the regenerate job reported
    release_head: str


def _stage(tmp_path: Path, *, bumped: bool = True) -> Stage:
    """What the push job starts from.

    The regenerate job runs on one checkout and reports strings. The push job
    gets a fresh depth-1 clone of the release branch, as actions/checkout makes
    it, and nothing else of the regenerate job.
    """
    work = _release_repo(tmp_path, bumped=bumped)
    release_head = _head(work)
    remote = tmp_path / "server" / "owner" / "repo.git"
    remote.mkdir(parents=True)
    _git(remote, "init", "-q", "--bare")
    main = "HEAD~1" if bumped else "HEAD"
    _git(work, "push", "-q", str(remote), f"HEAD:refs/heads/{RELEASE_BRANCH}", f"{main}:refs/heads/main")
    result, outputs = _regenerate(work, tmp_path)
    assert result.returncode == 0, result.stderr
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", RELEASE_BRANCH, f"file://{remote}", str(clone))
    return Stage(remote, clone, outputs, release_head)


def _commit(stage: Stage, tmp_path: Path, **override: str):
    supplied = {
        "REGENERATED_FROM": stage.outputs.get("head", ""),
        "MANIFEST": stage.outputs.get("manifest", ""),
        **override,
    }
    return _run("push", "commit", stage.clone, tmp_path, supplied=supplied)


def _git_wrapper(tmp_path: Path, *, stub_push: bool = False) -> Path:
    """Put a `git` on the PATH that logs one argument per line, then runs the real one.

    With *stub_push* a `push` is logged and not run, so a test can name any server.
    """
    log = tmp_path / "git.log"
    real = shutil.which("git")
    assert real is not None
    skip = 'for argument in "$@"; do [ "$argument" = push ] && exit 0; done\n' if stub_push else ""
    _executable(
        _bin(tmp_path) / "git",
        "#!/bin/sh\n"
        f"for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done >> {shlex.quote(str(log))}\n"
        f"printf '%s\\n' '---' >> {shlex.quote(str(log))}\n"
        f"{skip}"
        f"exec {shlex.quote(real)} \"$@\"\n",
    )
    return log


def _push(stage: Stage, tmp_path: Path, branch: str = RELEASE_BRANCH, *, server: str | None = None, stub_push: bool = False):
    log = _git_wrapper(tmp_path, stub_push=stub_push)
    result, _ = _run(
        "push",
        "push",
        stage.clone,
        tmp_path,
        supplied={"RELEASE_PLEASE_TOKEN": FAKE_TOKEN, "BRANCH": branch},
        runner={
            "GITHUB_SERVER_URL": server or f"file://{stage.remote.parents[1]}",
            "GITHUB_REPOSITORY": "owner/repo",
        },
    )
    return result, log


def _refs(remote: Path) -> dict[str, str]:
    listing = _git(remote, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    return dict(line.split(" ") for line in listing.splitlines())


@NEEDS
def test_commit_makes_one_ordinary_commit_of_the_manifest_alone_as_the_bot(tmp_path):
    stage = _stage(tmp_path)
    clone = stage.clone
    result, outputs = _commit(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    assert outputs == {"committed": "true"}
    assert release_manifest.check(clone) == []
    assert _git(clone, "status", "--porcelain").stdout == ""

    # One ordinary commit on top of the release head, never a merge.
    assert _git(clone, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[1:] == [stage.release_head]
    shown = _git(clone, "log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%s").stdout.splitlines()
    assert shown == [BOT_NAME, BOT_EMAIL, BOT_NAME, BOT_EMAIL, SUBJECT]
    # It touches the manifest alone, and keeps its mode.
    (line,) = _git(clone, "diff-tree", "--no-commit-id", "-r", "HEAD").stdout.splitlines()
    meta, path = line.split("\t")
    assert meta.split()[:2] == [":100644", "100644"] and meta.split()[4] == "M"
    assert path == "release-manifest.json"
    # The bytes are the ones the regenerate job reported.
    assert _git(clone, "show", "HEAD:release-manifest.json").stdout.encode() == base64.b64decode(stage.outputs["manifest"])


@NEEDS
def test_commit_commits_nothing_when_the_branch_already_carries_the_manifest(tmp_path):
    stage = _stage(tmp_path, bumped=False)
    current = (stage.clone / "release-manifest.json").read_bytes()
    result, outputs = _commit(
        stage,
        tmp_path,
        REGENERATED_FROM=_head(stage.clone),
        MANIFEST=base64.b64encode(current).decode(),
    )
    assert result.returncode == 0, result.stderr
    assert outputs == {"committed": "false"}
    assert _head(stage.clone) == stage.release_head
    assert _git(stage.clone, "status", "--porcelain").stdout == ""


def _refused(stage: Stage, tmp_path: Path, message: str, **override: str) -> None:
    """The commit step fails with *message*, commits nothing, and leaves the manifest alone."""
    before = (stage.clone / "release-manifest.json").read_bytes()
    head = _head(stage.clone)
    result, outputs = _commit(stage, tmp_path, **override)
    assert result.returncode != 0
    assert message in result.stdout, result.stdout
    assert "committed" not in outputs
    assert _head(stage.clone) == head
    assert (stage.clone / "release-manifest.json").read_bytes() == before


@NEEDS
@pytest.mark.parametrize(
    "head, message",
    [
        ("", "full commit hash"),
        ("main", "full commit hash"),
        ("HEAD", "full commit hash"),
        ("deadbeef", "full commit hash"),
        (HASH.upper(), "full commit hash"),
        (HASH + "0", "full commit hash"),
        # A well-formed hash that is not the checked-out head: the branch moved.
        ("0" * 40, "moved after the manifest was generated"),
    ],
)
def test_commit_refuses_a_head_that_is_not_the_checked_out_head(tmp_path, head, message):
    _refused(_stage(tmp_path), tmp_path, message, REGENERATED_FROM=head)


@NEEDS
@pytest.mark.parametrize(
    "manifest, message",
    [
        ("", "expected form"),
        ("not base64!", "expected form"),
        ("abc$", "expected form"),
        ("QUJD\nREVG", "expected form"),
        ("====", "expected form"),
        ("QUJD====", "expected form"),
        ("A" * 65537, "expected form"),
        # The right alphabet, but not base64: one character is not a whole group.
        ("A", "not valid base64"),
    ],
)
def test_commit_refuses_a_manifest_that_is_not_bounded_base64(tmp_path, manifest, message):
    stage = _stage(tmp_path)
    _refused(stage, tmp_path, message, MANIFEST=manifest, REGENERATED_FROM=_head(stage.clone))


@NEEDS
@pytest.mark.parametrize(
    "payload",
    [b"hello", b"[]", b"1", b"{}", b'{"files": []}', b'{"files": null}', b'"files"', b"\xff\xfe"],
)
def test_commit_refuses_bytes_that_are_not_a_manifest_object(tmp_path, payload):
    stage = _stage(tmp_path)
    _refused(
        stage,
        tmp_path,
        "not a JSON object with a files object",
        MANIFEST=base64.b64encode(payload).decode(),
    )


@NEEDS
def test_commit_never_writes_through_a_link_that_the_commit_holds(tmp_path):
    """A link at the manifest path would send the bytes to another file, `.git/config` here."""
    stage = _stage(tmp_path)
    config = stage.clone / ".git" / "config"
    before = config.read_bytes()
    (stage.clone / "release-manifest.json").unlink()
    (stage.clone / "release-manifest.json").symlink_to(".git/config")
    _commit_all(stage.clone, "plant a link")
    result, outputs = _commit(stage, tmp_path, REGENERATED_FROM=_head(stage.clone))
    assert result.returncode != 0
    assert "not a regular file" in result.stdout
    assert "committed" not in outputs
    assert config.read_bytes() == before


@NEEDS
def test_commit_never_writes_through_a_link_that_only_the_working_tree_holds(tmp_path):
    stage = _stage(tmp_path)
    config = stage.clone / ".git" / "config"
    before = config.read_bytes()
    (stage.clone / "release-manifest.json").unlink()
    (stage.clone / "release-manifest.json").symlink_to(".git/config")
    result, outputs = _commit(stage, tmp_path)
    assert result.returncode != 0
    assert "not a regular file" in result.stdout
    assert "committed" not in outputs
    assert config.read_bytes() == before


@NEEDS
def test_commit_refuses_a_branch_that_does_not_track_the_manifest(tmp_path):
    stage = _stage(tmp_path)
    _git(stage.clone, "rm", "-q", "release-manifest.json")
    _commit_all(stage.clone, "drop the manifest")
    result, outputs = _commit(stage, tmp_path, REGENERATED_FROM=_head(stage.clone))
    assert result.returncode != 0
    assert "not a regular file" in result.stdout
    assert "committed" not in outputs
    assert not (stage.clone / "release-manifest.json").exists()


@NEEDS
def test_push_sends_only_the_regeneration_commit_to_the_release_branch(tmp_path):
    stage = _stage(tmp_path)
    assert _commit(stage, tmp_path)[0].returncode == 0
    main = _refs(stage.remote)["refs/heads/main"]
    result, log = _push(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    assert _refs(stage.remote) == {
        f"refs/heads/{RELEASE_BRANCH}": _head(stage.clone),
        "refs/heads/main": main,
    }
    assert (stage.clone / ".git" / "shallow").exists(), "the checkout is shallow, as actions/checkout makes it"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert f"HEAD:refs/heads/{RELEASE_BRANCH}" in lines
    # A plain push: never a forced one.
    assert not [line for line in lines if line in {"--force", "-f", "--force-with-lease"} or line.startswith("+")]


@NEEDS
@pytest.mark.parametrize("move", ["rewrite", "advance"])
def test_push_is_rejected_when_the_release_branch_moved_after_the_checkout(tmp_path, move):
    """release-please rewrites the branch, or a person adds to it: the push must lose."""
    stage = _stage(tmp_path)
    assert _commit(stage, tmp_path)[0].returncode == 0
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "--branch", RELEASE_BRANCH, f"file://{stage.remote}", str(other))
    if move == "rewrite":
        _git(other, "reset", "-q", "--hard", "HEAD~1")
    (other / "notes.txt").write_text(move, encoding="utf-8")
    _commit_all(other, f"the branch moves: {move}")
    _git(other, "push", "-q", "--force", str(stage.remote), f"HEAD:refs/heads/{RELEASE_BRANCH}")
    moved = _refs(stage.remote)[f"refs/heads/{RELEASE_BRANCH}"]
    result, _ = _push(stage, tmp_path)
    assert result.returncode != 0
    assert _refs(stage.remote)[f"refs/heads/{RELEASE_BRANCH}"] == moved


@NEEDS
def test_push_puts_the_token_in_no_argument_and_no_config_file(tmp_path):
    stage = _stage(tmp_path)
    assert _commit(stage, tmp_path)[0].returncode == 0
    result, log = _push(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    assert FAKE_TOKEN not in log.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in (stage.clone / ".git" / "config").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in result.stdout + result.stderr


@NEEDS
def test_the_credential_helper_answers_for_the_server_of_the_repository_only(tmp_path):
    """An unscoped helper would hand the token to any host that git is pointed at."""
    stage = _stage(tmp_path)
    assert _commit(stage, tmp_path)[0].returncode == 0
    server = "https://forge.invalid"
    result, log = _push(stage, tmp_path, server=server, stub_push=True)
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    (scoped,) = [line for line in lines if line.startswith(f"credential.{server}.helper=!")]
    assert not [line for line in lines if line.startswith("credential.helper=!")], "no unscoped helper"
    # An empty value first, so no helper configured elsewhere answers before ours.
    assert lines.index("credential.helper=") < lines.index(scoped)
    key, _, helper = scoped.partition("=")
    assert "RELEASE_PLEASE_TOKEN" in helper and FAKE_TOKEN not in helper

    environment = {**_runner_env(tmp_path), "RELEASE_PLEASE_TOKEN": FAKE_TOKEN, "GIT_TERMINAL_PROMPT": "0"}

    def fill(host: str, setting: str):
        return subprocess.run(
            ["git", "-c", "credential.helper=", "-c", setting, "credential", "fill"],
            input=f"protocol=https\nhost={host}\n",
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    mine = fill("forge.invalid", f"{key}={helper}")
    assert mine.returncode == 0, mine.stderr
    assert f"password={FAKE_TOKEN}" in mine.stdout.splitlines()
    other = fill("other.invalid", f"{key}={helper}")
    assert other.returncode != 0
    assert FAKE_TOKEN not in other.stdout + other.stderr
    # The control: without the key the same helper answers for any host.
    control = fill("other.invalid", f"credential.helper={helper}")
    assert f"password={FAKE_TOKEN}" in control.stdout.splitlines()


HOOKS = ("pre-commit", "commit-msg", "post-commit", "pre-push")


def _hooks(root: Path, marks: Path) -> None:
    """Install every hook that a commit and a push can run; each leaves its own mark."""
    marks.mkdir(exist_ok=True)
    for name in HOOKS:
        _executable(
            root / ".git" / "hooks" / name,
            f"#!/bin/sh\ntouch {shlex.quote(str(marks / name))}\n",
        )


@NEEDS
def test_the_push_job_runs_no_hook_of_the_checkout(tmp_path):
    stage = _stage(tmp_path)
    marks = tmp_path / "marks"
    _hooks(stage.clone, marks)
    # The control: an ordinary commit and push run all four hooks, so the check
    # below can fail.
    control = tmp_path / "control"
    _git(tmp_path, "clone", "-q", f"file://{stage.remote}", str(control))
    _hooks(control, marks)
    (control / "notes.txt").write_text("control", encoding="utf-8")
    _commit_all(control, "control")
    _git(control, "push", "-q", f"file://{stage.remote}", "HEAD:refs/heads/control")
    assert sorted(path.name for path in marks.iterdir()) == sorted(HOOKS)
    for path in marks.iterdir():
        path.unlink()

    assert _commit(stage, tmp_path)[0].returncode == 0
    result, _ = _push(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in marks.iterdir()) == []


@NEEDS
def test_the_push_job_runs_nothing_that_the_checkout_plants(tmp_path):
    """Hostile files at the usual places: none of them runs, whatever the scripts call."""
    stage = _stage(tmp_path)
    marker = tmp_path / "planted-ran"
    python = f"open({str(marker)!r}, 'w').close()\n"
    shell = f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
    for relative in ("scripts/release_manifest.py", "release_manifest.py", "json.py", "sitecustomize.py"):
        _executable(stage.clone / relative, python)
    for relative in ("git", "jq", "base64", "bash", "python3", "make", "Makefile", "release.sh"):
        _executable(stage.clone / relative, shell)
    assert _commit(stage, tmp_path)[0].returncode == 0
    result, _ = _push(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@NEEDS
@pytest.mark.parametrize(
    "branch",
    ["main", "", "release-please--branches--a/../main", "release-please--branches--x y"],
)
def test_push_refuses_a_branch_that_is_not_a_release_branch(tmp_path, branch):
    stage = _stage(tmp_path)
    assert _commit(stage, tmp_path)[0].returncode == 0
    before = _refs(stage.remote)
    result, log = _push(stage, tmp_path, branch)
    assert result.returncode != 0
    assert "not a release-please branch" in result.stdout
    assert not log.exists(), "git must not run"
    assert _refs(stage.remote) == before


# -- the three jobs together -------------------------------------------------


@NEEDS
def test_the_jobs_regenerate_push_and_then_settle(tmp_path):
    """Job 2 reports, job 3 pushes from a fresh clone, and the next run of job 2 finds nothing to do."""
    stage = _stage(tmp_path)
    assert stage.outputs["changed"] == "true"
    committed, outputs = _commit(stage, tmp_path)
    assert committed.returncode == 0, committed.stderr
    assert outputs == {"committed": "true"}
    pushed, _ = _push(stage, tmp_path)
    assert pushed.returncode == 0, pushed.stderr

    settled = tmp_path / "settled"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", RELEASE_BRANCH, f"file://{stage.remote}", str(settled))
    assert _head(settled) == _head(stage.clone)
    assert release_manifest.check(settled) == []
    again, outputs = _run("regenerate", "regenerate", settled, tmp_path)
    assert again.returncode == 0, again.stderr
    assert outputs == {"changed": "false"}
    assert _head(settled) == _head(stage.clone)
