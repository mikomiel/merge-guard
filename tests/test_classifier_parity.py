"""The reference rule file, passed explicitly, reports the output recorded for it, and the
classifier refuses to classify when no rule set is passed.

``scripts/reference-rules.toml`` is an example layout. The classifier has no rule set of
its own and never reads that file unless a caller passes it, so every call below that
classifies passes it, loaded with ``load_rules(REFERENCE_RULES_PATH)``, and every call
that does not is refused. Four layers pin it, each at a different distance from the caller:

* every call that ``tests/test_classifier.py`` makes is made with that file, unmodified
  apart from the C3 allowlists that some of those tests fill in;
* a generated sweep of paths, built from the file's own entries and their near misses, is
  refused by every entry point without rules, and with the file reaches every reference
  rule and both C2 routes;
* ``scripts/assurance.py`` run the way a caller runs it, ``python3 -I`` from another
  directory, prints the rows that the function returns for the same paths when the file is
  passed, and refuses without it;
* the recorded output for that sweep, stored under ``tests/fixtures/``, is reproduced byte
  for byte with the reference file passed, over a stored set of paths that reaches every
  reference rule and every route.

The first three pin how a rule set reaches the classifier, not a wrong entry in the
reference file. The entries themselves are pinned by ``tests/test_classifier.py``, which
names the paths each rule was written for, and by the fourth layer, whose stored output
changes only when someone regenerates and reviews it. The comment above the fourth layer
says where that output came from.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import io
import itertools
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import assurance

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assurance.py"
CLASSIFIER_TESTS = ROOT / "tests" / "test_classifier.py"

#: The entry points a caller uses that accept ``rules=``; ``main`` takes it as ``--rules``.
WRAPPED = ("classify_change", "assurance_route_for", "c1_path_rules_by_path", "main")

# The real functions, taken before any test replaces them.
_ORIGINAL = {name: getattr(assurance, name) for name in WRAPPED}


def _run_main(lines, argv):
    """``main`` as a caller sees it: its status, its stdout and its stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = _ORIGINAL["main"](lines, argv)
    return status, out.getvalue(), err.getvalue()


def _refusal_line() -> str:
    """What the command line prints when no rule set is passed: the reason the function raises."""
    with pytest.raises(assurance.RulesRequired) as caught:
        assurance.classify_change(frozenset(), [])
    return f"merge_guard_refused: {caught.value}\n"


def _rows(paths, reference) -> str:
    """The rows the command line prints for *paths*, from the function it is built on."""
    return "".join(
        f"{rule}\t{path}\n" for rule, path in assurance.c1_path_rules_by_path(paths, rules=reference)
    )


class _Observer:
    """Note the rule set behind each call that the classifier tests make, and pass the call on.

    The call's result goes back to the caller untouched, so the tests being observed run
    exactly as they do on their own. A call that does not use the reference file is
    recorded, never raised, so one cannot hide another.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reference = assurance.load_rules(assurance.REFERENCE_RULES_PATH)
        self.calls = dict.fromkeys(WRAPPED, 0)
        self.strays: list[str] = []
        for name in WRAPPED:
            monkeypatch.setattr(assurance, name, getattr(self, name))

    def _note(self, name, arguments, rules):
        self.calls[name] += 1
        # tests/test_classifier.py fills in the C3 allowlists with dataclasses.replace: the
        # only way it can exercise C3 while the reference allowlists are empty. The path
        # rules and the documentation lists must still be the file's own.
        unmodified = None if rules is None else replace(
            rules, model_files=self.reference.model_files, model_fields=self.reference.model_fields
        )
        if unmodified != self.reference:
            self.strays.append(f"{name}{arguments!r}: rules={rules!r}")

    def classify_change(self, *args, rules=None, **kwargs):
        self._note("classify_change", args, rules)
        return _ORIGINAL["classify_change"](*args, rules=rules, **kwargs)

    def assurance_route_for(self, change_class, changed_paths, *, rules=None):
        self._note("assurance_route_for", (change_class, changed_paths), rules)
        return _ORIGINAL["assurance_route_for"](change_class, changed_paths, rules=rules)

    def c1_path_rules_by_path(self, changed_paths, *, rules=None):
        self._note("c1_path_rules_by_path", (changed_paths,), rules)
        return _ORIGINAL["c1_path_rules_by_path"](changed_paths, rules=rules)

    def main(self, stdin, argv=()):
        self.calls["main"] += 1
        if tuple(argv) != ("--rules", str(assurance.REFERENCE_RULES_PATH)):
            self.strays.append(f"main(argv={tuple(argv)!r})")
        return _ORIGINAL["main"](stdin, argv)


def _load_classifier_tests():
    """The classifier's own tests, loaded unmodified as a module of this process."""
    spec = importlib.util.spec_from_file_location("_classifier_cases", CLASSIFIER_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_call_in_the_classifier_tests_is_made_with_the_reference_file(monkeypatch):
    observer = _Observer(monkeypatch)
    suite = unittest.defaultTestLoader.loadTestsFromModule(_load_classifier_tests())
    cases = suite.countTestCases()
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream).run(suite)
    assert outcome.wasSuccessful(), stream.getvalue()
    assert outcome.testsRun == cases
    assert cases > 0
    assert observer.strays == []
    # Every entry point was reached, so a refactor of the observed tests that
    # stops calling one cannot thin the proof unnoticed.
    assert all(observer.calls.values()), observer.calls


