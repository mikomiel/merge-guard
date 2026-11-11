# Contributing

## Before you open a pull request

1. Branch from `main`. One branch, one author.
2. Run the checks locally: `python3 scripts/check_estate_identifiers.py`, then
   lint and tests once they exist.
3. Open one pull request. Keep it to one change, and say what evidence it adds.

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

An allowlist in that file decides which paths may land this way. `scripts/`,
`tests/`, `.github/`, `action.yml` and `requirements-dev.txt` are outside it, so a
change to the gate's own code or to its workflow is merged by a person after the
review. The author of a pull request never merges it here.

## Public by construction

This repository is public and has no private counterpart. Do not paste
material from a private environment: host names, private addresses, container
or ticket numbers, customer names, credentials. The identifier scan fails on
whole classes of that material. Repository-specific patterns belong in an
untracked `.estate-identifiers` file, never in the tree.

## Licence

Contributions are accepted under the Apache-2.0 licence; inbound equals
outbound.
