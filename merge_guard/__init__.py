"""merge-guard: a review gate and a single-pull-request merger for GitHub.

The package owns the engine and the command line. ``scripts/merge_guard.py``
stays a thin entry point because the composite action invokes that path, and
consumers pin the action by tag.
"""

from __future__ import annotations

from . import engine, policy

#: The source version. Release-please owns this value once the release workflow
#: (D13) lands; until then a release tag is set by hand and this value moves
#: with it.
__version__ = "0.5.0"

__all__ = ["__version__", "engine", "policy"]
