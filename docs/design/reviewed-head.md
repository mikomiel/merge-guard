# The reviewed head

**Status:** Accepted (2026-11-18) - the maintainer made the decisions under
[Decisions](#decisions).

**Issue:** #20, which follows #15 and #19.

This note records how an approval can prove which head the reviewer read.
[trusted-relay.md](../trusted-relay.md) states what the gate proves, and
[cli.md](../cli.md) owns the contract that this changes. Rollout step 1 is done
and step 2 is the release of the code: see [Rollout](#rollout).

## The threat

Today an approval counts for the head when its `commit_id` equals the head and
the head's committer date is no more than 300 seconds after the review was
submitted (#19). Neither fact comes from the reviewer.

- **The forge can move the association.** In #15 one approval reported three
  `commit_id` values while base merges updated the branch, and the gate
  published `approved_at_head` on heads created after the review.
  `original_commit_id` is `null` in observed data.
- **The author chooses the committer date.** A reviewer approves at 07:30:30Z.
  The author pushes another head dated 07:29:06Z. If the forge moves the
  review to that head, both checks pass, and nothing bounds the gap.
- **The forge picks the head when the reviewer does not.** A review created
  without `commit_id` gets the newest commit at submission
  ([REST reviews](https://docs.github.com/en/rest/pulls/reviews)).

`carry_unchanged_diff = false` does not help: the association itself is wrong.

## Options

### 1. Reviewer attestation

The approving review ends with a `Reviewed-Head: <40-hex>` line, and the gate
requires that value to equal the head. The reviewer states the association.

- **Cost.** Every approval must carry the line, so the reviewer service, every
  human reviewer and this repository's reviews change first. The gate also
  reads each review's edit state. The schema gains a field, so the change ships
  in a release. Adopters lose the unchanged-diff carry while the switch is on.
- **Residual risk.** The line is as trustworthy as the reviewer credential, the
  SHA that the reviewer service writes, and the forge's record of edits.

### 2. Trusted review-event record

The gate records the association when the review event arrives, and treats the
record as the evidence.

- **Cost.** A durable store and a recovery path for a missing record, where
  today the gate keeps no state ([direction.md](../direction.md)). Only
  `review-relay.yml` sees the event on arrival, and it must not write
  ([trusted-relay.md](../trusted-relay.md)). The trusted run starts later and
  reads the same mutable `commit_id`, so a record made on arrival needs a
  webhook receiver outside Actions: a hosted component that is not planned.
- **Residual risk.** The record trusts the forge's association when it is read
  and needs every event delivered. A missed event refuses an honest approval.

### 3. Document the guarantee and stop

The documentation states that the gate proves "an approval the forge
associates with a head, rejected when the head's recorded date postdates the
review", and nothing about what the reviewer read.

- **Cost.** No code. The promise "bind a merge to a review decision taken on
  the same exact head" becomes weaker.
- **Residual risk.** The attack above stays open with no bound on the gap.

## Recommendation

Adopt option 1 behind a policy switch that is off by default. The forge test
in rollout step 1 passed, and the maintainer accepted it on 2026-11-18. It
moves the evidence to the party that knows what it read and needs no state in
the gate; option 2 adds state and still trusts the forge's association. A
missing, malformed or mismatched line always refuses. An author with write
access can also edit an approval, so for that author option 1 closes the #20
attack only if the forge records every edit.

## The line

```text
Reviewed-Head: 0123456789abcdef0123456789abcdef01234567
```

The line is the last non-blank line in the body of a submitted `APPROVED`
review. The gate reads the raw Markdown `body`, never `body_text` or
`body_html`, and stops at the first step that gives a result:

1. An edited body: `reviewed_head_edited`. The gate does not parse it.
2. Split the body at LF. From each line, remove one trailing CR, then trailing
   spaces and tabs. Ignore the empty lines at the end.
3. A line is a *candidate* when, without its leading spaces and tabs, it starts
   with `reviewed-head`, ignoring case. No candidate: `reviewed_head_missing`.
4. More than one candidate, a candidate that is not the last line, or a last
   line that does not fully match `Reviewed-Head: [0-9a-f]{40}`:
   `reviewed_head_malformed`. The key has this exact case, one space follows
   the colon, and the value is 40 lowercase hex characters, as in `valid_sha`.
5. A value that is not the head: `reviewed_head_mismatch`. Otherwise the line
   attests the head.

A short SHA never counts: an author can grind a commit ID with a chosen prefix.
Two candidates never count: pull request content can steer a model reviewer
into printing a line, and the service appends its own, so the gate refuses.

## Who can author it

The gate reads the line only from a review that counts today: the latest
decisive review of its submitter, in state `APPROVED`. The submitter is the
configured reviewer (`reviewer_id`, `reviewer_login` and `reviewer_type` all
match) or a user that the API verifies as write, maintain or admin, and is
not the pull request author. An older approval with a matching line does not
count. Comments, `COMMENTED` reviews, line comments, the pull request
description and commit messages never attest.

An edit never attests, whoever made it. Anyone with write access can edit
comments on a pull request ([GitHub
docs](https://docs.github.com/en/communities/moderating-comments-and-conversations/managing-disruptive-comments)),
and the REST review object has no edit time, so a pull request author with
write access could otherwise rewrite the line. The gate reads each body and its
`lastEditedAt` (GraphQL `PullRequestReview`) from one response, so no edit can
fall between two reads. Rollout step 1 tested the field once: an author's REST
edit of the reviewer's approval set `lastEditedAt`, and it stayed set after the
content was restored and after a revision was deleted. The case of a second
account with write access was not tested. An edit means a `lastEditedAt` that is
not null. The gate never infers one from `userContentEdits`, which holds an
entry for the original content once a review has been edited.

## Evaluation with the switch on

The switch adds conditions and removes none, so turning it on can only turn a
success into a refusal.

- A trusted identity's change request still blocks, whatever head it names.
- An approval counts only when its line attests the head, its body is
  unedited, its `commit_id` equals the head, and the #19 timestamp check
  passes. Automatic merge still needs the configured reviewer's own approval,
  so that approval needs the line too.
- Merge mode reads the bodies and their edit state again before it merges.

The rank, highest first, is `changes_requested`, `approved_at_head`, the
`reviewed_head_*` code of the newest approval of the head set aside for its line
or its edit, and `current_head_approval_required`. A `reviewed_head_*` code
applies only to an approval whose `commit_id` is the head, and only after the
#19 timestamp check accepted it. An approval of an earlier head keeps
`current_head_approval_required`. A draft still reads `pull_request_is_draft`. A
bad line is a per-head `pending` reason, never an engine refusal: that would
stop the run, and the review workflow would mark every known head
`review_snapshot_unavailable`, blocking every pull request.

## Carry and refresh

An approval binds to the exact head that its line names. Reuse on another head
comes only through the clean-refresh rule.

- `carry_unchanged_diff` finds the older head through `commit_id`, the field
  that the switch stops trusting, and the key is `true` when absent. While the
  schema has the key, with the switch on, an absent key or `true` is refused
  at load time. Every base update then needs a new review. This repository
  already sets `false`.
- The clean-refresh release that [cli.md](../cli.md) names removes the key and
  retires this requirement and `policy_reviewed_head_requires_carry_off`,
  which keeps its meaning, is no longer emitted and is never reused. If that
  release ships first, neither is added. After it, a policy without the key
  loads and one with the key gets `policy_unknown_field`. A policy with the
  switch on writes the key, so it must delete the key at that release.
- Under the switch the clean-refresh rule then replaces the carry. The
  [README](../../README.md#carried-approvals) gives the rule one sentence. No
  document defines it, and this note does not. An approval names only the head
  in its line, so the rule must start from that head.

## Adding the line

### The reviewer service

The reviewer service is the automation that submits reviews as the configured
reviewer. Issue #20 calls it the trusted review relay. It is not
`review-relay.yml`, the no-op workflow in trusted-relay.md. The service must:

1. Take the SHA from the commit that it checked out and read, never from the
   pull request's head at submission or from generated text.
2. Append the line itself, last, after any generated text.
3. Send the same SHA as `commit_id` when it creates the review.
4. Use the lines that it wrote, not `commit_id`, to decide whether it already
   reviewed a head. In #15 the moved review blocked a review of the new head.
5. Never edit a submitted review. A correction is a new review.

### A human reviewer

1. Before you read, record the full 40-character head SHA: `git rev-parse
   HEAD` in your checkout, or the SHA of the newest commit in the pull
   request's commit list.
2. Review that commit, then approve it with a body that ends with the line, in
   the browser or with `gh pr review <number> --approve --body-file review.md`:

   ```text
   The retry limit now matches the documented bound.

   Reviewed-Head: 0123456789abcdef0123456789abcdef01234567
   ```
3. If the head moves first, the gate reports `current_head_approval_required`,
   or `reviewed_head_mismatch` when the forge moved your review to the new head.
   Review the new head and submit a new review. Do not edit the old review.

## Rollout

The switch is `[review] require_reviewed_head`, a boolean that is `false` by
default. When it is off, the engine does not read the line.

1. **Test the forge, before any release.** *Done, on test pull request #69.
   The results are on #20.* No engine code was needed. The plan was an approval
   by an account of the configured reviewer type and one by a second account
   with write access, an edit of each body by the author, and then the deletion
   of the edit from the history where the forge offers it. One GraphQL query had
   to show `lastEditedAt` set after each step, or nothing would be released.
   Result: `lastEditedAt` was set by an author's REST edit of the reviewer's
   approval, and it stayed set after the content was restored and after a
   revision was deleted. The case of the second account was not tested, by the
   maintainer's decision.
2. **Release** the field, the [codes](#refusal-codes) and the edit-state read,
   with `cli.md`, `examples/merge-guard.toml` and `troubleshooting.md`.
   `cli.md` freezes the schema, so this is a release, not a patch.
3. **The reviewer service emits the line** here, with the switch still off,
   and acts on the `reviewed_head_*` codes as it acts on a stale approval.
4. **Dry run.** Run `status` with a policy copy that turns the switch on.
   Every head that is approved now must still read `approved_at_head`.
5. **Turn it on here first**, in one change that a person merges: move both
   workflow pins to the release and set `require_reviewed_head = true`. The
   pinned engine (`65c985a`, v0.0.2) ignores an unknown key, so the key alone
   would claim a protection that does not run. Engines from #30 on refuse it.
6. **Canary.** On one pull request, an approval with the line reads
   `approved_at_head`, a new commit reads `current_head_approval_required`
   unless the forge moved the review to it (then `reviewed_head_mismatch`), and
   an approval without the line reads `reviewed_head_missing`.
7. **Adopters** turn it on when their reviewers emit the line. Making it the
   default is a later, breaking decision.

To roll back, a person merges `require_reviewed_head = false`. No state needs
migration, and approvals with the line keep counting.

## Refusal codes

These codes are new, and each fits the 140-character status description. No
published code changes its meaning. An approval of an earlier head keeps
`current_head_approval_required`: a `reviewed_head_*` code replaces it only for
an approval whose `commit_id` is the head. A caller that acts on
`current_head_approval_required` by asking for a review must act on the new
codes too, because an approval of the head that is set aside now reads one.

| Code | Channel | Meaning |
| --- | --- | --- |
| `reviewed_head_missing` | `pending` reason | No line of the approval's body is a candidate |
| `reviewed_head_malformed` | `pending` reason | The line breaks the grammar or the position rule, or there is more than one candidate |
| `reviewed_head_mismatch` | `pending` reason | The line is valid and names another commit |
| `reviewed_head_edited` | `pending` reason | The review body was edited after submission |
| `review_edit_state_unusable` | operation failure, exit 1 | The response with bodies and edit state is unreadable or lacks a counted review; in review mode the run then publishes `review_snapshot_unavailable` |
| `policy_require_reviewed_head_invalid` | unusable input, exit 2 | The switch is not a boolean |
| `policy_reviewed_head_requires_carry_off` | unusable input, exit 2 | The switch is on and `carry_unchanged_diff` is absent or `true`. Retired when the schema drops that key |
| `policy_reviewed_head_forge_unsupported` | unusable input, exit 2 | The switch is on and the forge is `forgejo`. This release reads the edit state on GitHub only |

## What it does not prove

- That anyone read the change with care. It proves only that a trusted
  identity named, in an unedited approval, the commit that its review covered.
- That the reviewer credential is safe. Its holder can attest any commit.
- That the reviewer service took the SHA from what it read. A service that
  writes the current head instead names a head nobody read, undetected.
- That the forge keeps recording edits. Rollout step 1 observed it once, for an
  author's edit of the reviewer's approval, and not for an approval by a second
  account with write access. After a forge change, anyone with write access
  could rewrite a line unseen.
- Anything stronger than SHA-1. The line names a 40-hex SHA-1 commit ID.
- Anything while the switch is off. trusted-relay.md then states the guarantee.

## Decisions

The maintainer decided these points on 2026-11-18. They are final, and they
replace the open questions that this note held.

1. **Verified maintainers write the line too.** Every counted approval needs it,
   because the forge can move any review.
2. **Every edit refuses, the reviewer's own included.** "Edited" means that the
   GraphQL field `PullRequestReview.lastEditedAt` is not null. The gate never
   infers an edit from `userContentEdits`, and it does not trust the editor
   field.
3. **A `reviewed_head_*` code applies only to an approval whose `commit_id`
   equals the head.** An approval of an earlier head keeps
   `current_head_approval_required`, and a mismatch flags a moved association.
4. **GitHub only in this release.** Only the GitHub adapter reads the body and
   the edit state of a review; the Forgejo adapter refuses. With the switch on
   and the forge `forgejo`, `require_forge` refuses with
   `policy_reviewed_head_forge_unsupported`, exit 2. The refusal comes when the
   forge is known, because the policy file may leave the forge to `--forge`.
