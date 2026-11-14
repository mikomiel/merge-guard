"""The release closure: which files a consumer copies, and their exact bytes.

A release here is an annotated tag on ``main``, but a tag names a commit, not a
closure. Before this manifest existed the closure was prose in the release
notes. Prose cannot be checked and it was not always written: ``v0.0.2`` states
no closure at all, and ``v0.0.1``'s tag annotation and release body name
different file sets. A release note is also uncorrectable once the tag exists,
because this repository does not move a tag.

``release-manifest.json`` at the repository root is therefore the closure's only
owner: the product, the entrypoints a consumer imports, and for every closure
file its SHA-256 and size. Every digest is taken from the blob in the
checked-out commit, never from the working tree: a checkout can rewrite line
endings, and a consumer reproduces bytes from the tag. It deliberately does not name a version -- the tag
names the commit, and the manifest at that commit is that tag's closure. Keeping
the version out is what lets a release pull request that only bumps the version
leave this file untouched, and it removes the circularity of a file that would
otherwise have to record the digest of the commit containing it.

``check`` is what CI runs on every pull request, so the manifest is true at every
commit a tag can point at. ``write`` regenerates the digests from the same
commit, so commit a closure change before regenerating the manifest; the closure
set itself is a release decision and stays a deliberate edit of this manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "release-manifest.json"
SCHEMA = "release-manifest.v1"
PRODUCT = "mikomiel/merge-guard"
TOP_LEVEL_KEYS = {"schema", "product", "entrypoints", "files"}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def digest(path: Path) -> tuple[str, int]:
    """Return the SHA-256 and byte size of a regular file."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _finding(relative: str, message: str) -> str:
    return f"{relative}: {message}"


def _closure_problem(root: Path, relative: str) -> str | None:
    """Refuse a closure path that escapes *root* or is not a plain file on disk."""
    if not relative or relative != relative.strip():
        return "path is empty or padded with whitespace"
    candidate = PurePosixPath(relative)
    if not candidate.parts:
        return "path is '.' or empty; a closure names a file"
    if "\\" in relative:
        return "path uses a backslash separator"
    if candidate.is_absolute():
        return "path is absolute"
    if ".." in candidate.parts:
        return "path contains '..'"
    if str(candidate) != relative:
        return "path is not in normalised repository-relative form"
    if relative == MANIFEST_NAME:
        return "the manifest never lists itself"
    current = root
    for part in candidate.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return "path traverses a symlink; a closure names regular files"
    target = current / candidate.parts[-1]
    if target.is_symlink():
        return "path is a symlink; a closure names regular files"
    if not target.is_file():
        return "path is missing or not a regular file"
    return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse a JSON object that declares the same key twice."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _tree_entries(root: Path) -> tuple[dict[str, tuple[str, str]] | None, str | None]:
    """Map every path in the checked-out commit to its Git mode and blob id.

    A closure names files a consumer extracts from the tag, so presence in the
    checkout is not enough: repository metadata, an untracked file and a
    submodule all exist on disk without being a regular blob in the commit.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-r", "-z", "--full-tree", "HEAD"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git is unavailable: {exc}"
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        return None, f"not a git checkout with a readable HEAD: {message}"
    entries: dict[str, tuple[str, str]] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) != 3:
            return None, "git ls-tree output is not understood"
        entries[path.decode("utf-8", "surrogateescape")] = (
            parts[0].decode("ascii"),
            parts[2].decode("ascii"),
        )
    return entries, None


def _tree_finding(entries: dict[str, tuple[str, str]], relative: str) -> str | None:
    entry = entries.get(relative)
    if entry is None:
        return "path is not tracked in HEAD"
    mode, _oid = entry
    if mode not in {"100644", "100755"}:
        return f"path is not a regular file in HEAD (mode {mode})"
    return None


def _blob_digest(root: Path, oid: str) -> tuple[tuple[str, int] | None, str | None]:
    """Digest the committed blob, never the possibly converted checkout bytes.

    A checkout can differ from the commit by design -- ``.gitattributes`` with
    ``eol=crlf`` rewrites the working tree -- so hashing the file would publish
    digests a consumer cannot reproduce from the tag.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", oid],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git is unavailable: {exc}"
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        return None, f"cannot read the committed blob: {message}"
    return (hashlib.sha256(completed.stdout).hexdigest(), len(completed.stdout)), None


