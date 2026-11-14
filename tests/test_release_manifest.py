"""The release closure manifest must describe the tree it sits in.

The manifest is the closure's only owner, so two things need pinning: the
repository's own manifest matches its tree, and a tree that drifts is named file
by file rather than accepted. The write path is pinned too, because a generator
that silently drops an entrypoint would publish a closure that no longer holds
what a consumer imports.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import release_manifest

ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path: Path) -> Path:
    """A tiny repository whose manifest matches its tree."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    target = root / "scripts" / "assurance.py"
    target.write_text("print('classify')\n", encoding="utf-8")
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
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / release_manifest.MANIFEST_NAME).read_text(encoding="utf-8"))


def _rewrite(root: Path, manifest: dict) -> None:
    (root / release_manifest.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def test_the_repository_manifest_matches_the_tree():
    assert release_manifest.check(ROOT) == []


def test_a_mutated_file_is_named_with_its_digest(tmp_path):
    root = _repo(tmp_path)
    (root / "scripts" / "assurance.py").write_text("print('changed')\n", encoding="utf-8")
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
    assert release_manifest.write(root) == []
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
    findings = release_manifest.write(root, ["scripts/other.py"])
    assert any("scripts/assurance.py" in finding for finding in findings)
    assert "scripts/other.py" not in _manifest(root)["files"]


def test_write_replaces_the_closure_when_paths_are_given(tmp_path):
    root = _repo(tmp_path)
    other = root / "scripts" / "other.py"
    other.write_text("print('other')\n", encoding="utf-8")
    assert release_manifest.write(root, ["scripts/assurance.py", "scripts/other.py"]) == []
    assert sorted(_manifest(root)["files"]) == [
        "scripts/assurance.py",
        "scripts/other.py",
    ]
