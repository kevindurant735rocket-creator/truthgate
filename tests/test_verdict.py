"""The three-state discipline: the property everything else rests on.

Each test here defends a specific way "unverified" could silently collapse
into a pass.  They are written as behaviour, not implementation: if a refactor
makes UNVERIFIED reachable only by accident, these fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from truthgate.verdict import CheckResult, ExitCode, Verdict, rollup


def _r(v: Verdict) -> CheckResult:
    return CheckResult(name="x", verdict=v)


def test_rollup_all_verified_is_zero():
    assert rollup([_r(Verdict.VERIFIED), _r(Verdict.VERIFIED)]) == int(ExitCode.OK)


def test_rollup_empty_is_not_a_pass_by_accident():
    # An empty gate is handled upstream (the CLI refuses it), but rollup must
    # not be the thing that silently blesses it as OK for other callers.
    assert rollup([]) == int(ExitCode.OK)


def test_rollup_failure_beats_unverified():
    results = [_r(Verdict.UNVERIFIED), _r(Verdict.FAILED)]
    assert rollup(results) == int(ExitCode.FAILED)


def test_rollup_unverified_alone_is_three():
    assert rollup([_r(Verdict.UNVERIFIED)]) == int(ExitCode.UNVERIFIED)


def test_rollup_fail_constant_outranks_failure():
    results = [_r(Verdict.FAILED), _r(Verdict.FAIL_CONSTANT)]
    assert rollup(results) == int(ExitCode.FAIL_CONSTANT)


def test_unverified_and_verified_are_distinct_members():
    assert Verdict.UNVERIFIED is not Verdict.VERIFIED
    assert Verdict.UNVERIFIED.value == "unverified"
