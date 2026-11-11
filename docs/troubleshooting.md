# Troubleshooting

## The pull request is `BLOCKED` and a required check never ran

**Symptom.** The roll-up carries the gate's `review` status and no run of the
consumer's required check — commonly `checks` — for the exact head. The branch
protection rule is `strict`, so the pull request cannot merge, and with
`enforce_admins` on there is no manual override.

**Cause.** `pull_request` workflows fire on `opened`, `synchronize` and
`reopened`. Some branch updates do not produce an event that GitHub dispatches
as a synchronize for the new head — a base merge performed with a user token or
through the update-branch API is the case observed in issue #14. The head then
exists with no run against it, and a required context that has never reported
cannot be satisfied.

**Recovery.** Any of these makes GitHub dispatch a new event for the head:

- close the pull request and reopen it;
- push any commit to the branch;
- re-run the missing workflow from the Actions tab for that ref.

The first two also re-run the gate, so the new head gets a fresh `review`
status. Prefer reopening: it leaves the branch's history untouched.

## The gate says `pending` and the review looks approved

Read the status description. `approved_at_head` means the approval is at this
head. `current_head_approval_required` means it is not, and one of these is why:

- the approval was submitted for an earlier commit, and the diff has changed
  since, so the unchanged-diff carry does not apply;
- the approval was submitted before the head commit existed — the forge can
  reassign a review's `commit_id`, and an approval cannot be evidence for a
  commit that did not exist when it was written (issue #15).

Request a fresh review of the current head rather than re-running the workflow:
nothing about the old approval changes by re-reading it.

## A pull request is approved, green, and still not merged

Check, in this order:

1. `required_checks` in `.github/merge-guard.toml` — every one must have a
   completed, successful run **on this head**, from the Actions app.
2. The policy's `sensitive` paths — a change under them needs a human review,
   which the automation will not supply for itself.
3. The base branch — `strict` refuses a head whose base has moved, and the
   automation re-reads the exact head before merging.
