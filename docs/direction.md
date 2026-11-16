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

## Target layers

These are the target layers. Today the engine still contains the GitHub
transport; the forge port (#34) separates it.

| Layer | Owns | Credential |
|---|---|---|
| Contract and fixtures | Refusal codes, verdict cases, policy schema, recorded forge responses as JSON data | None |
| Engine | The verdict, the classifier, later the executor; its only I/O is reading the classifier rule file | None |
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

**Decision (2026-11-15): the engine stays Python with the standard library.**
A bounded spike compared Python and Go before the merge executor is authored
here ([#35](https://github.com/mikomiel/merge-guard/issues/35)). The threshold
was fixed before any run, and Go had to meet all of it to be adopted.

Method:

- Two arms, Python 3.12 and Go 1.25, with three runs each.
- Every task ran in a fresh coding-agent session. Both arms used the same
  model and harness.
- Each session saw only the written specification, the visible fixtures and
  the program from the previous task. Held-out fixtures were scored outside
  the session.
- Tasks:
  - T1: the verdict and the path classifier.
  - T2: fail-closed normalisation of untrusted forge JSON.
  - T3: three independent changes to the T2 program, which were a new refusal
    code (T3a), a policy-driven approval count (T3b) and a new required forge
    field (T3c).

| Criterion (threshold) | Result |
|---|---|
| Zero fail-open in every run | Met by both arms: 0 in all 30 task runs; every held-out case passed |
| Go T3 median cost at most 70% of Python on two of three changes | **Not met.** Go/Python, net of fixed session context: 1.48, 1.04, 1.56; output tokens only: 1.23, 1.03, 1.55 |
| Review findings not higher than Python | Met on the reviewed run: one finding each, none fail-open |
| At most one third-party dependency | Met: Go used a hand-written TOML parser or one TOML module; Python uses `tomllib` |
| The Action runs code derivable from the pinned commit | Not tested; Go would need a build step at run time |

Go was not cheaper to change, so the threshold fails on its second criterion
whatever the others show. The review of the Python program found one crash
on an extreme input. The program was spike code, and the engine here has its
own tests.

## Not planned

- A hosted, multi-tenant service.
- An agent-initiated merge operation.
- A third-party HTTP client inside the gate.
- A registry release before the command-line contract settles.
