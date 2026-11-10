"""GT-01 §7: conservative, deterministic R×C change classifier.

Per GT-01 §5.1: consumes only changed paths and bounded structured diff
shape -- never executes code from the changed branch to classify itself
(§7.5's own constraint), never imports an operation module, and never
reads ``fleet_deploy`` (that stays ``deploy_chain/catalog.py``'s concern,
same as ``catalog.load_repository_control_policy``).

This module is pure: every function here takes already-derived facts
(changed paths, raw diff-shape lines) and returns a result. It performs no
I/O of its own -- deriving those facts from the current worktree is
``forge.py``'s ``policy()`` operation (GT-01 2.4), matching how
``git_gate/repository.py``'s ``_lane_changed_paths`` already derives the
changed-path list this module classifies.

Rule design notes, since §7.5 states the *conditions* but leaves several
concrete boundaries to the implementer:

- **``repos.yaml`` is treated as C1-triggering in its entirety**, not only
  when the specific authority/writable-forge/mirror/deployment/R-level/
  CI-applicability fields change. §7.5's own preamble calls the initial
  classifier "conservative"; field-level diff parsing (comparing old vs.
  new YAML content, not just changed paths) is real added complexity whose
  only benefit is *not* escalating a change that turns out to have touched
  only a comment. Over-escalating costs a stronger assurance route;
  under-escalating from an incorrect field-diff parse would not fail
  closed. The conservative choice is the whole file.
- **The C1-ANSIBLE-RUNTIME tests/docs exception** (§7.5) is implemented as:
  every ``ansible-homelab/`` path in the change must be under
  ``ansible-homelab/tests/`` or end in ``.md`` for the exception to apply;
  a single non-exempt path triggers the rule for the whole change, matching
  "mixed changes containing any C1 trigger" (§7.5's combining rule).
- **"Unbounded generated-output" and "missing-base"** are both projections
  of ``_lane_changed_paths`` returning ``None`` (unreadable, or over
  ``LANE_CHANGED_PATHS_MAX`` per GF-05C's existing collision-forecast
  bound) -- reusing the bound this package already enforces elsewhere
  rather than inventing a second one, and treating "cannot compute changed
  paths" as itself C1-triggering rather than a separate failure path,
  per §7.5's conservative bias.
- **"Contradictory diffs"** covers a raw-diff record this module cannot
  parse into (old_mode, new_mode, path) triples -- an unparseable shape is
  itself the trigger, not a reason to skip the check.
- **Binary files** are invisible to the raw-diff mode check (a binary
  file's mode is an ordinary ``100644``) -- ``git diff --numstat`` reports
  ``-\\t-\\t<path>`` in place of real added/deleted line counts for a
  binary file, which is what :func:`_diff_shape_is_contradictory` actually
  keys on for the binary case, via a second input the raw-diff check alone
  cannot supply.
- **C3's allowlist is empty**, but all five of §7.5's conditions are
  implemented. Condition 1 (changed files
  are a non-empty subset of ``_APPROVED_MODEL_DEFINITION_FILES``) can never
  be satisfied while that set is empty, so C3 never fires in production
  today -- deliberate, not a placeholder bug (see the C3 allowlist note
  further down). Conditions 2-3 (structured YAML diff touches only
  approved fields; no tool/MCP/permission/fallback/credential/endpoint/
  runtime-owner/generated-consumer change) collapse into one mechanism:
  *every* top-level field that changed, across every changed path, must be
  a member of ``_APPROVED_MODEL_DEFINITION_FIELDS``. This enforces
  condition 3 for free *provided* that set is scoped to strictly
  model/provider-shaped fields and never a tool/MCP/permission/credential/
  endpoint field -- an invariant on the allowlist's content, not something
  this module can verify about a field name it has never seen. A
  "generated consumer changed alongside model definition" case is caught
  earlier, by condition 1 alone: a generated-consumer path is (by
  definition) not itself a model-definition file, so it is never a member
  of ``_APPROVED_MODEL_DEFINITION_FILES`` and condition 1 already rejects
  the whole change. The field-level diff itself (*old* vs. *new* top-level
  keys) is supplied by the caller as ``field_diffs``, never computed here
  -- this module stays pure, and no caller can supply it validly while
  condition 1 already fails, so production always passes ``None``.
  Condition 4 ("required schema and routing tests exist and pass") is
  deliberately *not* implemented algorithmically: §7.5's own preamble
  forbids this classifier from executing code from the changed branch to
  classify itself, and "pass" cannot be determined any other way. It is
  enforced procedurally instead, by the same standing rule every other
  change in this repository already follows -- nothing merges without the
  pinned test suite green (``git_gate/README.md``'s verification method) --
  not by a fifth algorithmic condition here.

**Extraction provenance.** The rule identifiers, the section references and
the path data in this module are the policy of the estate it was extracted
from, not a general default. The module was moved here with its behaviour
unchanged so that the classification can be diffed against the copy it
replaced. Generalising it starts by moving the path data behind a policy
seam; until then it classifies one estate's layout, deliberately.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

_C1_GATE_AUTHORITY = "C1-GATE-AUTHORITY"
_C1_REPOSITORY_AUTHORITY = "C1-REPOSITORY-AUTHORITY"
_C1_CI_TRUST = "C1-CI-TRUST"
_C1_SECRETS_PERMISSIONS = "C1-SECRETS-PERMISSIONS"
_C1_NETWORK_PVE = "C1-NETWORK-PVE"
_C1_DEPLOY_ACTIVATE = "C1-DEPLOY-ACTIVATE"
_C1_ANSIBLE_RUNTIME = "C1-ANSIBLE-RUNTIME"
_C1_UNKNOWN_CONTRADICTORY = "C1-UNKNOWN-CONTRADICTORY"
C3_MODEL_DEFINITION_ONLY = "C3-MODEL-DEFINITION-ONLY"
DEFAULT_C2 = "DEFAULT-C2"

# §7.5 bullet 1-2: the gate itself, its package, and its MCP adapter/registry.
_GATE_AUTHORITY_EXACT = frozenset({
    "opencode-config/scripts/git_maintainer_gate.py",
    "MCPs/servers/git_maintainer_gate_mcp.py",
    "MCPs/registry.yaml",
})
_GATE_AUTHORITY_PREFIXES = ("platform/scripts/git_gate/", "opencode-config/scripts/git_gate/")

# §7.5 bullet 4: CI trust boundary (classifier rules are already covered by
# the git_gate/ prefix above -- git_gate/assurance.py lives there).
#
# The one workflow directory, and the only CI trust surface left: since the
# FJ-01 authority flip on 2026-08-17 the CI that judges a pull request is
# `.forgejo/workflows/`, and FJ-06 subtracted GitHub Actions (the workflows
# were deleted in PR #1373; the offsite copy runs none the gate can read), so
# the `.github/workflows/` prefix would classify a directory that no longer
# exists. Editing the Forgejo directory changes what CI trusts.
_CI_TRUST_PREFIXES = (".forgejo/workflows/",)
# The repository-owned CI entrypoint (FJ-01 §7: "the repository-owned CI
# entrypoint is a CI-trust change and therefore C1"). scripts/ci-profile is
# the single definition CI, a terminal and the OpenCode agents all call
# (WCI-02 3.1), and the workflows invoke it, so changing it changes what CI
# trusts. Exact path, not a scripts/ prefix: sibling scripts are ordinary
# repository code and keep the C2 default.
# scripts/validation-env is *sourced by* ci-profile (it owns the cache-var
# redirection every check runs under) and scripts/validation_lease.py gates
# every pytest check through the suite conftest adapters. Both decide what the
# trusted entrypoint actually does, so a change to either is a CI-trust change
# by the same reasoning that put ci-profile here; until 2026-08-26 both took
# the C2 default, which classified the entrypoint's own behaviour as ordinary
# repository code. Still exact paths, not a scripts/ prefix.
# scripts/node-install-fresh.sh is sourced by ci-profile (it decides whether
# the opencode-config Node install is refreshed) and scripts/toolgate.py
# decides whether a missing tool fails a check or skips it: same reasoning.
# scripts/ci_c1_reuse.py decides whether a push to main skips its leaf jobs on
# the strength of a pull request's full run of the same tree (2026-10-08), so
# it decides what CI runs on main: same reasoning again.
# scripts/ci-profile's CI_TRUST_PATHS mirrors this set (pinned by test).
_CI_TRUST_EXACT = frozenset({
    "scripts/ci-profile",
    "scripts/validation-env",
    "scripts/validation_lease.py",
    "scripts/node-install-fresh.sh",
    "scripts/toolgate.py",
    "scripts/ci_c1_reuse.py",
})

# §7.5 bullet 6 (first half): secrets, credential routing, permission policy.
_SECRETS_PERMISSIONS_EXACT = frozenset({
    "platform/config/credential-routing.yaml",
    "MCPs/servers/infisical_bootstrap.py",
    ".claude/settings.json",
    "harnesses/claude/settings.json",
})

# §7.5 bullet 6 (second half): firewall, ingress, egress, SSH trust,
# Traefik, Proxmox, Sablier, storage, backup, restore -- the MCP servers
# that directly administer this class of infrastructure. (Equivalent
# ansible-homelab roles are already caught by C1-ANSIBLE-RUNTIME below.)
_NETWORK_PVE_EXACT = frozenset({
    "MCPs/servers/proxmox_mcp.py",
})
_NETWORK_PVE_PREFIXES = ("MCPs/servers/pve_",)

# §7.5 bullet 5: publication, exact-HEAD review, merge, cleanup, deployment,
# activation, approval, broker boundaries. Publication/review/merge/cleanup
# are already covered by the git_gate/ prefix above; this covers the
# deployment/activation surface outside the gate package itself.
_DEPLOY_ACTIVATE_EXACT = frozenset({
    "opencode-config/scripts/oc_deploy.py",
    "platform/scripts/oc_deploy.py",
    "opencode-config/scripts/deploy_opencode_config.py",
})
_DEPLOY_ACTIVATE_PREFIXES = ("platform/scripts/deploy_chain/", "opencode-config/scripts/deploy_chain/")

# §7.5 bullet 7: ansible-homelab, except paths proven to be tests or source
# documentation only.
_ANSIBLE_RUNTIME_PREFIX = "ansible-homelab/"
_ANSIBLE_RUNTIME_EXEMPT_PREFIX = "ansible-homelab/tests/"
_HARNESS_ROLE_INPUT_PREFIXES = (
    "harnesses/pi/role/",
    "harnesses/codex/role/",
    "harnesses/claude/role/",
    "harnesses/deepseek/role/",
    "harnesses/vibe/role/",
)

# GT-01 §7.5: no approved profile model-definition file or field exists yet.
# See this module's docstring for why an empty set is the correct initial
# state, not a placeholder.
_APPROVED_MODEL_DEFINITION_FILES: frozenset[str] = frozenset()
_APPROVED_MODEL_DEFINITION_FIELDS: frozenset[str] = frozenset()

_CLASS_STRENGTH = {"C3": 0, "C2": 1, "C1": 2}


@dataclass(frozen=True)
class ClassificationResult:
    """§7.5's output: the gate-computed class and the rule IDs that matched.

    *gate_class* is what §7.4 calls ``gate_class`` -- the caller's own
    ``proposed_change_class`` is combined with this separately, by
    :func:`effective_class`, never inside this dataclass.
    """

    gate_class: str
    matched_rule_ids: tuple[str, ...]


def stronger(a: str, b: str) -> str:
    """§7.4: the stronger of two C-classes, C1 > C2 > C3."""

    return a if _CLASS_STRENGTH[a] >= _CLASS_STRENGTH[b] else b


def effective_class(gate_class: str, proposed_change_class: str | None) -> str:
    """§7.4: ``effective_class = stronger(gate_class, proposed_class)``.

    A caller-proposed class can only ever raise the effective class, never
    lower it -- an absent proposal (``None``) leaves ``gate_class`` as-is.
    """

    if proposed_change_class is None:
        return gate_class
    return stronger(gate_class, proposed_change_class)


def _matched_c1_path_rules(changed_paths: Iterable[str]) -> set[str]:
    matched: set[str] = set()
    ansible_paths: list[str] = []
    for path in changed_paths:
        if path in _GATE_AUTHORITY_EXACT or path.startswith(_GATE_AUTHORITY_PREFIXES):
            matched.add(_C1_GATE_AUTHORITY)
        if path == "repos.yaml":
            matched.add(_C1_REPOSITORY_AUTHORITY)
        if path in _CI_TRUST_EXACT or path.startswith(_CI_TRUST_PREFIXES):
            matched.add(_C1_CI_TRUST)
        if path in _SECRETS_PERMISSIONS_EXACT:
            matched.add(_C1_SECRETS_PERMISSIONS)
        if path in _NETWORK_PVE_EXACT or path.startswith(_NETWORK_PVE_PREFIXES):
            matched.add(_C1_NETWORK_PVE)
        if path in _DEPLOY_ACTIVATE_EXACT or path.startswith(_DEPLOY_ACTIVATE_PREFIXES):
            matched.add(_C1_DEPLOY_ACTIVATE)
        if path.startswith((_ANSIBLE_RUNTIME_PREFIX, *_HARNESS_ROLE_INPUT_PREFIXES)):
            ansible_paths.append(path)
    if ansible_paths and any(
        not (p.startswith(_ANSIBLE_RUNTIME_EXEMPT_PREFIX) or p.endswith(".md"))
        for p in ansible_paths
    ):
        matched.add(_C1_ANSIBLE_RUNTIME)
    return matched


def c1_path_rules_by_path(changed_paths: Iterable[str]) -> list[tuple[str, str]]:
    """``(rule_id, path)`` for every path-rule C1 class each path matches.

    Evaluated one path at a time: every path rule is per-path (the ansible
    rule needs *some* non-exempt path, which a single path satisfies on its
    own), so the union over single-path calls equals the whole-set verdict.
    Shared with ``scripts/ci-profile``, which escalates a pull-request run to
    the full manifest for any path listed here; that script runs this module
    as ``python3 -I assurance.py`` and never copies the rule lists.
    """

    return [
        (rule, path)
        for path in changed_paths
        for rule in sorted(_matched_c1_path_rules([path]))
    ]


def main(stdin: Iterable[str]) -> int:
    """Stdlib entry point: changed paths on stdin, ``<rule_id>\t<path>`` out."""

    paths = [line.strip() for line in stdin if line.strip()]
    for rule, path in c1_path_rules_by_path(paths):
        print(f"{rule}\t{path}")
    return 0


def _diff_shape_is_contradictory(
    raw_diff_lines: Iterable[str], numstat_lines: Iterable[str] = ()
) -> bool:
    """§7.5: symlink, submodule, binary, or unparseable ("contradictory")
    diff-shape records.

    *raw_diff_lines* is ``git diff --raw`` output -- each real record is
    ``:<old_mode> <new_mode> <old_sha> <new_sha> <status>\\t<path>``.
    *numstat_lines* is ``git diff --numstat`` output -- each real record is
    ``<added>\\t<deleted>\\t<path>``, with ``-`` in place of a line count
    for a binary file, which is the one signal that distinguishes it (a
    binary file's raw-diff mode is an ordinary ``100644``).
    """

    for line in raw_diff_lines:
        if not line:
            continue
        if not line.startswith(":"):
            return True  # unparseable -- conservative: treat as contradictory
        header, _, _path = line[1:].partition("\t")
        fields = header.split(" ")
        if len(fields) < 5:
            return True
        old_mode, new_mode = fields[0], fields[1]
        if old_mode == "120000" or new_mode == "120000":
            return True  # symlink
        if old_mode == "160000" or new_mode == "160000":
            return True  # submodule (gitlink)

    for line in numstat_lines:
        if not line:
            continue
        fields = line.split("\t", 2)
        if len(fields) < 3:
            return True  # unparseable -- conservative: treat as contradictory
        added, deleted, _path = fields
        if added == "-" or deleted == "-":
            return True  # binary file

    return False


def _c3_model_definition_only(
    changed_paths: AbstractSet[str],
    field_diffs: dict[str, AbstractSet[str]] | None,
) -> bool:
    """§7.5's five C3 conditions. See this module's docstring for how
    conditions 2-3 collapse into one field-allowlist check, why condition 3
    ("generated consumer changed alongside model definition") is actually
    enforced by condition 1, and why condition 4 is procedural, not
    algorithmic.
    """

    if not changed_paths or not changed_paths <= _APPROVED_MODEL_DEFINITION_FILES:
        return False  # condition 1
    if field_diffs is None:
        return False  # cannot prove conditions 2-3 without diff content
    for path in changed_paths:
        fields = field_diffs.get(path)
        if fields is None or not fields <= _APPROVED_MODEL_DEFINITION_FIELDS:
            return False  # conditions 2-3
    return True


def classify_change(
    changed_paths: AbstractSet[str] | None,
    raw_diff_lines: Iterable[str],
    numstat_lines: Iterable[str] = (),
    field_diffs: dict[str, AbstractSet[str]] | None = None,
) -> ClassificationResult:
    """§7.5: classify one changed-path set into C1, C3, or the C2 default.

    *changed_paths* is ``None`` when the caller could not compute it, or it
    exceeded ``LANE_CHANGED_PATHS_MAX`` -- both cases are §7.5's
    "unbounded generated-output"/"missing-base" triggers, escalated to C1
    rather than treated as a separate failure this module raises.

    *field_diffs* maps each changed path to the set of top-level fields
    that changed in it -- only meaningful, and only ever non-``None``, when
    every changed path is already a member of
    ``_APPROVED_MODEL_DEFINITION_FILES`` (condition 1), which cannot happen
    in production while that set is empty. Real callers always pass
    ``None``; tests populate it directly against a monkeypatched allowlist.
    """

    if changed_paths is None:
        return ClassificationResult("C1", (_C1_UNKNOWN_CONTRADICTORY,))

    matched = _matched_c1_path_rules(changed_paths)
    if _diff_shape_is_contradictory(raw_diff_lines, numstat_lines):
        matched.add(_C1_UNKNOWN_CONTRADICTORY)
    if matched:
        return ClassificationResult("C1", tuple(sorted(matched)))

    if _c3_model_definition_only(changed_paths, field_diffs):
        return ClassificationResult("C3", (C3_MODEL_DEFINITION_ONLY,))

    return ClassificationResult("C2", (DEFAULT_C2,))


# §7.3: doc/policy text where a broad test suite adds little confidence.
_DOC_ONLY_SUFFIXES = (".md",)
_DOC_ONLY_PREFIXES = (
    "docs/plans/",
    "opencode-config/openspec/",
    "platform/openspec/",
    "MCPs/openspec/",
    "ansible-homelab/openspec/",
)


def assurance_route_for(change_class: str, changed_paths: AbstractSet[str] | None) -> str:
    """§7.3: ``tests | review | tests_and_review`` for one C-class.

    C1 always maps to ``tests_and_review``; C3 maps to ``tests`` (the
    narrow profile plus operator complete-diff review at merge time, which
    §7.3 explicitly says is not an independent-reviewer requirement, so it
    does not count toward ``review`` here). For C2, §7.3 names three
    outcomes but hands the implementer only two concrete boundaries: this
    rule implements both -- doc/policy-text-only changes route to
    ``review`` (a broad test suite adds little confidence to prose); every
    other C2 change, including a mix of code and docs, routes to the
    stronger ``tests_and_review`` as the conservative default. A
    tests-only bucket for "materially machine-verifiable" C2 changes is
    deliberately deferred rather than guessed at: routing something that
    actually needed review to tests-only would be a real correctness gap,
    where defaulting to the stronger combined route never is.
    """

    if change_class == "C1":
        return "tests_and_review"
    if change_class == "C3":
        return "tests"
    if changed_paths and all(
        path.endswith(_DOC_ONLY_SUFFIXES) or path.startswith(_DOC_ONLY_PREFIXES)
        for path in changed_paths
    ):
        return "review"
    return "tests_and_review"


def required_check_profile_for(change_class: str, ci_applicability: str) -> str | None:
    """§8: ``checks/c1 | checks/c2 | checks/c3 | null``.

    ``null`` for a repository with no CI applicability at all
    (``not_onboarded``) regardless of class -- there is nothing to profile.

    The class alone decides this, and the §7.3 assurance route deliberately
    does not feed into it. The two answer different questions:
    ``assurance_route_for`` says where a change's assurance comes from (an
    independent reviewer, tests, or both), while this says what CI evidence
    the gate will not merge without. A doc-only C2 routes to ``review``
    because a broad test suite adds little confidence to prose -- that is a
    statement about review, and reading it as permission to accept weaker CI
    evidence would lower a trust-boundary minimum to tidy an inconsistency
    that is not one. Recorded here because the separation reads like an
    oversight otherwise: HB-937 was filed on exactly that reading, before
    the two spec sections were compared. Keep them independent.
    """

    if ci_applicability == "not_onboarded":
        return None
    return f"checks/{change_class.lower()}"


def independent_review_required_for(assurance_route: str) -> bool:
    """§7.3: an independent exact-HEAD review is required exactly when the
    assurance route includes ``review`` -- ``tests`` alone (C3's narrow
    profile, or a future C2 tests-only bucket) does not, matching §7.3's
    explicit statement that C3's operator complete-diff review is "not an
    independent reviewer requirement."
    """

    return assurance_route in ("review", "tests_and_review")


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.stdin))
