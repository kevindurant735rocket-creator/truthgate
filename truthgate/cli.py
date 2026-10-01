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
from .receipt import append_run, verify_chain, write_anchor
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
    if not enabled:
        # An empty gate reporting "everything verified" is the single most
        # dangerous default this tool could have, so it is refused outright.
        # The guard has to look at the *enabled* list, not the raw one: a spec
        # whose every check is switched off has just as many proofs to offer as
        # one that declares `checks: []`, and reporting either as a pass is a
        # lie.  (Checking `checks` here let `enabled: false` on every line walk
        # straight through the gate as "0 checks verified, exit 0".)
        detail = (
            "every check is disabled" if checks else "spec declares zero checks"
        )
        print(
            f"truthgate: {detail}. An empty gate proves nothing; "
            f"it is refused rather than reported as all-verified.",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE)

    results: list[CheckResult] = [evaluate_check(c, root) for c in enabled]
    findings, controls = find_constant_checks(enabled, root)

    # Match findings to results by object identity, not by name.  Two checks
    # may legitimately share a name (a spec assembled from fragments can),
    # and matching on the string meant one constant check relabelled every
    # check of that name -- overwriting a real UNVERIFIED verdict with
    # FAIL_CONSTANT and destroying the evidence of what actually happened.
    for finding in findings:
        if not 0 <= finding.index < len(results):  # pragma: no cover - defensive
            continue
        target = results[finding.index]
        # Keep what the check actually reported; the constant finding is
        # additional information about it, not a replacement for it.
        target.evidence["reported_verdict"] = target.verdict.value
        target.evidence["reported_detail"] = target.detail
        target.verdict = Verdict.FAIL_CONSTANT
        target.detail = finding.detail
        target.evidence["constant_control"] = finding.strategy

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
                # Pin the head outside the log. Without this, deleting the
                # last (failing) run leaves a chain that still verifies, and
                # there is nothing inside the file that could tell the
                # difference between "never failed" and "the failure was
                # deleted".
                if args.anchor:
                    anchor_path = root / args.anchor
                    write_anchor(receipt_path, anchor_path)
                print(f"\nreceipt appended: {receipt_path}")
            except OSError as exc:
                print(f"warning: could not write receipt: {exc}", file=sys.stderr)

    # In --json mode stdout must be parseable and nothing else: the closing
    # summary went to stdout too, so `truthgate verify --json | jq` failed with
    # "Extra data" on any run that found something to say.
    if not args.json:
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
    exit_code = _calibration_exit(report, as_json=args.json)
    if args.json:
        payload = report.to_dict()
        payload["verdict"] = "untrustworthy" if exit_code else "ok"
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"truthgate calibration  samples={samples}  root={root}")
        print(report.render())
    return exit_code


#: A gate that passes everything, or is mostly made of checks that cannot
#: fail, is not a gate -- but `calibrate` used to exit 0 for it, so a CI job
#: running this got a green tick next to a 100% false-positive rate. These
#: thresholds are deliberately blunt: the point is that a visibly broken gate
#: cannot report success, not that the number is a well-tuned parameter.
MAX_FALSE_POSITIVE = 0.0
MAX_FALSE_NEGATIVE = 0.0
MAX_CONSTANT_RATE = 0.25
#: More than this share of the sample set failing to evaluate means the
#: remaining numbers describe a shrinking pile, not the gate.
MAX_UNVERIFIED_SHARE = 0.5


def _calibration_exit(report, *, as_json: bool = False) -> int:
    """Exit non-zero when the measured gate should not be trusted."""
    reasons: list[str] = []
    if report.false_positive > MAX_FALSE_POSITIVE:
        reasons.append(
            f"it passed {report.false_positive_count}/{report.known_fail_count} known-fail samples"
        )
    if report.false_negative > MAX_FALSE_NEGATIVE:
        reasons.append(
            f"it failed {report.false_negative_count}/{report.known_pass_count} known-pass samples"
        )
    if report.constant_rate > MAX_CONSTANT_RATE:
        reasons.append(
            f"{report.constant_count}/{report.n} of its checks cannot discriminate"
        )
    if report.accuracy < 1.0 and (report.known_pass_count + report.known_fail_count) > 0:
        wrong = report.known_pass_count + report.known_fail_count - round(
            report.accuracy * (report.known_pass_count + report.known_fail_count)
        )
        reasons.append(f"it got {wrong} of {report.n} graded sample(s) wrong")
    total = report.n + report.unverified
    if total and report.unverified / total > MAX_UNVERIFIED_SHARE:
        reasons.append(
            f"{report.unverified} of {total} sample(s) could not be evaluated, so most of the "
            f"numbers above rest on a minority of the set"
        )
    if not reasons:
        return int(ExitCode.OK)
    if not as_json:
        # In --json mode stdout must stay parseable; the verdict travels in
        # the payload instead, which the block below emits.
        print(
            f"\n{_paint('UNTRUSTWORTHY', '31')}  this gate should not be used to block anything: "
            + "; ".join(reasons)
        )
    return int(ExitCode.FAILED)


def cmd_receipts(args: argparse.Namespace) -> int:
    path = Path(args.path)
    anchor = Path(args.anchor) if args.anchor else None
    ok, reason = verify_chain(path, min_lines=args.min_lines, anchor_path=anchor)
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
    v.add_argument(
        "--anchor",
        default=None,
        help=(
            "also write {count, head_hash} to this file, so a later `truthgate receipts "
            "--anchor` can detect entries deleted from the end of the log"
        ),
    )
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("calibrate", help="score the gate against a sample set with known outcomes")
    c.add_argument("samples", help="JSON file: [{\"check\": {...}, \"known_pass\": true|false}, ...]")
    c.add_argument("--root", help="directory the checks are evaluated in (default: the samples file's directory)")
    c.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    c.set_defaults(func=cmd_calibrate)

    r = sub.add_parser("receipts", help="verify a receipt log's hash chain")
    r.add_argument("path", nargs="?", default=DEFAULT_RECEIPT, help="receipt log path")
    r.add_argument(
        "--min-lines",
        type=int,
        default=None,
        help="fail if the log holds fewer lines than this (a tail deletion is invisible to the chain itself)",
    )
    r.add_argument(
        "--anchor",
        default=None,
        help="compare against this anchor file and fail on a length or head mismatch",
    )
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
    except (CheckSpecError, MiniYamlError, CalibrationError) as exc:
        # A malformed spec is the user's mistake, not a crash in the tool.
        # Reporting it as "internal error" both mislabels the cause and
        # hides which line of the spec needs fixing.
        print(f"truthgate: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)
    except Exception as exc:  # noqa: BLE001 - the CLI must not traceback at users
        print(f"truthgate: internal error: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
