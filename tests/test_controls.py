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
    assert not ctl.is_constant, f"an inverted contains check must never be convicted: {ctl.detail}"


def test_absent_needle_is_inconclusive_not_vacuous(world):
    """`contains: X` on a file without X is a *failing* check, not a vacuous one.

    It is worth being precise about the case, because it is easy to mistake
    for a negative assertion: `contains` means the file must contain the
    needle, so a clean file makes it fail.  There is no passing behaviour to
    compare against, so constancy is unproven -- and reporting it as
    constant-true would fire on every honest failing check in a real spec.
    """
    (world / "clean.txt").write_text("all good here\n")
    ctl = run_control(
        {"name": "no-todo", "type": "file_contains", "path": "clean.txt", "contains": "TODO"}, world
    )
    assert ctl.outcome == "inconclusive"
    assert not ctl.is_constant


def test_negative_regex_assertion_is_declined_not_convicted(world):
    """A lookahead regex cannot be inverted into a counterexample.

    "must not contain TODO" written as a lookahead is satisfied by any text
    lacking TODO, so mutating content never makes it fail.  Reporting that as
    constant-true would be a false accusation; the tool declines to judge and
    says so.
    """
    (world / "clean.txt").write_text("all good here\n")
    ctl = run_control(
        {
            "name": "no-todo-regex",
            "type": "file_contains",
            "path": "clean.txt",
            "regex": r"(?m)^((?!.*TODO).)*$",
        },
        world,
    )
    assert not ctl.is_constant
    assert ctl.outcome == "inconclusive"


def test_does_not_flag_correctly_failing_command(world):
    """A check that already fails is never called constant-true.

    A check asserting ``exit == 0`` on a command that exits 7 is a *correct*
    check that correctly reports FAILED.  An earlier version of the control
    rewrote ``expect_exit`` instead of mutating the world, so this check
    reported FAILED in both worlds and was flagged constant-true -- firing on
    every honest failing check.  "Same answer in both worlds" is only
    evidence of constancy when the answer is VERIFIED.
    """
    ctl = run_control({"name": "ok", "type": "command", "run": "python3 -c 'import sys;sys.exit(7)'", "expect_exit": 0}, world)
    assert not ctl.is_constant, f"a correct failing check must not be flagged: {ctl.detail}"
    assert ctl.outcome == "inconclusive"


def test_does_not_flag_command_that_inspects_the_project(world):
    """The healthy counterpart, and the one this module must never swallow.

    The previous version of this test used `python3 -c "sys.exit(0)"` as its
    "good" example.  That command inspects nothing and deserves to be called
    constant-true -- the test encoded the very bug it should have caught,
    because a tautological control also reports it as un-flagged.  The
    example is now a command that genuinely reads the project.
    """
    (world / "artifact.txt").write_text("x\n")
    ctl = run_control(
        {"name": "ok", "type": "command", "run": "test -f artifact.txt", "expect_exit": 0}, world
    )
    assert not ctl.is_constant, f"a check that inspects the project discriminates: {ctl.detail}"


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


# ===========================================================================
# command checks: the class the first version of this suite never covered.
#
# The original control substituted a stand-in command with a deliberately
# different exit status, so the two verdicts could never agree and it reported
# "caught" unconditionally.  Every vacuous command check passed while the suite
# stayed green -- because the only command tests asserted the *opposite*
# property ("do not flag a good check"), which a tautology satisfies for free.
#
# The tests below are the ones that would have caught it.
# ===========================================================================


@pytest.mark.parametrize(
    "run",
    [
        "echo hello",
        "echo",
        "true",
        "sleep 0",
        "pwd",
        "env",
        'python3 -c "pass"',
    ],
    ids=["echo", "echo-noop", "true", "sleep0", "pwd", "env", "python-pass"],
)
def test_command_that_never_looks_at_the_world_is_caught(world, run):
    """These pass no matter what exists; each must be reported constant-true."""
    ctl = run_control({"name": "v", "type": "command", "run": run, "expect_exit": 0}, world)
    assert ctl.is_constant, f"{run!r} verifies nothing but was reported {ctl.outcome}: {ctl.detail}"


def test_command_reading_a_real_file_is_not_flagged(world):
    """The mirror of the above, and the reason the detector is worth having."""
    (world / "payload.txt").write_text("data\n")
    ctl = run_control(
        {"name": "ok", "type": "command", "run": "test -s payload.txt", "expect_exit": 0}, world
    )
    assert not ctl.is_constant, f"a check that reads a real file discriminates: {ctl.detail}"


def test_silent_but_meaningful_command_is_not_flagged(world):
    """`grep -q` and `test -s` say nothing on purpose.

    An earlier revision treated "printed no output" as proof of a vacuous
    check.  That condemned the healthiest checks in any real spec, so the
    rule is gone: only the emptied world is evidence.
    """
    (world / "conf.txt").write_text("setting=1\n")
    for run in ("test -s conf.txt", "grep -q setting conf.txt", "test -f conf.txt"):
        ctl = run_control({"name": "ok", "type": "command", "run": run, "expect_exit": 0}, world)
        assert not ctl.is_constant, f"{run!r} is silent by design but discriminates: {ctl.detail}"


