"""Constant-true detection.

The heart of the suite.  A detector that never fires is worse than no
detector, so these tests use genuine constant-true checks -- ones that pass
or fail no matter what the world contains -- and require the control to catch
every one of them.  The positive tests are equally load-bearing: a detector
that flags everything is just as useless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from truthgate.controls import find_constant_checks, run_control
from truthgate.verdict import Verdict


@pytest.fixture()
def world(tmp_path: Path) -> Path:
    (tmp_path / "real.txt").write_text("hello world\n")
    (tmp_path / "empty.txt").write_text("")
    return tmp_path


# --- these MUST be caught as constant-true ----------------------------------


def test_catch_match_everything_regex(world):
    """``.*`` matches any file whatsoever -- the classic vacuous pass."""
    ctl = run_control({"name": "v", "type": "file_contains", "path": "real.txt", "regex": ".*"}, world)
    assert ctl.is_constant, f"regex '.*' is constant-true but was {ctl.outcome}: {ctl.detail}"


def test_catch_always_present_dot(world):
    ctl = run_control({"name": "v", "type": "file_contains", "path": "real.txt", "regex": "(?s)^."}, world)
    assert ctl.is_constant, f"regex '(?s)^.' is constant-true but was {ctl.outcome}: {ctl.detail}"


def test_catch_permanently_failing_file_exists(world):
    """Always FAILED is as undiscriminating as always VERIFIED."""
    ctl = run_control({"name": "v", "type": "file_exists", "path": "never_created.txt"}, world)
    assert ctl.is_constant, f"a check that always fails is constant, but was {ctl.outcome}"


# --- these MUST NOT be flagged ----------------------------------------------


def test_does_not_flag_real_file_exists(world):
    ctl = run_control({"name": "ok", "type": "file_exists", "path": "real.txt"}, world)
    assert not ctl.is_constant
    assert ctl.outcome == "caught"


def test_does_not_flag_real_contains(world):
    ctl = run_control({"name": "ok", "type": "file_contains", "path": "real.txt", "contains": "hello"}, world)
    assert not ctl.is_constant


def test_does_not_flag_real_regex(world):
    ctl = run_control({"name": "ok", "type": "file_contains", "path": "real.txt", "regex": "hello"}, world)
    assert not ctl.is_constant


def test_does_not_flag_inverted_contains(world):
    """'this text must NOT contain TODO' is a legitimate, discriminating check.

    An earlier version of the control only ever compared the real file with
    unrelated content, so a check that legitimately reports FAILED (because
    the text it is hunting for is absent) was reported as constant-true. The
    control now builds both a satisfying and an unsatisfying world and asks
    whether the check can tell them apart, which is the actual question.
    """
    ctl = run_control({"name": "ok", "type": "file_contains", "path": "real.txt", "contains": "TODO"}, world)
    assert not ctl.is_constant, f"an inverted contains check discriminates and must not be flagged: {ctl.detail}"
    assert ctl.outcome == "caught"


def test_does_not_flag_correctly_failing_command(world):
    """The regression that shaped this module.

    A check asserting ``exit == 0`` on a command that exits 7 is a *correct*
    check that correctly reports FAILED.  An earlier version of the control
    rewrote ``expect_exit`` instead of mutating the world, so this check
    reported FAILED in both worlds and was wrongly flagged as constant-true.
    """
    ctl = run_control({"name": "ok", "type": "command", "run": "python3 -c 'import sys;sys.exit(7)'", "expect_exit": 0}, world)
    assert not ctl.is_constant, f"a correct failing check must not be flagged: {ctl.detail}"
    assert ctl.outcome == "caught"


def test_does_not_flag_correctly_passing_command(world):
    ctl = run_control({"name": "ok", "type": "command", "run": "python3 -c 'import sys;sys.exit(0)'", "expect_exit": 0}, world)
    assert not ctl.is_constant


def test_missing_command_control_is_inconclusive_not_pass(world):
    ctl = run_control({"name": "m", "type": "command", "run": "not_a_real_binary_xyz"}, world)
    assert ctl.outcome == "inconclusive"
    assert not ctl.is_constant


def test_find_constant_reports_the_constant_one(world):
    checks = [
        {"name": "good", "type": "file_exists", "path": "real.txt"},
        {"name": "vacuous", "type": "file_contains", "path": "real.txt", "regex": ".*"},
    ]
    findings, results = find_constant_checks(checks, world)
    assert [f.name for f in findings] == ["vacuous"]
    assert len(results) == 2