# A generated sweep.

SYMLINK = [":000000 120000 000000 abc1234 A\tsome/link"]
BINARY = ["-\t-\tassets/logo.png"]
CLASSES = ("C1", "C2", "C3")


@pytest.fixture(scope="module")
def reference() -> assurance.Rules:
    return assurance.load_rules(assurance.REFERENCE_RULES_PATH)


def probe_paths(rules: assurance.Rules) -> list[str]:
    """Paths built from a rule set's own entries and their near misses.

    Each exact entry is probed as written and off by a suffix, a parent, a case,
    a missing character, a ``./`` and a trailing slash. Each prefix, including
    the exempt and documentation ones, is probed at, below and beside it, with
    every exemption suffix. Sorted, so a failure names the same paths each run.
    """

    probes = {"README.md", "notes.txt", "src/app.py", "docs/guide.md", "x/y/z.py", "a.md"}
    prefixes = set(rules.doc_only_prefixes)
    suffixes = set(rules.doc_only_suffixes)
    for rule in rules.path_rules:
        for exact in rule.exact:
            probes |= {
                exact, exact + ".bak", "x/" + exact, exact.upper(), exact[:-1], "./" + exact, exact + "/",
            }
        prefixes |= {*rule.prefixes, *rule.exempt_prefixes}
        suffixes |= set(rule.exempt_suffixes)
    for prefix in prefixes:
        stem = prefix.rstrip("/")
        probes |= {
            prefix, prefix + "f.py", prefix + "deep/er/f.py", stem, stem + "x/f.py",
            stem.upper() + "/f.py", "z/" + prefix + "f.py",
        }
        probes |= {prefix + "f" + suffix for suffix in suffixes}
    return sorted(probes)


@pytest.fixture(scope="module")
def probes(reference) -> list[str]:
    """The sweep of the reference file: :func:`probe_paths` of its rules."""
    return probe_paths(reference)


@pytest.fixture(scope="module")
def path_sets(probes) -> list[frozenset[str]]:
    """The empty change, every probe alone, and pairs and triples of a stride of them."""

    sets = [frozenset(), *(frozenset({path}) for path in probes)]
    sets += [frozenset(pair) for pair in itertools.combinations(probes[::4], 2)]
    sets += [frozenset(triple) for triple in itertools.combinations(probes[::17], 3)]
    return sets


def _calls(paths, **rules):
    """Every call that reports on one change, not yet made: each entry point, diff shape and class."""
    return [
        lambda: assurance.classify_change(paths, [], **rules),
        lambda: assurance.classify_change(paths, SYMLINK, **rules),
        lambda: assurance.classify_change(paths, [], BINARY, **rules),
        lambda: assurance.c1_path_rules_by_path(sorted(paths), **rules),
        *(
            functools.partial(assurance.assurance_route_for, change_class, paths, **rules)
            for change_class in CLASSES
        ),
    ]


def _unrefused(paths, **rules) -> list[int]:
    """The calls for *paths*, by position, that answer instead of refusing."""
    answered = []
    for index, call in enumerate(_calls(paths, **rules)):
        try:
            call()
        except assurance.RulesRequired:
            continue
        answered.append(index)
    return answered


