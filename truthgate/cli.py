"""truthgate command line interface.

    truthgate verify   [spec.yaml]      run checks, report verdicts, exit code
    truthgate calibrate samples.json    score the gate against known outcomes
    truthgate receipts [path]           verify the hash chain of a receipt log
    truthgate --version / --help

Exit codes are the contract (see :mod:`truthgate.verdict`):

    0 all verified | 2 a check failed | 3 something could not be checked
    4 a constant-true check was caught | 1 usage or internal error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .calibrate import CalibrationError, calibrate
from .checks import CheckSpecError, evaluate_check, parse_spec, spec_root
from .controls import find_constant_checks
from .miniyaml import MiniYamlError, loads
from .receipt import append_run, verify_chain
from .verdict import CheckResult, ExitCode, Verdict, rollup

DEFAULT_SPEC = "truthgate.yaml"
DEFAULT_RECEIPT = ".truthgate/receipts.jsonl"

def _colour_enabled() -> bool:
    """Colour only when stdout is a terminal and the user has not opted out."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


_COLOR = _colour_enabled()


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


_VERDICT_STYLE = {
    Verdict.VERIFIED: ("verified", "32"),
    Verdict.FAILED: ("failed", "31"),
    Verdict.UNVERIFIED: ("unverified", "33"),
    Verdict.FAIL_CONSTANT: ("FAIL_CONSTANT", "35"),
}


def _format_verdict(v: Verdict) -> str:
    label, code = _VERDICT_STYLE[v]
    return _paint(f"{label:<14}", code)


def load_spec(path: Path) -> tuple[list[dict[str, Any]], Path]:
    """Read and validate a check spec; return (checks, root directory).

    Relative paths inside the spec resolve against the spec's own directory
    unless it declares a top-level ``root:``, which lets a spec kept under
    e.g. ``examples/`` still describe the repository it lives in.
    """
    if not path.is_file():
        raise CheckSpecError(f"spec file not found: {path}")
    try:
        doc = loads(path.read_text(encoding="utf-8"))
    except MiniYamlError as exc:
        raise CheckSpecError(f"{path}: {exc}") from exc
    checks = parse_spec(doc)
    return checks, spec_root(doc, path.parent.resolve())


