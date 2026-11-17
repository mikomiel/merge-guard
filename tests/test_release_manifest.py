"""The release closure manifest must describe the tree it sits in.

The manifest is the closure's only owner, so two things need pinning: the
repository's own manifest matches its tree, and a tree that drifts is named file
by file rather than accepted. The write path is pinned too, because a generator
that silently drops an entrypoint would publish a closure that no longer holds
what a consumer imports.

The closure also covers the whole ``merge_guard/`` package. A file committed
there without a manifest entry is named, so a package file can never be released
outside the closure.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import release_manifest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "merge_guard/"


def _commit(root: Path, *paths: str) -> None:
    add = ["git", "-C", str(root), "add"]
    add.extend(paths) if paths else add.append("-A")
    subprocess.run(add, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "test",
        ],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    """A tiny repository whose manifest matches its committed tree."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    target = root / "scripts" / "assurance.py"
    target.write_text("print('classify')\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"], check=True, capture_output=True
    )
    _commit(root)
    digest, size = release_manifest.digest(target)
    manifest = {
        "schema": release_manifest.SCHEMA,
        "product": release_manifest.PRODUCT,
        "entrypoints": ["scripts/assurance.py"],
        "files": {"scripts/assurance.py": {"sha256": digest, "bytes": size}},
    }
    (root / release_manifest.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    _commit(root, release_manifest.MANIFEST_NAME)
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8"))


def _rewrite(root: Path, manifest: dict) -> None:
    (root / release_manifest.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    _commit(root, release_manifest.MANIFEST_NAME)


def _unlisted_package_files(root: Path) -> list[str]:
    """The files under the package in the commit at HEAD that the manifest omits.

    The tree is the commit, the same one ``check`` reads. A walk of the disk would
    count ``__pycache__`` and files nobody committed. There is no suffix filter:
    a data file in the package is released like a module.
    """
    entries, problem = release_manifest._tree_entries(root)
    assert entries is not None, problem
    listed = set(_manifest(root)["files"])
    return sorted(
        path for path in entries if path.startswith(PACKAGE) and path not in listed
    )


def _package_repo(tmp_path: Path) -> Path:
    """A repository whose package holds two committed files and lists only one."""
    root = _repo(tmp_path)
    package = root / "merge_guard"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "extra.py").write_text("print('extra')\n", encoding="utf-8")
    _commit(root)
    digest, size = release_manifest.digest(package / "__init__.py")
    manifest = _manifest(root)
    manifest["files"]["merge_guard/__init__.py"] = {"sha256": digest, "bytes": size}
    _rewrite(root, manifest)
    return root


def test_the_repository_manifest_matches_the_tree():
    assert release_manifest.check(ROOT) == []


def test_every_package_file_is_in_the_closure():
    unlisted = _unlisted_package_files(ROOT)
    assert unlisted == [], (
        "committed under merge_guard/ without an entry in release-manifest.json "
        "(docs/pin-bump.md says how to add one): " + ", ".join(unlisted)
    )


def test_a_package_file_without_a_manifest_entry_is_named(tmp_path):
    root = _package_repo(tmp_path)
    assert _unlisted_package_files(root) == ["merge_guard/extra.py"]
    nested = root / "merge_guard" / "adapters"
    nested.mkdir()
    (nested / "port.py").write_text("print('nested')\n", encoding="utf-8")
    (root / "merge_guard" / "rules.toml").write_text("a = 1\n", encoding="utf-8")
    _commit(root)
    assert _unlisted_package_files(root) == [
        "merge_guard/adapters/port.py",
        "merge_guard/extra.py",
        "merge_guard/rules.toml",
    ]


def test_listing_a_package_file_clears_the_finding(tmp_path):
    root = _package_repo(tmp_path)
    extra = root / "merge_guard" / "extra.py"
    digest, size = release_manifest.digest(extra)
    manifest = _manifest(root)
    manifest["files"]["merge_guard/extra.py"] = {"sha256": digest, "bytes": size}
    _rewrite(root, manifest)
    assert _unlisted_package_files(root) == []
    assert release_manifest.check(root) == []


def test_only_committed_package_files_are_counted(tmp_path):
    root = _package_repo(tmp_path)
    cache = root / "merge_guard" / "__pycache__"
    cache.mkdir()
    (cache / "extra.cpython-312.pyc").write_bytes(b"\0")
    (root / "merge_guard" / "untracked.py").write_text("print('x')\n", encoding="utf-8")
    # A file outside the package may stay out of the closure: the rule is the package's.
    (root / "scripts" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
    _commit(root, "scripts/tool.py")
    assert "scripts/tool.py" not in _manifest(root)["files"]
    assert _unlisted_package_files(root) == ["merge_guard/extra.py"]


def test_a_mutated_file_is_named_with_its_digest(tmp_path):
    root = _repo(tmp_path)
    (root / "scripts" / "assurance.py").write_text("print('changed')\n", encoding="utf-8")
    _commit(root)
    findings = release_manifest.check(root)
    assert any(
        "scripts/assurance.py" in finding and "sha256 differs" in finding
        for finding in findings
    )


def test_a_removed_file_is_named(tmp_path):
    root = _repo(tmp_path)
    (root / "scripts" / "assurance.py").unlink()
    findings = release_manifest.check(root)
    assert any(
        "scripts/assurance.py" in finding and "missing" in finding for finding in findings
    )


def test_a_symlink_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "elsewhere.py").write_text("print('x')\n", encoding="utf-8")
    target = root / "scripts" / "assurance.py"
    target.unlink()
    target.symlink_to(root / "elsewhere.py")
    findings = release_manifest.check(root)
    assert any("symlink" in finding for finding in findings)


def test_a_path_outside_the_repository_is_refused(tmp_path):
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest["files"]["../outside.py"] = {"sha256": "0" * 64, "bytes": 1}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("'..'" in finding for finding in findings)


def test_the_manifest_never_lists_itself(tmp_path):
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest["files"][release_manifest.MANIFEST_NAME] = {
        "sha256": "0" * 64,
        "bytes": 1,
    }
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("never lists itself" in finding for finding in findings)


def test_an_undeclared_entrypoint_is_named(tmp_path):
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest["entrypoints"] = ["scripts/missing.py"]
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("entrypoint is not part" in finding for finding in findings)


def test_an_unknown_top_level_key_is_refused(tmp_path):
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest["extra"] = True
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("unknown keys: extra" in finding for finding in findings)


def test_malformed_json_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / release_manifest.MANIFEST_NAME).write_text("{", encoding="utf-8")
    findings = release_manifest.check(root)
    assert any("not valid JSON" in finding for finding in findings)


