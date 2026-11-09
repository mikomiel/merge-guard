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