def cmd_verify(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    try:
        checks, root = load_spec(spec_path)
    except CheckSpecError as exc:
        print(f"truthgate: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)

    enabled = [c for c in checks if c.get("enabled") is not False]
    disabled = len(checks) - len(enabled)
    if not checks:
        # An empty gate reporting "everything verified" is the single most
        # dangerous default this tool could have, so it is refused outright.
        print(
            "truthgate: spec declares zero checks. An empty gate proves nothing; "
            "it is refused rather than reported as all-verified.",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE)

    results: list[CheckResult] = [evaluate_check(c, root) for c in enabled]
    findings, controls = find_constant_checks(enabled, root)

    for finding in findings:
        for r in results:
            if r.name == finding.name:
                r.verdict = Verdict.FAIL_CONSTANT
                r.detail = finding.detail
                r.evidence["constant_control"] = finding.strategy

    exit_code = rollup(results)

    if args.json:
        print(
            json.dumps(
                {
                    "spec": str(spec_path),
                    "exit_code": exit_code,
                    "checks": [r.to_dict() for r in results],
                    "controls": [c.to_dict() for c in controls],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"truthgate {__version__}  spec={spec_path}  ({len(enabled)} check(s)" + (f", {disabled} disabled" if disabled else "") + ")")
        for r in results:
            print(f"  {_format_verdict(r.verdict)} {r.name}")
            if r.detail and (args.verbose or r.verdict is not Verdict.VERIFIED):
                print(f"                  {r.detail}")
        if findings:
            print()
            print("constant-true checks caught (these cannot detect failure):")
            for f in findings:
                print(f"  {finding_label(f)} {f.name}: {f.detail}")
        if not args.no_receipt:
            receipt_path = root / args.receipt
            try:
                append_run(
                    receipt_path,
                    spec_path,
                    exit_code,
                    [r.to_dict() for r in results],
                    [f.to_dict() for f in findings],
                )
                print(f"\nreceipt appended: {receipt_path}")
            except OSError as exc:
                print(f"warning: could not write receipt: {exc}", file=sys.stderr)

    _announce(exit_code, results, findings)
    return exit_code


def finding_label(f: Any) -> str:
    return _paint("FAIL_CONSTANT", "35")


def _announce(exit_code: int, results: list[CheckResult], findings: list[Any]) -> None:
    """One closing line that says what the gate actually concluded."""
    if exit_code == int(ExitCode.OK):
        print(f"\n{_paint('PASS', '32')}  every check verified ({len(results)} check(s))")
    elif exit_code == int(ExitCode.FAILED):
        n = sum(1 for r in results if r.verdict is Verdict.FAILED)
        print(f"\n{_paint('FAIL', '31')}  {n} check(s) failed")
    elif exit_code == int(ExitCode.UNVERIFIED):
        n = sum(1 for r in results if r.verdict is Verdict.UNVERIFIED)
        print(
            f"\n{_paint('UNVERIFIED', '33')}  {n} check(s) could not be evaluated. "
            f"Exit {ExitCode.UNVERIFIED} means 'not proven', which is not the same as passing."
        )
    elif exit_code == int(ExitCode.FAIL_CONSTANT):
        n = len(findings)
        print(
            f"\n{_paint('FAIL_CONSTANT', '35')}  {n} check(s) cannot discriminate; "
            f"they pass regardless of the work. Fix or delete them before trusting this gate."
        )


def cmd_calibrate(args: argparse.Namespace) -> int:
    samples = Path(args.samples)
    root = Path(args.root).resolve() if args.root else samples.parent.resolve()
    try:
        report = calibrate(samples, root)
    except CalibrationError as exc:
        print(f"truthgate: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"truthgate calibration  samples={samples}  root={root}")
        print(report.render())
        if report.unverified:
            print(f"  ({report.unverified} sample(s) were UNVERIFIED and are excluded from the rates)")
    return int(ExitCode.OK)


def cmd_receipts(args: argparse.Namespace) -> int:
    path = Path(args.path)
    ok, reason = verify_chain(path)
    print(f"{path}: {reason}")
    return int(ExitCode.OK) if ok else int(ExitCode.FAILED)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="truthgate",
        description=(
            "A deterministic verification gate. Reports verified / failed / unverified "
            "per check, derives a negative control for every check to prove it is not "
            "constant-true, and can score the gate itself against known outcomes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes:\n"
            "  0  every check verified\n"
            "  1  usage or internal error\n"
            "  2  a check failed\n"
            "  3  a check could not be evaluated (not the same as passing)\n"
            "  4  a constant-true check was caught\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"truthgate {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{verify,calibrate,receipts}")

    v = sub.add_parser("verify", help="run the checks in a spec file")
    v.add_argument("spec", nargs="?", default=DEFAULT_SPEC, help=f"spec file (default: {DEFAULT_SPEC})")
    v.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    v.add_argument("-v", "--verbose", action="store_true", help="show detail for passing checks too")
    v.add_argument("--receipt", default=DEFAULT_RECEIPT, help="receipt log path, or 'none' to disable")
    v.add_argument("--no-receipt", action="store_true", help="do not write a receipt")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("calibrate", help="score the gate against a sample set with known outcomes")
    c.add_argument("samples", help="JSON file: [{\"check\": {...}, \"known_pass\": true|false}, ...]")
    c.add_argument("--root", help="directory the checks are evaluated in (default: the samples file's directory)")
    c.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    c.set_defaults(func=cmd_calibrate)

    r = sub.add_parser("receipts", help="verify a receipt log's hash chain")
    r.add_argument("path", nargs="?", default=DEFAULT_RECEIPT, help="receipt log path")
    r.set_defaults(func=cmd_receipts)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return int(ExitCode.USAGE)
    if getattr(args, "receipt", None) == "none":
        args.no_receipt = True
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI must not traceback at users
        print(f"truthgate: internal error: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
