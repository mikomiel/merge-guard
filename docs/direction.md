# Direction

This page records where merge-guard is going and what it deliberately does not
do. It is a direction, not a promise of dates. The contract that exists today
is [cli.md](cli.md); nothing here changes it.

## Users

- Developers who let AI coding agents open pull requests on GitHub and want a
  merge to happen only on evidence bound to the exact head.
- Self-hosters who run a fleet of agent sessions against Forgejo or Gitea and
  need the same rules there.

## Scope, in order

1. **Evidence gate** (today): a merge needs an approving review and a green CI
   verdict that both belong to the exact head. Every refusal has a stable code.
2. **Two forges**: one engine behind a forge port, with a GitHub adapter and a
   Forgejo adapter, with the same rules and the same refusal vocabulary on both
   ([#34](https://github.com/mikomiel/merge-guard/issues/34)).
3. **Merge executor**: a merge queue and a merge train that run the gate
   repeatedly, park what cannot land, and merge with the expected-head
   primitive. Agents never merge; the executor merges ordinary paths when the
   evidence holds, and a person merges sensitive paths.
4. **The agent workflow around it**, later and only where measured need exists:
   an executor container, webhook ingestion with periodic reconciliation as the
   fallback, an event stream for agent sessions, and a local component for task
   branches and publication.

## Layers

| Layer | Owns | Credential |
|---|---|---|
| Contract and fixtures | Refusal codes, verdict cases, policy schema, recorded forge responses as JSON data | None |
| Engine | The verdict, the classifier, later the executor; no I/O of its own | None |
| Forge adapters | GitHub and Forgejo transport behind one port | The caller's |
| Surfaces | The composite Action; the command line installed from a release tag; later an executor container | The surface's own |

The rules exist once, in the engine. A consumer that cannot depend on this
repository at run time vendors a pinned, digest-checked copy of a release, as
[pin-bump.md](pin-bump.md) describes. A vendored copy keeps working when this
repository or a future service is unavailable.

## Operation

- **Single tenant.** One installation serves one owner's repositories. Nothing
  in the design keeps global state, shares credentials between installations
  or embeds an operator's identifiers, so hosting can be decided later without
  a redesign.
- **No hosted service** and no installable public App are planned now.
- **Review is a contract, not a built-in reviewer.** The gate defines what an
  approval must prove about the head it read. The reviewer is whatever the
  repository uses: a person, a review App or an agent.

## Language

The engine is Python with the standard library today. Before the merge
executor is authored here, a bounded spike compares Python and Go against the
shared fixtures, with a threshold fixed in advance
([#35](https://github.com/mikomiel/merge-guard/issues/35)). The result and the
decision are recorded on this page.

## Not planned

- A hosted, multi-tenant service.
- An agent-initiated merge operation.
- A third-party HTTP client inside the gate.
- A registry release before the command-line contract settles.
