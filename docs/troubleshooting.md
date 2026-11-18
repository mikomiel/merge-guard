# Troubleshooting

## The pull request is `BLOCKED` and a required check never ran

**Symptom.** The roll-up carries the gate's `review` status and no run of the
consumer's required check — commonly `checks` — for the exact head. The branch
protection rule is `strict`, so the pull request cannot merge, and with
`enforce_admins` on there is no manual override.

The gate names the condition: `review` is `pending` with the description
`required_check_missing: <name>`, and a merge run refuses
`required_check_run_missing`. A required check that exists but is red or still
running keeps its own `checks` context and the `required_check_not_success`
refusal; only a missing run produces this state.

**Cause.** `pull_request` workflows fire on `opened`, `synchronize` and
`reopened`. In the case reported in issue #14 a base merge left the new head with
no `checks` run while the gate's own workflow did run for it. Why GitHub did not
dispatch a run for that head is not established from the workflows alone; what is
established is the consequence, and that a new event fixes it.

**Recovery.** Close the pull request and reopen it, or push any commit to the
branch. Either makes GitHub dispatch a new event for the head, so both required
contexts run again; reopening leaves the branch's history untouched.

A consumer whose required checks **all** run under `workflow_dispatch` can
recover with a command instead:

`gh workflow run <ci-workflow>.yml --ref <pr-branch>`

A dispatch restores only the required checks that the dispatched workflow
produces. In this repository that is not the full set: `dependency-review` runs
only on `pull_request`, so a dispatch of `checks.yml` leaves it skipped and the
gate still refuses. This repository therefore recovers by reopening or pushing.
A dispatch also works only for a same-repository branch; close/reopen is the
fork-safe fallback.

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

With `require_reviewed_head` on, an approval at this head can also be set aside
for its body. The description then names a `reviewed_head_*` reason: see the
next section.

The description `required_check_missing: <name>` is not a review verdict at all:
the approval is at this head, but a required check has no run for it. Use the
recovery in the section above.

## The gate says `pending` with a `reviewed_head_*` reason

These reasons exist only when `[review] require_reviewed_head` is `true`. The
approval is at this head, but the gate does not count it. Each reason names one
approval, the newest that was set aside. A different approval of the same head
that attests it still turns the gate green. An approval of an earlier head never
gets one of these reasons: it keeps `current_head_approval_required`.

| Reason | Meaning | What to do |
| --- | --- | --- |
| `reviewed_head_missing` | No line of the approval's body starts with `Reviewed-Head` (in any case, after leading spaces and tabs) | Submit a new approval whose body ends with `Reviewed-Head: <the 40-character head SHA>` |
| `reviewed_head_malformed` | The body has such a line, but it breaks the grammar: it is not the last line, there is more than one, or it is not `Reviewed-Head: ` and then 40 lowercase hexadecimal characters, with no text before or after | Submit a new approval with one line at the end, in exactly that form. A short SHA or an upper-case one never counts |
| `reviewed_head_mismatch` | The line is valid and names another commit than the head | Review the head, then submit a new approval that names it. If the head moved while you reviewed, the new commit needs its own review |
| `reviewed_head_edited` | The review was edited after it was submitted, by anyone, the reviewer included. The gate does not read the body of an edited review | Do not repair the old review. Submit a new approval of the head with the line |

A new approval replaces the old one: only the latest decisive review of a person
counts. Do not try to repair a submitted review. In the forge test that this rule
rests on, the edit time stayed set after the old text was restored and after a
revision was deleted.

A verified maintainer's approval needs the line too. `merge` also needs the
configured reviewer's own approval with the line: when only a maintainer's
approval attests, the gate is green and the merge run refuses with
`configured_reviewer_required_for_automerge`.

## A run fails with `review_edit_state_unusable`

**Symptom.** The `review` or `merge` run exits 1 with
`merge_guard_refused: review_edit_state_unusable`. With `--apply`, `review` also
publishes `review_snapshot_unavailable` for the heads it knows, so no head keeps
an old green.

**Cause.** With `require_reviewed_head` on, the gate reads the body and the edit
time of each review from GitHub's GraphQL API. The run fails when that read does
not complete: the call failed or timed out, the response is too large or is not
the shape that was asked for, the list of reviews is incomplete, or the response
lacks a review that counts. The gate never reads a missing record as an unedited
review.

**Recovery.** Run the workflow again. If the failure repeats, check that the
workflow grants `pull-requests: read`, as the example workflows do, and check
the status of GitHub. Do not turn the switch off to get past it: that removes
the protection for every pull request.

## A run is refused at the policy

These refusals exit 2 before the run reads the forge. Each names the field that
you change:

| Code | Cause | What to do |
| --- | --- | --- |
| `policy_require_reviewed_head_invalid` | `require_reviewed_head` is not `true` or `false` (a string such as `"true"` is not a boolean) | Write a TOML boolean |
| `policy_reviewed_head_requires_carry_off` | The switch is on and `carry_unchanged_diff` is absent or `true` | Set `carry_unchanged_diff = false`. An approval binds to the one head that its line names, so it never carries, and every base update needs a new review |
| `policy_reviewed_head_forge_unsupported` | The switch is on and the forge is `forgejo` | Turn the switch off for that policy. This release reads the edit state of a review on GitHub only |

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
