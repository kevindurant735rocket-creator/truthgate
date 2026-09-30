"""Constant-true detection: automatic negative controls.

A gate that can only ever say "verified" is worse than no gate -- it converts
an absence of checking into a record of checking.  The obvious defence is to
ask users to write a negative control by hand.  That defence is known not to
work: bevis, the only competitor shipping one, documents why it made the flag
opt-in and why automating it is impossible -- "a control bevis chose would be
exactly the fake check this tool exists to refuse."  A user asked to invent a
failing example will invent a weak one, and the weak one is the one that gets
remembered.

This module takes the other route: **derive the negative control mechanically
from the check itself**.  A check asserts something about the world.  Mutate
the world in a way that is guaranteed to break that assertion, run the
original check against the mutated world, and see whether it noticed.

    original world  --[mutate]-->  mutated world  --[same check]-->  verdict?
                                                                     |
                                              still VERIFIED --> the check
                                              is constant-true. Report it.

The mutation is chosen per check type, and each mutation is written so that a
correct evaluator *must* notice it.  If a mutated world still yields
VERIFIED, the check is not measuring what it claims to measure.  This turns
"did you write a good check?" from an opinion into a measurement, and it
needs no cooperation from the author.

The three-state discipline carries through: if the mutated world cannot be
evaluated at all, the control is UNVERIFIED (we could not prove the check is
constant either), never a pass.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checks import CheckSpecError, evaluate_check
from .verdict import CheckResult, Unverifiable, Verdict

#: How a mutation was constructed, for the receipt.
MUTATE_RENAME = "rename_target_absent"
MUTATE_EMPTY = "empty_the_target"
MUTATE_EXITCODE = "change_expected_exit"
MUTATE_NEGATE_CONTAINS = "negate_contains"


@dataclass
class ConstantFinding:
    """A check that survived its own negative control."""

    name: str
    strategy: str
    detail: str
    original_verdict: str
    mutated_verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "strategy": self.strategy,
            "detail": self.detail,
            "original_verdict": self.original_verdict,
            "mutated_verdict": self.mutated_verdict,
        }


@dataclass
class ControlResult:
    """Outcome of running one check's automatically derived control."""

    name: str
    #: ``caught`` (control failed as it should -- the check discriminates),
    #: ``survived`` (control passed -- the check is constant-true),
    #: ``inconclusive`` (the control could not be run), or ``skipped``.
    outcome: str
    strategy: str
    detail: str
    mutated_verdict: str | None = None

    @property
    def is_constant(self) -> bool:
        return self.outcome == "survived"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "strategy": self.strategy,
            "detail": self.detail,
            "mutated_verdict": self.mutated_verdict,
        }


def _temp_world() -> Path:
    return Path(tempfile.mkdtemp(prefix="truthgate-control-"))


def _command_runs(check: dict[str, Any], root: Path) -> CheckResult:
    return evaluate_check(check, root)


