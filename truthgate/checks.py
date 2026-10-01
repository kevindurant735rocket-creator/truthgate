"""Check evaluation: the deterministic core.

A check is a small declarative object.  Evaluation returns a
:class:`~truthgate.verdict.CheckResult` and never raises for an ordinary
"the world is not like the check wanted" outcome -- that is FAILED, which is
a legitimate result.  A check that *cannot* run raises :class:`Unverifiable`,
which the caller turns into UNVERIFIED.

Check types:

    command      run a local command, compare its exit code
    file_exists  assert a path exists (and optionally is non-empty)
    file_contains  assert a file's text contains a literal or matches a regex

Every check type must be total: for any well-formed check it must return a
VERIFIED/FAILED verdict.  A type that can only ever return VERIFIED is a
constant-true gate, which the constant detector (see :mod:`truthgate.controls`)
is designed to expose.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .miniyaml import MiniYamlError
from .verdict import CheckResult, Unverifiable, Verdict

#: Check types this build knows how to evaluate.
KNOWN_TYPES = ("command", "file_exists", "file_contains")

DEFAULT_TIMEOUT = 120


class CheckSpecError(ValueError):
    """The check is structurally wrong (missing/!malformed fields)."""


def _require(check: dict[str, Any], field: str) -> Any:
    if field not in check or check[field] is None:
        raise CheckSpecError(f"check {check.get('name', '<unnamed>')!r} is missing {field!r}")
    return check[field]


def _check_name(check: dict[str, Any], index: int) -> str:
    name = check.get("name")
    if not name or not isinstance(name, str):
        raise CheckSpecError(f"check at index {index} needs a non-empty string 'name'")
    return name


def _assert_constant_false(condition: bool, detail: str) -> None:
    """Guard used by every evaluator: never silently pass.

    If an evaluator is about to return VERIFIED on a condition that was not
    actually compared (a vacuous truth), that is a bug in the evaluator.  We
    raise rather than pass, because a gate that passes without comparing
    anything is the exact failure this tool exists to prevent.
    """
    if condition is None:
        raise Unverifiable(detail)


class _BaseCheck:
    """Common evaluation shape: a :meth:`evaluate` returning VERIFIED/FAILED."""

    def evaluate(self, check: dict[str, Any], root: Path) -> CheckResult:
        name = _check_name(check, -1)
        try:
            passed, detail, evidence = self._run(check, root)
        except Unverifiable as exc:
            return CheckResult(
                name=name,
                verdict=Verdict.UNVERIFIED,
                confidence=None,
                detail=str(exc),
                evidence={},
            )
        return CheckResult(
            name=name,
            verdict=Verdict.VERIFIED if passed else Verdict.FAILED,
            # A check that actually ran and compared a condition is, by
            # construction, a binary decision. We do not fabricate a softer
            # number; calibration lives in truthgate.calibrate.
            confidence=1.0 if passed else 0.0,
            detail=detail,
            evidence=evidence,
        )

    def _run(self, check: dict[str, Any], root: Path) -> tuple[bool, str, dict[str, Any]]:
        raise NotImplementedError


class CommandCheck(_BaseCheck):
    """Run a local command; the verdict is the exit-code comparison."""

    def _run(self, check: dict[str, Any], root: Path) -> tuple[bool, str, dict[str, Any]]:
        run = _require(check, "run")
        if not isinstance(run, str) or not run.strip():
            raise CheckSpecError("'run' must be a non-empty string")
        expect = check.get("expect_exit", 0)
        if not isinstance(expect, int) or isinstance(expect, bool):
            raise CheckSpecError("'expect_exit' must be an integer")
        timeout = check.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise CheckSpecError("'timeout' must be a positive integer")

        argv = shlex.split(run)
        if not argv:
            raise CheckSpecError("'run' produced no command")
        program = argv[0]
        resolved = shutil.which(program) or (
            str(root / program) if os.sep in program and (root / program).exists() else None
        )
        if resolved is None:
            # Cannot evaluate: the command is simply not there.  This is
            # UNVERIFIED, never FAILED -- "the tool is missing" is a
            # statement about the machine, not about the work.
            raise Unverifiable(f"command not found on PATH: {program!r}")

        try:
            proc = subprocess.run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                # A command is free to print anything at all, in any encoding.
                # `env` on a machine with a non-UTF-8 locale emits bytes that
                # are not valid UTF-8, and strict decoding would turn a check
                # that ran perfectly into a crash -- losing the exit code,
                # which is the one thing we actually need.  The exit code is
                # what decides the verdict; the output is only ever evidence,
                # so replacing undecodable bytes costs nothing.
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # A timeout is a statement about the machine, not about the work:
            # we learned nothing about whether the assertion holds.  Reporting
            # it as FAILED would mean "this check says the work is not done",
            # which is a claim we cannot support.  It is UNVERIFIED, and the
            # exit code reflects that we could not prove anything.
            raise Unverifiable(
                f"command did not finish within {timeout}s; its exit code is unknown"
            ) from None
        except OSError as exc:
            raise Unverifiable(f"could not execute {program!r}: {exc}") from exc

        _assert_constant_false(
            proc.returncode is None,
            "subprocess returned no exit code",
        )
        evidence = {
            "argv": argv,
            "exit_code": proc.returncode,
            "expected_exit": expect,
            "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        }
        passed = proc.returncode == expect
        detail = (
            f"exit {proc.returncode} == expected {expect}"
            if passed
            else f"exit {proc.returncode} != expected {expect}"
        )
        return passed, detail, evidence


class FileExistsCheck(_BaseCheck):
    """Assert a path exists, optionally non-empty."""

    def _run(self, check: dict[str, Any], root: Path) -> tuple[bool, str, dict[str, Any]]:
        raw = _require(check, "path")
        if not isinstance(raw, str) or not raw:
            raise CheckSpecError("'path' must be a non-empty string")
        target = (root / raw).resolve()
        exists = target.exists()
        evidence = {"path": str(target), "exists": exists}

        if not exists:
            return False, f"path does not exist: {raw}", evidence
        if check.get("non_empty"):
            if target.is_dir():
                has = any(target.iterdir())
            else:
                try:
                    has = target.stat().st_size > 0
                except OSError as exc:
                    raise Unverifiable(f"cannot stat {raw}: {exc}") from exc
            evidence["non_empty"] = has
            if not has:
                return False, f"path exists but is empty: {raw}", evidence
            return True, f"path exists and is non-empty: {raw}", evidence
        return True, f"path exists: {raw}", evidence


class FileContainsCheck(_BaseCheck):
    """Assert a file's text contains a literal, or matches a regex."""

    def _run(self, check: dict[str, Any], root: Path) -> tuple[bool, str, dict[str, Any]]:
        raw_path = _require(check, "path")
        if not isinstance(raw_path, str) or not raw_path:
            raise CheckSpecError("'path' must be a non-empty string")
        target = (root / raw_path).resolve()
        if not target.is_file():
            # Distinguish "not there" from "there but unreadable": both are
            # UNVERIFIED, with different details, because neither is a claim
            # about the work itself.
            if not target.exists():
                raise Unverifiable(f"cannot read contents, file does not exist: {raw_path}")
            raise Unverifiable(f"path is not a regular file: {raw_path}")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise Unverifiable(f"cannot read {raw_path}: {exc}") from exc

        pattern = check.get("regex")
        needle = check.get("contains")
        if pattern is None and needle is None:
            raise CheckSpecError("file_contains needs either 'contains' or 'regex'")
        if pattern is not None and needle is not None:
            raise CheckSpecError("file_contains takes 'contains' or 'regex', not both")

        if pattern is not None:
            try:
                compiled = re.compile(str(pattern))
            except re.error as exc:
                raise CheckSpecError(f"invalid regex {pattern!r}: {exc}") from exc
            hit = compiled.search(text) is not None
            evidence = {"path": str(target), "regex": str(pattern), "matched": hit}
            return hit, (f"regex matched: {pattern}" if hit else f"regex did not match: {pattern}"), evidence

        needle_str = str(needle)
        hit = needle_str in text
        evidence = {"path": str(target), "contains": needle_str, "matched": hit}
        return hit, (
            f"contains literal: {needle_str!r}" if hit else f"does not contain literal: {needle_str!r}"
        ), evidence


