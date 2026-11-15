"""GT-01 §7: conservative, deterministic R×C change classifier.

Per GT-01 §5.1: consumes only changed paths and bounded structured diff
shape -- never executes code from the changed branch to classify itself
(§7.5's own constraint), never imports an operation module, and never
reads ``fleet_deploy`` (that stays ``deploy_chain/catalog.py``'s concern,
same as ``catalog.load_repository_control_policy``).

**Mechanism and policy.** This module is the mechanism: how a changed-path set
becomes a class, a set of rule ids and an assurance route. The policy -- which
paths escalate to C1, which paths count as documentation, which files C3 may
narrow -- is data in a TOML rule file, checked by :func:`validate_rules` and
documented in ``docs/classifier-rules.md``. A caller supplies its own file with
``rules=`` (``--rules`` on the command line). With none supplied the reference
set beside this module, ``reference-rules.toml``, is used, so the default is the
policy this module was extracted with and its results do not change.

The classification functions are pure: each takes already-derived facts
(changed paths, raw diff-shape lines, a :class:`Rules`) and returns a result.
The only I/O is reading a rule file, in :func:`load_rules` and once at import
for the reference set. A reference set that cannot be used fails the import
with :class:`RulesUnusable`, never with a fallback; run as a script, the same
failure is the command line's refusal (see :func:`main`).

Deriving the facts from the current worktree is
``forge.py``'s ``policy()`` operation (GT-01 2.4), matching how
``git_gate/repository.py``'s ``_lane_changed_paths`` already derives the
changed-path list this module classifies.

Rule design notes, since §7.5 states the *conditions* but leaves several
concrete boundaries to the implementer. The reasons for the individual path
rules are written next to them in ``reference-rules.toml``; what stays here is
what the mechanism does with any rule set:

- **A rule's exemptions are per path.** A path that matches a rule's selectors
  and none of its exemptions triggers the rule for the whole change, matching
  "mixed changes containing any C1 trigger" (§7.5's combining rule): a single
  non-exempt path is enough. Every rule is tested against every path and every
  matching id is reported, so the order of the rules never changes a result.
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
- **C3 needs an allowlist, and the reference set leaves it empty**, but all
  five of §7.5's conditions are implemented. Condition 1 (changed files
  are a non-empty subset of the rule set's approved model-definition files,
  ``Rules.model_files``) can never be satisfied while that set is empty, so
  C3 never fires with the reference set -- deliberate, not a placeholder bug
  (``reference-rules.toml`` says why). Conditions 2-3 (structured YAML diff
  touches only approved fields; no tool/MCP/permission/fallback/credential/
  endpoint/runtime-owner/generated-consumer change) collapse into one
  mechanism: *every* top-level field that changed, across every changed path,
  must be a member of the rule set's approved fields (``Rules.model_fields``).
  This enforces condition 3 for free *provided* that set is scoped to strictly
  model/provider-shaped fields and never a tool/MCP/permission/credential/
  endpoint field -- an invariant on the allowlist's content, not something
  this module can verify about a field name it has never seen. A
  "generated consumer changed alongside model definition" case is caught
  earlier, by condition 1 alone: a generated-consumer path is (by
  definition) not itself a model-definition file, so it is never a member
  of the approved files and condition 1 already rejects the whole change.
  The field-level diff itself (*old* vs. *new* top-level keys) is supplied by
  the caller as ``field_diffs``, never computed here -- this module stays
  pure, and no caller can supply it validly while condition 1 already fails,
  so production with the reference set always passes ``None``.
  Condition 4 ("required schema and routing tests exist and pass") is
  deliberately *not* implemented algorithmically: §7.5's own preamble
  forbids this classifier from executing code from the changed branch to
  classify itself, and "pass" cannot be determined any other way. It is
  enforced procedurally instead, by the same standing rule every other
  change in this repository already follows -- nothing merges without the
  pinned test suite green (``git_gate/README.md``'s verification method) --
  not by a fifth algorithmic condition here.

**Extraction provenance.** The rule identifiers, the section references and
the path data in ``reference-rules.toml`` are the policy of the estate this
module was extracted from, not a general default: they are the reference set,
kept so the classification can still be diffed against the copy it replaced,
and a consumer with another layout supplies its own file instead. The module
was moved here with its behaviour unchanged, and the path data was moved out of
it into that file without changing a result.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
import unicodedata
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# The rule ids this module emits itself. They are not policy, so no rule file
# defines them, and a path rule may not reuse one: an id names exactly one rule.
_C1_UNKNOWN_CONTRADICTORY = "C1-UNKNOWN-CONTRADICTORY"
C3_MODEL_DEFINITION_ONLY = "C3-MODEL-DEFINITION-ONLY"
DEFAULT_C2 = "DEFAULT-C2"
_ENGINE_RULE_IDS = frozenset(
    {_C1_UNKNOWN_CONTRADICTORY, C3_MODEL_DEFINITION_ONLY, DEFAULT_C2}
)

# A path rule only ever escalates a change, so the one class it can select is
# the strongest. Weaker classes come from the C3 allowlist and the C2 default.
_PATH_RULE_CLASS = "C1"


class RulesUnusable(ValueError):
    """A rule file or rule table cannot be used.

    The message is a stable ``rules_*`` reason, optionally followed by ``: `` and
    detail naming the offending key, value or file. Callers branch on the reason;
    the detail is for people. ``docs/classifier-rules.md`` lists every reason.
    """


#: Every field a rule file accepts, by section. An unknown field is a refusal, not
#: a warning: a typo would otherwise silently change which paths escalate. ``rule``
#: is an array of tables (``[[rule]]``); the other two sections are plain tables.
RULE_FILE_FIELDS: dict[str, frozenset[str]] = {
    "rule": frozenset({"id", "class", "exact", "prefixes", "exempt_prefixes", "exempt_suffixes"}),
    "doc_only": frozenset({"suffixes", "prefixes"}),
    "model_definition": frozenset({"files", "fields"}),
}

# Entries are matched as plain text. These characters almost always mean the
# entry was written as a glob, which would silently match nothing, so it is
# refused. A path that really contains one is covered by a prefix that stops
# before it.
_GLOB_CHARACTERS = re.compile(r"[*?\[\]]")


@dataclass(frozen=True)
class PathRule:
    """One path rule: the paths it selects, minus the paths it exempts."""

    rule_id: str
    exact: frozenset[str]
    prefixes: tuple[str, ...]
    exempt_prefixes: tuple[str, ...]
    exempt_suffixes: tuple[str, ...]

    def matches(self, path: str) -> bool:
        if path not in self.exact and not path.startswith(self.prefixes):
            return False
        return not (path.startswith(self.exempt_prefixes) or path.endswith(self.exempt_suffixes))


@dataclass(frozen=True)
class Rules:
    """A validated rule set: the policy the classification functions apply."""

    path_rules: tuple[PathRule, ...]
    doc_only_suffixes: tuple[str, ...]
    doc_only_prefixes: tuple[str, ...]
    model_files: frozenset[str]
    model_fields: frozenset[str]


def _entries(value: Any, where: str, *, anchored: bool) -> tuple[str, ...]:
    """A list of literal strings; an *anchored* entry is a repository-relative path.

    An entry that is almost certainly a mistake -- empty (an empty prefix matches
    every path and an empty exemption exempts every path), padded, a glob, a
    backslash, or (anchored) absolute or ``./``-relative -- is refused instead of
    silently misfiring.
    """

    if not isinstance(value, list):
        raise RulesUnusable(f"rules_wrong_type: {where}")
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            raise RulesUnusable(f"rules_wrong_type: {where}[{index}]")
        if (
            not entry
            or entry != entry.strip()
            or _GLOB_CHARACTERS.search(entry)
            or "\\" in entry
            or "\0" in entry
            or (anchored and entry.startswith(("/", "./")))
        ):
            raise RulesUnusable(f"rules_entry_invalid: {where}[{index}]")
    return tuple(value)


def _unknown_fields(data: dict[str, Any]) -> list[str]:
    """Fields the format does not accept, as dotted paths.

    A section of the wrong type is left to :func:`validate_rules`, which reports
    it as ``rules_wrong_type``; this only reports the fields nothing reads.
    """

    found = [key for key in data if key not in RULE_FILE_FIELDS]
    for section in ("doc_only", "model_definition"):
        table = data.get(section)
        if isinstance(table, dict):
            found += [f"{section}.{key}" for key in table if key not in RULE_FILE_FIELDS[section]]
    rules = data.get("rule")
    if isinstance(rules, list):
        for index, item in enumerate(rules):
            if isinstance(item, dict):
                found += [
                    f"rule[{index}].{key}" for key in item if key not in RULE_FILE_FIELDS["rule"]
                ]
    return found


def _has_control_character(text: str) -> bool:
    """Whether *text* holds a character that could end an output line early.

    A rule id is the first column of every row the command line prints, and the
    refusal for a duplicate id names it. A tab would shift the columns, and a
    newline, a carriage return or any other control character (Unicode category
    ``Cc``), or U+2028 or U+2029, which line-splitting readers also break on,
    would start what reads as another row.
    """

    return any(unicodedata.category(char) == "Cc" or char in "\u2028\u2029" for char in text)


def _path_rule(index: int, item: Any, seen: set[str]) -> PathRule:
    where = f"rule[{index}]"
    if not isinstance(item, dict):
        raise RulesUnusable(f"rules_wrong_type: {where}")
    for field in ("id", "class"):
        if field not in item:
            raise RulesUnusable(f"rules_missing_field: {where}.{field}")
    rule_id = item["id"]
    if not isinstance(rule_id, str):
        raise RulesUnusable(f"rules_wrong_type: {where}.id")
    if not rule_id.strip():
        raise RulesUnusable(f"rules_rule_id_empty: {where}.id")
    if _has_control_character(rule_id):
        # The detail names the field, never the id: the id is the text at fault.
        raise RulesUnusable(f"rules_rule_id_invalid: {where}.id")
    if rule_id in seen or rule_id in _ENGINE_RULE_IDS:
        raise RulesUnusable(f"rules_rule_id_duplicate: {rule_id}")
    seen.add(rule_id)
    if item["class"] != _PATH_RULE_CLASS:
        raise RulesUnusable(f"rules_class_invalid: {where}.class")
    exact = _entries(item.get("exact", []), f"{where}.exact", anchored=True)
    prefixes = _entries(item.get("prefixes", []), f"{where}.prefixes", anchored=True)
    if not exact and not prefixes:
        # A rule that selects nothing never fires: a hole, not a policy.
        raise RulesUnusable(f"rules_missing_field: {where}.exact or {where}.prefixes")
    return PathRule(
        rule_id,
        frozenset(exact),
        prefixes,
        _entries(item.get("exempt_prefixes", []), f"{where}.exempt_prefixes", anchored=True),
        _entries(item.get("exempt_suffixes", []), f"{where}.exempt_suffixes", anchored=False),
    )


def validate_rules(data: Any) -> Rules:
    """Validate a parsed rule table and return the rules it declares.

    Raises :class:`RulesUnusable`. Nothing here falls back to another rule set:
    an unusable table classifies nothing, so it can never classify as weaker.
    """

    if not isinstance(data, dict):
        raise RulesUnusable("rules_wrong_type: (top level)")
    unknown = _unknown_fields(data)
    if unknown:
        raise RulesUnusable("rules_unknown_field: " + ", ".join(sorted(unknown)))
    missing = [name for name in RULE_FILE_FIELDS if name not in data]
    if data.get("rule") == []:
        missing.append("rule")
    if missing:
        raise RulesUnusable("rules_missing_section: " + ", ".join(sorted(missing)))
    if not isinstance(data["rule"], list):
        raise RulesUnusable("rules_wrong_type: rule")
    for section in ("doc_only", "model_definition"):
        if not isinstance(data[section], dict):
            raise RulesUnusable(f"rules_wrong_type: {section}")
    seen: set[str] = set()
    path_rules = tuple(_path_rule(index, item, seen) for index, item in enumerate(data["rule"]))
    doc_only, model = data["doc_only"], data["model_definition"]
    return Rules(
        path_rules,
        _entries(doc_only.get("suffixes", []), "doc_only.suffixes", anchored=False),
        _entries(doc_only.get("prefixes", []), "doc_only.prefixes", anchored=True),
        frozenset(_entries(model.get("files", []), "model_definition.files", anchored=True)),
        frozenset(_entries(model.get("fields", []), "model_definition.fields", anchored=False)),
    )


def load_rules(path: str | os.PathLike[str]) -> Rules:
    """Read and validate a TOML rule file; raises :class:`RulesUnusable`."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RulesUnusable(f"rules_unreadable: {path}: {type(exc).__name__}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RulesUnusable(f"rules_not_toml: {path}: {exc}") from exc
    except RecursionError as exc:
        # Nesting deeper than the parser can follow is not a usable rule file.
        raise RulesUnusable(f"rules_not_toml: {path}: nesting too deep") from exc
    return validate_rules(data)


