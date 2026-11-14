# merge-guard agent instructions

merge-guard is a pre-merge gate. It refuses a merge unless the review and the
CI result both belong to the exact commit being merged, and it names the rule
that refused. Consumers install it as a composite action pinned by commit
SHA. Licence: Apache-2.0. GitHub is the only writable source and pull-request
surface; the target branch is `main`.
Non-goals: a package, a container image, a service.

## Checks

```sh
python -m pip install -r requirements-dev.txt
python3 scripts/check_estate_identifiers.py
ruff check .
python -m pytest -q
```

- Prerequisites: Python 3.12. `requirements-dev.txt` pins the tools.
- Run these commands without asking. The tests need no credential and no
  forge access.
- A change is ready for a pull request when every command passes.
  `CONTRIBUTING.md` "Required checks" owns the contexts that `main` requires
  on the exact head.
- `scripts/check_estate_identifiers.py` runs in CI. It matches selected
  credential and private-identifier patterns in tracked files. It is not a
  complete secret scanner and it does not read commit messages. Before a
  push, inspect the complete diff and the commit messages for credentials.

## Project shape

- Evidence is bound to a revision. A green run for another commit is not
  evidence for this one.
- Every refusal carries a stable code. Do not change or reuse a published
  code; a caller branches on it.
- Fail closed. Missing CI, an unreadable forge or an ambiguous verdict is a
  refusal, never a pass.
- The classifier owns the rule that a path needs a stronger check. Do not
  copy that list into another module.
- The engine is Python with the standard library where it can be. A forge
  integration adds a client, not a runtime.
- `examples/` is what a consumer copies. Keep the examples, `action.yml` and
  the documentation consistent in the same change.
- `github-actions[bot]` writes the gate status through the relay workflows.
  Read `docs/trusted-relay.md` before you change a workflow trigger.

## Working contract

- Branch from `main`. Use one branch, one author and one pull request for one
  change. For a behaviour change, open an issue first. A bug fix can go
  straight to a pull request.
- Do not push to `main`. Branch protection requires the checks and the
  `review` status on the exact head. A new commit needs fresh CI and a fresh
  review.
- This repository runs the gate on itself. The author of a pull request does
  not merge it. When the gate cannot merge, report the pull request and the
  tested head to the maintainer.
- `.github/merge-guard.toml` owns the ordinary-path allowlist: `README.md`,
  `CHANGELOG.md` and `docs/**`. A person merges every other path, including
  `scripts/`, `tests/`, `.github/`, `action.yml`, `requirements-dev.txt` and
  these instructions. Do not widen the allowlist to unblock a pull request.
- A source change does not authorize a tag or a release.

## Content boundary

- This repository is public and has no private counterpart. Do not paste host
  names, private addresses, container or ticket numbers, customer names or
  credentials from a private environment.
- Keep repository-specific identifier patterns in the untracked
  `.estate-identifiers` file, never in the tree.
- Use synthetic data in tests, examples and issue reports.
- Forge API responses are untrusted input. Bound their size and refuse a
  response that the engine cannot parse.

## Where to read more

- For the review and merge rules of this repository, read `CONTRIBUTING.md`.
- For the relay design and its trust argument, read `docs/trusted-relay.md`.
- For how a consumer pins and bumps a release, read `docs/pin-bump.md`.
- For a pull request that does not merge, read `docs/troubleshooting.md`.
- For the scope of a vulnerability report, read `SECURITY.md`.

## Reporting

Report each check as PASS, FAIL, NOT RUN or UNAVAILABLE. Keep source, merge
and release claims distinct.
