"""The classifier's policy is data: the rule file format, its validator, and what a
supplied file does.

``tests/test_classifier.py`` pins how the reference set classifies, unchanged.
This file pins the seam around it: every way a rule file is refused, that a
supplied file replaces the reference set instead of extending it, that an
unusable file never falls back to a weaker answer, and that the documentation
and the worked example match the code. Every layout in here is synthetic.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import assurance

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assurance.py"
EXAMPLE = ROOT / "examples" / "classifier-rules.toml"
DOCUMENT = ROOT / "docs" / "classifier-rules.md"
ENGINE_IDS = ("C1-UNKNOWN-CONTRADICTORY", "C3-MODEL-DEFINITION-ONLY", "DEFAULT-C2")


def _valid() -> dict:
    return {
        "rule": [{"id": "ONE", "class": "C1", "exact": ["a.txt"]}],
        "doc_only": {},
        "model_definition": {},
    }


def _with_rule(**fields) -> dict:
    data = _valid()
    data["rule"] = [{"id": "ONE", "class": "C1", "exact": ["a.txt"], **fields}]
    return data


def _without(key: str) -> dict:
    data = _valid()
    del data[key]
    return data


def _with(**sections) -> dict:
    return {**_valid(), **sections}


def _two_rules(first: str, second: str) -> dict:
    return _with(rule=[
        {"id": first, "class": "C1", "exact": ["a.txt"]},
        {"id": second, "class": "C1", "exact": ["b.txt"]},
    ])


#: (label, data, stable reason, a fragment the detail must contain)
REFUSALS = [
    ("top level is not a table", lambda: ["rule"], "rules_wrong_type", "(top level)"),
    ("unknown section", lambda: _with(extras={}), "rules_unknown_field", "extras"),
    ("unknown rule field", lambda: _with_rule(colour="red"), "rules_unknown_field", "rule[0].colour"),
    ("singular selector name", lambda: _with_rule(prefix=["a/"]), "rules_unknown_field", "rule[0].prefix"),
    ("unknown doc_only field", lambda: _with(doc_only={"suffix": [".md"]}),
     "rules_unknown_field", "doc_only.suffix"),
    ("unknown model_definition field", lambda: _with(model_definition={"file": []}),
     "rules_unknown_field", "model_definition.file"),
    ("every unknown field is listed", lambda: _with(zeta=1, alpha=2),
     "rules_unknown_field", "alpha, zeta"),
    ("rule section absent", lambda: _without("rule"), "rules_missing_section", "rule"),
    ("doc_only section absent", lambda: _without("doc_only"), "rules_missing_section", "doc_only"),
    ("model_definition section absent", lambda: _without("model_definition"),
     "rules_missing_section", "model_definition"),
    ("several sections absent", lambda: {"rule": _valid()["rule"]},
     "rules_missing_section", "doc_only, model_definition"),
    ("empty file", dict, "rules_missing_section", "doc_only, model_definition, rule"),
    ("rule list empty", lambda: _with(rule=[]), "rules_missing_section", "rule"),
    ("rule is a table, not an array of tables", lambda: _with(rule={"id": "ONE"}),
     "rules_wrong_type", "rule"),
    ("doc_only is not a table", lambda: _with(doc_only=[".md"]), "rules_wrong_type", "doc_only"),
    ("model_definition is not a table", lambda: _with(model_definition="none"),
     "rules_wrong_type", "model_definition"),
    ("a rule is not a table", lambda: _with(rule=["ONE"]), "rules_wrong_type", "rule[0]"),
    ("rule without an id", lambda: _with(rule=[{"class": "C1", "exact": ["a.txt"]}]),
     "rules_missing_field", "rule[0].id"),
    ("rule without a class", lambda: _with(rule=[{"id": "ONE", "exact": ["a.txt"]}]),
     "rules_missing_field", "rule[0].class"),
    ("rule that selects nothing", lambda: _with(rule=[{"id": "ONE", "class": "C1"}]),
     "rules_missing_field", "rule[0].exact or rule[0].prefixes"),
    ("rule whose selectors are empty lists",
     lambda: _with(rule=[{"id": "ONE", "class": "C1", "exact": [], "prefixes": []}]),
     "rules_missing_field", "rule[0].exact or rule[0].prefixes"),
    ("id is not a string", lambda: _with(rule=[{"id": 7, "class": "C1", "exact": ["a.txt"]}]),
     "rules_wrong_type", "rule[0].id"),
    ("id is empty", lambda: _with(rule=[{"id": "", "class": "C1", "exact": ["a.txt"]}]),
     "rules_rule_id_empty", "rule[0].id"),
    ("id is only whitespace", lambda: _with(rule=[{"id": "  ", "class": "C1", "exact": ["a.txt"]}]),
     "rules_rule_id_empty", "rule[0].id"),
    ("id repeats", lambda: _two_rules("ONE", "ONE"), "rules_rule_id_duplicate", "ONE"),
    *[
        (f"id reuses the engine id {engine_id}", lambda engine_id=engine_id: _two_rules("ONE", engine_id),
         "rules_rule_id_duplicate", engine_id)
        for engine_id in ENGINE_IDS
    ],
    *[
        (f"class {value!r}", lambda value=value: _with_rule(**{"class": value}),
         "rules_class_invalid", "rule[0].class")
        for value in ("C2", "C3", "C4", "c1", "", 1, ["C1"])
    ],
    ("exact is not a list", lambda: _with_rule(exact="a.txt"), "rules_wrong_type", "rule[0].exact"),
    ("prefixes is not a list", lambda: _with_rule(prefixes="a/"), "rules_wrong_type", "rule[0].prefixes"),
    ("an entry is not a string", lambda: _with_rule(exact=[3]), "rules_wrong_type", "rule[0].exact[0]"),
    ("an entry is empty", lambda: _with_rule(exact=["a.txt", ""]),
     "rules_entry_invalid", "rule[0].exact[1]"),
    ("an empty prefix would match every path", lambda: _with_rule(prefixes=[""]),
     "rules_entry_invalid", "rule[0].prefixes[0]"),
    ("an empty exempt prefix would exempt every path", lambda: _with_rule(exempt_prefixes=[""]),
     "rules_entry_invalid", "rule[0].exempt_prefixes[0]"),
    ("an entry is padded", lambda: _with_rule(exact=[" a.txt"]), "rules_entry_invalid", "rule[0].exact[0]"),
    ("a glob prefix", lambda: _with_rule(prefixes=["docs/**"]),
     "rules_entry_invalid", "rule[0].prefixes[0]"),
    ("a glob exact", lambda: _with_rule(exact=["src/*.py"]),
     "rules_entry_invalid", "rule[0].exact[0]"),
    ("a bracket class", lambda: _with_rule(exact=["src/[ab].py"]),
     "rules_entry_invalid", "rule[0].exact[0]"),
    ("an absolute path", lambda: _with_rule(exact=["/etc/a.txt"]),
     "rules_entry_invalid", "rule[0].exact[0]"),
    ("a ./ path", lambda: _with_rule(prefixes=["./src/"]),
     "rules_entry_invalid", "rule[0].prefixes[0]"),
    ("a backslash is not a separator", lambda: _with_rule(exact=["src\\a.py"]),
     "rules_entry_invalid", "rule[0].exact[0]"),
    ("exempt_prefixes is not a list", lambda: _with_rule(exempt_prefixes="t/"),
     "rules_wrong_type", "rule[0].exempt_prefixes"),
    ("a glob exempt suffix", lambda: _with_rule(exempt_suffixes=["*.md"]),
     "rules_entry_invalid", "rule[0].exempt_suffixes[0]"),
    ("doc_only suffixes is not a list", lambda: _with(doc_only={"suffixes": ".md"}),
     "rules_wrong_type", "doc_only.suffixes"),
    ("a glob doc_only suffix", lambda: _with(doc_only={"suffixes": ["*.md"]}),
     "rules_entry_invalid", "doc_only.suffixes[0]"),
    ("a glob doc_only prefix", lambda: _with(doc_only={"prefixes": ["docs/**"]}),
     "rules_entry_invalid", "doc_only.prefixes[0]"),
    ("a model_definition file is a glob", lambda: _with(model_definition={"files": ["m/*.yaml"]}),
     "rules_entry_invalid", "model_definition.files[0]"),
    ("a model_definition field is not a string", lambda: _with(model_definition={"fields": [1]}),
     "rules_wrong_type", "model_definition.fields[0]"),
    ("a model_definition field is empty", lambda: _with(model_definition={"fields": [""]}),
     "rules_entry_invalid", "model_definition.fields[0]"),
]

#: Refusals that come from reading the file, not from validating its table.
FILE_REFUSALS = ("rules_unreadable", "rules_not_toml")


def _refusal(data) -> str:
    with pytest.raises(assurance.RulesUnusable) as caught:
        assurance.validate_rules(data)
    return str(caught.value)


@pytest.mark.parametrize(
    ("label", "build", "reason", "fragment"), REFUSALS, ids=[case[0] for case in REFUSALS]
)
def test_an_unusable_table_is_refused_with_a_stable_reason(label, build, reason, fragment):
    message = _refusal(build())
    assert message.partition(":")[0] == reason, message
    assert fragment in message.partition(":")[2], message


def test_the_smallest_usable_table_is_accepted():
    rules = assurance.validate_rules(_valid())
    assert [rule.rule_id for rule in rules.path_rules] == ["ONE"]
    assert rules.doc_only_suffixes == () and rules.doc_only_prefixes == ()
    assert rules.model_files == frozenset() and rules.model_fields == frozenset()


def test_every_published_reason_has_a_failing_case_and_a_document_row():
    """Each ``rules_*`` code is permanent, so none ships untested or undocumented."""
    in_code = set(re.findall(r'"(rules_[a-z_]+)', SCRIPT.read_text(encoding="utf-8")))
    tested = {case[2] for case in REFUSALS} | set(FILE_REFUSALS)
    assert in_code == tested
    document = DOCUMENT.read_text(encoding="utf-8")
    for reason in sorted(in_code):
        assert f"`{reason}`" in document, reason


def test_a_missing_or_unreadable_file_is_refused(tmp_path):
    with pytest.raises(assurance.RulesUnusable, match="rules_unreadable"):
        assurance.load_rules(tmp_path / "absent.toml")
    with pytest.raises(assurance.RulesUnusable, match="rules_unreadable"):
        assurance.load_rules(tmp_path)


def test_a_file_that_is_not_utf8_is_refused(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_bytes(b"\xff\xfe[[rule]]\n")
    with pytest.raises(assurance.RulesUnusable, match="rules_unreadable"):
        assurance.load_rules(path)


def test_a_file_that_is_not_toml_is_refused(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text("[[rule]\nid = 'ONE'\n", encoding="utf-8")
    with pytest.raises(assurance.RulesUnusable, match="rules_not_toml"):
        assurance.load_rules(path)


def test_a_repeated_toml_key_is_refused_by_the_parser(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(
        '[[rule]]\nid = "ONE"\nid = "TWO"\nclass = "C1"\nexact = ["a"]\n'
        "[doc_only]\n[model_definition]\n",
        encoding="utf-8",
    )
    with pytest.raises(assurance.RulesUnusable, match="rules_not_toml"):
        assurance.load_rules(path)


def test_a_file_loads_the_same_from_a_string_and_from_a_path():
    assert assurance.load_rules(str(EXAMPLE)) == assurance.load_rules(EXAMPLE)


# The reference set and the worked example.

def test_the_reference_file_is_usable_and_keeps_its_published_ids():
    rules = assurance.load_rules(assurance.REFERENCE_RULES_PATH)
    assert sorted(rule.rule_id for rule in rules.path_rules) == [
        "C1-ANSIBLE-RUNTIME",
        "C1-CI-TRUST",
        "C1-DEPLOY-ACTIVATE",
        "C1-GATE-AUTHORITY",
        "C1-NETWORK-PVE",
        "C1-REPOSITORY-AUTHORITY",
        "C1-SECRETS-PERMISSIONS",
    ]


def _layout(rules: assurance.Rules) -> set[str]:
    """Every path entry a rule set names, as the layout it encodes."""
    entries = set(rules.doc_only_prefixes) | rules.model_files
    for rule in rules.path_rules:
        entries |= rule.exact | set(rule.prefixes) | set(rule.exempt_prefixes)
    return entries


def test_the_worked_example_is_not_the_reference_layout():
    example = assurance.load_rules(EXAMPLE)
    reference = assurance.load_rules(assurance.REFERENCE_RULES_PATH)
    assert _layout(example).isdisjoint(_layout(reference))
    assert {rule.rule_id for rule in example.path_rules}.isdisjoint(
        {rule.rule_id for rule in reference.path_rules}
    )


def test_a_copy_of_the_module_has_no_layout_of_its_own(tmp_path):
    """Without its reference file the module refuses to load: it carries no rules."""
    copy = tmp_path / "assurance.py"
    shutil.copy(SCRIPT, copy)
    done = subprocess.run(
        [sys.executable, "-I", str(copy)], input="repos.yaml\n",
        capture_output=True, text=True, check=False,
    )
    assert done.returncode != 0
    assert done.stdout == ""
    assert "rules_unreadable" in done.stderr


def test_the_file_beside_the_module_is_the_policy(tmp_path):
    """Swap the reference file and the same module classifies another layout."""
    copy = tmp_path / "assurance.py"
    shutil.copy(SCRIPT, copy)
    shutil.copy(EXAMPLE, tmp_path / "reference-rules.toml")
    done = subprocess.run(
        [sys.executable, "-I", str(copy)], input="repos.yaml\nsrc/auth/login.py\n",
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "AUTH\tsrc/auth/login.py\n"


# A supplied file.

@pytest.fixture(scope="module")
def example() -> assurance.Rules:
    return assurance.load_rules(EXAMPLE)


def _classify(paths, rules, raw=()):
    return assurance.classify_change(frozenset(paths), list(raw), rules=rules)


def test_a_supplied_file_replaces_the_reference_set(example):
    assert _classify({"repos.yaml"}, None).gate_class == "C1"
    assert _classify({"repos.yaml"}, example) == assurance.ClassificationResult(
        "C2", ("DEFAULT-C2",)
    )
    assert _classify({"src/auth/login.py"}, None).gate_class == "C2"
    assert _classify({"src/auth/login.py"}, example) == assurance.ClassificationResult(
        "C1", ("AUTH",)
    )


def test_every_matching_rule_is_reported_in_sorted_order(example):
    result = _classify({"src/auth/a.py", "Dockerfile", "db/migrations/1.sql"}, example)
    assert result == assurance.ClassificationResult("C1", ("AUTH", "MIGRATIONS", "RELEASE-PATH"))


def test_the_order_of_the_rules_never_changes_a_result():
    forward = assurance.validate_rules(_two_rules("A-RULE", "B-RULE"))
    backward = assurance.validate_rules(
        _with(rule=list(reversed(_two_rules("A-RULE", "B-RULE")["rule"])))
    )
    paths = {"a.txt", "b.txt"}
    assert _classify(paths, forward) == _classify(paths, backward)
    assert _classify(paths, forward).matched_rule_ids == ("A-RULE", "B-RULE")


def test_one_matching_path_is_enough_whatever_else_the_change_holds(example):
    result = _classify({"src/ui/button.py", "docs/intro.md", "Dockerfile"}, example)
    assert result == assurance.ClassificationResult("C1", ("RELEASE-PATH",))


def test_an_exemption_applies_per_path(example):
    assert _classify({"db/migrations/README.md"}, example).gate_class == "C2"
    mixed = _classify({"db/migrations/README.md", "db/migrations/1.sql"}, example)
    assert mixed == assurance.ClassificationResult("C1", ("MIGRATIONS",))


def test_exact_entries_match_whole_paths_and_prefixes_match_literally(example):
    for path in ("Dockerfile.bak", "x/Dockerfile", "ci/release.shx", "src/authors/a.py", "src/auth"):
        assert _classify({path}, example).gate_class == "C2", path
    for path in ("Dockerfile", "ci/release.sh", "src/auth/a.py", ".ci/deep/er/a.yml"):
        assert _classify({path}, example).gate_class == "C1", path


def test_a_rule_file_can_escalate_changes_to_itself(example):
    assert _classify({"ci/classifier-rules.toml"}, example).matched_rule_ids == ("RELEASE-PATH",)


def test_the_engine_ids_do_not_depend_on_the_file(example):
    assert assurance.classify_change(None, [], rules=example) == assurance.ClassificationResult(
        "C1", ("C1-UNKNOWN-CONTRADICTORY",)
    )
    symlink = [":000000 120000 000000 abc1234 A\tsrc/link"]
    result = _classify({"src/auth/a.py", "src/link"}, example, raw=symlink)
    assert result.matched_rule_ids == ("AUTH", "C1-UNKNOWN-CONTRADICTORY")


def test_rows_follow_the_supplied_rules(example):
    rows = assurance.c1_path_rules_by_path(
        ["src/auth/a.py", "docs/x.md", "Dockerfile", ".ci/b.yml"], rules=example
    )
    assert rows == [
        ("AUTH", "src/auth/a.py"),
        ("RELEASE-PATH", "Dockerfile"),
        ("RELEASE-PATH", ".ci/b.yml"),
    ]


def test_the_documentation_lists_come_from_the_supplied_file(example):
    def route(*paths, rules):
        return assurance.assurance_route_for("C2", frozenset(paths), rules=rules)

    assert route("notes.rst", rules=None) == "tests_and_review"
    assert route("notes.rst", rules=example) == "review"
    assert route("docs/a.py", rules=example) == "review"
    assert route("docs/a.py", "src/app.py", rules=example) == "tests_and_review"
    assert route("docs/a.py", rules=None) == "tests_and_review"
    assert assurance.assurance_route_for("C1", frozenset({"docs/a.py"}), rules=example) == (
        "tests_and_review"
    )
    assert assurance.assurance_route_for("C3", frozenset({"docs/a.py"}), rules=example) == "tests"


def test_a_supplied_model_definition_allowlist_enables_c3_for_that_set_only():
    data = _with(model_definition={"files": ["m/model.yaml"], "fields": ["model", "provider"]})
    rules = assurance.validate_rules(data)
    paths = frozenset({"m/model.yaml"})

    def classify(fields, *, rules):
        return assurance.classify_change(paths, [], field_diffs={"m/model.yaml": fields}, rules=rules)

    assert classify(frozenset({"model"}), rules=rules) == assurance.ClassificationResult(
        "C3", ("C3-MODEL-DEFINITION-ONLY",)
    )
    assert classify(frozenset({"model", "tools"}), rules=rules).gate_class == "C2"
    assert classify(frozenset({"model"}), rules=None).gate_class == "C2"
    assert assurance.classify_change(paths, [], rules=rules).gate_class == "C2"
    escalated = assurance.validate_rules(_with(
        rule=[{"id": "ONE", "class": "C1", "exact": ["m/model.yaml"]}],
        model_definition=data["model_definition"],
    ))
    assert classify(frozenset({"model"}), rules=escalated).gate_class == "C1"


# The entry point.

def _main(capsys, lines, argv):
    status = assurance.main(lines, argv)
    captured = capsys.readouterr()
    return status, captured.out.splitlines(), captured.err.splitlines()


def test_the_entry_point_reads_a_supplied_file(capsys):
    status, rows, errors = _main(
        capsys, ["src/auth/login.py\n", "repos.yaml\n", "Dockerfile\n"], ["--rules", str(EXAMPLE)]
    )
    assert (status, errors) == (0, [])
    assert rows == ["AUTH\tsrc/auth/login.py", "RELEASE-PATH\tDockerfile"]


def test_the_entry_point_refuses_an_unusable_file_and_prints_no_rows(capsys, tmp_path):
    bad = tmp_path / "rules.toml"
    bad.write_text(
        '[[rule]]\nid = "ONE"\nclass = "C2"\nexact = ["repos.yaml"]\n[doc_only]\n[model_definition]\n',
        encoding="utf-8",
    )
    status, rows, errors = _main(capsys, ["repos.yaml\n"], ["--rules", str(bad)])
    assert status == 2
    assert rows == []
    assert errors == ["merge_guard_refused: rules_class_invalid: rule[0].class"]


def test_the_entry_point_refuses_a_missing_file(capsys, tmp_path):
    missing = tmp_path / "absent.toml"
    status, rows, errors = _main(capsys, ["repos.yaml\n"], ["--rules", str(missing)])
    assert (status, rows) == (2, [])
    assert len(errors) == 1 and errors[0].startswith("merge_guard_refused: rules_unreadable: ")


def test_empty_input_checks_a_file_without_classifying_anything(capsys, tmp_path):
    """The documented way to validate a file: run it with no changed paths."""
    assert _main(capsys, [], ["--rules", str(EXAMPLE)]) == (0, [], [])
    bad = tmp_path / "rules.toml"
    bad.write_text("[doc_only]\n[model_definition]\n", encoding="utf-8")
    status, rows, errors = _main(capsys, [], ["--rules", str(bad)])
    assert (status, rows) == (2, [])
    assert errors == ["merge_guard_refused: rules_missing_section: rule"]


@pytest.mark.parametrize(
    "argv", [["--rules"], ["--rules", "a", "b"], ["--bogus"], ["rules.toml"], ["--rules=a.toml"]]
)
def test_the_entry_point_rejects_a_malformed_command_line(capsys, argv):
    status, rows, errors = _main(capsys, ["repos.yaml\n"], argv)
    assert (status, rows) == (2, [])
    assert errors == ["usage: assurance.py [--rules FILE] < changed-paths"]


def test_without_arguments_the_entry_point_uses_the_reference_set(capsys):
    status, rows, errors = _main(capsys, ["repos.yaml\n", "src/auth/login.py\n"], [])
    assert (status, errors) == (0, [])
    assert rows == ["C1-REPOSITORY-AUTHORITY\trepos.yaml"]


def _run_script(tmp_path, stdin, *argv):
    """The script as ``ci-profile`` runs it: isolated, from another directory."""
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *argv], input=stdin, cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )


def test_the_script_finds_its_reference_file_from_another_directory(tmp_path):
    done = _run_script(tmp_path, "repos.yaml\nREADME.md\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "C1-REPOSITORY-AUTHORITY\trepos.yaml\n"


def test_the_script_runs_a_supplied_file(tmp_path):
    done = _run_script(tmp_path, "repos.yaml\nDockerfile\n", "--rules", str(EXAMPLE))
    assert done.returncode == 0, done.stderr
    assert done.stdout == "RELEASE-PATH\tDockerfile\n"


def test_the_script_never_falls_back_when_the_supplied_file_is_unusable(tmp_path):
    done = _run_script(tmp_path, "repos.yaml\n", "--rules", str(tmp_path / "absent.toml"))
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("merge_guard_refused: rules_unreadable: ")


# The documentation.

def test_the_document_names_every_section_and_field():
    document = DOCUMENT.read_text(encoding="utf-8")
    assert "[[rule]]" in document
    for section, fields in assurance.RULE_FILE_FIELDS.items():
        assert f"`{section}`" in document, section
        for field in fields:
            assert f"`{field}`" in document, f"{section}.{field}"


def test_the_document_embeds_the_example_file_verbatim():
    assert EXAMPLE.read_text(encoding="utf-8").strip() in DOCUMENT.read_text(encoding="utf-8")


#: Changed-path sets the document classifies with the worked example.
DOCUMENT_CASES = [
    ["src/auth/login.py"],
    ["db/migrations/0042_add_index.sql"],
    ["db/migrations/README.md"],
    ["Dockerfile"],
    ["ci/classifier-rules.toml"],
    ["src/ui/button.py"],
    ["docs/intro.md"],
    ["docs/intro.md", "src/ui/button.py"],
    ["src/auth/login.py", "Dockerfile"],
]


def _document_row(paths, rules) -> str:
    result = assurance.classify_change(frozenset(paths), [], rules=rules)
    route = assurance.assurance_route_for(result.gate_class, frozenset(paths), rules=rules)
    named = ", ".join(f"`{path}`" for path in paths)
    ids = ", ".join(f"`{rule_id}`" for rule_id in result.matched_rule_ids)
    return f"| {named} | {ids} | {result.gate_class} | `{route}` |"


@pytest.mark.parametrize("paths", DOCUMENT_CASES, ids=[", ".join(paths) for paths in DOCUMENT_CASES])
def test_the_document_table_is_what_the_classifier_reports(paths, example):
    assert _document_row(paths, example) in DOCUMENT.read_text(encoding="utf-8")
