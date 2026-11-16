"""Derive the action's `merged` output from the engine's JSON envelope.

`docs/cli.md` owns the envelope, the modes and the exit codes. This script reads
only what the engine already prints (`result[*].merged`), so reporting whether a
run landed a pull request costs no new command-line flag and no second source of
truth for the same fact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def merged(envelope: dict[str, Any]) -> bool:
    """True only for an applied merge that landed one pull request."""
    if envelope.get("mode") != "merge" or envelope.get("apply") is not True:
        return False
    result = envelope.get("result")
    return isinstance(result, list) and any(
        isinstance(item, dict) and item.get("merged") is True for item in result
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    if len(args) != 3:
        print("merge_guard_refused: envelope_arguments", file=sys.stderr)
        return 2
    envelope_path, output_path = Path(args[1]), Path(args[2])
    try:
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Fail closed: an unreadable envelope must never read as a merge.
        print("merge_guard_refused: envelope_unreadable", file=sys.stderr)
        return 1
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(f"merged={'true' if merged(envelope) else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