def _control_command(check: dict[str, Any], root: Path) -> tuple[ControlResult, CheckResult | None]:
    """Control for ``command`` checks: make the expected exit code impossible.

    The most common constant-true command check is ``expect_exit: 0`` on a
    command that always exits 0, or a check whose real assertion is vacuous
    (e.g. it measures nothing).  We mutate the *expectation* to an exit code
    the command cannot produce (we pick one and verify the command genuinely
    does not already return it), which any evaluator comparing exit codes must
    fail.
    """
    run = check.get("run")
    if not isinstance(run, str) or not run.strip():
        return ControlResult(check.get("name", "<unnamed>"), "skipped", MUTATE_EXITCODE, "no run field"), None
    original = _command_runs(check, root)
    if original.verdict is Verdict.UNVERIFIED:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "inconclusive",
                MUTATE_EXITCODE,
                f"original check is UNVERIFIED ({original.detail}); cannot build a control",
            ),
            original,
        )

    # The control must mutate the WORLD, never the check.  Rewriting
    # expect_exit would be testing a different assertion than the user wrote,
    # and it is exactly wrong for a *correct* comparator: a check that asserts
    # "exit == 0" reports FAILED both when the command returns 7 and when we
    # ask it to expect 1007, because 7 matches neither.  That check is
    # working perfectly and would be reported as constant-true.
    #
    # For a command check the world is the process's exit status.  So we keep
    # the check byte-identical and run it against a stand-in command whose
    # exit status is guaranteed to differ from the original's.  A check that
    # really compares exit codes must flip its verdict; one that ignores them
    # cannot.
    import shlex
    import subprocess
    import sys as _sys

    argv = shlex.split(run)
    resolved = shutil.which(argv[0])
    if resolved is None:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "inconclusive",
                MUTATE_EXITCODE,
                f"command not found: {argv[0]!r}",
            ),
            original,
        )
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=check.get("timeout", 120), check=False)
    except Exception as exc:  # noqa: BLE001 - control must never crash the gate
        return (
            ControlResult(check.get("name", "<unnamed>"), "inconclusive", MUTATE_EXITCODE, f"control exec failed: {exc}"),
            original,
        )
    real = proc.returncode
    if real == 0:
        stand_in_code = 1
    else:
        stand_in_code = 0
    # A tiny script that exits with a status the original command did not
    # produce, so a genuine comparison is forced to disagree.
    stand_in = (
        f'import sys; sys.exit({stand_in_code})'
    )
    mutated_spec = dict(check)
    mutated_spec["run"] = f'{_sys.executable} -c "{stand_in}"'
    # The stand-in lives in the same interpreter as the original, so resolve
    # must succeed; if it does not, the control is inconclusive, never a pass.
    if shutil.which(shlex.split(mutated_spec["run"])[0]) is None:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "inconclusive",
                MUTATE_EXITCODE,
                "could not resolve the interpreter for the stand-in command",
            ),
            original,
        )
    mutated = _command_runs(mutated_spec, root)
    if mutated.verdict is Verdict.UNVERIFIED:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "inconclusive",
                MUTATE_EXITCODE,
                f"mutated check could not be evaluated ({mutated.detail})",
                mutated.verdict.value,
            ),
            original,
        )
    if mutated.verdict is original.verdict:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "survived",
                MUTATE_EXITCODE,
                (
                    f"check reported {original.verdict.value} both when the command exited {real} "
                    f"and when it exited {stand_in_code}; the exit code is not being compared"
                ),
                mutated.verdict.value,
            ),
            original,
        )
    return (
        ControlResult(
            check.get("name", "<unnamed>"),
            "caught",
            MUTATE_EXITCODE,
            f"check discriminates: {original.verdict.value} at exit {real} vs {mutated.verdict.value} at exit {stand_in_code}",
            mutated.verdict.value,
        ),
        original,
    )


