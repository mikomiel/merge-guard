# Troubleshooting

## The pull request is `BLOCKED` and a required check never ran

**Symptom.** The roll-up carries the gate's `review` status and no run of the
consumer's required check — commonly `checks` — for the exact head. The branch
protection rule is `strict`, so the pull request cannot merge, and with
`enforce_admins` on there is no manual override.

The gate names the condition: `review` is `pending` with the description
`required_check_missing: <name>`, and a merge run refuses
`REQUIRED_CHECK_RUN_MISSING`. A required check that exists but is red or still
running keeps its own `checks` context and the `required_check_not_success`
refusal; only a missing run produces this state.

**Cause.** `pull_request` workflows fire on `opened`, `synchronize` and
`reopened`. In the case reported in issue #14 a base merge left the new head with
no `checks` run while the gate's own workflow did run for it. Why GitHub did not
dispatch a run for that head is not established from the workflows alone; what is
established is the consequence, and that a new event fixes it.

**Recovery.** Either of these makes GitHub dispatch a new event for the head:

- dispatch the consumer's CI workflow for the pull request branch:
  `gh workflow run checks.yml --ref <pr-branch>`
- close the pull request and reopen it;
- push any commit to the branch.

The dispatched workflow must declare `workflow_dispatch`; this repository's
`checks.yml` does, and a consumer adds it to its own CI workflow. A dispatch
runs the workflow file at the ref and only works for a same-repository branch:
GitHub does not offer it for a branch in a fork. Close/reopen is the fork-safe
fallback.

Prefer the dispatch or reopening: neither rewrites the branch's history.

Re-running an older workflow run is **not** a recovery. A re-run acts on that
run's own SHA, so it produces no evidence for the new head.

## The gate says `pending` and the review looks approved

Read the status description. `approved_at_head` means a qualifying approval is at
this head. `current_head_approval_required` means none is, and two causes are
worth checking:

- **The approval is at an earlier commit.** This repository sets
  `carry_unchanged_diff = false` in `.github/merge-guard.toml`, so an approval of
  an earlier head never carries — not after a base update, and not after a commit
  that leaves the patch identical. Every head needs its own approval.
- **The approving identity does not qualify.** Only the configured reviewer, or a
  user the API reports as write, maintain or admin, counts. A review from anyone
  else leaves the status pending however visible the approval looks, and asking
  that same person again will not change it.

Request a fresh review of the current head, from a qualifying reviewer, rather
than re-running the review workflow: re-reading the old approval cannot change
either fact.

The description `required_check_missing: <name>` is not a review verdict at all:
the approval is at this head, but a required check has no run for it. Use the
recovery in the section above.

## A pull request is approved, green, and still not merged

Check, in this order:

1. **The required checks, on this head.** Every name in `required_checks` needs a
   completed, successful run for the exact head, from the Actions app. A run on an
   earlier head does not count.
2. **The ordinary-path allowlist.** The automation merges only paths listed under
   `[paths].ordinary` — here `README.md`, `CHANGELOG.md` and `docs/**`. Anything
   else, including this repository's own `scripts/` and `tests/`, is refused by
   the merge decision itself: a person reviews it and a person merges it. That is
   the rule, not a stuck queue, and no amount of approval changes it.
3. **The base branch.** Protection is `strict`, so a head whose base has moved is
   refused until the branch is updated; the automation re-reads the exact head
   before merging.
