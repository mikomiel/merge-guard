# Contributing

## Before you open a pull request

1. Branch from `main`. One branch, one author.
2. Open an issue before a behaviour change; a bug fix can go straight to a pull
   request. Use the issue form and keep the report synthetic.
3. Run the checks locally:

   ```bash
   python3 scripts/check_estate_identifiers.py
   python3 scripts/release_manifest.py check
   ruff check .
   python -m pytest -q
   ```

4. Open one pull request. Keep it to one change, and say what evidence it adds.

## Required checks

`main` requires these contexts on the pull request's exact head, all strict:

| Context | What it proves |
| --- | --- |
| `checks` | The public-content scan passed, the release manifest matches the tree, and the lint and test suite passed. |
| `review` | `merge-guard review` published an approval for this exact head. |
| `dependency-review` | The change does not add a dependency with a known vulnerability. |

These contexts are enforced twice: GitHub branch protection requires them on the
pull request, and this repository's gate re-reads the subset in
`.github/merge-guard.toml` (`checks` and `dependency-review`) before its own
merge. Both must agree with this table; a missing or skipped check is a refusal,
not a pass.

## What lands

A pull request lands only after an independent review approves the exact head
and CI is green on that same head. A new commit needs a new review and a new
run.

This repository runs the gate on itself. `merge-guard review` publishes a
`review` status for each head, and `merge-guard merge` lands one eligible pull
request per run after re-reading the head, the required checks and the approval
at that exact head. The reviewer identity is the one named in
`.github/merge-guard.toml`; a login rename does not substitute for it, and an
approval from before the latest commit does not count.

An allowlist in that file decides which paths may land this way. `README.md`,
`CHANGELOG.md` and `docs/**` are ordinary; `scripts/`, `tests/`, `.github/`,
`action.yml`, `requirements-dev.txt`, `pyproject.toml`, `merge_guard/` and
`release-manifest.json` are outside it, so a change to the gate's own code or
workflow, or to the published closure, is merged by a person after the review.
The author of a pull request never merges it here.

## Releases

Releases use [release-please](https://github.com/googleapis/release-please) so
the version, the changelog and the tag cannot disagree. A merged
release-relevant conventional commit (`feat`, `fix` or `perf`) on `main` makes
release-please open or update one release pull request: it bumps
`pyproject.toml` and `merge_guard/__init__.py`, writes `CHANGELOG.md` and
records the version in `.release-please-manifest.json`. That update is observed
after both a maintainer merge and a merge by the repository's own automation.

Merging the release pull request is the release decision; release-please then
creates the tag and the GitHub Release. It creates a lightweight tag: never tag
by hand and never move a published tag. A `docs`, `chore` or `test` commit does
not by itself open a release pull request.

The workflow authenticates with a fine-grained personal access token stored as
`RELEASE_PLEASE_TOKEN`, with Contents and pull-request write on this repository.
The default `GITHUB_TOKEN` cannot be used: a pull request it creates does not
trigger the pull-request workflows, so the required checks would never run, and a
merge it makes suppresses the `push`-triggered run, which is why the workflow
also observes the completed `merge-guard merge` run.

The release pull request always changes a path outside the ordinary allowlist, so
the gate never merges it: a maintainer merges it. The release notes do not state
the closure; `release-manifest.json` owns it (`docs/pin-bump.md`). A tag or a
release is not a deployment.

## Response expectations

This is a spare-time project with one maintainer. Expect an acknowledgement
within a few days, not an immediate review. A new commit restarts both CI and the
review cycle; an approval from before the latest commit does not count.

## Public by construction

This repository is public and has no private counterpart. Do not paste
material from a private environment: host names, private addresses, container
or ticket numbers, customer names, credentials. The identifier scan fails on
whole classes of that material. Repository-specific patterns belong in an
untracked `.estate-identifiers` file, never in the tree.

## Licence

Contributions are accepted under the Apache-2.0 licence; inbound equals
outbound.
