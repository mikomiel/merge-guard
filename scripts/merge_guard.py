"""Entry point used by the composite action: ``scripts/merge_guard.py <mode>``.

The engine lives in the ``merge_guard`` package. The action's ``run:`` line
invokes this path and consumers pin the action by tag, so the path stays even
though it is a shim. This script runs with ``scripts/`` as ``sys.path[0]``, so
the package root has to be added explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from merge_guard.cli import main

if __name__ == "__main__":
    sys.exit(main())
