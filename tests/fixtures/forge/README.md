# Recorded forge cases

These files are the conformance cases for the forge port. Each file is one
case: the requests an adapter makes, the response the forge gives to each, and
the verdict or refusal the gate must reach. A case is plain JSON, so any
implementation can replay it with a stand-in for its own transport; Python is
not needed.

All cases are **synthetic**. The identities, commits and timestamps are made
up, and no response was captured from a live forge. Each response has the shape
that its forge's REST API returns, reduced to the fields the adapter reads and a
few that the forge sends alongside them. The Forgejo shapes were checked
against the Forgejo source and against public responses; the CI job in
`.github/workflows/forgejo.yml` runs the Forgejo adapter against a real
instance.

## Layout

- `github/` holds the cases for the GitHub adapter, and `forgejo/` holds the
  cases for the Forgejo adapter.
- A case named `<name>.json` has `"case": "<name>"`, and its `forge` names its
  directory.
- Every `verdict` case exists in both directories, with the same `run` and the
  same `expect`. That is the shared verdict set: the same rules and the same
  refusal codes on both forges.

## A case

```json
{
  "case": "approved-at-head",
  "forge": "github",
  "kind": "verdict",
  "description": "One sentence: the situation this case pins.",
  "policy": {"repository": {"name": "example/widgets", "base_branch": "main", "forge": "github"}},
  "run": {"mode": "status", "apply": false, "event_head": null},
  "exchanges": [
    {"request": {"method": "GET", "path": "/repos/example/widgets/pulls/7"},
     "response": {"status": 200, "headers": {"X-Total-Count": "1"}, "body": {}}}
  ],
  "expect": {"result": {"heads": {"<sha>": ["success", "approved_at_head"]}}}
}
```

(The policy and the bodies above are shortened.)

| Field | Meaning |
| --- | --- |
| `kind` | `verdict`: in the shared set. `malformed`: a response the adapter must refuse. `forge`: behaviour of one forge only. |
| `covers` | `malformed` only: `missing_field`, `wrong_type`, `unknown_state` or `partial_page`. Each adapter has a case for each of the first three. |
| `policy` | The policy file as JSON: the same sections and fields as the TOML file that `docs/cli.md` defines. |
| `run.mode` | `status`, `review` or `merge`: the engine call that the command line makes for this mode. |
| `run.apply` | Whether the adapter may write. Writes in the exchanges appear only when it is `true`. |
| `run.event_head` | `status` and `review` cases have it: the head that triggered a `review` run (`EVENT_HEAD` on the command line), or `null`. A `status` run does not read it. A `merge` case has no `event_head`. |
| `exchanges` | Every request the adapter makes in this run, each with its recorded response. |
| `expect` | Exactly one key: `result` or `refusal`. See [The expected outcome](#the-expected-outcome). |

### The expected outcome

`expect` is the outcome of the engine call for `run.mode`, made with an adapter
built with `run.apply`:

- `result` is the value that the call returns, serialised as the command line
  serialises the `result` of its JSON output.
- `refusal` is the code of the refusal that the call raises. The command line
  prints that code after `merge_guard_refused:`.

A replay enters the engine below the command line's own input checks. A case
therefore does not pass through the trusted-context check (`docs/cli.md`,
"Writes are trusted-context only") or through the refusal of a write to Forgejo
(`forgejo_write_unsupported`). A case with `run.apply` set to `true` bypasses
both. It records what the engine does when its caller has allowed the write.
On GitHub that caller is the command line in a trusted workflow context, and the
case format has no field for that context. On Forgejo it is library code that
builds the adapter with `apply=True`, because the command line refuses every
Forgejo write.

### Requests

- `method` is the HTTP method.
- `path` is the API path with its query string. It is relative to the API
  root: `https://api.github.com` for GitHub, and `<FORGEJO_URL>/api/v1` for
  Forgejo. The Python replay uses `https://forgejo.example` as `FORGEJO_URL`
  and the token `replay-token`. A case never records a host or a credential.
- `body` is the JSON body of a write. A request without a body has no `body`.

### Responses

- `status` is the HTTP status.
- `headers` holds the response headers that the adapter reads. Most Forgejo
  lists carry their count in `X-Total-Count`. The list of Actions runs is an
  object instead, with the count in `total_count`. The Forgejo adapter reads a
  list only when it holds as many items as the count names, and no item twice.
  Another number of items is refused with `forgejo_pagination_incomplete`, and
  for the review list with `forgejo_review_list_incomplete`: Forgejo counts the
  pending reviews of other accounts without returning them, and a response
  cannot show that a review it left out is one of those.
- `body` is the JSON body. A response without `body` has an empty body, which
  is what Forgejo returns for a merge.

On Forgejo, the CI evidence is the Actions jobs of the head. A Forgejo case
that reads it has the run list for the head
(`/actions/runs?head_sha=<head>`) and the jobs of each run
(`/actions/runs/<id>/jobs`). The status that Forgejo Actions writes for each
job has no `creator` and the context `<workflow> / <job> (<event>)`, for
example `ci / checks (pull_request)`. The adapter reads the commit statuses
with `sort=leastindex`, and each changed file names the commit of its list in
`contents_url`.

For GitHub the adapter calls `gh api`. A replay answers the command
`gh api --hostname github.com --method <method> <path without the leading />`
with the body on standard output, and exits 0 for a 2xx status and 1 for any
other status.

## Replaying a case

1. Build the policy from `policy`, and build the adapter for `forge` with
   `run.apply`.
2. Answer each request from `exchanges`. A request matches an exchange when its
   method and path are equal, its query parameters are equal in any order, and
   its JSON body is equal. A request takes the first matching exchange that no
   earlier request took, so a repeated request gets its responses in the
   recorded order. A request with no matching exchange fails the case.
3. Make the engine call for `run.mode`. `status` computes the review verdicts
   with no write and no event head. `review` computes them with `run.apply`
   and `run.event_head`. `merge` decides the merge with `run.apply`.
4. Compare the outcome with `expect`, as [The expected
   outcome](#the-expected-outcome) defines it. Every exchange must have been
   taken: an exchange that no request asked for also fails the case.

`tests/forge_replay.py` is the Python replay, and `tests/test_forge_fixtures.py`
runs every case with it.

## Changing a case

Write `expect` from the scenario first, then record or edit the exchanges. An
expectation copied from the engine's output proves nothing. Keep the files in
the canonical form that the test checks: `json.dumps(case, indent=2)` and a
final newline.