def test_write_refreshes_digests(tmp_path):
    root = _repo(tmp_path)
    (root / "scripts" / "assurance.py").write_text("print('new')\n", encoding="utf-8")
    _commit(root)
    assert release_manifest.write(root) == []
    _commit(root, release_manifest.MANIFEST_NAME)
    assert release_manifest.check(root) == []
    digest, size = release_manifest.digest(root / "scripts" / "assurance.py")
    assert _manifest(root)["files"]["scripts/assurance.py"] == {
        "sha256": digest,
        "bytes": size,
    }


def test_write_refuses_to_drop_a_declared_entrypoint(tmp_path):
    root = _repo(tmp_path)
    other = root / "scripts" / "other.py"
    other.write_text("print('other')\n", encoding="utf-8")
    _commit(root)
    findings = release_manifest.write(root, ["scripts/other.py"])
    assert any("scripts/assurance.py" in finding for finding in findings)
    assert "scripts/other.py" not in _manifest(root)["files"]


def test_write_replaces_the_closure_when_paths_are_given(tmp_path):
    root = _repo(tmp_path)
    other = root / "scripts" / "other.py"
    other.write_text("print('other')\n", encoding="utf-8")
    _commit(root)
    assert release_manifest.write(root, ["scripts/assurance.py", "scripts/other.py"]) == []
    assert sorted(_manifest(root)["files"]) == [
        "scripts/assurance.py",
        "scripts/other.py",
    ]


def test_a_parent_symlink_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "alias").symlink_to("scripts")
    digest, size = release_manifest.digest(root / "scripts" / "assurance.py")
    manifest = _manifest(root)
    manifest["entrypoints"] = ["alias/assurance.py"]
    manifest["files"] = {"alias/assurance.py": {"sha256": digest, "bytes": size}}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("traverses a symlink" in finding for finding in findings)