def _refuse(reason: RulesUnusable) -> int:
    """Print the stable refusal line for a rule file that cannot be used; return 2."""

    print(f"merge_guard_refused: {reason}", file=sys.stderr)
    return 2


#: The reference rule set: the policy this module was extracted with. It sits
#: beside this file, so a copy of the module needs a copy of the file.
REFERENCE_RULES_PATH = Path(__file__).resolve().with_name("reference-rules.toml")
try:
    _REFERENCE_RULES = load_rules(REFERENCE_RULES_PATH)
except RulesUnusable as exc:
    # Without a usable reference set the module must not classify, so importing
    # it fails closed with the reason. Run as a script, the same failure is the
    # command line's refusal (exit 2, no rows, no traceback). It comes before
    # any argument is read, so a usable --rules file cannot hide it.
    if __name__ != "__main__":
        raise
    raise SystemExit(_refuse(exc)) from None

# The default rule set's C3 allowlists stay module attributes, read at call time
# by :func:`_resolve`: that is how a caller, and the C3 structure tests,
# substitute an allowlist in-process. They start as the reference file's lists.
_APPROVED_MODEL_DEFINITION_FILES: frozenset[str] = _REFERENCE_RULES.model_files
_APPROVED_MODEL_DEFINITION_FIELDS: frozenset[str] = _REFERENCE_RULES.model_fields


