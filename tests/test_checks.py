"""Check evaluation, including the paths that must be UNVERIFIED.

The negative tests here are the ones that matter most: a check that cannot
run must never report FAILED (that trains users to ignore failures) and must
never report VERIFIED (that manufactures evidence).  Both are asserted
explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from truthgate.checks import CheckSpecError, evaluate_check, parse_spec
from truthgate.miniyaml import MiniYamlError, loads
from truthgate.verdict import Verdict


@pytest.fixture()
def world(tmp_path: Path) -> Path:
    (tmp_path / "present.txt").write_text("hello world\n")
    (tmp_path / "empty.txt").write_text("")
    return tmp_path


def test_command_passing(world):
    r = evaluate_check({"name": "c", "type": "command", "run": "python3 -c 'import sys;sys.exit(0)'"}, world)
    assert r.verdict is Verdict.VERIFIED


def test_command_failing(world):
    r = evaluate_check({"name": "c", "type": "command", "run": "python3 -c 'import sys;sys.exit(3)'", "expect_exit": 0}, world)
    assert r.verdict is Verdict.FAILED


def test_command_missing_is_unverified_not_failed(world):
    r = evaluate_check({"name": "c", "type": "command", "run": "definitely_not_here_xyz --v"}, world)
    assert r.verdict is Verdict.UNVERIFIED
    assert r.confidence is None


def test_unknown_type_is_unverified(world):
    r = evaluate_check({"name": "c", "type": "quantum_flux"}, world)
    assert r.verdict is Verdict.UNVERIFIED


def test_file_exists_true_and_false(world):
    assert evaluate_check({"name": "f", "type": "file_exists", "path": "present.txt"}, world).verdict is Verdict.VERIFIED
    assert evaluate_check({"name": "f", "type": "file_exists", "path": "absent.txt"}, world).verdict is Verdict.FAILED


def test_file_exists_non_empty(world):
    assert evaluate_check({"name": "f", "type": "file_exists", "path": "empty.txt", "non_empty": True}, world).verdict is Verdict.FAILED
    assert evaluate_check({"name": "f", "type": "file_exists", "path": "present.txt", "non_empty": True}, world).verdict is Verdict.VERIFIED


def test_file_contains(world):
    assert evaluate_check({"name": "f", "type": "file_contains", "path": "present.txt", "contains": "hello"}, world).verdict is Verdict.VERIFIED
    assert evaluate_check({"name": "f", "type": "file_contains", "path": "present.txt", "contains": "absent"}, world).verdict is Verdict.FAILED


def test_file_contains_missing_file_is_unverified(world):
    r = evaluate_check({"name": "f", "type": "file_contains", "path": "absent.txt", "contains": "x"}, world)
    assert r.verdict is Verdict.UNVERIFIED


def test_parse_spec_rejects_missing_checks_key():
    with pytest.raises(CheckSpecError, match="no 'checks' key"):
        parse_spec({"something_else": 1})


def test_parse_spec_allows_explicit_empty_list():
    assert parse_spec({"checks": []}) == []


def test_parse_spec_rejects_null_checks():
    with pytest.raises(CheckSpecError):
        parse_spec({"checks": None})


# --- miniyaml ---------------------------------------------------------------

def test_miniyaml_parses_nested_and_scalars():
    doc = loads(
        """
        checks:
          - name: a
            type: command
            run: echo hi
            expect_exit: 0
            tags: [x, y]
        timeout: 30
        enabled: true
        """
    )
    assert doc["checks"][0]["name"] == "a"
    assert doc["checks"][0]["expect_exit"] == 0
    assert doc["checks"][0]["tags"] == ["x", "y"]
    assert doc["timeout"] == 30
    assert doc["enabled"] is True


def test_miniyaml_strips_comments():
    assert loads("a: 1  # trailing\nb: two") == {"a": 1, "b": "two"}


def test_miniyaml_rejects_tab_indent():
    with pytest.raises(MiniYamlError):
        loads("a:\n\tb: 1")