def _load(root: Path) -> tuple[dict[str, Any] | None, bytes, list[str]]:
    path = root / MANIFEST_NAME
    if path.is_symlink():
        return None, b"", [_finding(MANIFEST_NAME, "must be a regular file, not a symlink")]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, b"", [_finding(MANIFEST_NAME, f"missing or unreadable: {exc}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, b"", [_finding(MANIFEST_NAME, f"not valid UTF-8: {exc}")]
    try:
        manifest = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        return None, b"", [_finding(MANIFEST_NAME, f"not valid JSON: {exc}")]
    except ValueError as exc:
        return None, b"", [_finding(MANIFEST_NAME, f"invalid JSON object: {exc}")]
    if not isinstance(manifest, dict):
        return None, b"", [_finding(MANIFEST_NAME, "must be a JSON object")]
    return manifest, raw, []


def _validate_structure(manifest: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    unknown = sorted(set(manifest) - TOP_LEVEL_KEYS)
    if unknown:
        findings.append(_finding(MANIFEST_NAME, f"unknown keys: {', '.join(unknown)}"))
    if manifest.get("schema") != SCHEMA:
        findings.append(
            _finding(MANIFEST_NAME, f"'schema' must be {SCHEMA!r}")
        )
    if manifest.get("product") != PRODUCT:
        findings.append(_finding(MANIFEST_NAME, f"'product' must be {PRODUCT!r}"))

    entrypoints = manifest.get("entrypoints")
    if not isinstance(entrypoints, list) or any(
        not isinstance(item, str) for item in entrypoints
    ):
        findings.append(_finding(MANIFEST_NAME, "'entrypoints' is not a list of strings"))

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        findings.append(_finding(MANIFEST_NAME, "'files' is not a non-empty JSON object"))
    return findings


def _manifest_self_problem(
    root: Path, entries: dict[str, tuple[str, str]], raw: bytes
) -> str | None:
    """Refuse a manifest whose committed form differs from the copy just read.

    A Git attribute can transform a checkout, so a manifest that is only valid
    after conversion is not the manifest a consumer extracts from the tag.
    """
    entry = entries.get(MANIFEST_NAME)
    if entry is None:
        return "the manifest is not tracked in HEAD"
    mode, oid = entry
    if mode not in {"100644", "100755"}:
        return f"the manifest is not a regular file in HEAD (mode {mode})"
    blob, problem = _blob_digest(root, oid)
    if blob is None:
        return problem or "the manifest blob is unreadable"
    if blob != (hashlib.sha256(raw).hexdigest(), len(raw)):
        return "the working-tree copy differs from HEAD; commit or discard it"
    return None


def _declaration_findings(
    manifest: dict[str, Any],
    root: Path,
    entries: dict[str, tuple[str, str]],
) -> list[str]:
    findings: list[str] = []
    files = manifest.get("files")
    entrypoints = manifest.get("entrypoints")
    if not isinstance(files, dict) or not isinstance(entrypoints, list):
        return findings

    for relative, entry in sorted(files.items()):
        if not isinstance(relative, str):
            findings.append(_finding(MANIFEST_NAME, "a 'files' key is not a string"))
            continue
        if not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"}:
            findings.append(
                _finding(relative, "entry must hold exactly 'sha256' and 'bytes'")
            )
            continue
        recorded, size = entry["sha256"], entry["bytes"]
        if not isinstance(recorded, str) or not _SHA256.fullmatch(recorded):
            findings.append(_finding(relative, "'sha256' is not 64 lowercase hex digits"))
            continue
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            findings.append(_finding(relative, "'bytes' is not a non-negative integer"))
            continue
        problem = _closure_problem(root, relative)
        if problem is not None:
            findings.append(_finding(relative, problem))
            continue
        tree_finding = _tree_finding(entries, relative)
        if tree_finding is not None:
            findings.append(_finding(relative, tree_finding))
            continue
        _mode, oid = entries[relative]
        blob, blob_problem = _blob_digest(root, oid)
        if blob is None:
            findings.append(_finding(relative, blob_problem or "blob is unreadable"))
            continue
        actual, actual_size = blob
        if actual_size != size:
            findings.append(
                _finding(relative, f"size differs: manifest {size}, tree {actual_size}")
            )
        if actual != recorded:
            findings.append(
                _finding(relative, f"sha256 differs: manifest {recorded}, tree {actual}")
            )

    for entrypoint in entrypoints:
        if isinstance(entrypoint, str) and entrypoint not in files:
            findings.append(
                _finding(entrypoint, "entrypoint is not part of the declared closure")
            )
    return findings


def check(root: Path) -> list[str]:
    """Return every finding for a commit whose manifest does not describe it."""
    manifest, raw, findings = _load(root)
    if manifest is None:
        return findings
    findings += _validate_structure(manifest)
    if not isinstance(manifest.get("files"), dict):
        return findings

    entries, tree_problem = _tree_entries(root)
    if entries is None:
        findings.append(_finding(MANIFEST_NAME, tree_problem or "no tree"))
        return findings
    self_problem = _manifest_self_problem(root, entries, raw)
    if self_problem is not None:
        findings.append(_finding(MANIFEST_NAME, self_problem))
        return findings
    return findings + _declaration_findings(manifest, root, entries)


def write(root: Path, paths: list[str] | None = None) -> list[str]:
    """Regenerate the manifest; *paths* replaces the closure when given.

    The declarations come from the committed manifest, so a declaration change
    is committed before the digests are regenerated; *paths* is the explicit way
    to change the closure itself.
    """
    manifest, raw, findings = _load(root)
    if manifest is None:
        return findings
    findings += _validate_structure(manifest)
    if findings:
        return findings
    files = manifest["files"]
    closure = sorted(paths) if paths else sorted(files)

    entries, tree_problem = _tree_entries(root)
    if entries is None:
        return [_finding(MANIFEST_NAME, tree_problem or "no tree")]
    self_problem = _manifest_self_problem(root, entries, raw)
    if self_problem is not None:
        return [_finding(MANIFEST_NAME, self_problem)]

    replaced: dict[str, dict[str, Any]] = {}
    for relative in closure:
        problem = _closure_problem(root, relative)
        if problem is not None:
            findings.append(_finding(relative, problem))
            continue
        tree_finding = _tree_finding(entries, relative)
        if tree_finding is not None:
            findings.append(_finding(relative, tree_finding))
            continue
        _mode, oid = entries[relative]
        blob, blob_problem = _blob_digest(root, oid)
        if blob is None:
            findings.append(_finding(relative, blob_problem or "blob is unreadable"))
            continue
        actual, size = blob
        replaced[relative] = {"sha256": actual, "bytes": size}
    if findings:
        return findings

    for entrypoint in manifest["entrypoints"]:
        if entrypoint not in replaced:
            return [
                _finding(
                    entrypoint,
                    "would leave the closure, but is still an entrypoint; "
                    f"edit 'entrypoints' in {MANIFEST_NAME} first",
                )
            ]

    updated = dict(manifest)
    updated["files"] = replaced
    problems = _declaration_findings(updated, root, entries)
    if problems:
        return problems
    mode = (root / MANIFEST_NAME).stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root, prefix=f".{MANIFEST_NAME}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(updated, indent=2, sort_keys=False) + "\n")
        os.chmod(temporary, mode)
        os.replace(temporary, root / MANIFEST_NAME)
    finally:
        temporary.unlink(missing_ok=True)
    return []


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    action = "check"
    rest = list(argv)
    if rest and rest[0] in {"check", "write"}:
        action = rest[0]
        rest = rest[1:]
    elif rest:
        print(f"unknown action: {rest[0]}", file=sys.stderr)
        return 2
    if action == "check" and rest:
        print("check takes no paths", file=sys.stderr)
        return 2

    findings = check(root) if action == "check" else write(root, rest)
    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