def _resolve(rules: Rules | None) -> Rules:
    """*rules*, or the reference set with the C3 allowlists as they stand now."""

    if rules is not None:
        return rules
    return replace(
        _REFERENCE_RULES,
        model_files=_APPROVED_MODEL_DEFINITION_FILES,
        model_fields=_APPROVED_MODEL_DEFINITION_FIELDS,
    )


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


def _matched_path_rules(changed_paths: Iterable[str], rules: Rules) -> set[str]:
    return {
        rule.rule_id
        for path in changed_paths
        for rule in rules.path_rules
        if rule.matches(path)
    }


def c1_path_rules_by_path(
    changed_paths: Iterable[str], *, rules: Rules | None = None
) -> list[tuple[str, str]]:
    """``(rule_id, path)`` for every path-rule C1 class each path matches.

    Evaluated one path at a time: every path rule is per-path (a rule's
    exemptions need *some* non-exempt path, which a single path satisfies on its
    own), so the union over single-path calls equals the whole-set verdict.
    Shared with ``scripts/ci-profile``, which escalates a pull-request run to
    the full manifest for any path listed here; that script runs this module
    as ``python3 -I assurance.py`` and never copies the rule lists.

    *rules* is the rule set to match against; ``None`` is the reference set.
    """

    rules = _resolve(rules)
    return [
        (rule, path)
        for path in changed_paths
        for rule in sorted(_matched_path_rules([path], rules))
    ]


