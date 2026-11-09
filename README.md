# merge-guard

Bind a merge to a review decision and a CI verdict taken on the **same exact
head**.

merge-guard is a pre-merge gate. It refuses a merge unless the review and the
CI result both belong to the commit being merged, and it names the rule that
refused. The first public release is a walking skeleton — the change
classifier and the check profile it selects — and the rest of the gate follows.

## Status

Pre-release. The repository is public from its first commit; no artefact is
published yet. There is no package, no container image and no supported
integration until a tagged release says so.

## Design principles

- **Evidence is bound to a revision.** A green run for another commit is not
  evidence for this one.
- **Refusals are named.** Every refusal carries a stable code, so a caller can
  branch on it without parsing prose.
- **Fail closed.** Missing CI, an unreadable forge or an ambiguous verdict is a
  refusal, never a pass.
- **One owner per fact.** The classifier owns the rule that a path needs a
  stronger check; nothing else copies that list.


## Use it in a repository

1. Copy `examples/merge-guard.toml` to `.github/merge-guard.toml` and replace
   every placeholder: `OWNER/REPO`, `REVIEWER[bot]`, its numeric `reviewer_id`,
   `CHECKS`, the workflow file names and the ordinary-path allowlist.
2. Add the three workflows from `examples/` under the file names the policy
   defaults to: `merge-guard-review.yml`, `merge-guard-merge.yml` and
   `review-relay.yml`. Pin `mikomiel/merge-guard` by 40-hex SHA, and replace
   the placeholder values each file names at the top: the base branch, your CI
   workflow's display name, and the policy placeholders.
3. Grant them the permissions each file declares and nothing more. The gate
   status is written by `github-actions[bot]`; the `token` input cannot change
   that identity, and a status from any other creator is refused.

Read [docs/trusted-relay.md](docs/trusted-relay.md) before changing the
triggers: the relay is what makes the writes trustworthy.

## Development

The gate is Python, standard library where it can be, so a forge integration
adds a client rather than a runtime. CI runs the public-content scan now, and
lint and tests once there is code:

```
python3 scripts/check_estate_identifiers.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
