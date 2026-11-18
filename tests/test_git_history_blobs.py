from __future__ import annotations

import subprocess
import sys
from ipaddress import IPv4Address
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_history_scan_catches_content_added_and_removed_in_merges(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        completed = subprocess.run(
            [
                "git", "-c", "user.name=Synthetic Test",
                "-c", "user.email=test@example.test", "-c", "commit.gpgsign=false",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "Base commit")
    git("checkout", "-b", "addition")
    git("commit", "--allow-empty", "-m", "Addition branch")
    git("checkout", "main")
    git("merge", "--no-ff", "--no-commit", "addition")
    identifier = str(IPv4Address(bytes((192, 168, 40, 7))))
    (tmp_path / "synthetic.txt").write_text(identifier, encoding="utf-8")
    git("add", "synthetic.txt")
    git("commit", "-m", "Merge the addition")

    git("checkout", "-b", "removal")
    git("commit", "--allow-empty", "-m", "Removal branch")
    git("checkout", "main")
    git("merge", "--no-ff", "--no-commit", "removal")
    git("rm", "synthetic.txt")
    git("commit", "-m", "Merge the removal")

    assert git("ls-tree", "-r", "--name-only", "HEAD") == ""
    assert identifier not in git("log", "HEAD", "--format=", "--patch")

    history = subprocess.check_output(["bash", SCRIPTS / "git_history_blobs.sh"], cwd=tmp_path)
    assert identifier.encode() in history
    result = subprocess.run(
        [sys.executable, SCRIPTS / "check_estate_identifiers.py", "--stdin"],
        cwd=tmp_path,
        input=history,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert b"stdin:1: private-ipv4" in result.stdout
    assert identifier.encode() not in result.stdout + result.stderr