def main(stdin: Iterable[str], argv: Sequence[str] = ()) -> int:
    """Stdlib entry point: changed paths on stdin, ``<rule_id>\t<path>`` out.

    ``--rules FILE`` classifies against that rule file instead of the reference
    set. A file that cannot be used prints ``merge_guard_refused: <reason>`` on
    stderr, prints no rows and returns 2, so empty output means "no path
    matched" only when the status is 0: the paths are never classified against
    a weaker rule set than the one asked for. A reference set that cannot be
    used is refused the same way when the module runs as a script, before this
    function is reached and whatever *argv* holds.
    """

    rules = None
    if argv:
        if len(argv) != 2 or argv[0] != "--rules":
            print("usage: assurance.py [--rules FILE] < changed-paths", file=sys.stderr)
            return 2
        try:
            rules = load_rules(argv[1])
        except RulesUnusable as exc:
            return _refuse(exc)
    paths = [line.strip() for line in stdin if line.strip()]
    for rule, path in c1_path_rules_by_path(paths, rules=rules):
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
    rules: Rules,
) -> bool:
    """§7.5's five C3 conditions. See this module's docstring for how
    conditions 2-3 collapse into one field-allowlist check, why condition 3
    ("generated consumer changed alongside model definition") is actually
    enforced by condition 1, and why condition 4 is procedural, not
    algorithmic.
    """

    if not changed_paths or not changed_paths <= rules.model_files:
        return False  # condition 1
    if field_diffs is None:
        return False  # cannot prove conditions 2-3 without diff content
    for path in changed_paths:
        fields = field_diffs.get(path)
        if fields is None or not fields <= rules.model_fields:
            return False  # conditions 2-3
    return True


