"""Supplying the reference rule file changes nothing the classifier reports.

``scripts/reference-rules.toml`` is the policy the classifier ran with before its
rules became data, and the rule set it uses when none is supplied. Supplying that
file explicitly must report the same class, rule ids, rows and route as supplying
nothing. Three layers pin it, each at a different distance from the caller:

* every call that ``tests/test_classifier.py`` makes, with that file unmodified,
  is repeated with the file supplied and the two results are compared;
* a generated sweep of paths, built from the file's own entries and their near
  misses, is classified both ways in process;
* ``scripts/assurance.py`` run the way a caller runs it, ``python3 -I`` from
  another directory, prints the same rows both ways.

Both sides of a comparison run the same code, so this catches drift in how a
supplied rule set is read, not a wrong entry in the reference file. The entries
themselves are pinned by ``tests/test_classifier.py``, which names the paths each
rule was written for and still passes unchanged.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import subprocess
import sys
import unittest
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import assurance

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assurance.py"
CLASSIFIER_TESTS = ROOT / "tests" / "test_classifier.py"

#: The entry points a caller uses that accept ``rules=``.
WRAPPED = ("classify_change", "assurance_route_for", "c1_path_rules_by_path", "main")

# The real functions, taken before any test replaces them.
_ORIGINAL = {name: getattr(assurance, name) for name in WRAPPED}


def _run_main(lines, argv):
    """``main`` as a caller sees it: its status, its stdout and its stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = _ORIGINAL["main"](lines, argv)
    return status, out.getvalue(), err.getvalue()


def _settled(values):
    """Iterators are single-use, and a call made twice needs them as lists."""
    return [list(value) if isinstance(value, Iterator) else value for value in values]


class _Pairing:
    """Make each default-rules call a pair: the call, then the same call with the
    reference file supplied.

    The default call's result goes back to the caller untouched, so the tests
    being observed run exactly as they do on their own. A difference is
    recorded, never raised, so one cannot hide another.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reference = assurance.load_rules(assurance.REFERENCE_RULES_PATH)
        self.calls = dict.fromkeys(WRAPPED, 0)
        self.mismatches: list[str] = []
        for name in WRAPPED:
            monkeypatch.setattr(assurance, name, getattr(self, name))

    def _compare(self, name, arguments, default, supplied):
        self.calls[name] += 1
        if default != supplied:
            self.mismatches.append(f"{name}{arguments!r}: default={default!r} supplied={supplied!r}")

    def classify_change(self, *args, rules=None, **kwargs):
        original = _ORIGINAL["classify_change"]
        if rules is not None:
            return original(*args, rules=rules, **kwargs)
        args = _settled(args)
        kwargs = dict(zip(kwargs, _settled(kwargs.values())))
        default = original(*args, **kwargs)
        # tests/test_classifier.py substitutes the C3 allowlists by assigning the
        # module attributes: the only way it can exercise C3 while the reference
        # allowlists are empty. A supplied file would carry those lists in its
        # [model_definition] section, so overlay them. The path rules and the
        # documentation lists still come from the file.
        rules = replace(
            self.reference,
            model_files=assurance._APPROVED_MODEL_DEFINITION_FILES,
            model_fields=assurance._APPROVED_MODEL_DEFINITION_FIELDS,
        )
        self._compare("classify_change", args, default, original(*args, rules=rules, **kwargs))
        return default

    def assurance_route_for(self, change_class, changed_paths, *, rules=None):
        original = _ORIGINAL["assurance_route_for"]
        if rules is not None:
            return original(change_class, changed_paths, rules=rules)
        default = original(change_class, changed_paths)
        supplied = original(change_class, changed_paths, rules=self.reference)
        self._compare("assurance_route_for", (change_class, changed_paths), default, supplied)
        return default

    def c1_path_rules_by_path(self, changed_paths, *, rules=None):
        original = _ORIGINAL["c1_path_rules_by_path"]
        paths = list(changed_paths)
        if rules is not None:
            return original(paths, rules=rules)
        default = original(paths)
        self._compare("c1_path_rules_by_path", (paths,), default, original(paths, rules=self.reference))
        return default

    def main(self, stdin, argv=()):
        lines = list(stdin)
        if argv:
            return _ORIGINAL["main"](lines, argv)
        default = _run_main(lines, ())
        supplied = _run_main(lines, ("--rules", str(assurance.REFERENCE_RULES_PATH)))
        self._compare("main", (lines,), default, supplied)
        status, out, err = default
        sys.stdout.write(out)
        sys.stderr.write(err)
        return status


def _load_classifier_tests():
    """The classifier's own tests, loaded unmodified as a module of this process."""
    spec = importlib.util.spec_from_file_location("_classifier_cases", CLASSIFIER_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_call_in_the_classifier_tests_reports_the_same_with_the_reference_file(monkeypatch):
    pairing = _Pairing(monkeypatch)
    suite = unittest.defaultTestLoader.loadTestsFromModule(_load_classifier_tests())
    cases = suite.countTestCases()
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream).run(suite)
    assert outcome.wasSuccessful(), stream.getvalue()
    assert outcome.testsRun == cases
    assert cases > 0
    assert pairing.mismatches == []
    # Every entry point was reached, so a refactor of the observed tests that
    # stops calling one cannot thin the proof unnoticed.
    assert all(pairing.calls.values()), pairing.calls


