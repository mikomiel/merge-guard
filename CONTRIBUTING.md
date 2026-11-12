# Contributing

## Before you open a pull request

1. Branch from `main`. One branch, one author.
2. Open an issue before a behaviour change; a bug fix can go straight to a pull
   request. Use the issue form and keep the report synthetic.
3. Run the checks locally:

   ```bash
   python3 scripts/check_estate_identifiers.py
   ruff check .
   python -m pytest -q
   ```

4. Open one pull request. Keep it to one change, and say what evidence it adds.

## Required checks

`main` requires these contexts on the pull request's exact head, all strict:

| Context | What it proves |
| --- | --- |
| `checks` | The public-content scan passed, and the lint and test suite passed. |
| `review` | `merge-guard review` published an approval for this exact head. |
| `dependency-review` | The change does not add a dependency with a known vulnerability. |

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
`action.yml` and `requirements-dev.txt` are outside it, so a change to the gate's
own code or to its workflow is merged by a person after the review. The author of
a pull request never merges it here.

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
