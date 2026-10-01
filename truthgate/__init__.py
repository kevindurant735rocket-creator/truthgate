"""truthgate -- a deterministic verification gate with receipts.

A gate that can only say "verified" is worse than no gate: it turns the
absence of checking into a record of checking.  truthgate exists to make
three things impossible to fake:

1. **"Unverified" stays its own answer.**  A check that could not run reports
   exit 3, never 0.  See :mod:`truthgate.verdict`.
2. **Every check is run against a world that should break it.**  A check
   that passes anyway is reported ``FAIL_CONSTANT`` with exit 4.  See
   :mod:`truthgate.controls`.
3. **The gate is itself measured.**  ``truthgate calibrate`` reports Brier
   score, ECE, false-positive rate and constant rate.  See
   :mod:`truthgate.calibrate`.

Public API:

    from truthgate import evaluate_check, find_constant_checks, rollup
"""

from __future__ import annotations

__version__ = "0.2.0"

from .calibrate import CalibrationError, CalibrationReport, calibrate
from .checks import CheckSpecError, evaluate_check, parse_spec
from .controls import ConstantFinding, ControlResult, find_constant_checks, run_control
from .receipt import append, append_run, verify_chain
from .verdict import CheckResult, ExitCode, Unverifiable, Verdict, rollup

__all__ = [
    "__version__",
    "CalibrationError",
    "CalibrationReport",
    "CheckResult",
    "CheckSpecError",
    "ConstantFinding",
    "ControlResult",
    "ExitCode",
    "Unverifiable",
    "Verdict",
    "append",
    "append_run",
    "calibrate",
    "evaluate_check",
    "find_constant_checks",
    "parse_spec",
    "rollup",
    "run_control",
    "verify_chain",
]
