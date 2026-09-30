"""Evidence receipts: an append-only, hash-chained record of each gate run.

A verdict you cannot look up later is an opinion.  Every truthgate run writes
one JSON object per line to a receipt file, and each line carries the SHA-256
of the previous line.  That makes the log tamper-evident: if someone edits or
removes a past verdict, the chain from that point forward no longer verifies.

This is not a signature scheme and does not pretend to be.  It proves the log
has not been silently rewritten *after the fact*; it cannot stop a determined
editor from rewriting the whole chain.  The threat model is "an agent or a
careless edit quietly changes what was recorded", not an adversary with write
access to your repository.  Stating that limit here is the point -- a receipt
system that oversells its guarantees is worse than none.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain_hash(prev: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_hash(receipt_path: Path) -> str:
    """Hash of the final line, or GENESIS when there is no log yet."""
    if not receipt_path.is_file():
        return GENESIS
    last = GENESIS
    for line in receipt_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            # The next line must chain to THIS line's hash, not to its
            # prev_hash (which is what the line before chained to). Using
            # prev_hash here silently breaks the chain on the second write.
            last = json.loads(line).get("hash", last)
        except json.JSONDecodeError:
            continue
    return last


def append(receipt_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one receipt line, chaining it to the previous one.

    The file is written atomically: a half-written receipt would itself be a
    corrupted evidence trail, so we write a temp file in the same directory
    and rename over the target.
    """
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    prev = last_hash(receipt_path)
    entry = dict(record)
    entry.setdefault("timestamp", _utc_now())
    entry["prev_hash"] = prev
    entry["hash"] = _chain_hash(prev, entry)
    line = _canonical(entry) + "\n"

    existing = receipt_path.read_text(encoding="utf-8") if receipt_path.is_file() else ""
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(receipt_path.parent), prefix=".receipt-", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(existing)
            handle.write(line)
        os.replace(tmp_name, receipt_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return entry


def append_run(
    receipt_path: Path,
    spec_path: Path,
    exit_code: int,
    results: Iterable[dict[str, Any]],
    controls: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Record a whole gate run as one receipt line plus per-check detail."""
    results_list = list(results)
    controls_list = list(controls)
    return append(
        receipt_path,
        {
            "kind": "run",
            "spec": str(spec_path),
            "exit_code": int(exit_code),
            "checks": results_list,
            "constant_findings": controls_list,
        },
    )


def verify_chain(receipt_path: Path) -> tuple[bool, str]:
    """Re-walk the chain; return (ok, human-readable reason)."""
    if not receipt_path.is_file():
        return True, "no receipt file yet"
    prev = GENESIS
    count = 0
    for lineno, line in enumerate(receipt_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            return False, f"line {lineno} is not valid JSON: {exc}"
        if entry.get("prev_hash") != prev:
            return False, f"line {lineno} does not chain to the previous line"
        body = {k: v for k, v in entry.items() if k != "hash"}
        expected = _chain_hash(prev, body)
        if entry.get("hash") != expected:
            return False, f"line {lineno} has been altered since it was written"
        prev = entry["hash"]
        count += 1
    return True, f"{count} receipt line(s) verified"


__all__ = ["GENESIS", "append", "append_run", "last_hash", "verify_chain"]
