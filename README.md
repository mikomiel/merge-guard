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