def _control_file_exists(check: dict[str, Any], root: Path) -> tuple[ControlResult, CheckResult | None]:
    """Control for ``file_exists``: point the check at a path that cannot exist.

    We create a throwaway world, build a deep path under it that is
    guaranteed not to be created, and re-run the same check there.  A correct
    existence check must report FAILED.  Note we use a *new* empty directory as
    the mutated root so we never mutate the user's real tree.
    """
    raw = check.get("path")
    if not isinstance(raw, str) or not raw:
        return ControlResult(check.get("name", "<unnamed>"), "skipped", MUTATE_RENAME, "no path field"), None
    original = evaluate_check(check, root)
    if original.verdict is Verdict.UNVERIFIED:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "inconclusive",
                MUTATE_RENAME,
                f"original check is UNVERIFIED ({original.detail})",
            ),
            original,
        )
    mutant = _temp_world()
    try:
        # A check is constant-true when it returns the SAME verdict no matter
        # what the world looks like.  So we evaluate it in the real world and
        # in a provably different world, and compare.  Judging either side on
        # its own is the bug this shape avoids: a check that always says
        # FAILED is equally unable to discriminate, and reporting it "caught"
        # because it failed in an empty directory would be exactly backwards.
        #
        # A path that escapes the mutant root ('..', or absolute) would let the
        # real world leak back in and make the control vacuous, so refuse to
        # run one rather than report a meaningless result.
        candidate = (mutant / raw).resolve()
        try:
            candidate.relative_to(mutant.resolve())
        except ValueError:
            return (
                ControlResult(
                    check.get("name", "<unnamed>"),
                    "inconclusive",
                    MUTATE_RENAME,
                    f"path {raw!r} escapes the sandboxed world; cannot build a meaningful control",
                ),
                original,
            )
        mutated_spec = dict(check)
        mutated = evaluate_check(mutated_spec, mutant)
        if mutated.verdict is Verdict.UNVERIFIED:
            return (
                ControlResult(
                    check.get("name", "<unnamed>"),
                    "inconclusive",
                    MUTATE_RENAME,
                    f"mutated world could not be evaluated ({mutated.detail})",
                    mutated.verdict.value,
                ),
                original,
            )
        if mutated.verdict is original.verdict:
            return (
                ControlResult(
                    check.get("name", "<unnamed>"),
                    "survived",
                    MUTATE_RENAME,
                    (
                        f"check returned {original.verdict.value} both in the real world and in an "
                        f"empty world; it does not depend on {raw!r} at all"
                    ),
                    mutated.verdict.value,
                ),
                original,
            )
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "caught",
                MUTATE_RENAME,
                f"check discriminates: {original.verdict.value} in the real world vs {mutated.verdict.value} in an empty world",
                mutated.verdict.value,
            ),
            original,
        )
    finally:
        shutil.rmtree(mutant, ignore_errors=True)


def _control_file_contains(check: dict[str, Any], root: Path) -> tuple[ControlResult, CheckResult | None]:
    """Control for ``file_contains``: build two worlds the check must tell apart.

    Constant-ness means "same answer whatever the world looks like", so the
    honest test is whether the check can *distinguish* two worlds that a real
    reader would call different.  We build both:

        a world whose text contains the needle (or matches the regex)
        a world whose text contains neither

    A check that reads its assertion off the content answers differently in
    the two.  That covers both directions a person might reasonably write:
    "this text must contain X" and its mirror image "this text must not
    contain X" (which is how you check for leftover TODO markers).  Only an
    assertion that holds for *any* content -- ``regex: ".*"`` being the
    classic -- answers the same in both and is genuinely constant-true.

    The check is never rewritten.  Rewriting the needle to an absent sentinel
    tests a different assertion than the user wrote, and is exactly how a
    perfectly good check gets wrongly condemned.
    """
    raw = check.get("path")
    if not isinstance(raw, str) or not raw:
        return ControlResult(check.get("name", "<unnamed>"), "skipped", MUTATE_NEGATE_CONTAINS, "no path field"), None
    original = evaluate_check(check, root)
    if original.verdict is Verdict.UNVERIFIED:
        return (
            ControlResult(
                check.get("name", "<unnamed>"),
                "inconclusive",
                MUTATE_NEGATE_CONTAINS,
                f"original check is UNVERIFIED ({original.detail})",
            ),
            original,
        )
    name = str(check.get("name", "<unnamed>"))
    needle = check.get("contains")
    regex = check.get("regex")
    if needle is None and regex is None:
        return ControlResult(name, "skipped", MUTATE_NEGATE_CONTAINS, "no contains/regex field"), None

    # The "satisfied" world: text built to satisfy the assertion.
    if needle is not None:
        satisfied_body = f"truthgate control fixture\n{needle}\n"
    else:
        # A regex the user wrote may not be constructible from a literal, so
        # we cannot invent matching text.  Copying the real file is the only
        # honest source of "text this regex might match"; if the real file
        # does not satisfy it, we fall back to a body that satisfies nothing
        # and the comparison still tells us whether the check reads content.
        src_real = root / raw
        if src_real.is_file():
            try:
                satisfied_body = src_real.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return ControlResult(name, "inconclusive", MUTATE_NEGATE_CONTAINS, f"cannot read {raw}: {exc}"), original
        else:
            satisfied_body = ""

    # The "unsatisfied" world: text that cannot match a literal needle and
    # holds no line break, so anchors like ^ or . have nothing to bite on.
    unsatisfied_body = "\x00\x01\x02"

    mutant = _temp_world()
    try:
        sat = mutant / "satisfied.txt"
        unsat = mutant / "unsatisfied.txt"
        sat.write_text(satisfied_body, encoding="utf-8")
        unsat.write_text(unsatisfied_body, encoding="utf-8")

        sat_spec = dict(check); sat_spec["path"] = "satisfied.txt"
        unsat_spec = dict(check); unsat_spec["path"] = "unsatisfied.txt"
        on_satisfied = evaluate_check(sat_spec, mutant)
        on_unsatisfied = evaluate_check(unsat_spec, mutant)

        if on_satisfied.verdict is Verdict.UNVERIFIED or on_unsatisfied.verdict is Verdict.UNVERIFIED:
            return (
                ControlResult(
                    name,
                    "inconclusive",
                    MUTATE_NEGATE_CONTAINS,
                    "one of the control worlds could not be evaluated",
                    on_unsatisfied.verdict.value,
                ),
                original,
            )
        if on_satisfied.verdict is on_unsatisfied.verdict:
            return (
                ControlResult(
                    name,
                    "survived",
                    MUTATE_NEGATE_CONTAINS,
                    (
                        f"check reported {on_satisfied.verdict.value} both for text that satisfies the "
                        f"assertion and for text that does not; it holds for any content"
                    ),
                    on_unsatisfied.verdict.value,
                ),
                original,
            )
        return (
            ControlResult(
                name,
                "caught",
                MUTATE_NEGATE_CONTAINS,
                (
                    f"check discriminates: {on_satisfied.verdict.value} on satisfying text vs "
                    f"{on_unsatisfied.verdict.value} on unsatisfying text"
                ),
                on_unsatisfied.verdict.value,
            ),
            original,
        )
    finally:
        shutil.rmtree(mutant, ignore_errors=True)