def test_an_escaping_parent_symlink_is_refused(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = outside / "payload.py"
    payload.write_text("print('outside')\n", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    digest, size = release_manifest.digest(payload)
    manifest = _manifest(root)
    manifest["entrypoints"] = ["escape/payload.py"]
    manifest["files"] = {"escape/payload.py": {"sha256": digest, "bytes": size}}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("traverses a symlink" in finding for finding in findings)


def test_a_refused_write_leaves_the_manifest_unchanged(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.py").write_text("print('outside')\n", encoding="utf-8")
    (root / "alias").symlink_to("scripts")
    (root / "escape").symlink_to(outside)
    before = (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    for relative in ("alias/assurance.py", "escape/payload.py"):
        findings = release_manifest.write(root, [relative])
        assert any("symlink" in finding for finding in findings), relative
        assert (
            root / release_manifest.MANIFEST_NAME
        ).read_text(encoding="utf-8") == before


def test_duplicate_keys_are_refused(tmp_path):
    root = _repo(tmp_path)
    path = root / release_manifest.MANIFEST_NAME
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '"schema": "release-manifest.v1",',
        '"schema": "release-manifest.v1",\n  "schema": "release-manifest.v1",',
        1,
    )
    path.write_text(text, encoding="utf-8")
    findings = release_manifest.check(root)
    assert any("duplicate key" in finding for finding in findings)


def test_an_untracked_file_is_refused(tmp_path):
    root = _repo(tmp_path)
    local = root / "scripts" / "local.py"
    local.write_text("print('local')\n", encoding="utf-8")
    digest, size = release_manifest.digest(local)
    manifest = _manifest(root)
    manifest["files"]["scripts/local.py"] = {"sha256": digest, "bytes": size}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("not tracked in HEAD" in finding for finding in findings)


def test_repository_metadata_is_refused(tmp_path):
    root = _repo(tmp_path)
    metadata = root / ".git" / "description"
    digest, size = release_manifest.digest(metadata)
    manifest = _manifest(root)
    manifest["files"][".git/description"] = {"sha256": digest, "bytes": size}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("not tracked in HEAD" in finding for finding in findings)


def test_a_refused_write_leaves_untracked_bytes_out(tmp_path):
    root = _repo(tmp_path)
    local = root / "scripts" / "local.py"
    local.write_text("print('local')\n", encoding="utf-8")
    before = (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    findings = release_manifest.write(root, ["scripts/assurance.py", "scripts/local.py"])
    assert any("not tracked in HEAD" in finding for finding in findings)
    assert (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8") == before


def test_digests_are_the_committed_bytes_not_the_checkout(tmp_path):
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text(
        "scripts/assurance.py text eol=crlf\n", encoding="utf-8"
    )
    _commit(root)
    target = root / "scripts" / "assurance.py"
    target.unlink()
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--", "scripts/assurance.py"],
        check=True,
        capture_output=True,
    )
    worktree = target.read_bytes()
    assert b"\r\n" in worktree

    assert release_manifest.check(root) == []
    assert release_manifest.write(root) == []
    blob = subprocess.run(
        ["git", "-C", str(root), "show", "HEAD:scripts/assurance.py"],
        check=True,
        capture_output=True,
    ).stdout
    recorded = _manifest(root)["files"]["scripts/assurance.py"]
    assert recorded == {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
    assert recorded["sha256"] != hashlib.sha256(worktree).hexdigest()


def test_a_committed_symlink_is_refused_even_when_the_checkout_is_regular(tmp_path):
    root = _repo(tmp_path)
    link = root / "link.py"
    link.symlink_to("scripts/assurance.py")
    _commit(root)
    link.unlink()
    link.write_text("print('regular now')\n", encoding="utf-8")
    digest, size = release_manifest.digest(link)
    manifest = _manifest(root)
    manifest["files"]["link.py"] = {"sha256": digest, "bytes": size}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("mode 120000" in finding for finding in findings)


def test_a_refused_write_keeps_a_committed_symlink_out(tmp_path):
    root = _repo(tmp_path)
    link = root / "link.py"
    link.symlink_to("scripts/assurance.py")
    _commit(root)
    link.unlink()
    link.write_text("print('regular now')\n", encoding="utf-8")
    before = (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    findings = release_manifest.write(root, ["scripts/assurance.py", "link.py"])
    assert any("mode 120000" in finding for finding in findings)
    assert (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8") == before

def test_an_uncommitted_manifest_edit_is_refused(tmp_path):
    root = _repo(tmp_path)
    path = root / release_manifest.MANIFEST_NAME
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    findings = release_manifest.check(root)
    assert any("differs from HEAD" in finding for finding in findings)


def test_a_transformed_manifest_checkout_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text(
        "release-manifest.json text eol=crlf\n", encoding="utf-8"
    )
    _commit(root)
    path = root / release_manifest.MANIFEST_NAME
    path.unlink()
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--", release_manifest.MANIFEST_NAME],
        check=True,
        capture_output=True,
    )
    assert b"\r\n" in path.read_bytes()
    findings = release_manifest.check(root)
    assert any("differs from HEAD" in finding for finding in findings)


def test_a_refused_write_requires_a_committed_manifest(tmp_path):
    root = _repo(tmp_path)
    path = root / release_manifest.MANIFEST_NAME
    manifest = _manifest(root)
    manifest["entrypoints"] = []
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    findings = release_manifest.write(root, ["scripts/assurance.py"])
    assert any("differs from HEAD" in finding for finding in findings)
    assert path.read_text(encoding="utf-8") == before


def test_a_planted_temporary_symlink_is_not_followed(tmp_path):
    root = _repo(tmp_path)
    target = root / "scripts" / "assurance.py"
    target.write_text("print('new')\n", encoding="utf-8")
    _commit(root)
    planted = root / f".{release_manifest.MANIFEST_NAME}.tmp"
    planted.symlink_to(release_manifest.MANIFEST_NAME)
    assert release_manifest.write(root) == []
    manifest_path = root / release_manifest.MANIFEST_NAME
    assert not manifest_path.is_symlink()
    assert planted.is_symlink()
    digest, size = release_manifest.digest(target)
    assert _manifest(root)["files"]["scripts/assurance.py"] == {
        "sha256": digest,
        "bytes": size,
    }


def test_a_failed_replace_leaves_the_manifest_unchanged(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    target = root / "scripts" / "assurance.py"
    target.write_text("print('new')\n", encoding="utf-8")
    _commit(root)
    manifest_path = root / release_manifest.MANIFEST_NAME
    before = manifest_path.read_bytes()

    def refuse(*_args, **_kwargs):
        raise OSError("replace refused")

    monkeypatch.setattr(release_manifest.os, "replace", refuse)
    with pytest.raises(OSError):
        release_manifest.write(root)
    assert manifest_path.read_bytes() == before


def test_a_dot_path_is_refused(tmp_path):
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest["files"]["."] = {"sha256": "0" * 64, "bytes": 1}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("path is '.'" in finding for finding in findings)


def test_an_overlong_path_is_refused(tmp_path):
    root = _repo(tmp_path)
    before = (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    findings = release_manifest.write(root, ["x" * 256])
    assert any("x" * 256 in finding for finding in findings)
    assert (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8") == before


def test_an_overlong_path_in_the_manifest_is_refused(tmp_path):
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest["files"]["x" * 256] = {"sha256": "0" * 64, "bytes": 1}
    manifest["files"]["y" * 256 + "/file.py"] = {"sha256": "0" * 64, "bytes": 1}
    _rewrite(root, manifest)
    findings = release_manifest.check(root)
    assert any("x" * 256 in finding for finding in findings)
    assert any("y" * 256 in finding for finding in findings)


# ``--commit REV`` reads commit REV from the object database. These tests make the
# working tree disagree with the commit, or take it away, so a read of the disk shows.


def _head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file of the working tree, so a test can show that nothing changed."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _gitlink(root: Path, path: str) -> None:
    """Stage a submodule entry at *path*; the next commit records it."""
    subprocess.run(
        ["git", "-C", str(root), "update-index", "--add", "--cacheinfo"]
        + [f"160000,{'1' * 40},{path}"],
        check=True,
        capture_output=True,
    )


def _refused(root: Path, rev: str, expected: str) -> None:
    """Both entry points refuse commit *rev* with a finding that holds *expected*."""
    findings = release_manifest.check_commit(root, rev)
    assert any(expected in finding for finding in findings), findings
    text, findings = release_manifest.render(root, None, rev)
    assert text is None
    assert any(expected in finding for finding in findings), findings


def test_check_commit_reads_the_commit_and_not_the_working_tree(tmp_path):
    root = _repo(tmp_path)
    rev = _head(root)
    target = root / "scripts" / "assurance.py"
    target.write_text("print('edited')\n", encoding="utf-8")
    (root / release_manifest.MANIFEST_NAME).write_text("{", encoding="utf-8")
    assert release_manifest.check(root) != []
    assert release_manifest.check_commit(root, rev) == []


def test_a_bare_repository_has_no_working_tree_to_read(tmp_path):
    root = _repo(tmp_path)
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(root), str(bare)],
        check=True,
        capture_output=True,
    )
    rev = _head(root)
    assert release_manifest.check_commit(bare, rev) == []
    text, findings = release_manifest.render(bare, None, rev)
    assert findings == []
    assert text == (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")


def test_check_commit_names_a_stale_digest_at_the_commit_that_has_it(tmp_path):
    root = _repo(tmp_path)
    good = _head(root)
    target = root / "scripts" / "assurance.py"
    target.write_text("print('changed')\n", encoding="utf-8")
    _commit(root)
    assert release_manifest.check_commit(root, good) == []
    findings = release_manifest.check_commit(root, _head(root))
    assert any(
        "scripts/assurance.py" in finding and "sha256 differs" in finding
        for finding in findings
    )


@pytest.mark.parametrize("paths", [None, ["scripts/assurance.py", "scripts/other.py"]])
def test_render_is_what_write_produces_byte_for_byte(tmp_path, paths):
    root = _repo(tmp_path)
    (root / "scripts" / "other.py").write_text("print('other')\n", encoding="utf-8")
    (root / "scripts" / "assurance.py").write_text("print('new')\n", encoding="utf-8")
    _commit(root)
    before = (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    text, findings = release_manifest.render(root, paths, _head(root))
    assert findings == []
    assert release_manifest.write(root, paths) == []
    written = (root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8")
    assert text == written
    assert text != before


def test_render_writes_nothing_and_ignores_the_working_tree(tmp_path):
    root = _repo(tmp_path)
    (root / "scripts" / "assurance.py").write_text("print('new')\n", encoding="utf-8")
    _commit(root)
    rev = _head(root)
    expected = release_manifest.render(root, None, rev)
    assert expected[0] is not None and expected[1] == []
    # The working tree now disagrees with the commit wherever a read could land.
    (root / "scripts" / "assurance.py").write_text("print('dirty')\n", encoding="utf-8")
    (root / release_manifest.MANIFEST_NAME).write_text("{", encoding="utf-8")
    (root / "scripts" / "untracked.py").write_text("print('x')\n", encoding="utf-8")
    before = _snapshot(root)
    assert release_manifest.render(root, None, rev) == expected
    assert _snapshot(root) == before


def test_render_refuses_to_drop_a_declared_entrypoint(tmp_path):
    root = _repo(tmp_path)
    (root / "scripts" / "other.py").write_text("print('other')\n", encoding="utf-8")
    _commit(root)
    text, findings = release_manifest.render(root, ["scripts/other.py"], _head(root))
    assert text is None
    assert any("would leave the closure" in finding for finding in findings)


@pytest.mark.parametrize(
    "relative, message",
    [
        ("scripts/gone.py", "path is not tracked in {rev}"),
        ("scripts/local.py", "path is not tracked in {rev}"),
        (".git/description", "path is not tracked in {rev}"),
        ("alias/assurance.py", "path is not tracked in {rev}"),
        ("link.py", "path is not a regular file in {rev} (mode 120000)"),
        ("sub", "path is not a regular file in {rev} (mode 160000)"),
        ("../outside.py", "'..'"),
    ],
)
def test_a_closure_path_that_is_not_a_regular_blob_of_the_commit_is_refused(
    tmp_path, relative, message
):
    root = _repo(tmp_path)
    # This one is on disk and never committed, so a read of the disk would accept it.
    (root / "scripts" / "local.py").write_text("print('local')\n", encoding="utf-8")
    (root / "link.py").symlink_to("scripts/assurance.py")
    (root / "alias").symlink_to("scripts")
    _gitlink(root, "sub")
    _commit(root, "link.py", "alias")
    manifest = _manifest(root)
    manifest["files"][relative] = {"sha256": "0" * 64, "bytes": 1}
    _rewrite(root, manifest)
    rev = _head(root)
    _refused(root, rev, message.format(rev=rev))


def _delete_manifest(root: Path) -> None:
    (root / release_manifest.MANIFEST_NAME).unlink()


def _link_manifest(root: Path) -> None:
    _delete_manifest(root)
    (root / release_manifest.MANIFEST_NAME).symlink_to("scripts/assurance.py")


def _text_manifest(text: str):
    path = release_manifest.MANIFEST_NAME
    return lambda root: (root / path).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "change, message",
    [
        (_delete_manifest, "path is not tracked in {rev}"),
        (_link_manifest, "path is not a regular file in {rev} (mode 120000)"),
        (_text_manifest("{"), "not valid JSON"),
        (_text_manifest("[]"), "must be a JSON object"),
        (_text_manifest('{"schema": "a", "schema": "b"}'), "duplicate key"),
    ],
)
def test_a_manifest_that_cannot_be_read_at_the_commit_is_refused(
    tmp_path, change, message
):
    root = _repo(tmp_path)
    change(root)
    _commit(root)
    rev = _head(root)
    _refused(root, rev, message.format(rev=rev))


def test_an_unknown_revision_is_refused(tmp_path):
    _refused(_repo(tmp_path), "no-such-revision", "no-such-revision")


# The command line is what the workflow calls, so its boundary is pinned here
# rather than left to the functions it delegates to.

def test_cli_defaults_to_check(monkeypatch, capsys):
    monkeypatch.setattr(release_manifest, "check", lambda root: [])
    assert release_manifest.main([]) == 0
    assert capsys.readouterr().err == ""


def test_cli_check_prints_each_finding_and_exits_1(monkeypatch, capsys):
    findings = ["digest mismatch: scripts/assurance.py", "missing: docs/x.md"]
    monkeypatch.setattr(release_manifest, "check", lambda root: findings)
    assert release_manifest.main(["check"]) == 1
    err = capsys.readouterr().err
    assert "digest mismatch: scripts/assurance.py" in err
    assert "missing: docs/x.md" in err


def test_cli_check_refuses_paths(capsys):
    assert release_manifest.main(["check", "scripts/assurance.py"]) == 2
    assert "check takes no paths" in capsys.readouterr().err


def test_cli_refuses_an_unknown_action(capsys):
    assert release_manifest.main(["frobnicate"]) == 2
    assert "unknown action: frobnicate" in capsys.readouterr().err


def test_cli_write_passes_paths_and_exits_1_on_findings(monkeypatch, capsys):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        release_manifest, "write",
        lambda root, paths: seen.append(list(paths)) or ["not tracked in HEAD: x"])
    assert release_manifest.main(["write", "a.py", "b.py"]) == 1
    assert seen == [["a.py", "b.py"]]
    assert "not tracked in HEAD: x" in capsys.readouterr().err


def test_cli_write_without_findings_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(release_manifest, "write", lambda root, paths: [])
    assert release_manifest.main(["write"]) == 0
    assert capsys.readouterr().err == ""


def test_cli_check_commit_passes_the_revision(monkeypatch, capsys):
    seen: list[str] = []
    results = [[], ["stale: x"]]
    monkeypatch.setattr(
        release_manifest,
        "check_commit",
        lambda root, rev: seen.append(rev) or results.pop(0),
    )
    assert release_manifest.main(["check", "--commit", "abc123"]) == 0
    assert capsys.readouterr().err == ""
    assert release_manifest.main(["check", "--commit", "def456"]) == 1
    assert seen == ["abc123", "def456"]
    assert "stale: x" in capsys.readouterr().err


def test_cli_write_commit_prints_the_manifest_and_writes_no_file(monkeypatch, capsys):
    seen: list[tuple[list[str], str]] = []

    def render(root, paths, rev):
        seen.append((list(paths), rev))
        return '{"x": 1}\n', []

    def write(root, paths):
        raise AssertionError("write --commit must not write a file")

    monkeypatch.setattr(release_manifest, "render", render)
    monkeypatch.setattr(release_manifest, "write", write)
    assert release_manifest.main(["write", "--commit", "abc123", "a.py"]) == 0
    assert seen == [(["a.py"], "abc123")]
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ('{"x": 1}\n', "")


def test_cli_write_commit_prints_nothing_when_there_are_findings(monkeypatch, capsys):
    monkeypatch.setattr(
        release_manifest, "render", lambda root, paths, rev: (None, ["x: refused"])
    )
    assert release_manifest.main(["write", "--commit", "abc123"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "x: refused" in captured.err


@pytest.mark.parametrize(
    "argv, message",
    [
        (["check", "--commit"], "--commit needs a revision"),
        (["write", "--commit", "--help"], "--commit needs a revision"),
        (["check", "--commit", "abc123", "x.py"], "check takes no paths"),
    ],
)
def test_cli_commit_option_refuses_bad_arguments(capsys, argv, message):
    assert release_manifest.main(argv) == 2
    assert message in capsys.readouterr().err