def test_correctly_failing_command_control_is_inconclusive_not_constant(world):
    """A check that already fails has no passing behaviour to compare against.

    Reporting it constant-true would fire on every honest failing check and
    make exit 4 meaningless.
    """
    ctl = run_control(
        {"name": "failing", "type": "command", "run": "test -f no_such_file.txt", "expect_exit": 0}, world
    )
    assert not ctl.is_constant
    assert ctl.outcome == "inconclusive"


def test_command_reaching_outside_the_sandbox_is_refused(world):
    """Not re-running is the honest answer; running it and believing the
    result would be the one thing this module must never do."""
    ctl = run_control(
        {"name": "net", "type": "command", "run": "curl -s https://example.com", "expect_exit": 0}, world
    )
    assert ctl.outcome == "inconclusive"
    assert not ctl.is_constant


def test_command_with_absolute_path_is_refused(world):
    ctl = run_control(
        {"name": "abs", "type": "command", "run": "cat /etc/hosts", "expect_exit": 0}, world
    )
    assert ctl.outcome == "inconclusive"
    assert not ctl.is_constant


def test_command_escaping_via_parent_dir_is_refused(world):
    ctl = run_control(
        {"name": "up", "type": "command", "run": "cat ../outside.txt", "expect_exit": 0}, world
    )
    assert ctl.outcome == "inconclusive"
    assert not ctl.is_constant


# ===========================================================================
# Regressions from an adversarial audit.  Each of these was a real defect
# found by trying to break the tool, not a hypothetical.
# ===========================================================================


def test_timeout_is_unverified_not_failed(world):
    """A command that never finishes told us nothing about the work.

    Reporting it FAILED claims "this check says the job is not done", which
    is a statement the tool cannot support.
    """
    from truthgate.checks import evaluate_check

    r = evaluate_check(
        {"name": "slow", "type": "command", "run": "sleep 5", "timeout": 1, "expect_exit": 0}, world
    )
    assert r.verdict is Verdict.UNVERIFIED, f"a timeout must be UNVERIFIED, got {r.verdict}"
    assert r.confidence is None


def test_command_output_with_invalid_utf8_does_not_crash(world):
    """`env` on a non-UTF-8 locale emits bytes strict decoding rejects.

    Crashing there loses the exit code, which is the only thing the verdict
    actually depends on.
    """
    from truthgate.checks import evaluate_check

    r = evaluate_check(
        {"name": "env", "type": "command", "run": "env", "expect_exit": 0}, world
    )
    assert r.verdict in (Verdict.VERIFIED, Verdict.FAILED, Verdict.UNVERIFIED)
    assert r.evidence.get("exit_code") is not None


# ===========================================================================
# Second adversarial pass.  The first round's command control only covered
# bare programs, so a check could opt out of detection with one character.
# ===========================================================================


def test_sh_c_void_command_is_caught(world):
    """`sh -c "exit 0"` is the same vacuity as `echo`, wearing a shell.

    The first fix put `sh -c` on a "cannot be sandboxed" list and returned
    inconclusive -- which handed every shell-wrapped no-op a free pass while
    the gate still reported success.  A parent shell is perfectly runnable
    inside an emptied world; refusing to run it was the bug, not the safety.
    """
    ctl = run_control({"name": "v", "type": "command", "run": 'sh -c "exit 0"', "expect_exit": 0}, world)
    assert ctl.is_constant, f"a shell-wrapped no-op verifies nothing: {ctl.outcome} {ctl.detail}"


def test_relative_script_that_checks_nothing_is_caught(world):
    """`./verify.sh` is the most common real-world check; it must not be a
    hole.  Resolving the program the way the evaluator does is what makes
    the control runnable at all."""
    script = world / "vacuous.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    ctl = run_control({"name": "v", "type": "command", "run": "./vacuous.sh", "expect_exit": 0}, world)
    assert ctl.is_constant, f"a relative script that always succeeds is vacuous: {ctl.outcome} {ctl.detail}"


def test_relative_script_that_reads_the_project_is_not_flagged(world):
    """The mirror image, and the reason the fix above is not just "flag all
    scripts": emptying the world must remove the *data* a script reads while
    leaving the program runnable."""
    (world / "src.txt").write_text("hello\n")
    script = world / "honest.sh"
    script.write_text("#!/bin/sh\ngrep -q hello src.txt\n")
    script.chmod(0o755)
    ctl = run_control({"name": "ok", "type": "command", "run": "./honest.sh", "expect_exit": 0}, world)
    assert not ctl.is_constant, f"a script that reads the project discriminates: {ctl.detail}"
    assert ctl.outcome == "caught"


def test_a_fifo_in_the_tree_does_not_turn_the_gate_red(world):
    """One named pipe makes copytree abort, leaving an *empty* world behind.

    Every command then "passes with the project deleted", so a stray build
    artefact produced a fully red gate with a factually false accusation.
    """
    import os as _os

    try:
        _os.mkfifo(world / "build-pipe")
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        pytest.skip("FIFO creation unsupported here")
    ctl = run_control(
        {"name": "ok", "type": "command", "run": "test -f real.txt", "expect_exit": 0}, world
    )
    assert not ctl.is_constant, f"an unbuildable control must not convict: {ctl.detail}"
    assert ctl.outcome == "inconclusive"