_CONTROLS = {
    "command": _control_command,
    "file_exists": _control_file_exists,
    "file_contains": _control_file_contains,
}


def run_control(check: dict[str, Any], root: Path) -> ControlResult:
    """Derive and run the negative control for one check.

    Never raises for a control problem: a control that cannot be built is
    reported ``inconclusive``, because "we could not prove this check is
    constant" is not the same claim as "this check is sound".
    """
    ctype = str(check.get("type", ""))
    handler = _CONTROLS.get(ctype)
    name = check.get("name", "<unnamed>")
    if handler is None:
        return ControlResult(str(name), "skipped", "none", f"no control strategy for type {ctype!r}")
    try:
        result, _ = handler(check, root)
        return result
    except Unverifiable as exc:
        return ControlResult(str(name), "inconclusive", "none", f"control could not run: {exc}")
    except CheckSpecError as exc:
        return ControlResult(str(name), "inconclusive", "none", f"check spec invalid: {exc}")
    except Exception as exc:  # noqa: BLE001 - a control must never break the gate
        return ControlResult(str(name), "inconclusive", "none", f"control error: {exc}")


def find_constant_checks(
    checks: list[dict[str, Any]], root: Path
) -> tuple[list[ConstantFinding], list[ControlResult]]:
    """Run every check's control; return (constant findings, all results)."""
    findings: list[ConstantFinding] = []
    results: list[ControlResult] = []
    for check in checks:
        if check.get("enabled") is False:
            results.append(ControlResult(str(check.get("name", "<unnamed>")), "skipped", "none", "check disabled"))
            continue
        res = run_control(check, root)
        results.append(res)
        if res.is_constant:
            findings.append(
                ConstantFinding(
                    name=res.name,
                    strategy=res.strategy,
                    detail=res.detail,
                    original_verdict="verified",
                    mutated_verdict=res.mutated_verdict or "verified",
                )
            )
    return findings, results


__all__ = [
    "ConstantFinding",
    "ControlResult",
    "find_constant_checks",
    "run_control",
]
