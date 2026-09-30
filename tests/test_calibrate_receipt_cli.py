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