def test_a_generated_sweep_is_refused_without_rules(path_sets):
    answered = {}
    for paths in path_sets:
        for rules in ({}, {"rules": None}):
            done = _unrefused(paths, **rules)
            if done:
                answered[tuple(sorted(paths)), repr(rules)] = done
    assert answered == {}, list(answered.items())[:3]


def test_a_change_that_cannot_be_computed_is_refused_without_rules_too(reference):
    """Its answer, C1, reads no path rule, so it is the one that an early return would give
    without a rule set. It is refused like any other, and answered when one is passed."""
    with pytest.raises(assurance.RulesRequired):
        assurance.classify_change(None, [])
    with pytest.raises(assurance.RulesRequired):
        assurance.assurance_route_for("C2", None)
    assert assurance.classify_change(None, [], rules=reference) == assurance.ClassificationResult(
        "C1", ("C1-UNKNOWN-CONTRADICTORY",)
    )
    assert assurance.assurance_route_for("C2", None, rules=reference) == "tests_and_review"


def test_the_sweep_reaches_every_rule_and_both_c2_routes(path_sets, reference):
    """The refusal above is only as good as the sweep: it has to tell the rules apart."""
    seen = {
        rule_id
        for paths in path_sets
        for rule_id in assurance.classify_change(paths, [], rules=reference).matched_rule_ids
    }
    assert seen == {rule.rule_id for rule in reference.path_rules} | {assurance.DEFAULT_C2}
    assert {assurance.assurance_route_for("C2", paths, rules=reference) for paths in path_sets} == {
        "review",
        "tests_and_review",
    }


# The entry point.

def test_the_entry_point_prints_the_rows_of_the_reference_file_and_refuses_without_it(
    probes, reference
):
    supplied = ("--rules", str(assurance.REFERENCE_RULES_PATH))
    refusal = _refusal_line()
    printed = 0
    for start in range(0, len(probes), 7):
        chunk = probes[start : start + 7]
        lines = [path + "\n" for path in chunk]
        assert _run_main(lines, ()) == (2, "", refusal), lines
        assert _run_main(lines, supplied) == (0, _rows(chunk, reference), ""), lines
        printed += len(_rows(chunk, reference).splitlines())
    assert printed > 0


def _run_script(tmp_path, stdin, *argv):
    """The script as a CI entrypoint runs it: isolated, from another directory."""
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *argv], input=stdin, cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )


def test_the_script_prints_the_rows_of_the_reference_file_and_refuses_without_it(
    tmp_path, probes, reference
):
    stdin = "".join(path + "\n" for path in probes)
    refused = _run_script(tmp_path, stdin)
    supplied = _run_script(tmp_path, stdin, "--rules", str(assurance.REFERENCE_RULES_PATH))
    assert (refused.returncode, refused.stdout, refused.stderr) == (2, "", _refusal_line())
    assert supplied.returncode == 0, supplied.stderr
    assert supplied.stderr == ""
    assert supplied.stdout == _rows(probes, reference)
    printed = {row.partition("\t")[0] for row in supplied.stdout.splitlines()}
    assert printed == {rule.rule_id for rule in reference.path_rules}


