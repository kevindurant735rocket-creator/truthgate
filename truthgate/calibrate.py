"""Calibration: measuring the gate, not just running it.

Every gate in this space reports whether a task passed.  Almost none report
how often they are right, and the one project that ships a negative control
states outright that it cannot calibrate itself.  That gap is the reason a
gate's verdict is trusted far more than it has earned: 19,000 stars of
LLM-as-judge tooling produce a score, and a score is not a measurement of the
scorer.

This module closes that loop for truthgate.  Given a sample set of checks with
known outcomes, it reports:

    brier_score       mean squared error between predicted probability and
                      known outcome, over [0, 1] -- lower is better
    ece               expected calibration error: how far predicted
                      probabilities are from observed frequencies, binned
    false_positive    how often the gate says "verified" when the truth is
                      "not verified" -- the rate that matters most, because
                      a false pass is the failure that ships bad work
    constant_rate     fraction of the gate's own checks that could not
                      discriminate, i.e. the share of the gate that is
                      decorative

A sample file is a JSON list of entries:

    [
      {"check": {"type": "file_exists", "path": "setup.py"}, "known_pass": true},
      {"check": {"type": "file_exists", "path": "nope.py"},   "known_pass": false}
    ]

``known_pass`` is the ground truth: would this assertion honestly hold?
truthgate runs each check and compares its verdict to that label.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import evaluate_check
from .controls import run_control
from .verdict import Verdict

#: Probabilities are read off verdicts; a check that ran is treated as
#: confident in its binary call.  The ECE therefore measures whether the gate
#: is right at the confidence it claims, which for a deterministic gate means
#: "is the confidence ever misplaced at all".
CONF_VERIFIED = 1.0
CONF_FAILED = 0.0

DEFAULT_ECE_BINS = 10


@dataclass
class CalibrationSample:
    check: dict[str, Any]
    known_pass: bool
    predicted: str | None = None
    predicted_conf: float | None = None
    correct: bool | None = None
    constant: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicted": self.predicted,
            "predicted_conf": self.predicted_conf,
            "known_pass": self.known_pass,
            "correct": self.correct,
            "constant": self.constant,
        }


@dataclass
class CalibrationReport:
    n: int
    brier: float
    ece: float
    false_positive: float
    false_negative: float
    constant_rate: float
    constant_count: int
    accuracy: float
    unverified: int
    bins: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "brier_score": self.brier,
            "ece": self.ece,
            "false_positive_rate": self.false_positive,
            "false_negative_rate": self.false_negative,
            "constant_rate": self.constant_rate,
            "constant_count": self.constant_count,
            "accuracy": self.accuracy,
            "unverified": self.unverified,
            "bins": self.bins,
        }

    def render(self) -> str:
        """Human-readable report, four headline numbers on four lines."""
        def pct(x: float) -> str:
            return f"{x * 100:.1f}%"

        return "\n".join(
            [
                f"Brier score       : {self.brier:.4f}   (0 = perfect, 0.25 = always guess 0.5)  n={self.n}",
                f"ECE               : {self.ece:.4f}   (expected calibration error, {DEFAULT_ECE_BINS} bins)",
                f"False positive    : {pct(self.false_positive)}   ({pct(self.false_positive)} of "
                f"known-fail samples were passed anyway)",
                f"Constant checks   : {self.constant_count}/{self.n} ({pct(self.constant_rate)}) cannot discriminate",
            ]
        )


class CalibrationError(ValueError):
    """The sample set is malformed or empty."""


def load_samples(path: Path) -> list[CalibrationSample]:
    """Read a JSON sample set from disk."""
    if not path.is_file():
        raise CalibrationError(f"sample file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"sample file is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise CalibrationError("sample file must contain a JSON list of samples")
    if not raw:
        raise CalibrationError("sample file contains no samples; nothing to calibrate against")

    samples: list[CalibrationSample] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CalibrationError(f"sample at index {idx} must be an object")
        check = item.get("check")
        known = item.get("known_pass")
        if not isinstance(check, dict):
            raise CalibrationError(f"sample at index {idx} is missing a 'check' object")
        if not isinstance(known, bool):
            raise CalibrationError(f"sample at index {idx} needs 'known_pass' to be a boolean")
        # A calibration sample describes a bare assertion, so it need not
        # carry a display name the way a spec check does. Give it a stable
        # synthetic one so the evaluator's name requirement is satisfied
        # without forcing every sample to repeat boilerplate.
        check = dict(check)
        check.setdefault("name", f"sample[{idx}]")
        samples.append(CalibrationSample(check=check, known_pass=known))
    return samples


def run_samples(samples: list[CalibrationSample], root: Path) -> list[CalibrationSample]:
    """Execute each sample, recording prediction, correctness, constant-ness."""
    for sample in samples:
        result = evaluate_check(sample.check, root)
        sample.predicted = result.verdict.value
        if result.verdict is Verdict.UNVERIFIED:
            # An unverified sample is not a wrong answer, it is a missing one.
            # Excluding it from the rates (and reporting the count) is the
            # honest treatment: forcing it into either class would inflate or
            # deflate the numbers by a choice, not a measurement.
            sample.predicted_conf = None
            sample.correct = None
            continue
        sample.predicted_conf = CONF_VERIFIED if result.verdict is Verdict.VERIFIED else CONF_FAILED
        said_pass = result.verdict is Verdict.VERIFIED
        sample.correct = said_pass == sample.known_pass
        control = run_control(sample.check, root)
        sample.constant = control.is_constant
    return samples


def compute_report(samples: list[CalibrationSample], n_bins: int = DEFAULT_ECE_BINS) -> CalibrationReport:
    """Turn executed samples into the four headline numbers plus detail."""
    graded = [s for s in samples if s.correct is not None]
    n = len(graded)
    if n == 0:
        raise CalibrationError("no sample could be evaluated; every check was UNVERIFIED")

    brier = sum((s.predicted_conf - (1.0 if s.known_pass else 0.0)) ** 2 for s in graded) / n
    accuracy = sum(1 for s in graded if s.correct) / n

    # False positive = gate said pass, truth says it should fail.  These are
    # the ones that let broken work through, so they get their own number.
    known_fail = [s for s in graded if not s.known_pass]
    false_pos = (
        sum(1 for s in known_fail if s.predicted_conf == CONF_VERIFIED) / len(known_fail)
        if known_fail
        else 0.0
    )
    known_pass = [s for s in graded if s.known_pass]
    false_neg = (
        sum(1 for s in known_pass if s.predicted_conf == CONF_FAILED) / len(known_pass)
        if known_pass
        else 0.0
    )

    constant_count = sum(1 for s in graded if s.constant)
    constant_rate = constant_count / n

    bins = _ece_bins(graded, n_bins)
    ece = sum(b["gap"] * b["count"] for b in bins) / n

    return CalibrationReport(
        n=n,
        brier=brier,
        ece=ece,
        false_positive=false_pos,
        false_negative=false_neg,
        constant_rate=constant_rate,
        constant_count=constant_count,
        accuracy=accuracy,
        unverified=len(samples) - n,
        bins=bins,
    )


def _ece_bins(graded: list[CalibrationSample], n_bins: int) -> list[dict[str, Any]]:
    """Bucket predictions by confidence, compare to observed frequency."""
    buckets: list[list[CalibrationSample]] = [[] for _ in range(n_bins)]
    for s in graded:
        conf = s.predicted_conf
        if conf is None:
            continue
        # conf is 0.0 or 1.0; put it in the last or first bin.
        idx = min(n_bins - 1, int(conf * n_bins))
        buckets[idx].append(s)

    out: list[dict[str, Any]] = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        mean_conf = sum(s.predicted_conf for s in bucket if s.predicted_conf is not None) / len(bucket)
        observed = sum(1.0 if s.known_pass else 0.0 for s in bucket) / len(bucket)
        out.append(
            {
                "bin": i,
                "lo": round(i / n_bins, 4),
                "hi": round((i + 1) / n_bins, 4),
                "count": len(bucket),
                "mean_confidence": round(mean_conf, 6),
                "observed_pass_rate": round(observed, 6),
                "gap": round(abs(mean_conf - observed), 6),
            }
        )
    return out


def calibrate(path: Path, root: Path) -> CalibrationReport:
    """Load, execute, and score a sample set in one call."""
    samples = load_samples(path)
    samples = run_samples(samples, root)
    return compute_report(samples)


__all__ = [
    "CalibrationError",
    "CalibrationReport",
    "CalibrationSample",
    "calibrate",
    "compute_report",
    "load_samples",
    "run_samples",
]
