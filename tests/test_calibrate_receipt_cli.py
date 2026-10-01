"""Calibration and receipt chain, plus a CLI smoke test."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from truthgate.calibrate import CalibrationError, calibrate
from truthgate.receipt import GENESIS, append, verify_chain


@pytest.fixture()
def world(tmp_path: Path) -> Path:
    (tmp_path / "present.txt").write_text("hello world\n")
    return tmp_path


def _samples(world: Path) -> Path:
    path = world / "samples.json"
    path.write_text(
        json.dumps(
            [
                {"check": {"type": "file_exists", "path": "present.txt"}, "known_pass": True},
                {"check": {"type": "file_exists", "path": "absent.txt"}, "known_pass": False},
            ]
        )
    )
    return path


def test_calibration_perfect_gate_is_near_zero_brier(world):
    report = calibrate(_samples(world), world)
    # Both samples behave exactly as their labels say.
    assert report.brier == pytest.approx(0.0)
    assert report.accuracy == pytest.approx(1.0)
    assert report.false_positive == pytest.approx(0.0)


def test_calibration_detects_a_greedy_gate(world):
    """A gate that says 'verified' to everything is maximally over-confident.

    This is the failure mode the whole tool exists to make visible, so the
    report must score it as badly as it is.
    """
    path = world / "greedy.json"
    path.write_text(
        json.dumps(
            [
                {"check": {"type": "file_contains", "path": "present.txt", "regex": ".*"}, "known_pass": True},
                {"check": {"type": "file_contains", "path": "present.txt", "regex": ".*"}, "known_pass": False},
            ]
        )
    )
    report = calibrate(path, world)
    # It passes the known-fail sample -> false positive 1.0, and it is also
    # constant, so the constant rate flags it too.
    assert report.false_positive == pytest.approx(1.0)
    assert report.constant_rate == pytest.approx(1.0)


def test_calibration_rejects_bad_input(world):
    bad = world / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(CalibrationError):
        calibrate(bad, world)
    empty = world / "empty.json"
    empty.write_text("[]")
    with pytest.raises(CalibrationError):
        calibrate(empty, world)


# --- receipts ---------------------------------------------------------------


def test_receipt_chain_links_and_verifies(tmp_path: Path):
    log = tmp_path / "receipts.jsonl"
    append(log, {"kind": "run", "exit_code": 0})
    append(log, {"kind": "run", "exit_code": 2})
    ok, reason = verify_chain(log)
    assert ok, reason
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert lines[0]["prev_hash"] == GENESIS
    assert lines[1]["prev_hash"] == lines[0]["hash"]


def test_tampered_receipt_is_detected(tmp_path: Path):
    log = tmp_path / "receipts.jsonl"
    append(log, {"kind": "run", "exit_code": 0})
    append(log, {"kind": "run", "exit_code": 2})
    # Rewrite a past verdict from 0 to 3 -- the classic "quietly make a failure
    # look like a different failure". The chain must break.
    lines = log.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["exit_code"] = 3
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    log.write_text("\n".join(lines) + "\n")
    ok, reason = verify_chain(log)
    assert not ok
    assert "altered" in reason


# --- CLI --------------------------------------------------------------------


def _cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "truthgate.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PATH": "/usr/bin:/bin:/usr/local/bin", "NO_COLOR": "1"},
    )


def test_cli_version_and_help():
    r = _cli(["--version"], ".")
    assert r.returncode == 0 and "truthgate" in r.stdout
    h = _cli(["--help"], ".")
    assert h.returncode == 0 and "verify" in h.stdout and "calibrate" in h.stdout


def test_cli_empty_spec_is_refused(tmp_path: Path):
    (tmp_path / "empty.yaml").write_text("checks: []\n")
    r = _cli(["verify", "empty.yaml"], tmp_path)
    assert r.returncode == 1
    assert "proves nothing" in r.stderr


def test_cli_mixed_exit_codes(tmp_path: Path):
    (tmp_path / "present.txt").write_text("hi\n")
    (tmp_path / "s.yaml").write_text(
        "checks:\n"
        "  - name: ok\n"
        "    type: file_exists\n"
        "    path: present.txt\n"
        "  - name: nope\n"
        "    type: file_exists\n"
        "    path: absent.txt\n"
    )
    r = _cli(["verify", "s.yaml"], tmp_path)
    # A file_exists check aimed at a path that never exists reports FAILED in
    # the real world and FAILED in an empty world: it cannot discriminate, so
    # the constant-true detector is right to flag it and the run exits 4, not
    # 2. (Asserting "this path must not exist" is a different, legitimate
    # check -- it needs a file_not_exists type, which truthgate does not have
    # yet; flagging it is the honest answer, not a bug.)
    assert r.returncode == 4
    assert "FAIL_CONSTANT" in r.stdout


# ===========================================================================
# CLI-level regressions from the adversarial audit.
# ===========================================================================


def test_cli_all_checks_disabled_is_refused(tmp_path: Path):
    """`enabled: false` on every check used to walk straight through the
    empty-gate guard, which tested the raw list, and print
    "PASS every check verified (0 check(s))" with exit 0."""
    (tmp_path / "s.yaml").write_text(
        "checks:\n"
        "  - name: a\n"
        "    type: file_exists\n"
        "    path: x.txt\n"
        "    enabled: false\n"
        "  - name: b\n"
        "    type: file_exists\n"
        "    path: y.txt\n"
        "    enabled: no\n"
    )
    r = _cli(["verify", "s.yaml"], tmp_path)
    assert r.returncode == 1, f"a gate with nothing enabled must not pass: {r.stdout} {r.stderr}"
    assert "disabled" in r.stderr


def test_cli_duplicate_names_do_not_cross_contaminate(tmp_path: Path):
    """One constant check used to relabel every check sharing its name,
    overwriting a real UNVERIFIED verdict with FAIL_CONSTANT."""
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / "s.yaml").write_text(
        "checks:\n"
        "  - name: same\n"
        "    type: file_contains\n"
        "    path: does_not_exist.txt\n"
        "    contains: x\n"
        "  - name: same\n"
        "    type: file_contains\n"
        "    path: a.txt\n"
        "    regex: \".*\"\n"
    )
    r = _cli(["verify", "s.yaml", "--json", "--no-receipt"], tmp_path)
    payload = json.loads(r.stdout)
    by_detail = {c["detail"]: c["verdict"] for c in payload["checks"]}
    # The missing file must still be reported unverified, not relabelled.
    assert any(v == "unverified" for v in by_detail.values()), payload["checks"]
    assert any(v == "fail_constant" for v in by_detail.values()), payload["checks"]


def test_receipts_missing_file_is_not_a_pass(tmp_path: Path):
    """'No evidence' and 'the evidence is sound' are opposite answers."""
    r = _cli(["receipts", str(tmp_path / "nope.jsonl")], tmp_path)
    assert r.returncode != 0, f"a missing receipt log must not verify: {r.stdout}"


def test_receipts_detects_truncation_with_anchor(tmp_path: Path):
    """Deleting the last (failing) run leaves a chain that still verifies.

    A self-contained hash chain cannot notice this -- it is arithmetic, not
    a bug -- so the head has to be pinned outside the file.
    """
    from truthgate.receipt import append, verify_chain, write_anchor

    log = tmp_path / "receipts.jsonl"
    anchor = tmp_path / "anchor.json"
    append(log, {"kind": "run", "exit_code": 0})
    append(log, {"kind": "run", "exit_code": 2})
    write_anchor(log, anchor)

    assert verify_chain(log)[0] is True
    # Truncate to the last green run and re-sign nothing.
    lines = log.read_text().splitlines()
    log.write_text(lines[0] + "\n")

    # Without external information the chain cannot tell.
    assert verify_chain(log)[0] is True
    # With the anchor it can.
    ok, reason = verify_chain(log, anchor_path=anchor)
    assert not ok
    assert "removed" in reason or "does not match" in reason


def test_receipts_min_lines_catches_truncation(tmp_path: Path):
    from truthgate.receipt import append, verify_chain

    log = tmp_path / "receipts.jsonl"
    append(log, {"kind": "run", "exit_code": 0})
    ok, reason = verify_chain(log, min_lines=3)
    assert not ok and "truncated" in reason


def test_calibrate_exit_code_reflects_a_broken_gate(tmp_path: Path):
    """A gate that passes known-fail samples used to exit 0, handing CI a
    green tick beside a 100% false-positive rate."""
    (tmp_path / "real.txt").write_text("hello\n")
    (tmp_path / "s.json").write_text(
        json.dumps(
            [
                {"check": {"type": "file_contains", "path": "real.txt", "regex": ".*"}, "known_pass": False},
                {"check": {"type": "file_contains", "path": "real.txt", "regex": ".*"}, "known_pass": False},
            ]
        )
    )
    r = _cli(["calibrate", "s.json"], tmp_path)
    assert r.returncode != 0, f"a gate that passes everything must not report success: {r.stdout}"
    assert "UNTRUSTWORTHY" in r.stdout


def test_calibrate_reports_unmeasured_rates_as_na(tmp_path: Path):
    """0.0% from an empty denominator reads as a perfect score."""
    (tmp_path / "real.txt").write_text("hello world\n")
    (tmp_path / "s.json").write_text(
        json.dumps(
            [
                {"check": {"type": "file_contains", "path": "real.txt", "contains": "hello"}, "known_pass": True},
                {"check": {"type": "file_contains", "path": "real.txt", "contains": "world"}, "known_pass": True},
            ]
        )
    )
    r = _cli(["calibrate", "s.json"], tmp_path)
    assert "n/a" in r.stdout, f"an unmeasured rate must not be printed as a measurement: {r.stdout}"
    assert "LOW SAMPLE" in r.stdout


def test_calibrate_flags_a_gate_that_fails_everything(tmp_path: Path):
    """0% accuracy used to exit 0, so CI got a green tick beside a
    false-negative rate of 100%."""
    (tmp_path / "real.txt").write_text("hello\n")
    (tmp_path / "s.json").write_text(
        json.dumps(
            [
                {"check": {"type": "file_contains", "path": "real.txt", "contains": "NOPE1"}, "known_pass": True},
                {"check": {"type": "file_contains", "path": "real.txt", "contains": "NOPE2"}, "known_pass": True},
            ]
        )
    )
    r = _cli(["calibrate", "s.json"], tmp_path)
    assert r.returncode != 0, f"a gate that fails everything must not report success: {r.stdout}"
    assert "UNTRUSTWORTHY" in r.stdout


def test_calibrate_flags_a_set_mostly_made_of_unevaluable_samples(tmp_path: Path):
    """Half the set never ran, yet the remaining numbers were reported as a
    clean measurement and the command still exited 0."""
    (tmp_path / "real.txt").write_text("hello world\n")
    (tmp_path / "s.json").write_text(
        json.dumps(
            [
                {"check": {"type": "file_contains", "path": "real.txt", "contains": "hello"}, "known_pass": True},
                {"check": {"type": "command", "run": "no_such_tool_aaa"}, "known_pass": False},
                {"check": {"type": "command", "run": "no_such_tool_bbb"}, "known_pass": False},
                {"check": {"type": "command", "run": "no_such_tool_ccc"}, "known_pass": False},
            ]
        )
    )
    r = _cli(["calibrate", "s.json"], tmp_path)
    assert r.returncode != 0, f"a mostly-unevaluable set is not a measurement: {r.stdout}"


def test_calibrate_json_stdout_is_pure(tmp_path: Path):
    """The UNTRUSTWORTHY line went to stdout, so `calibrate --json | jq`
    failed to parse -- the same defect that was already fixed for verify."""
    (tmp_path / "real.txt").write_text("hello\n")
    (tmp_path / "s.json").write_text(
        json.dumps(
            [
                {"check": {"type": "file_contains", "path": "real.txt", "regex": ".*"}, "known_pass": False},
                {"check": {"type": "file_contains", "path": "real.txt", "regex": ".*"}, "known_pass": False},
            ]
        )
    )
    r = _cli(["calibrate", "s.json", "--json"], tmp_path)
    payload = json.loads(r.stdout)  # must not raise
    assert payload["verdict"] == "untrustworthy"
