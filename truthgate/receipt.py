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

import fcntl
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
    # The read-modify-write below is a lost update waiting to happen: two
    # processes both read N lines, both chain onto line N, and the second
    # os.replace silently discards the first one's line.  The chain would
    # still verify -- it is internally consistent -- so the loss would be
    # invisible while the caller had already been told "receipt appended".
    # An exclusive lock makes the read and the replace one critical section.
    lock_path = receipt_path.with_name(receipt_path.name + ".lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            prev = last_hash(receipt_path)
            entry = dict(record)
            entry.setdefault("timestamp", _utc_now())
            entry["prev_hash"] = prev
            entry["hash"] = _chain_hash(prev, entry)
            line = _canonical(entry) + "\n"

            existing = receipt_path.read_text(encoding="utf-8") if receipt_path.is_file() else ""
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(receipt_path.parent), prefix=".receipt-", suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                    handle.write(existing)
                    handle.write(line)
                    handle.flush()
                    # Without this the bytes can still be in the page cache
                    # when the rename lands, and a crash loses a receipt that
                    # was already reported as written.
                    os.fsync(handle.fileno())
                os.replace(tmp_name, receipt_path)
                _fsync_dir(receipt_path.parent)
            except BaseException:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return entry


def _fsync_dir(directory: Path) -> None:
    """Persist a directory entry so a rename survives a crash."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - not every platform allows this
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


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


def write_anchor(receipt_path: Path, anchor_path: Path) -> dict[str, Any]:
    """Record the chain's current head somewhere else.

    A hash chain can prove nothing was edited *in the middle*: every line
    still links.  It cannot, on its own, prove nothing was removed from the
    *end* -- delete the last line and the remaining prefix is a perfectly
    valid chain.  That is not a weakness of SHA-256; it is arithmetic, and no
    amount of hashing inside the file fixes it, because the file is the only
    thing being checked.

    So the head has to be pinned somewhere the chain itself cannot reach.
    Writing ``{count, head_hash}`` to a second file gives the check something
    to compare against.  Keep that file out of the repository, or make it
    append-only in CI, and truncating the log becomes detectable.
    """
    prev, count = GENESIS, 0
    if receipt_path.is_file():
        for line in receipt_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                break
            prev = entry.get("hash", prev)
            count += 1
    anchor = {
        "count": count,
        "head_hash": prev,
        "written_at": _utc_now(),
        "receipt": str(receipt_path),
    }
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    anchor_path.write_text(json.dumps(anchor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return anchor


def verify_chain(
    receipt_path: Path,
    *,
    min_lines: int | None = None,
    anchor_path: Path | None = None,
) -> tuple[bool, str]:
    """Re-walk the chain; return (ok, human-readable reason).

    ``min_lines`` and ``anchor_path`` are the only ways to notice truncation
    at the tail.  Without them this function can say "every line that is
    present is intact" and nothing more -- so a log whose last run failed can
    be shortened to its last green run and still verify clean.

    A missing file is a *failure*, not a pass.  "No evidence" and "the
    evidence is sound" are opposite answers, and returning True for the first
    one let an empty log certify itself.
    """
    if not receipt_path.is_file():
        return False, "no receipt file: there is no evidence to verify"
    if min_lines is not None and receipt_path.stat().st_size == 0:
        if min_lines > 0:
            return False, f"receipt file is empty but at least {min_lines} line(s) were expected"
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
    if count == 0:
        return False, "receipt file contains no entries: there is no evidence to verify"
    if min_lines is not None and count < min_lines:
        return False, (
            f"chain is truncated: {count} line(s) present but at least {min_lines} expected; "
            f"a tail deletion is invisible to a self-contained chain"
        )
    if anchor_path is not None:
        if not anchor_path.is_file():
            return False, f"anchor file not found: {anchor_path}"
        try:
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"anchor file is not valid JSON: {exc}"
        if anchor.get("count") != count:
            return False, (
                f"chain length {count} does not match the anchored length {anchor.get('count')}; "
                f"entries have been removed since the anchor was written"
            )
        if anchor.get("head_hash") != prev:
            return False, "chain head does not match the anchored head; the log has been rewritten"
    return True, f"{count} receipt line(s) verified"


__all__ = ["GENESIS", "append", "append_run", "last_hash", "verify_chain", "write_anchor"]
