"""Verdict states and the exit-code contract.

The whole point of truthgate is that "could not check" is a first-class
outcome.  A gate that folds a check it never ran into "pass" is worse than no
gate at all, because it manufactures the evidence it claims to collect.

Three states, never merged:

    VERIFIED   the check ran and the assertion held
    FAILED     the check ran and the assertion did not hold
    UNVERIFIED the check could not be evaluated at all

Exit codes are the contract; anything a CI system reads is a process status.

    0  every check VERIFIED
    2  at least one FAILED  (a real, behaviour-failing gate)
    3  no FAILED, at least one UNVERIFIED  (the gate could not be proven)
    4  at least one check is constant-true and was caught
    1  usage / internal error
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Verdict(str, enum.Enum):
    """Outcome of evaluating a single check."""

    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    FAIL_CONSTANT = "fail_constant"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: A check may legitimately be reported as one of these; FAIL_CONSTANT is
#: reserved for the constant-true detector and never comes from evaluation.
EVALUATED_STATES = (Verdict.VERIFIED, Verdict.FAILED, Verdict.UNVERIFIED)


class ExitCode(enum.IntEnum):
    """Process exit codes.  These are the API, not an implementation detail."""

    OK = 0
    USAGE = 1
    FAILED = 2
    UNVERIFIED = 3
    FAIL_CONSTANT = 4

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class Unverifiable(Exception):
    """Raised by a check that cannot be evaluated.

    This is deliberately *not* the same as an assertion failing.  Callers must
    catch it and report UNVERIFIED; letting it escape as a crash, or turning it
    into FAILED, both destroy the distinction the tool exists to keep.
    """


@dataclass
class CheckResult:
    """The verdict for one named check, plus why it came out that way."""

    name: str
    verdict: Verdict
    #: 0-1 confidence, or None when the check never ran.  Never invent a
    #: number for a check that did not execute.
    confidence: float | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_evaluated(self) -> bool:
        return self.verdict in EVALUATED_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def rollup(results: list[CheckResult]) -> int:
    """Fold per-check verdicts into the process exit code.

    Precedence is behaviour-first: a real failure (FAIL_CONSTANT, FAILED) is
    more informative than "we could not check", so it wins the exit code.
    Reporting the opposite order would make an incomplete gate report
    ExitCode.UNVERIFIED and hide a known failure behind a coverage gap.
    """
    if not results:
        return int(ExitCode.OK)
    if any(r.verdict is Verdict.FAIL_CONSTANT for r in results):
        return int(ExitCode.FAIL_CONSTANT)
    if any(r.verdict is Verdict.FAILED for r in results):
        return int(ExitCode.FAILED)
    if any(r.verdict is Verdict.UNVERIFIED for r in results):
        return int(ExitCode.UNVERIFIED)
    return int(ExitCode.OK)
