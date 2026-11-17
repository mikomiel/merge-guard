"""The release workflow regenerates the release manifest on the release branch.

release-please's release pull request always edits ``merge_guard/__init__.py``, a file of the
release closure, so ``release-manifest.json`` is stale there and ``checks`` fails until somebody
regenerates it. One job does that, and the only code it runs is the default branch's: the
release branch is data, fetched as one ref, read through git and committed to without a checkout.

The structure tests pin the steps and their conditions, the one checkout, where each token
appears, and that nothing the release branch holds is checked out or run. The behaviour tests run
the real step scripts in bash against local repositories and a stub ``gh``.

A real run needs an open release pull request, so none of this proves that the ``if`` and output
expressions evaluate on GitHub. The first release pull request is the first real run.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from scripts import release_manifest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"

PINNED_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")
RELEASE_BRANCH = "release-please--branches--main--components--merge-guard"
MANIFEST = "release-manifest.json"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
SUBJECT = "chore(release): regenerate the release manifest"
# Not a credential: a marker that must appear in no argument and no config file.
FAKE_TOKEN = "synthetic-secret-value"
JOB = "release-please"
# Every step in order. A step that is not in this list fails the first test below.
STEPS = ["release", "target", "checkout", "commit", "push"]
# The outputs that the pinned release-please action sets (v5.0.0): `pr` is set only in a run
# that created or updated the pull request.
ACTION_OUTPUTS = {"pr"}
# The git commands that the script of each step may run. The check names the subcommand only, so
# the one call that can write files, `read-tree`, is also pinned in full. A step with a script
# that is not listed here fails the test.
GIT_COMMANDS = {
    "target": set(),
    "commit": {"fetch", "rev-parse", "hash-object", "ls-tree", "read-tree", "update-index",
               "write-tree", "commit-tree", "diff"},
    "push": {"push"},
}


def _requires(*tools: str):
    missing = [tool for tool in tools if shutil.which(tool) is None]
    return pytest.mark.skipif(bool(missing), reason="needs " + ", ".join(missing))


NEEDS = _requires("bash", "git")


def _document() -> dict:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _job() -> dict:
    return _document()["jobs"][JOB]


def _steps() -> list[dict]:
    return _job()["steps"]


def _step(step_id: str) -> dict:
    found = [step for step in _steps() if step.get("id") == step_id]
    assert len(found) == 1, f"exactly one step must carry the id {step_id!r}"
    return found[0]


def _leaves(node: object, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Every string of the workflow with the keys that lead to it; a step is named by its id."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            label = str(item["id"]) if isinstance(item, dict) and "id" in item else str(index)
            yield from _leaves(item, (*path, label))
    elif isinstance(node, str):
        yield path, node


def _holders(needle: str) -> set[str]:
    """The ids of the steps that contain *needle* anywhere: key, value or script."""
    found = set()
    for path, value in _leaves(_document()):
        if needle in value or any(needle in part for part in path):
            assert path[:2] == ("jobs", JOB), f"{needle!r} must appear only inside the job: {path}"
            found.add(path[3] if path[2] == "steps" else "")
    return found


def _script(step_id: str) -> str:
    """A step's script without its comments, with continued lines joined."""
    return re.sub(r"(?m)^\s*#.*$", "", _step(step_id)["run"].replace("\\\n", " "))


def _git_subcommands(script: str) -> set[str]:
    """The word after each ``git`` in *script*, once ``--no-pager`` and ``-c key=value`` are skipped.

    Any other global option (``-C``, ``-P``, ``--git-dir``), a quoted word and a variable are read
    as that word. None of them is an allowed subcommand, so such a call is refused, not skipped.
    """
    return set(re.findall(r"\bgit(?:\s+(?:--no-pager|-c\s+\S+))*\s+([^\s;|&()<>]+)", script))


# -- structure ---------------------------------------------------------------