# The recorded output.
#
# The files under tests/fixtures/ are not recomputed by these tests. They are the
# classifier's output for the sweep, recorded and reviewed:
#
#   classifier-golden-paths.txt   the changed paths, one per line: the sweep's probes
#                                 (the reference file's entries, their near misses
#                                 and a few ordinary names), exactly
#   classifier-golden-rows.txt    the command line's output for those paths, as printed
#   classifier-golden-routes.txt  each path, then its route as a C1, a C2 and a C3
#                                 change, tab-separated
#
# In v0.4.0 and every release before it, the reference file held the layout of the
# repository the classifier was extracted from, and these files were the output of the
# released classifier: scripts/assurance.py at 54facfab602c8e25569187e04c03db37eff400aa,
# the file of the v0.0.1, v0.0.2 and v0.1.0 releases, with the rules in the code. The
# reference file then became an example layout by a one-to-one rename of its path
# segments and of two rule ids, and the stored output was carried over by the same
# rename: the released classifier, run on the paths renamed back, printed these rows and
# routes; renamed forward, they equal the stored files. The rename is not kept in this
# repository, so the files are no longer regenerated from the release. They are the
# recorded output of this checkout.
#
# To regenerate them, run from the repository root with the development
# requirements installed:
#
#   python3 -c 'from scripts import assurance; from tests.test_classifier_parity import probe_paths; print(*probe_paths(assurance.load_rules(assurance.REFERENCE_RULES_PATH)), sep="\n")' \
#       > tests/fixtures/classifier-golden-paths.txt
#   python3 -I scripts/assurance.py --rules scripts/reference-rules.toml \
#       < tests/fixtures/classifier-golden-paths.txt > tests/fixtures/classifier-golden-rows.txt
#
# The command line prints no route, so the routes file comes from a short script that
# takes scripts/assurance.py and scripts/reference-rules.toml as its two arguments.
# Redirect its output to tests/fixtures/classifier-golden-routes.txt:
#
#   import importlib.util, sys
#   spec = importlib.util.spec_from_file_location("classifier", sys.argv[1])
#   classifier = importlib.util.module_from_spec(spec)
#   sys.modules["classifier"] = classifier  # its dataclasses look the module up by name
#   spec.loader.exec_module(classifier)
#   rules = classifier.load_rules(sys.argv[2])
#   with open("tests/fixtures/classifier-golden-paths.txt", encoding="utf-8") as handle:
#       for path in handle.read().split():
#           routes = (
#               classifier.assurance_route_for(c, frozenset({path}), rules=rules)
#               for c in ("C1", "C2", "C3")
#           )
#           print(path, *routes, sep="\t")
#
# Regenerate all three when the reference file changes. A line that changes for a path
# that was already there means the reference policy now classifies that path
# differently: that is a decision to review, not an update to accept.

GOLDEN = ROOT / "tests" / "fixtures"
GOLDEN_PATHS = GOLDEN / "classifier-golden-paths.txt"
GOLDEN_ROWS = GOLDEN / "classifier-golden-rows.txt"
GOLDEN_ROUTES = GOLDEN / "classifier-golden-routes.txt"

#: The one case the recorded output is checked in: the reference file, passed explicitly.
#: Without rules the classifier refuses, so there is no recorded output for "default rules".
SUPPLIED = ["reference file supplied"]


def _routes_text(paths, route_for) -> str:
    """The format of the routes file: a path, then its route as a C1, a C2 and a C3 change."""
    return "".join(
        "\t".join([path, *(route_for(change_class, frozenset({path})) for change_class in CLASSES)])
        + "\n"
        for path in paths
    )


def test_the_golden_files_reach_every_reference_rule_and_every_route(reference):
    """A stored output that never shows a rule or a route pins nothing of it."""
    rows = GOLDEN_ROWS.read_text(encoding="utf-8").splitlines()
    assert {row.partition("\t")[0] for row in rows} == {rule.rule_id for rule in reference.path_rules}
    routes = {
        route
        for line in GOLDEN_ROUTES.read_text(encoding="utf-8").splitlines()
        for route in line.split("\t")[1:]
    }
    assert routes == {"review", "tests_and_review", "tests"}


def test_the_golden_paths_are_the_sweep_of_the_reference_file(probes):
    """The stored paths are the sweep itself, so an entry the file gains is recorded too."""
    assert GOLDEN_PATHS.read_text(encoding="utf-8").splitlines() == probes


@pytest.mark.parametrize("argv", [("--rules", str(assurance.REFERENCE_RULES_PATH))], ids=SUPPLIED)
def test_the_script_prints_the_recorded_rows(tmp_path, argv):
    # Bytes in and bytes out, so that no newline translation can hide a difference.
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *argv], input=GOLDEN_PATHS.read_bytes(), cwd=tmp_path,
        capture_output=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == b""
    assert done.stdout == GOLDEN_ROWS.read_bytes()


@pytest.mark.parametrize("rules_path", [assurance.REFERENCE_RULES_PATH], ids=SUPPLIED)
def test_the_routes_are_the_recorded_ones(rules_path):
    route_for = functools.partial(assurance.assurance_route_for, rules=assurance.load_rules(rules_path))
    paths = GOLDEN_PATHS.read_text(encoding="utf-8").splitlines()
    assert _routes_text(paths, route_for).encode("utf-8") == GOLDEN_ROUTES.read_bytes()
