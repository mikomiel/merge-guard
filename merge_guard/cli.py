"""The merge-guard command line: the consumer contract.

``docs/cli.md`` documents the modes, the JSON envelope, the exit codes and the
policy schema; ``tests/test_contract.py`` pins them. Changing any of them is a
breaking change, not a cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from . import __version__
from . import policy as mg
from .engine import ApiError, run_merge, run_review, trusted_context
from .forgejo import Forgejo
from .github import GitHub

MODES = ("status", "review", "merge")

#: The run completed. It does NOT mean the verdict was positive: the verdict is
#: in the JSON ``result`` and in the forge status. Never gate on this code.
EXIT_OK = 0
#: An operation failed after the inputs were accepted (API, postcondition).
EXIT_OPERATIONAL = 1
#: The inputs are unusable, or a write was attempted outside a trusted context.
EXIT_UNUSABLE = 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="merge-guard", description=__doc__)
    result.add_argument("mode", choices=MODES)
    result.add_argument("--policy", default=".github/merge-guard.toml")
    result.add_argument("--forge", choices=mg.FORGES,
                        help="the forge, when the policy file does not name it")
    result.add_argument("--apply", action="store_true",
                        help="publish or merge; trusted default-branch workflow only")
    result.add_argument("--json", action="store_true",
                        help="machine output; already the default, accepted explicitly")
    result.add_argument("--version", action="version",
                        version=f"merge-guard {__version__}")
    return result


def read_policy(path: str) -> mg.Policy:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return mg.load_policy(data)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.mode == "status" and args.apply:
        print("merge_guard_refused: status_is_read_only", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        policy = read_policy(args.policy)
    except UnicodeDecodeError:
        print("merge_guard_refused: policy_not_utf8", file=sys.stderr)
        return EXIT_UNUSABLE
    except (OSError, tomllib.TOMLDecodeError, mg.Unusable) as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        policy = mg.require_forge(policy, args.forge)
    except mg.Unusable as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    # The trusted context is a GitHub workflow context. A Forgejo write needs a
    # trust argument of its own, which this release does not define, so no
    # context makes one: that is its own code, not the trusted-context refusal.
    if args.mode != "status" and args.apply and policy.forge == "forgejo":
        print("merge_guard_refused: forgejo_write_unsupported", file=sys.stderr)
        return EXIT_UNUSABLE
    if args.mode != "status" and args.apply and not trusted_context(policy, args.mode):
        print("write_refused_outside_trusted_workflow", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        api = (GitHub(policy, apply=args.apply) if policy.forge == "github"
               else Forgejo.from_environment(policy, apply=args.apply))
    except mg.Unusable as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        if args.mode == "review":
            result = run_review(api, policy, args.apply, os.environ.get("EVENT_HEAD"))
        elif args.mode == "merge":
            result = run_merge(api, policy, args.apply)
        else:
            result = run_review(api, policy, False, None)
    except (ApiError, mg.Unusable) as exc:
        print(f"merge_guard_refused: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    print(json.dumps({"mode": args.mode, "apply": args.apply, "result": result},
                     sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
