# The trusted relay

A review event cannot run code that is allowed to write to a repository. GitHub
runs `pull_request_review` workflows with the permissions of the pull request's
head, so granting them `statuses: write` or `contents: write` would let a fork
publish a green gate or merge itself.

merge-guard therefore uses two hops:

1. **`review-relay.yml`** — a no-op workflow triggered by
   `pull_request_review` (`submitted`, `dismissed`). It runs with
   `permissions: {}` and does nothing except complete.
2. **`merge-guard-review.yml`** and **`merge-guard-merge.yml`** — triggered by
   `workflow_run` on the relay (and on the review workflow respectively). A
   `workflow_run` workflow executes the copy of the file that lives on the
   repository's default branch, with the repository token, even when the
   triggering event came from a pull request. That is the reason its writes are
   trustworthy in practice.

The two rules that follow from this:

- **Nothing that runs on a pull request head may write.** The engine accepts
  `workflow_run`, `schedule` and `pull_request_target` for `review`, and
  `workflow_run` and `schedule` for `merge`; `workflow_dispatch` is not accepted.
  It also refuses `--apply` unless `GITHUB_REF` is `refs/heads/<policy
  base_branch>`, `GITHUB_REF_PROTECTED` is `true`, and `GITHUB_WORKFLOW_REF`
  names the policy's workflow file at that branch.
- **`merge` runs when the gate publishes.** `merge-guard-merge.yml` is
  triggered by `workflow_run` on `merge-guard review`, so a fresh gate verdict
  is followed by a merge attempt. A schedule alone leaves an approved pull
  request blocked for up to an hour, which is how advisory-radar#37 happened.

## The status, not the review decision

The gate publishes the `review` commit status for the exact head SHA. That
status must be written by `github-actions[bot]`: the `token` input cannot change
which identity a workflow using the repository token gets, and the engine
refuses a gate status published by any other creator.

Identity is not the only control, and this document should not pretend it is: a
same-repository workflow on any branch with `statuses: write` can also post as
`github-actions[bot]`. What makes the green meaningful is that the engine
re-derives the verdict from reviews and checks before merging, and that
`trusted_context` pins the workflow file reference at the policy base branch.
Grant `statuses: write` only to the gate workflows. Native
`reviewDecision` is not an input: it is a derived value that GitHub recalculates
and that drops approvals on a new commit in ways the gate cannot verify from a
snapshot. The status records what the gate saw, for the head it saw it on.

## What an approval proves

The gate applies a timestamp plausibility check to an approval of `head`. It
compares the review's `submitted_at` — the forge's own timestamp — with the head
commit's committer date, and refuses `approved_at_head` when the head committed
more than 300 seconds after the review was submitted.

This is a narrowing, not a proof, and the difference matters. It rejects the
association observed in practice, where the forge reassigned a review to a head
created 34 minutes after it was submitted (#15). It cannot establish what the
reviewer read: a committer date is chosen by whoever creates the commit, so a
head authored with a backdated date defeats the comparison. Closing that gap
needs an assurance the forge does not own — a review body naming the head it
reviewed, or a trusted record of the association at review time — which this
gate does not yet require (issue #20).

`commit_id` alone is not trusted. The forge can reassign it to a commit that did
not exist at submission time, and `original_commit_id` is `null` in observed
data, so there is no immutable field to fall back to. A review whose
`submitted_at` is missing or unreadable, or a head without a usable committer
date, is a refusal rather than a pass. When an approval is skipped this way, the
pull request falls back to `current_head_approval_required` unless the
unchanged-diff carry applies.
