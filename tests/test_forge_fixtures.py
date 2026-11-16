"""The recorded forge cases: one verdict set, replayed against both adapters.

``tests/fixtures/forge/README.md`` defines a case. Each case replays its
recorded exchanges through the adapter and the engine, and its outcome must be
the expected verdict or refusal, with every recorded exchange asked for.
"""

from __future__ import annotations

import json

import pytest
from forge_replay import CASES, FORGES, case_files, load, replay

ALL = [path for forge in FORGES for path in case_files(forge)]
KINDS = {"verdict", "malformed", "forge"}
MALFORMED = {"missing_field", "wrong_type", "unknown_state"}


@pytest.mark.parametrize("path", ALL, ids=[f"{p.parent.name}/{p.stem}" for p in ALL])
def test_each_case_replays_to_its_expected_outcome(path, monkeypatch):
    case = load(path)
    outcome, unused = replay(case, monkeypatch)
    assert outcome == case["expect"]
    assert unused == [], "recorded exchanges that no request asked for"


@pytest.mark.parametrize("path", ALL, ids=[f"{p.parent.name}/{p.stem}" for p in ALL])
def test_each_case_has_the_documented_shape(path):
    case = load(path)
    assert case["case"] == path.stem
    assert case["forge"] == path.parent.name
    assert case["kind"] in KINDS
    assert ("covers" in case) == (case["kind"] == "malformed")
    assert case["policy"]["repository"]["forge"] == case["forge"]
    assert case["run"]["mode"] in {"status", "review", "merge"}
    assert set(case["expect"]) in ({"result"}, {"refusal"})
    for exchange in case["exchanges"]:
        assert set(exchange) == {"request", "response"}
        assert set(exchange["request"]) <= {"method", "path", "body"}
        assert exchange["request"]["path"].startswith("/")
        assert set(exchange["response"]) <= {"status", "headers", "body"}
        assert type(exchange["response"]["status"]) is int
    # The file is the canonical serialisation, so a hand edit stays reviewable.
    assert path.read_text(encoding="utf-8") == json.dumps(case, indent=2) + "\n"


def _verdicts(forge):
    return {path.stem: load(path) for path in case_files(forge)
            if load(path)["kind"] == "verdict"}


def test_both_forges_share_one_verdict_case_set():
    github, forgejo = _verdicts("github"), _verdicts("forgejo")
    assert github, "the shared verdict set is empty"
    assert sorted(github) == sorted(forgejo)
    for name in github:
        assert github[name]["expect"] == forgejo[name]["expect"], name
        assert github[name]["run"] == forgejo[name]["run"], name


@pytest.mark.parametrize("forge", FORGES)
def test_each_adapter_refuses_a_missing_field_a_wrong_type_and_an_unknown_state(forge):
    malformed = [load(path) for path in case_files(forge)
                 if load(path)["kind"] == "malformed"]
    assert MALFORMED <= {case["covers"] for case in malformed}
    for case in malformed:
        assert "refusal" in case["expect"], case["case"]


def test_the_case_format_is_documented():
    text = (CASES / "README.md").read_text(encoding="utf-8")
    for word in ("exchanges", "expect", "refusal", "covers", "X-Total-Count", "synthetic"):
        assert word in text