# A generated sweep.

SYMLINK = [":000000 120000 000000 abc1234 A\tsome/link"]
BINARY = ["-\t-\tassets/logo.png"]
CLASSES = ("C1", "C2", "C3")


@pytest.fixture(scope="module")
def reference() -> assurance.Rules:
    return assurance.load_rules(assurance.REFERENCE_RULES_PATH)


@pytest.fixture(scope="module")
def probes(reference) -> list[str]:
    """Paths built from the reference file's own entries and their near misses.

    Each exact entry is probed as written and off by a suffix, a parent, a case,
    a missing character, a ``./`` and a trailing slash. Each prefix, including
    the exempt and documentation ones, is probed at, below and beside it, with
    every exemption suffix. Sorted, so a failure names the same paths each run.
    """

    probes = {"README.md", "notes.txt", "src/app.py", "docs/guide.md", "x/y/z.py", "a.md"}
    prefixes = set(reference.doc_only_prefixes)
    suffixes = set(reference.doc_only_suffixes)
    for rule in reference.path_rules:
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
def path_sets(probes) -> list[frozenset[str]]:
    """The empty change, every probe alone, and pairs and triples of a stride of them."""

    sets = [frozenset(), *(frozenset({path}) for path in probes)]
    sets += [frozenset(pair) for pair in itertools.combinations(probes[::4], 2)]
    sets += [frozenset(triple) for triple in itertools.combinations(probes[::17], 3)]
    return sets


def _snapshot(paths, **rules):
    """Everything the classifier reports for one change, with or without ``rules=``."""
    return (
        assurance.classify_change(paths, [], **rules),
        assurance.classify_change(paths, SYMLINK, **rules),
        assurance.classify_change(paths, [], BINARY, **rules),
        assurance.c1_path_rules_by_path(sorted(paths), **rules),
        *(assurance.assurance_route_for(change_class, paths, **rules) for change_class in CLASSES),
    )


def test_a_generated_sweep_is_classified_the_same_with_the_reference_file(path_sets, reference):
    different = [
        sorted(paths) for paths in path_sets if _snapshot(paths) != _snapshot(paths, rules=reference)
    ]
    assert different == [], different[:3]


def test_a_change_that_cannot_be_computed_is_classified_the_same(reference):
    assert assurance.classify_change(None, []) == assurance.classify_change(None, [], rules=reference)
    assert assurance.assurance_route_for("C2", None) == assurance.assurance_route_for(
        "C2", None, rules=reference
    )


def test_the_sweep_reaches_every_rule_and_both_c2_routes(path_sets, reference):
    """The comparison above can only fail where the sweep can tell the rules apart."""
    seen = {
        rule_id
        for paths in path_sets
        for rule_id in assurance.classify_change(paths, []).matched_rule_ids
    }
    assert seen == {rule.rule_id for rule in reference.path_rules} | {assurance.DEFAULT_C2}
    assert {assurance.assurance_route_for("C2", paths) for paths in path_sets} == {
        "review",
        "tests_and_review",
    }


# The entry point.

def test_the_entry_point_prints_the_same_rows_with_the_reference_file(probes):
    supplied = ("--rules", str(assurance.REFERENCE_RULES_PATH))
    printed = 0
    for start in range(0, len(probes), 7):
        lines = [path + "\n" for path in probes[start : start + 7]]
        default = _run_main(lines, ())
        assert default == _run_main(lines, supplied), lines
        printed += len(default[1].splitlines())
    assert printed > 0


def _run_script(tmp_path, stdin, *argv):
    """The script as ``ci-profile`` runs it: isolated, from another directory."""
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *argv], input=stdin, cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )


def test_the_script_prints_the_same_rows_with_the_reference_file(tmp_path, probes, reference):
    stdin = "".join(path + "\n" for path in probes)
    default = _run_script(tmp_path, stdin)
    supplied = _run_script(tmp_path, stdin, "--rules", str(assurance.REFERENCE_RULES_PATH))
    assert default.returncode == supplied.returncode == 0, (default.stderr, supplied.stderr)
    assert default.stderr == supplied.stderr == ""
    assert default.stdout == supplied.stdout
    printed = {row.partition("\t")[0] for row in default.stdout.splitlines()}
    assert printed == {rule.rule_id for rule in reference.path_rules}
