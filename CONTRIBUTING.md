# Contributing

## Before you open a pull request

1. Branch from `main`. One branch, one author.
2. Run the checks locally: `python3 scripts/check_estate_identifiers.py`, then
   lint and tests once they exist.
3. Open one pull request. Keep it to one change, and say what evidence it adds.

## What lands

A pull request lands only after an independent review approves the exact head
and CI is green on that same head. A new commit needs a new review and a new
run. Until the gate in issue #2 lands here, a person merges after the review approves the exact head and CI is green; the author does not merge their own pull request.

## Public by construction

This repository is public and has no private counterpart. Do not paste
material from a private environment: host names, private addresses, container
or ticket numbers, customer names, credentials. The identifier scan fails on
whole classes of that material. Repository-specific patterns belong in an
untracked `.estate-identifiers` file, never in the tree.

## Licence

Contributions are accepted under the Apache-2.0 licence; inbound equals
outbound.
