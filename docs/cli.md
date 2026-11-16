# merge-guard: the consumer contract

This document is the frozen contract between the action and the repositories
that consume it. `tests/test_contract.py` pins the parts that can be checked
mechanically: the action inputs, the policy field set, the modes and the exit
codes. A change to any of them is a breaking change and needs a new release,
not a patch.

## The action

`action.yml` takes three inputs, and publishes one output, `merged`, which is `true`
only when an applied `merge` run landed a pull request. It is derived from the
envelope below, so it adds no command-line flag:

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | yes | — | `review` publishes the gate status; `merge` lands one eligible pull request. `status` is CLI-only (see below) |
| `policy` | no | `.github/merge-guard.toml` | Path to the consumer policy file, relative to the repository root |
| `token` | no | the workflow token | Token used by `gh` |

The action runs `python3 "$GITHUB_ACTION_PATH/scripts/merge_guard.py" <mode>
--apply --forge github --policy <policy>`. `scripts/merge_guard.py` is an entry
point, not the engine; the engine is the `merge_guard` package. The action is
the GitHub surface, so it names the forge itself: a policy file without `forge`
works through the action, and a policy file that names `forgejo` is refused
with `policy_forge_conflict`. See [The forge](#the-forge).

## The command line

```
python3 scripts/merge_guard.py <mode> [--policy PATH] [--forge FORGE] [--apply] [--json] [--version]
```

- `status` — read-only, **CLI only**. It computes the review verdict and writes
  nothing. The action always passes `--apply`, so `mode: status` through the
  action is refused; use the command line for it. `--apply` with `status` is an
  unusable input: exit 2, refusal on stderr.
- `review` — with `--apply`, publishes the gate status for the current heads.
  Without `--apply` it is a dry run.
- `merge` — with `--apply`, merges at most one eligible pull request. Without
  `--apply` it lists what it would decide.
- A head whose review verdict is a success is still reported `pending` with
  `required_check_missing: <name>` when a required check has no run for that
  head; the merge then refuses `required_check_run_missing`. A run that exists
  but is red or still running stays the `checks` context's signal.
- `--json` is accepted explicitly for scripted callers. It changes nothing:
  JSON is the only stdout format on a completed run.
- `--forge github` or `--forge forgejo` names the forge when the policy file
  does not. A run that names no forge is refused with `policy_forge_missing`,
  exit 2. See [The forge](#the-forge).

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The run completed. **It does not mean the verdict was positive.** The verdict is carried by the JSON `result` and by the forge status; a gate must never be built on the step's exit code alone |
| 1 | An operation failed after the inputs were accepted (forge API error, unproven postcondition) |
| 2 | The inputs are unusable (unreadable or invalid policy, no forge named, no usable Forgejo URL or token, or a write to Forgejo), or a write was attempted outside a trusted workflow context |

## Output

stdout is one JSON object on a completed run:

```json
{"mode": "<mode>", "apply": false, "result": {"heads": {"<sha>": ["pending", "<reason>"]}}}
```

Two exceptions: `--version` and `--help` print text, and a refusal prints no
stdout object at all — the refusal line goes to stderr.

- `status` and `review` return `result.heads`: a map from head SHA to a
  `[state, reason]` pair.
- `merge` returns a list of `{number, head, eligible, merged, reason?}`.
- `apply` is the flag the run used. `status` always reports `false`.

Refusals go to stderr, one line each: `merge_guard_refused: <code>` for an
unusable input or a failed operation, and `write_refused_outside_trusted_workflow`
for a write attempted outside the trusted context. A write to Forgejo is the
unusable input `merge_guard_refused: forgejo_write_unsupported`: no context
makes it, so it is not the trusted-context refusal.

## Writes are trusted-context only

`--apply` publish and merge paths run only from the workflow file named in the
policy, on the default branch, with `GITHUB_REF`, `GITHUB_REF_PROTECTED`,
`GITHUB_REPOSITORY`, `GITHUB_EVENT_NAME` and `GITHUB_WORKFLOW_REF` agreeing.
See `docs/trusted-relay.md` for how the workflow is reached and
`docs/troubleshooting.md` for the blocked-head recovery.

The trusted context is a GitHub Actions context. With a run whose forge is
`forgejo`, the command line refuses `review --apply` and `merge --apply` with
`merge_guard_refused: forgejo_write_unsupported`, exit 2, in every context: a
Forgejo write needs a trust argument of its own, and this release does not
define one. Running from another context does not change this refusal. Code
that builds the Forgejo adapter with `apply=True` itself owns that argument.

## Policy schema

`.github/merge-guard.toml` has exactly these fields. A field the schema does
not list is an unusable input, exit 2 (`policy_unknown_field: <path>`), not a
warning: a typo would otherwise silently change which rule runs.

| Section | Field | Meaning |
| --- | --- | --- |
| `[repository]` | `name` | `OWNER/REPO` this policy governs |
| | `base_branch` | Branch a merge lands on |
| | `forge` | The forge this policy governs: `github` or `forgejo`. There is no default: see [The forge](#the-forge) |
| `[review]` | `reviewer_id` | Numeric reviewer identity (a login can be renamed; an id cannot) |
| | `reviewer_login` | Reviewer login, checked together with the id |
| | `reviewer_type` | Reviewer actor type, `Bot` by default |
| | `required_checks` | Check contexts that must be green on the exact head |
| | `gate_context` | Status context this gate publishes, `review` by default |
| | `review_workflow` | Workflow file allowed to publish, `merge-guard-review.yml` by default |
| | `merge_workflow` | Workflow file allowed to merge, `merge-guard-merge.yml` by default |
| | `carry_unchanged_diff` | **Deprecated.** Accepted until the clean-refresh release; an approval of an identical patch on a new head inherits nothing when it is off. It is removed by that release, which is a documented breaking change |
| `[paths]` | `ordinary` | Allowlist of paths an authorised merge may touch; anything else is human-only |
| | `dependency_update_exemption` | Whether a dependency update is ordinary, off by default |

A renamed file is judged on its old path and on its new path. When the forge
returns a rename without its old path, `merge` reports that pull request as not
eligible, with the reason `invalid_changed_path`. So it does for a changed file
whose status is missing or is not one of `added`, `removed`, `deleted`,
`modified`, `changed`, `renamed`, `copied` and `unchanged`, because such a file
could be a rename that names no old path.

Changing the policy file is a human decision in a consumer repository: it is a
sensitive path, and so are the workflows that read it.

## The forge

A run reads exactly one forge, and there is no default forge. The policy file
names it as `forge` in `[repository]`, and the command line names it with
`--forge`. The action passes `--forge github`.

| `forge` in the policy | `--forge` | Result |
| --- | --- | --- |
| absent | absent | Refused: `policy_forge_missing`, exit 2 |
| absent | `github` or `forgejo` | The forge that `--forge` names |
| `github` or `forgejo` | absent | The forge that the policy names |
| `github` or `forgejo` | the same forge | That forge |
| `github` or `forgejo` | the other forge | Refused: `policy_forge_conflict`, exit 2 |

A `forge` value other than `github` or `forgejo` is refused with
`policy_forge_invalid`, exit 2. In the library, `merge_guard.policy.require_forge`
applies the same rule: it returns the policy with its forge named, or it
refuses with these codes. Each adapter refuses a policy that names the other
forge with `policy_forge_conflict`. The Forgejo adapter also refuses a policy
that names no forge with `policy_forge_missing`. The GitHub adapter still
accepts such a policy, as it did before the key existed, so library code
written against it keeps working; call `require_forge` first to refuse it.

**This is a breaking change for the command line.** A command-line run whose
policy file names no forge now exits 2 with `policy_forge_missing`. Add
`--forge github` to the command, or add `forge = "github"` to the policy file.
The action does not change: it names GitHub on its run line, so a policy file
without the key keeps working there.

Add the key to a policy file in the same change as the pin of a release that
knows it. A release without the key refuses it with
`policy_unknown_field: repository.forge`.

## Forgejo

The Forgejo adapter reads the Forgejo REST API (`/api/v1`) with the Python
standard library. The command line takes the instance and the token from the
environment:

| Variable | Meaning |
| --- | --- |
| `FORGEJO_URL` | The base URL of the instance, for example `https://forgejo.example`. It must use `https`, or `http` to `localhost`, `127.0.0.1` or `::1`. It must not contain a user name or a password, not even an empty one before an `@`, and it must not contain `?`, `#`, a space or a control character (Unicode category Cc: U+0000 to U+001F and U+007F to U+009F). The host and the port must not hold `%`, percent-encoded or not. A port, when it names one, is a number from 1 to 65535 |
| `FORGEJO_TOKEN` | An access token with the scopes `read:user` and `read:repository`. A caller that writes through the library also needs `write:repository` and `write:issue` |

The account of the token needs admin permission on the repository. Forgejo
shows the permission of another account only to an administrator, and the gate
reads it for each person whose review is decisive. Without it, that read fails
and the run is refused with `forgejo_api_failed`, exit 1.

The adapter was checked against the source of Forgejo 17.0.1. On an instance
without the endpoints for Actions runs and jobs, the read of the CI evidence
fails with `forgejo_api_failed`.

The repository must have Forgejo Actions turned on, because the adapter reads
the CI evidence from the Actions runs and jobs. `read:repository` covers those
reads. When Actions is off for the repository, the read fails with
`forgejo_api_failed`: `review` and `status` report an approved head as
`required_check_unknown: forgejo_api_failed`, and `merge` refuses, exit 1.

A missing or unusable value is refused before the first request with
`forgejo_url_missing`, `forgejo_url_invalid` or `forgejo_token_missing`, exit 2.
A URL that does not parse, for example an IPv6 address without its closing
bracket, is `forgejo_url_invalid`, and so is a port outside 1 to 65535.
The adapter sends the token only in the `Authorization` header. It does not
follow a redirect, it reads at most 4 MiB of a response, and it refuses a body
that is not JSON. An `http` request never goes through a proxy, even when
`http_proxy` names one, because the token travels in clear text. An `https`
request uses the proxy that `https_proxy` names, and the token stays inside the
encrypted tunnel.

The verdict rules and the refusal codes are the same as on GitHub. A code that
names the forge starts with `forgejo_` where the GitHub code starts with
`github_`. Forgejo returns other records than GitHub, and the adapter maps them
as follows:

- **Checks.** Forgejo has no check runs. A check is a job of a Forgejo Actions
  run for the exact head. The adapter lists the runs for the head
  (`actions/runs?head_sha=`), refuses a run that names another commit with
  `forgejo_run_unusable`, and reads the jobs of each run. Forgejo Actions sets
  the state of a job, so an account cannot write the result of a check through
  the API.
- **The name of a required check** is the name of a job, as on GitHub: the
  `name` that Forgejo returns for the job (`actions/runs/<id>/jobs`). It is
  also the job part of the context of the status that Forgejo Actions writes
  for the job, `<workflow> / <job> (<event>)`: for the context
  `ci / test (pull_request)`, the name is `test`.
- **Job states.** `success` and `skipped` keep their meaning, and `failure` and
  `cancelled` are failures. `waiting`, `running`, `blocked` and `unknown` are a
  check that has not finished. Any other state is refused with
  `forgejo_job_unusable`.
- **Jobs with one name.** When jobs of more than one run for the head have the
  same name, they are one check. A re-run changes a job in place, so Forgejo
  shows no newer job to prefer. The check has not finished while one of the
  jobs has not. When all have finished, the check failed when one of the jobs
  failed or was cancelled. Otherwise it passed when one of the jobs passed, and
  it was skipped when all of the jobs were skipped.
- **Statuses.** A commit status is never a check, whoever wrote it. Forgejo
  Actions writes a status for each job of a push, a pull request or a release,
  with the context `<workflow> / <job> (<event>)` and no `creator`. Forgejo
  also shows no `creator` for a status whose account was deleted. The adapter
  leaves out a status with no creator only when its context ends as the status
  of a job of the head does (`/ <job> (<event>)`): that job is the evidence.
  Every other status is a status, as on GitHub, and it must be `success`.
- **The gate status.** The gate status (`gate_context`) counts only when the
  account of `FORGEJO_TOKEN` wrote it. A gate status from another account is
  refused with `untrusted_review_status`. Give the gate an account of its own:
  a person who can use that account can write a gate status that counts.
- **Reviews.** `APPROVED`, `REQUEST_CHANGES`, `COMMENT` and `PENDING` have the
  meaning of the GitHub states. A dismissed approval or change request is
  `DISMISSED`, as on GitHub. A review request is not a review. Any other state
  is refused with `unknown_review_state`. The configured reviewer is the
  account whose id and login both match `reviewer_id` and `reviewer_login`, and
  its actor type is `reviewer_type`. Every other account is a user.
- **The review list** must hold as many reviews as its count names. Forgejo
  counts the pending reviews of other accounts but returns them only to a site
  administrator, and no response tells such a review from a review that the
  list left out, for example a change request. A review list that does not
  hold its count is refused with `forgejo_review_list_incomplete`, exit 1.
- **Maintainers.** The rules for a maintainer's review are the same. Forgejo
  reports a maintainer's permission as `write`, `admin` or `owner`. A Forgejo
  login can hold `_` and `.`. A login that starts with `.` or holds `..` is
  refused with `maintainer_identity_unusable`. On GitHub the login alphabet and
  the permissions do not change.
- **Mergeability.** A pull request can merge only when Forgejo reports it as
  `mergeable`.
- **Merge.** The adapter merges with `head_commit_id` set to the head that the
  gate read, so Forgejo refuses the merge when the head moved. The adapter then
  reads the pull request again and refuses with `merge_postcondition_unproven`
  unless it is merged at that head.
- **Changed files.** Forgejo computes the changed files of a pull request at
  `refs/pull/<number>/head`, and it moves that ref to the head branch in the
  background. Each file names the commit of its list (`contents_url` ends with
  `?ref=<commit>`). A list for another commit than the head is refused with
  `forgejo_files_head_mismatch`.
- **Lists.** Forgejo returns lists in pages. The adapter reads the page size
  from `/api/v1/settings/api`, and reads pages until it holds as many items as
  the count of the list names: `X-Total-Count`, or `total_count` for the
  Actions runs. A count that is missing or that changes between pages is a
  refusal, and so is an item that appears twice
  (`forgejo_page_item_repeated`). A list that does not hold as many items as
  its count names is refused with `forgejo_pagination_incomplete`, and the
  review list with `forgejo_review_list_incomplete`. The adapter reads the
  commit statuses in the order of their index (`sort=leastindex`), which
  Forgejo keeps unique for each commit, so that a status cannot move from one
  page to another.

Limits of this release:

- The command line does not write to Forgejo. `review --apply` and
  `merge --apply` are refused with `forgejo_write_unsupported`: see [Writes are
  trusted-context only](#writes-are-trusted-context-only). `status` works, and
  so do `review` and `merge` without `--apply`.
- An approval never carries to a new head. Forgejo returns no per-file patch,
  so the adapter cannot prove an unchanged diff.
- While another account has a pending review on an open pull request, every run
  is refused with `forgejo_review_list_incomplete`, exit 1, for all pull
  requests, unless the account of `FORGEJO_TOKEN` is a site administrator:
  Forgejo counts that review but does not return it. Any account that can
  submit a review on the repository can start such a halt. The runs work again
  when the author of the pending review submits or deletes it, when a site
  administrator deletes it, or when that pull request is closed.
- A result that only a commit status reports, for example a result of another
  CI system, is not a check. It blocks the merge while it is not `success`, as
  on GitHub, but it cannot be a required check.
- A status with no creator whose context ends as the status of a job of the
  head does is left out, also when a deleted account wrote it. The job decides
  that check.
- Jobs of two runs for one head that have one name are one check. On GitHub the
  newest check run of a name counts. On Forgejo a failed job of the older run
  keeps the check red although a newer run passed: re-run the failed job.
- `mergeable` shows a conflict, but not a block by Forgejo's own branch
  protection. When branch protection blocks a pull request that the gate finds
  eligible, the merge fails with `forgejo_api_failed`, exit 1.