def classify_change(
    changed_paths: AbstractSet[str] | None,
    raw_diff_lines: Iterable[str],
    numstat_lines: Iterable[str] = (),
    field_diffs: dict[str, AbstractSet[str]] | None = None,
    *,
    rules: Rules | None = None,
) -> ClassificationResult:
    """§7.5: classify one changed-path set into C1, C3, or the C2 default.

    *changed_paths* is ``None`` when the caller could not compute it, or it
    exceeded ``LANE_CHANGED_PATHS_MAX`` -- both cases are §7.5's
    "unbounded generated-output"/"missing-base" triggers, escalated to C1
    rather than treated as a separate failure this module raises.

    *field_diffs* maps each changed path to the set of top-level fields
    that changed in it -- only meaningful, and only ever non-``None``, when
    every changed path is already a member of the rule set's approved
    model-definition files (condition 1), which cannot happen with the
    reference set while that allowlist is empty. Real callers always pass
    ``None``; tests populate it directly against a substituted allowlist.

    *rules* is the rule set to classify against; ``None`` is the reference set.
    """

    rules = _resolve(rules)
    if changed_paths is None:
        return ClassificationResult("C1", (_C1_UNKNOWN_CONTRADICTORY,))

    matched = _matched_path_rules(changed_paths, rules)
    if _diff_shape_is_contradictory(raw_diff_lines, numstat_lines):
        matched.add(_C1_UNKNOWN_CONTRADICTORY)
    if matched:
        return ClassificationResult("C1", tuple(sorted(matched)))

    if _c3_model_definition_only(changed_paths, field_diffs, rules):
        return ClassificationResult("C3", (C3_MODEL_DEFINITION_ONLY,))

    return ClassificationResult("C2", (DEFAULT_C2,))


def assurance_route_for(
    change_class: str, changed_paths: AbstractSet[str] | None, *, rules: Rules | None = None
) -> str:
    """§7.3: ``tests | review | tests_and_review`` for one C-class.

    C1 always maps to ``tests_and_review``; C3 maps to ``tests`` (the
    narrow profile plus operator complete-diff review at merge time, which
    §7.3 explicitly says is not an independent-reviewer requirement, so it
    does not count toward ``review`` here). For C2, §7.3 names three
    outcomes but hands the implementer only two concrete boundaries: this
    rule implements both -- doc/policy-text-only changes (every path ends with
    one of the rule set's ``doc_only`` suffixes or starts with one of its
    ``doc_only`` prefixes) route to ``review``, since a broad test suite adds
    little confidence to prose; every other C2 change, including a mix of code
    and docs, routes to the stronger ``tests_and_review`` as the conservative
    default. A tests-only bucket for "materially machine-verifiable" C2 changes
    is deliberately deferred rather than guessed at: routing something that
    actually needed review to tests-only would be a real correctness gap,
    where defaulting to the stronger combined route never is.

    *rules* is the rule set to read the documentation lists from; ``None`` is
    the reference set.
    """

    if change_class == "C1":
        return "tests_and_review"
    if change_class == "C3":
        return "tests"
    rules = _resolve(rules)
    if changed_paths and all(
        path.endswith(rules.doc_only_suffixes) or path.startswith(rules.doc_only_prefixes)
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
    raise SystemExit(main(sys.stdin, sys.argv[1:]))