def test_the_workflow_is_one_job_with_exactly_these_steps():
    """Pin every step: one inserted before the push shares its runner, and could point git at
    another server that the credential helper would answer for."""
    document = _document()
    assert list(document["jobs"]) == [JOB]
    assert [step.get("id") for step in _steps()] == STEPS
    assert _steps()[0]["uses"].startswith("googleapis/release-please-action@")
    # An `env` of the workflow or the job would reach every step. A `needs` or an `if` of the
    # job would skip the action itself.
    assert not {"env", "defaults"} & set(document)
    assert not {"needs", "if", "outputs", "env", "defaults"} & set(_job())
    # With no release pull request open there is nothing to read, build or push.
    assert {step["id"]: step["if"] for step in _steps() if "if" in step} == {
        "checkout": "steps.target.outputs.branch != ''",
        "commit": "steps.target.outputs.branch != ''",
        "push": "steps.commit.outputs.commit != ''",
    }
    # Nothing by default, so a job added later has to ask. This one needs two reads.
    assert document["permissions"] == {}
    assert _job()["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_every_output_that_an_expression_reads_is_set():
    """GitHub reads an unset output as an empty string, so a misspelt output skips a step.

    The action sets `pr` only in a run that created or updated the pull request, so `target` runs
    on every run and falls back to the list of open pull requests.
    """
    for step_id, output in re.findall(r"steps\.([\w-]+)\.outputs\.(\w+)", json.dumps(_job())):
        step = _step(step_id)
        if "uses" in step:
            assert output in ACTION_OUTPUTS, (step_id, output)
        else:
            assert re.search(rf'echo "{output}=', step["run"]), (step_id, output)
    assert set(re.findall(r"steps\.release\.outputs\.(\w+)", WORKFLOW.read_text())) == {"pr"}


def test_the_one_checkout_is_the_default_branch_with_no_stored_credential():
    """No `ref` and no `repository`, so the checkout is the ref that started the run.

    CodeQL's untrusted-checkout and cache-poisoning queries start at a checkout of a pull request
    head: an `actions/checkout` with a `ref` or `repository` that looks like a head, or a
    `git fetch` of a head ref, a pull request number or a head SHA, named in the command or in
    the expression behind an env var that the command names. None appears here: the release
    branch is fetched by its validated name, and no checkout of it exists.
    """
    uses = [step for step in _steps() if step.get("uses", "").startswith("actions/checkout@")]
    assert [step["id"] for step in uses] == ["checkout"]
    assert uses[0]["with"] == {"persist-credentials": False}
    for step in (_step("commit"), _step("push")):
        text = json.dumps(step)
        for name in ("head_ref", "head.ref", "head_sha", "head.sha", "head_branch", "merge_ref",
                     "merge_sha", "pr_number", "pr_id", "GITHUB_HEAD_REF", "pull_request"):
            assert name not in text, (step["id"], name)


def test_each_token_appears_only_in_the_steps_that_need_it():
    # The personal access token: the action and the push, with exactly one reference each.
    assert _holders("secrets") == _holders("RELEASE_PLEASE_TOKEN") == {"release", "push"}
    assert [path for path, value in _leaves(_document()) if "secrets." in value] == [
        ("jobs", JOB, "steps", "release", "with", "token"),
        ("jobs", JOB, "steps", "push", "env", "RELEASE_PLEASE_TOKEN"),
    ]
    assert _step("push")["env"] == {
        "RELEASE_PLEASE_TOKEN": "${{ secrets.RELEASE_PLEASE_TOKEN }}",
        "BRANCH": "${{ steps.target.outputs.branch }}",
        "COMMIT": "${{ steps.commit.outputs.commit }}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    # The job token: only the lookup of the open pull requests.
    assert _holders("github.token") == _holders("GH_TOKEN") == {"target"}
    assert _holders("GITHUB_TOKEN") == set()
    assert _step("target")["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "REPORTED_PR": "${{ steps.release.outputs.pr }}",
    }


def test_the_step_that_reads_the_release_branch_holds_no_secret_and_fetches_anonymously():
    """The fetch is anonymous because this repository is public."""
    step = _step("commit")
    text = json.dumps(step).lower()
    for forbidden in ("secret", "token", "credential", "git config", "extraheader"):
        assert forbidden not in text, forbidden
    assert step["env"] == {
        "BRANCH": "${{ steps.target.outputs.branch }}",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": BOT_NAME,
        "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": BOT_NAME,
        "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    }
    (fetch,) = re.findall(r"git fetch .*", _script("commit"))
    assert fetch == (
        'git fetch --quiet --no-tags --depth=1 "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}.git"'
        ' "refs/heads/${BRANCH}"'
    )


def test_no_step_checks_out_merges_or_runs_a_file_of_the_release_branch():
    """The only repository file that runs is ``scripts/release_manifest.py`` of the default branch,
    in the commit step, and only to read the fetched commit."""
    for step in _steps():
        if "run" in step:
            assert _git_subcommands(_script(step["id"])) == GIT_COMMANDS[step["id"]], step["id"]
    commit = _script("commit")
    # With `-u`, `read-tree` writes the tree to the working tree, or to wherever GIT_WORK_TREE
    # points. It reads into the temporary index here and nothing else.
    assert re.findall(r"read-tree .*", commit) == ['read-tree "${tip}"']
    assert re.findall(r'python3? \S+ \w+ --commit "\$\{\w+\}"', commit) == [
        'python3 scripts/release_manifest.py write --commit "${tip}"',
        'python3 scripts/release_manifest.py check --commit "${commit}"',
    ]
    assert commit.count("python") == 2
    # The step that holds the token runs no file of the repository at all.
    push = _script("push")
    for forbidden in ("python", "scripts/", "./", "source ", "pytest", "pip ", "make ", "uv "):
        assert forbidden not in push, forbidden
    # A plain push of the commit that was built, to the one validated branch.
    assert re.findall(r"push .*", push) == [
        'push "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}.git" "${COMMIT}:refs/heads/${BRANCH}"'
    ]
    for forbidden in ("--force", "--mirror", "--delete", "--all", "--tags", " +", " -f "):
        assert forbidden not in push, forbidden


def test_the_git_check_is_not_fooled_by_an_option_a_path_a_quote_or_a_variable():
    """The check above can fail. Each call below hides a checkout or a read of the release branch
    behind one of them, and none of them is allowed. The first call is allowed, so the extractor
    is not refusing everything."""
    allowed = set().union(*GIT_COMMANDS.values())
    call = 'x="$(git --no-pager -c user.name=a diff --stat)"; git push'
    assert _git_subcommands(call) == {"diff", "push"}
    for hostile in (
        "git clone -q url dir",
        "git -C dir clone -q url",
        'git -P show "${tip}:file" | bash',
        "git -c user.name=a -C dir clone -q url",
        'git -c "user.name=a b" -C dir clone -q url',
        'git --git-dir="${PWD}/.git" archive "${tip}" | tar -x',
        "/usr/bin/git -C dir clone -q url",
        'git "clone" -q url dir',
        "git $verb -q url dir",
    ):
        assert _git_subcommands(hostile) - allowed, hostile


def test_no_script_expands_an_expression_and_no_step_fails_silently():
    """A value reaches a script through `env`, never through `${{ }}` in its text."""
    for step in _steps():
        assert "continue-on-error" not in step, step["id"]
        assert "${{" not in step.get("run", ""), step["id"]
        if "run" in step:  # `shell: bash` is `bash -eo pipefail`, and the scripts rely on it
            assert step["shell"] == "bash", step["id"]


def test_every_action_is_pinned_by_full_sha_with_its_version_in_a_comment():
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if re.match(r"\s*(?:- )?uses:", line)]
    assert len(lines) == 2
    for line in lines:
        assert re.fullmatch(r"\s*(?:- )?uses: [\w./-]+@[0-9a-f]{40}\s+# v\d+\.\d+\.\d+", line), line
    used = [step["uses"] for step in _steps() if "uses" in step]
    assert all(PINNED_ACTION.fullmatch(value) for value in used), used
    # The runner image carries Python, git, jq and gh, so no setup action is needed.
    assert sorted(value.partition("@")[0] for value in used) == [
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
    """A directory of stubs, first on the PATH, with a `python3` that is the test interpreter."""
    directory = tmp_path / "bin"
    if not directory.exists():
        _executable(directory / "python3", f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    return directory


def _runner_env(tmp_path: Path) -> dict[str, str]:
    for name in ("home", "scratch"):
        (tmp_path / name).mkdir(exist_ok=True)
    return {
        "PATH": os.pathsep.join([str(_bin(tmp_path)), os.environ.get("PATH", os.defpath)]),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "scratch"),
        "RUNNER_TEMP": str(tmp_path / "scratch"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def _run(step_id, cwd, tmp_path, supplied: dict | None = None, runner: dict | None = None):
    """Run a step's script as GitHub Actions runs `shell: bash`; return the result and its outputs.

    The step's literal `env` entries come from the workflow itself. The values of its expression
    entries are *supplied*, and a test must supply exactly those.
    """
    step = _step(step_id)
    declared = step.get("env", {})
    expressions = {name for name, value in declared.items() if "${{" in str(value)}
    assert set(supplied or {}) == expressions
    run_id = next(_RUNS)
    script, output = tmp_path / f"{step_id}-{run_id}.sh", tmp_path / f"{step_id}-{run_id}.output"
    script.write_text(step["run"], encoding="utf-8")
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
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    return result, dict(line.split("=", 1) for line in lines if "=" in line)


# -- the target step ---------------------------------------------------------


def _gh(tmp_path: Path, listing: object, status: int = 0) -> Path:
    """A stub `gh` that logs its arguments and prints *listing*."""
    log, body = tmp_path / "gh.log", tmp_path / "gh-body.json"
    body.write_text(listing if isinstance(listing, str) else json.dumps(listing), encoding="utf-8")
    _executable(
        _bin(tmp_path) / "gh",
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        f"cat {shlex.quote(str(body))}\nexit {status}\n",
    )
    return log


def _pull(ref: str, repo: str | None = "owner/repo") -> dict:
    return {"head": {"ref": ref, "repo": None if repo is None else {"full_name": repo}}}


def _reported(branch: str = RELEASE_BRANCH, number: int = 12) -> str:
    """The pull request as the action reports it."""
    return json.dumps({"headBranchName": branch, "baseBranchName": "main", "number": number})


def _target(tmp_path: Path, reported: str, listing: object = (), status: int = 0):
    log = _gh(tmp_path, listing, status)
    supplied = {"GH_TOKEN": "unused", "REPORTED_PR": reported}
    runner = {"GITHUB_REPOSITORY": "owner/repo"}
    result, outputs = _run("target", tmp_path, tmp_path, supplied, runner)
    return result, outputs, log


GHOST = "release-please--branches--ghost"


@_requires("bash", "jq")
@pytest.mark.parametrize(
    "reported, listing, branch, lookups",
    [
        # The action's own report needs no lookup.
        (_reported(), [], RELEASE_BRANCH, 0),
        # release-please can report `number: 0` when it detected no code change.
        (_reported(GHOST, 0), [_pull(RELEASE_BRANCH)], RELEASE_BRANCH, 1),
        # No report: only a release branch of this repository counts.
        (
            "",
            [_pull("feature/unrelated"), _pull(RELEASE_BRANCH, "someone-else/repo"),
             _pull(RELEASE_BRANCH, None), _pull(RELEASE_BRANCH)],
            RELEASE_BRANCH,
            1,
        ),
        ("", [], "", 1),
        ("", [_pull("feature/unrelated")], "", 1),
    ],
)
def test_target_finds_the_one_open_release_branch_of_this_repository(
    tmp_path, reported, listing, branch, lookups
):
    result, outputs, log = _target(tmp_path, reported, listing)
    assert result.returncode == 0, result.stderr
    assert outputs == {"branch": branch}
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert len(calls) == lookups
    assert all("repos/owner/repo/pulls?state=open" in call for call in calls)


@_requires("bash", "jq")
@pytest.mark.parametrize(
    "listing, status, message",
    [
        (
            [_pull(RELEASE_BRANCH), _pull("release-please--branches--main--components--other")],
            0,
            "more than one open release pull request",
        ),
        ("", 1, ""),  # a failed lookup is a failure, not "nothing found"
    ],
)
def test_target_fails_on_two_open_release_pull_requests_and_on_a_failed_lookup(
    tmp_path, listing, status, message
):
    result, outputs, _ = _target(tmp_path, "", listing, status)
    assert result.returncode != 0
    assert message in result.stderr
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
    """This is the one place that validates the name: every later step reads its output."""
    result, outputs, _ = _target(tmp_path, _reported(name))
    assert result.returncode != 0
    assert "not a release-please branch name" in result.stdout
    assert "branch" not in outputs


# -- the commit and push steps -----------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    command = ["git", "-C", str(root), *args]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result


def _commit_all(root: Path, message: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    _git(root, "add", "-A")
    _git(root, *identity, "commit", "-q", "-m", message)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _clone(remote: Path, target: Path, *options: str) -> Path:
    _git(target.parent, "clone", "-q", *options, f"file://{remote}", str(target))
    return target


class Stage(NamedTuple):
    remote: Path  # a bare repository that holds `main` and the release branch
    runner: Path  # the job's checkout of `main`, depth 1, as actions/checkout makes it
    tip: str  # the head of the release branch when the stage was made


def _stage(tmp_path: Path) -> Stage:
    """What the job starts from: the default branch checked out, and on the server a release
    branch that bumps ``__version__`` as the release pull request does, so its manifest is stale."""
    work = tmp_path / "work"
    for name in ("scripts", "merge_guard"):
        (work / name).mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "release_manifest.py", work / "scripts")
    (work / "scripts" / "assurance.py").write_text("print('classify')\n", encoding="utf-8")
    init = work / "merge_guard" / "__init__.py"
    init.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    _git(work, "init", "-q")
    _commit_all(work, "closure")
    files = {}
    for relative in ("merge_guard/__init__.py", "scripts/assurance.py"):
        sha256, size = release_manifest.digest(work / relative)
        files[relative] = {"sha256": sha256, "bytes": size}
    manifest = {
        "schema": release_manifest.SCHEMA,
        "product": release_manifest.PRODUCT,
        "entrypoints": ["scripts/assurance.py"],
        "files": files,
    }
    (work / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _commit_all(work, "manifest")
    init.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    _commit_all(work, "chore(main): release 0.2.0")

    remote = tmp_path / "server" / "owner" / "repo.git"
    remote.mkdir(parents=True)
    _git(remote, "init", "-q", "--bare")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    branches = [f"HEAD:refs/heads/{RELEASE_BRANCH}", "HEAD~1:refs/heads/main"]
    _git(work, "push", "-q", str(remote), *branches)
    runner = _clone(remote, tmp_path / "runner", "--depth", "1")
    return Stage(remote, runner, _head(work))


def _amend_release(stage: Stage, tmp_path, change: Callable[[Path], None], *, rewrite=False):
    """Move the release branch on the server, as release-please or a person would."""
    other = _clone(stage.remote, tmp_path / f"other-{next(_RUNS)}", "--branch", RELEASE_BRANCH)
    if rewrite:
        _git(other, "reset", "-q", "--hard", "HEAD~1")
    change(other)
    _commit_all(other, "the release branch moves")
    _git(other, "push", "-q", "--force", str(stage.remote), f"HEAD:refs/heads/{RELEASE_BRANCH}")
    return _head(other)


def _write(name: str, data: bytes) -> Callable[[Path], None]:
    return lambda root: (root / name).write_bytes(data)


def _remove(name: str) -> Callable[[Path], None]:
    return lambda root: _git(root, "rm", "-q", name)


def _replace_with_link(root: Path) -> None:
    (root / MANIFEST).unlink()
    (root / MANIFEST).symlink_to(".git/config")


def _server(stage: Stage) -> dict[str, str]:
    url = f"file://{stage.remote.parents[1]}"
    return {"GITHUB_SERVER_URL": url, "GITHUB_REPOSITORY": "owner/repo"}


def _commit(stage: Stage, tmp_path: Path, branch: str = RELEASE_BRANCH):
    return _run("commit", stage.runner, tmp_path, {"BRANCH": branch}, _server(stage))


def _built(stage: Stage, tmp_path: Path) -> str:
    result, outputs = _commit(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    return outputs["commit"]


def _git_wrapper(tmp_path: Path, *, stub_push: bool = False) -> Path:
    """Put a `git` on the PATH that logs one argument per line, then runs the real one.

    With *stub_push* a `push` is logged and not run, so a test can name any server.
    """
    log = tmp_path / "git.log"
    quoted = shlex.quote(str(log))
    real = shutil.which("git")
    assert real is not None
    skip = 'for argument in "$@"; do [ "$argument" = push ] && exit 0; done\n' if stub_push else ""
    _executable(
        _bin(tmp_path) / "git",
        "#!/bin/sh\n"
        f"for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done >> {quoted}\n"
        f"printf '%s\\n' '---' >> {quoted}\n"
        f"{skip}exec {shlex.quote(real)} \"$@\"\n",
    )
    return log


def _push(stage: Stage, tmp_path, commit, *, server=None, stub_push=False):
    log = _git_wrapper(tmp_path, stub_push=stub_push)
    runner = _server(stage) | ({"GITHUB_SERVER_URL": server} if server else {})
    supplied = {"RELEASE_PLEASE_TOKEN": FAKE_TOKEN, "BRANCH": RELEASE_BRANCH, "COMMIT": commit}
    result, _ = _run("push", stage.runner, tmp_path, supplied, runner)
    return result, log


def _refs(remote: Path) -> dict[str, str]:
    listing = _git(remote, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    return dict(line.split(" ") for line in listing.splitlines())


def _state(runner: Path) -> tuple[str, bytes, str]:
    """What a checkout holds: its head, its index and its refs."""
    index = (runner / ".git" / "index").read_bytes()
    return _head(runner), index, _git(runner, "for-each-ref").stdout


def _changes(runner: Path, commit: str) -> list[str]:
    return _git(runner, "diff-tree", "--no-commit-id", "-r", commit).stdout.splitlines()


@NEEDS
def test_commit_builds_one_ordinary_commit_of_the_manifest_alone_as_the_bot(tmp_path):
    stage = _stage(tmp_path)
    runner = stage.runner
    before = _state(runner)
    main_manifest = _git(runner, "show", f"HEAD:{MANIFEST}").stdout

    result, outputs = _commit(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    commit = outputs["commit"]
    assert outputs == {"commit": commit} and re.fullmatch(r"[0-9a-f]{40}", commit)

    # One ordinary commit on top of the fetched head, never a merge, by the fixed identity.
    parents = _git(runner, "rev-list", "--parents", "-n", "1", commit).stdout.split()[1:]
    assert parents == [stage.tip]
    shown = _git(runner, "log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%s", commit).stdout.splitlines()
    assert shown == [BOT_NAME, BOT_EMAIL, BOT_NAME, BOT_EMAIL, SUBJECT]
    # It touches the manifest alone, and keeps its mode.
    (line,) = _changes(runner, commit)
    meta, path = line.split("\t")
    assert meta.split()[:2] == [":100644", "100644"] and meta.split()[4] == "M"
    assert path == MANIFEST
    # It is true of the release branch, read from the object database alone, and it is not the
    # manifest that the default branch carries.
    assert release_manifest.check_commit(runner, commit) == []
    assert _git(runner, "show", f"{commit}:{MANIFEST}").stdout != main_manifest

    # Nothing of the release branch reached the checkout, its index or its refs.
    assert _state(runner) == before
    assert _git(runner, "status", "--porcelain").stdout == ""
    version = (runner / "merge_guard" / "__init__.py").read_text(encoding="utf-8")
    assert version == '__version__ = "0.1.0"\n'


@NEEDS
def test_commit_keeps_an_executable_mode_of_the_manifest(tmp_path):
    stage = _stage(tmp_path)
    _amend_release(stage, tmp_path, lambda root: (root / MANIFEST).chmod(0o755))
    (line,) = _changes(stage.runner, _built(stage, tmp_path))
    assert line.split("\t")[0].split()[:2] == [":100755", "100755"]


@NEEDS
@pytest.mark.parametrize(
    "change, message",
    [
        (_remove("scripts/assurance.py"), "scripts/assurance.py: path is not tracked in"),
        (_remove(MANIFEST), "release-manifest.json: path is not tracked in"),
        (_replace_with_link, "path is not a regular file in"),
        (_write(MANIFEST, b"{"), "not valid JSON"),
        (_write(MANIFEST, b"[]"), "must be a JSON object"),
        (_write(MANIFEST, b'{"files": []}'), "'files' is not a non-empty JSON object"),
    ],
)
def test_commit_refuses_a_branch_whose_closure_or_manifest_is_unreadable(tmp_path, change, message):
    """The step fails with the finding, builds nothing, and writes through no link."""
    stage = _stage(tmp_path)
    _amend_release(stage, tmp_path, change)
    config = (stage.runner / ".git" / "config").read_bytes()
    result, outputs = _commit(stage, tmp_path)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert outputs == {}
    assert (stage.runner / ".git" / "config").read_bytes() == config
    assert _git(stage.runner, "status", "--porcelain").stdout == ""


@NEEDS
def test_commit_fails_when_the_release_branch_cannot_be_fetched(tmp_path):
    stage = _stage(tmp_path)
    result, outputs = _commit(stage, tmp_path, branch=GHOST)
    assert result.returncode != 0
    assert outputs == {}


@NEEDS
def test_nothing_that_the_release_branch_holds_runs(tmp_path):
    """Hostile files at the usual places: none of them runs, whatever the scripts call."""
    stage = _stage(tmp_path)
    marker = tmp_path / "planted-ran"
    python = f"open({str(marker)!r}, 'w').close()\n"
    shell = f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"

    def plant(root: Path) -> None:
        for name in ("scripts/release_manifest.py", "release_manifest.py", "json.py",
                     "sitecustomize.py"):
            _executable(root / name, python)
        for name in ("git", "jq", "bash", "python3", "make", "Makefile", "release.sh"):
            _executable(root / name, shell)

    _amend_release(stage, tmp_path, plant)
    # The control: a checkout of the same branch does run the planted script, so the check can fail.
    control = _clone(stage.remote, tmp_path / "control", "--branch", RELEASE_BRANCH)
    command = [sys.executable, "scripts/release_manifest.py", "check"]
    subprocess.run(command, cwd=control, capture_output=True, check=False)
    assert marker.exists()
    marker.unlink()

    result, _ = _push(stage, tmp_path, _built(stage, tmp_path))
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


HOOKS = ("pre-commit", "commit-msg", "post-commit", "pre-push")


def _hooks(root: Path, marks: Path) -> None:
    """Install every hook that a commit and a push can run; each leaves its own mark."""
    marks.mkdir(exist_ok=True)
    for name in HOOKS:
        script = f"#!/bin/sh\ntouch {shlex.quote(str(marks / name))}\n"
        _executable(root / ".git" / "hooks" / name, script)


@NEEDS
def test_the_job_runs_no_hook_of_the_checkout(tmp_path):
    stage = _stage(tmp_path)
    marks = tmp_path / "marks"
    _hooks(stage.runner, marks)
    # The control: an ordinary commit and push run all four hooks, so the check can fail.
    control = _clone(stage.remote, tmp_path / "control")
    _hooks(control, marks)
    (control / "notes.txt").write_text("control", encoding="utf-8")
    _commit_all(control, "control")
    _git(control, "push", "-q", f"file://{stage.remote}", "HEAD:refs/heads/control")
    assert sorted(path.name for path in marks.iterdir()) == sorted(HOOKS)
    for path in marks.iterdir():
        path.unlink()

    result, _ = _push(stage, tmp_path, _built(stage, tmp_path))
    assert result.returncode == 0, result.stderr
    assert list(marks.iterdir()) == []


@NEEDS
def test_push_sends_only_the_built_commit_as_a_plain_push_and_hides_the_token(tmp_path):
    stage = _stage(tmp_path)
    commit = _built(stage, tmp_path)
    main = _refs(stage.remote)["refs/heads/main"]
    result, log = _push(stage, tmp_path, commit)
    assert result.returncode == 0, result.stderr
    assert _refs(stage.remote) == {f"refs/heads/{RELEASE_BRANCH}": commit, "refs/heads/main": main}
    assert (stage.runner / ".git" / "shallow").exists()  # shallow, as actions/checkout makes it
    lines = log.read_text(encoding="utf-8").splitlines()
    assert f"{commit}:refs/heads/{RELEASE_BRANCH}" in lines
    forced = {"--force", "-f", "--force-with-lease"}
    assert not [line for line in lines if line in forced or line.startswith("+")]
    # The token is in no argument, no config file and no output.
    assert FAKE_TOKEN not in "\n".join(lines)
    assert FAKE_TOKEN not in (stage.runner / ".git" / "config").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in result.stdout + result.stderr


@NEEDS
@pytest.mark.parametrize("move", ["rewrite", "advance"])
def test_push_is_rejected_when_the_release_branch_moved_after_the_fetch(tmp_path, move):
    """release-please rewrites the branch, or a person adds to it: the push must lose."""
    stage = _stage(tmp_path)
    commit = _built(stage, tmp_path)
    change = _write("notes.txt", move.encode())
    moved = _amend_release(stage, tmp_path, change, rewrite=move == "rewrite")
    result, _ = _push(stage, tmp_path, commit)
    assert result.returncode != 0
    assert _refs(stage.remote)[f"refs/heads/{RELEASE_BRANCH}"] == moved


@NEEDS
def test_the_credential_helper_answers_for_the_server_of_the_repository_only(tmp_path):
    """An unscoped helper would hand the token to any host that git is pointed at."""
    stage = _stage(tmp_path)
    server = "https://forge.invalid"
    result, log = _push(stage, tmp_path, _built(stage, tmp_path), server=server, stub_push=True)
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    (scoped,) = [line for line in lines if line.startswith(f"credential.{server}.helper=!")]
    assert not any(line.startswith("credential.helper=!") for line in lines), "unscoped helper"
    # An empty value first, so no helper configured elsewhere answers before ours.
    assert lines.index("credential.helper=") < lines.index(scoped)
    key, _, helper = scoped.partition("=")
    assert "RELEASE_PLEASE_TOKEN" in helper and FAKE_TOKEN not in helper

    environment = _runner_env(tmp_path) | {"GIT_TERMINAL_PROMPT": "0"}
    environment["RELEASE_PLEASE_TOKEN"] = FAKE_TOKEN

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


@NEEDS
def test_the_job_builds_pushes_and_then_settles(tmp_path):
    """The next run reads the pushed head, finds the manifest right, and builds nothing."""
    stage = _stage(tmp_path)
    pushed, _ = _push(stage, tmp_path, _built(stage, tmp_path))
    assert pushed.returncode == 0, pushed.stderr
    full = _clone(stage.remote, tmp_path / "full", "--branch", RELEASE_BRANCH)
    assert release_manifest.check(full) == []

    result, outputs = _commit(stage, tmp_path)
    assert result.returncode == 0, result.stderr
    assert outputs == {}, "no output, so the push step does not run"
    assert "nothing to push" in result.stdout
    assert _refs(stage.remote)[f"refs/heads/{RELEASE_BRANCH}"] == _head(full)
