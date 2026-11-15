"""The ``merged`` output comes from the envelope the engine already prints.

`docs/cli.md` owns the envelope and the modes; this pins only the derivation, so a
consumer can gate a release on "this run landed something" without a new flag.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "merge_guard_envelope.py"


def _module():
    spec = importlib.util.spec_from_file_location("merge_guard_envelope", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path, envelope: object) -> tuple[int, str]:
    source = tmp_path / "envelope.json"
    source.write_text(json.dumps(envelope), encoding="utf-8")
    output = tmp_path / "output"
    output.write_text("", encoding="utf-8")
    code = _module().main(["envelope", str(source), str(output)])
    return code, output.read_text(encoding="utf-8")


def test_an_applied_merge_that_landed_a_pull_request_reports_true(tmp_path):
    envelope = {
        "mode": "merge",
        "apply": True,
        "result": [{"number": 1, "merged": False}, {"number": 2, "merged": True}],
    }
    assert _run(tmp_path, envelope) == (0, "merged=true\n")


def test_a_sweep_that_landed_nothing_reports_false(tmp_path):
    envelope = {"mode": "merge", "apply": True, "result": [{"number": 1, "merged": False}]}
    assert _run(tmp_path, envelope) == (0, "merged=false\n")


def test_an_empty_merge_result_reports_false(tmp_path):
    envelope = {"mode": "merge", "apply": True, "result": []}
    assert _run(tmp_path, envelope) == (0, "merged=false\n")


def test_a_truthy_non_boolean_never_reports_a_merge(tmp_path):
    # The engine prints real booleans; a string must not be read as one.
    envelope = {"mode": "merge", "apply": True, "result": [{"merged": "true"}]}
    assert _run(tmp_path, envelope) == (0, "merged=false\n")


def test_a_result_that_is_not_a_list_reports_false(tmp_path):
    envelope = {"mode": "merge", "apply": True, "result": {"merged": True}}
    assert _run(tmp_path, envelope) == (0, "merged=false\n")


def test_a_review_run_never_claims_a_merge(tmp_path):
    envelope = {"mode": "review", "apply": True, "result": [{"merged": True}]}
    assert _run(tmp_path, envelope) == (0, "merged=false\n")


def test_a_dry_run_never_claims_a_merge(tmp_path):
    envelope = {"mode": "merge", "apply": False, "result": [{"merged": True}]}
    assert _run(tmp_path, envelope) == (0, "merged=false\n")


def test_an_unreadable_envelope_fails_closed_without_writing_an_output(tmp_path):
    output = tmp_path / "output"
    output.write_text("", encoding="utf-8")
    code = _module().main(["envelope", str(tmp_path / "missing.json"), str(output)])
    assert code == 1
    assert output.read_text(encoding="utf-8") == ""


def test_malformed_json_fails_closed_without_writing_an_output(tmp_path):
    source = tmp_path / "envelope.json"
    source.write_text("{not json", encoding="utf-8")
    output = tmp_path / "output"
    output.write_text("", encoding="utf-8")
    code = _module().main(["envelope", str(source), str(output)])
    assert code == 1
    assert output.read_text(encoding="utf-8") == ""
