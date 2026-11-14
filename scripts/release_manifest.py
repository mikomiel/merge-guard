"""The release closure: which files a consumer copies, and their exact bytes.

A release here is an annotated tag on ``main``, but a tag names a commit, not a
closure. Before this manifest existed the closure was prose in the release
notes. Prose cannot be checked and it was not always written: ``v0.0.2`` states
no closure at all, and ``v0.0.1``'s tag annotation and release body name
different file sets. A release note is also uncorrectable once the tag exists,
because this repository does not move a tag.

``release-manifest.json`` at the repository root is therefore the closure's only
owner: the product, the entrypoints a consumer imports, and for every closure
file its SHA-256 and size. It deliberately does not name a version -- the tag
names the commit, and the manifest at that commit is that tag's closure. Keeping
the version out is what lets a release pull request that only bumps the version
leave this file untouched, and it removes the circularity of a file that would
otherwise have to record the digest of the commit containing it.

``check`` is what CI runs on every pull request, so the manifest is true at every
commit a tag can point at. ``write`` regenerates the digests; the closure set
itself is a release decision and stays a deliberate edit of this manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
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


def _closure_path(root: Path, relative: str) -> tuple[Path | None, str | None]:
    """Resolve one closure path inside *root*, refusing anything that escapes it."""
    if not relative or relative != relative.strip():
        return None, "path is empty or padded with whitespace"
    candidate = PurePosixPath(relative)
    if "\\" in relative:
        return None, "path uses a backslash separator"
    if candidate.is_absolute():
        return None, "path is absolute"
    if ".." in candidate.parts:
        return None, "path contains '..'"
    if str(candidate) != relative:
        return None, "path is not in normalised repository-relative form"
    if relative == MANIFEST_NAME:
        return None, "the manifest never lists itself"
    current = root
    for part in candidate.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return None, "path traverses a symlink; a closure names regular files"
    target = current / candidate.parts[-1]
    if target.is_symlink():
        return None, "path is a symlink; a closure names regular files"
    if not target.is_file():
        return None, "path is missing or not a regular file"
    return target, None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse a JSON object that declares the same key twice."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _tree_modes(root: Path) -> tuple[dict[str, str] | None, str | None]:
    """Map every path in the checked-out commit to its Git file mode.

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
    modes: dict[str, str] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) != 3:
            return None, "git ls-tree output is not understood"
        modes[path.decode("utf-8", "surrogateescape")] = parts[0].decode("ascii")
    return modes, None


def _tree_finding(modes: dict[str, str], relative: str) -> str | None:
    mode = modes.get(relative)
    if mode is None:
        return "path is not tracked in HEAD"
    if mode not in {"100644", "100755"}:
        return f"path is not a regular file in HEAD (mode {mode})"
    return None


def _load(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = root / MANIFEST_NAME
    if path.is_symlink():
        return None, [_finding(MANIFEST_NAME, "must be a regular file, not a symlink")]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return None, [_finding(MANIFEST_NAME, f"missing or unreadable: {exc}")]
    try:
        manifest = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        return None, [_finding(MANIFEST_NAME, f"not valid JSON: {exc}")]
    except ValueError as exc:
        return None, [_finding(MANIFEST_NAME, f"invalid JSON object: {exc}")]
    if not isinstance(manifest, dict):
        return None, [_finding(MANIFEST_NAME, "must be a JSON object")]
    return manifest, []


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


def check(root: Path) -> list[str]:
    """Return every finding for a tree whose manifest does not describe it."""
    manifest, findings = _load(root)
    if manifest is None:
        return findings
    findings += _validate_structure(manifest)
    files = manifest.get("files")
    entrypoints = manifest.get("entrypoints")
    if not isinstance(files, dict) or not isinstance(entrypoints, list):
        return findings

    modes, tree_problem = _tree_modes(root)
    if modes is None:
        findings.append(_finding(MANIFEST_NAME, tree_problem or "no tree"))
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
        target, problem = _closure_path(root, relative)
        if problem is not None:
            findings.append(_finding(relative, problem))
            continue
        tree_finding = _tree_finding(modes, relative)
        if tree_finding is not None:
            findings.append(_finding(relative, tree_finding))
            continue
        assert target is not None
        actual, actual_size = digest(target)
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


def write(root: Path, paths: list[str] | None = None) -> list[str]:
    """Regenerate the manifest; *paths* replaces the closure when given."""
    manifest, findings = _load(root)
    if manifest is None:
        return findings
    findings += _validate_structure(manifest)
    if findings:
        return findings
    files = manifest["files"]
    entrypoints = manifest["entrypoints"]
    closure = sorted(paths) if paths else sorted(files)

    modes, tree_problem = _tree_modes(root)
    if modes is None:
        return [_finding(MANIFEST_NAME, tree_problem or "no tree")]

    replaced: dict[str, dict[str, Any]] = {}
    for relative in closure:
        target, problem = _closure_path(root, relative)
        if problem is not None:
            findings.append(_finding(relative, problem))
            continue
        tree_finding = _tree_finding(modes, relative)
        if tree_finding is not None:
            findings.append(_finding(relative, tree_finding))
            continue
        assert target is not None
        actual, size = digest(target)
        replaced[relative] = {"sha256": actual, "bytes": size}
    if findings:
        return findings

    for entrypoint in entrypoints:
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
    (root / MANIFEST_NAME).write_text(
        json.dumps(updated, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return check(root)


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
