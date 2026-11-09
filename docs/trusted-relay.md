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
   default branch, with the repository token, even when the triggering event
   came from a pull request. That is the only reason its writes are trusted.

The two rules that follow from this:

- **Nothing that runs on a pull request head may write.** If a workflow needs
  the token to write, it is triggered by `workflow_run`, `workflow_dispatch`,
  `schedule`, or `pull_request_target` — and `merge-guard` additionally refuses
  `--apply` unless `GITHUB_REF` is the default branch and
  `GITHUB_REF_PROTECTED` is `true`.
- **`merge` runs when the gate publishes.** `merge-guard-merge.yml` is
  triggered by `workflow_run` on `merge-guard review`, so a fresh gate verdict
  is followed by a merge attempt. A schedule alone leaves an approved pull
  request blocked for up to an hour, which is how advisory-radar#37 happened.

## The status, not the review decision

The gate publishes the `review` commit status for the exact head SHA. Native
`reviewDecision` is not an input: it is a derived value that GitHub recalculates
and that drops approvals on a new commit in ways the gate cannot verify from a
snapshot. The status records what the gate saw, for the head it saw it on.