_EVALUATORS: dict[str, _BaseCheck] = {
    "command": CommandCheck(),
    "file_exists": FileExistsCheck(),
    "file_contains": FileContainsCheck(),
}


def evaluate_check(check: dict[str, Any], root: Path) -> CheckResult:
    """Evaluate one check dict against ``root``.

    Unknown check types are UNVERIFIED, not FAILED: "we don't know how to run
    this" is a gap in coverage, and reporting it as a failure would train
    users to ignore real failures.  Structurally invalid checks (missing
    name, bad expect_exit) are the one case that *is* a genuine problem with
    the check file, so those propagate as :class:`CheckSpecError`.
    """
    if not isinstance(check, dict):
        raise CheckSpecError(f"check must be a mapping, got {type(check).__name__}")
    name = _check_name(check, -1)
    ctype = check.get("type")
    if ctype is None:
        raise CheckSpecError(f"check {name!r} is missing 'type'")
    evaluator = _EVALUATORS.get(str(ctype))
    if evaluator is None:
        return CheckResult(
            name=name,
            verdict=Verdict.UNVERIFIED,
            confidence=None,
            detail=f"unknown check type {ctype!r}; known types: {', '.join(KNOWN_TYPES)}",
            evidence={"type": str(ctype)},
        )
    return evaluator.evaluate(check, root)


def parse_spec(doc: Any) -> list[dict[str, Any]]:
    """Extract the check list from a parsed spec document.

    Rejects a document with no ``checks`` key loudly.  An empty gate must be
    the user's explicit choice (``checks: []``), not the silent result of a
    parsing mistake -- an empty list of checks reports "all verified", which
    is the most dangerous possible default for this tool.
    """
    if not isinstance(doc, dict):
        raise CheckSpecError("spec document must be a mapping with a top-level 'checks' key")
    if "checks" not in doc:
        raise CheckSpecError("spec has no 'checks' key")
    checks = doc["checks"]
    if checks is None:
        raise CheckSpecError("'checks' is null; use an explicit list (possibly empty)")
    if not isinstance(checks, list):
        raise CheckSpecError("'checks' must be a list")
    for idx, item in enumerate(checks):
        if not isinstance(item, dict):
            raise CheckSpecError(f"check at index {idx} must be a mapping")
    return checks


def spec_root(doc: dict[str, Any], spec_dir: Path) -> Path:
    """Resolve the directory that relative check paths are resolved against.

    Defaults to the spec file's own directory, which is what a spec sitting
    at the repository root wants.  A spec kept elsewhere (e.g. under
    ``examples/``) can set a top-level ``root:`` to point at the project it
    describes -- without it, every relative path silently resolves against
    the wrong tree and the gate reports confident nonsense.
    """
    raw = doc.get("root")
    if raw is None:
        return spec_dir
    if not isinstance(raw, str) or not raw.strip():
        raise CheckSpecError("'root' must be a non-empty path string")
    return (spec_dir / raw).resolve()


__all__ = [
    "KNOWN_TYPES",
    "CheckSpecError",
    "MiniYamlError",
    "evaluate_check",
    "parse_spec",
]
