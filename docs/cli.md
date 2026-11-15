# merge-guard: the consumer contract

This document is the frozen contract between the action and the repositories
that consume it. `tests/test_contract.py` pins the parts that can be checked
mechanically: the action inputs, the policy field set, the modes and the exit
codes. A change to any of them is a breaking change and needs a new release,
not a patch.

## The action

`action.yml` takes three inputs and nothing else:

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | yes | — | `review` publishes the gate status; `merge` lands one eligible pull request. `status` is CLI-only (see below) |
| `policy` | no | `.github/merge-guard.toml` | Path to the consumer policy file, relative to the repository root |
| `token` | no | the workflow token | Token used by `gh` |

The action runs `python3 "$GITHUB_ACTION_PATH/scripts/merge_guard.py" <mode>
--apply --policy <policy>`. `scripts/merge_guard.py` is an entry point, not the
engine; the engine is the `merge_guard` package.

## The command line

```
python3 scripts/merge_guard.py <mode> [--policy PATH] [--apply] [--json] [--version]
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

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The run completed. **It does not mean the verdict was positive.** The verdict is carried by the JSON `result` and by the forge status; a gate must never be built on the step's exit code alone |
| 1 | An operation failed after the inputs were accepted (GitHub API error, unproven postcondition) |
| 2 | The inputs are unusable (unreadable or invalid policy), or a write was attempted outside a trusted workflow context |

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
for a write attempted outside the trusted context.

## Writes are trusted-context only

`--apply` publish and merge paths run only from the workflow file named in the
policy, on the default branch, with `GITHUB_REF`, `GITHUB_REF_PROTECTED`,
`GITHUB_REPOSITORY`, `GITHUB_EVENT_NAME` and `GITHUB_WORKFLOW_REF` agreeing.
See `docs/trusted-relay.md` for how the workflow is reached and
`docs/troubleshooting.md` for the blocked-head recovery.

## Policy schema

`.github/merge-guard.toml` has exactly these fields. A field the schema does
not list is an unusable input, exit 2 (`policy_unknown_field: <path>`), not a
warning: a typo would otherwise silently change which rule runs.

| Section | Field | Meaning |
| --- | --- | --- |
| `[repository]` | `name` | `OWNER/REPO` this policy governs |
| | `base_branch` | Branch a merge lands on |
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

Changing the policy file is a human decision in a consumer repository: it is a
sensitive path, and so are the workflows that read it.
