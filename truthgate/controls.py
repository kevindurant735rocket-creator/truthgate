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

import os
import re
import uuid
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checks import DEFAULT_TIMEOUT, CheckSpecError, evaluate_check
from .verdict import CheckResult, Unverifiable, Verdict

#: How a mutation was constructed, for the receipt.
MUTATE_RENAME = "rename_target_absent"
MUTATE_EMPTY = "empty_the_target"
MUTATE_EXITCODE = "change_expected_exit"
MUTATE_NEGATE_CONTAINS = "negate_contains"
#: The real one: run the same command in a copy of the world with the
#: artefacts taken away.  See :func:`_control_command`.
MUTATE_EMPTY_WORLD = "run_in_emptied_world"

#: A control that re-runs the user's command is bounded well below the check's
#: own timeout.  A `pytest` gate is allowed to take two minutes; its control
#: spending two minutes again would make every verify three times slower for
#: no extra evidence.
CONTROL_TIMEOUT_CAP = 30

#: Commands a control refuses to re-run, because running them a second time
#: could reach outside the sandbox we control: the network, a path we cannot
#: intercept, or an interpreter that was handed the parent environment.  These
#: are reported as inconclusive rather than executed -- pretending to have
#: checked something we did not check is the failure this module exists to
#: prevent, so it applies to itself.
UNSAFE_FOR_CONTROL = re.compile(
    r"""(?xi)
    (?:^|[\s"'=/])          # start of a token, so "echo -e ." does not match
    (?: curl | wget | ssh | scp | rsync | nc | telnet )
    | \bpip\s+install\b
    | \bnpm\s+(?:install|i|ci)\b
    | \bgit\s+-C\b
    | \bsudo\b
    | \brm\s+-rf?\b
    | \bmkfs\b
    | \bdd\s+if=
    """
)

#: An absolute or parent-relative path in ``run`` points the command straight
#: back at the real project (or anywhere else we did not empty), which would
#: make the mutated world a sham: the command would read the very files the
#: control removed.  Flags the shape; :func:`_escapes_sandbox` decides.
_ABSOLUTE_PATH = re.compile(
    r"""(?xi)
    (?:^|[\s"'=(])            # token start
    (?: ~ | /[^\s/] )         # ~/... or /...
    | \.\.[/\\]               # ../ or ..\
    """
)


def _escapes_sandbox(run: str) -> str | None:
    """Return why ``run`` points outside the control sandbox, or None.

    Best-effort and deliberately narrow, because a false accusation here is
    expensive: refusing a control means a vacuous check ships unchallenged.
    So the rule is *only* about paths -- an absolute path or a ``..`` climbs
    straight out of the emptied tree and would read the very files the
    control removed, which makes the whole comparison a sham.

    Flags are deliberately not grounds for refusal.  ``grep -q pattern
    file`` and ``python3 -c "pass"`` are full of them, and ``python3 -c
    "pass"`` is in fact one of the vacuous checks we most want to catch.
    Command *content* cannot be judged from the command line; anything that
    reaches further is handled by :data:`UNSAFE_FOR_CONTROL`, and whatever
    still slips through is caught empirically by the emptied-world run.
    """
    import shlex

    try:
        tokens = shlex.split(run)
    except ValueError:
        return "the command could not be parsed"
    # argv[0] is exempt: a relative program path (`./run_tests.sh`) is exactly
    # what the emptied world is there to test, and it resolves inside the
    # sandbox. What matters is the *arguments*, which may point anywhere.
    for token in tokens[1:]:
        if _ABSOLUTE_PATH.search(token):
            return f"the command references a path outside the sandbox ({token!r})"
    return None


@dataclass
class ConstantFinding:
    """A check that survived its own negative control."""

    name: str
    strategy: str
    detail: str
    original_verdict: str
    mutated_verdict: str
    #: Position in the list the controls were run over.  Carried explicitly
    #: because the findings list is *shorter* than the checks list -- skipped
    #: and inconclusive checks contribute no finding -- so matching a finding
    #: back to its result by position silently relabels the wrong check, and
    #: matching by name relabels every check that happens to share one.
    index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "strategy": self.strategy,
            "detail": self.detail,
            "original_verdict": self.original_verdict,
            "mutated_verdict": self.mutated_verdict,
            "index": self.index,
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


#: Never copy these into a control world: they are the machinery of the
#: project's own tooling and their presence would let a command pass by
#: reading a stale result instead of doing the work.
_SKIP_NAMES = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".venv", "venv", "node_modules", ".tox", ".eggs",
    "dist", "build", ".truthgate", ".DS_Store",
}


def _copy_world(root: Path, dest: Path) -> None:
    """Copy a project into ``dest``, skipping VCS metadata and caches.

    A full ``copytree`` of a large repository is both slow and pointless: the
    control only needs the files the command might read.  Skipping the noise
    keeps a control roughly as cheap as the check it guards.
    """
    def _ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _SKIP_NAMES}

    shutil.copytree(root, dest, ignore=_ignore, symlinks=False, dirs_exist_ok=True)


def _emptied_copy(root: Path, workdir: Path) -> Path:
    """A copy of the project with the files a check would assert on removed.

    The mutation has to break the assertion, so it cannot be "run somewhere
    else" on its own: a command that reads ``src/app.py`` has to find that
    file *gone*.  We therefore copy the tree and then delete every tracked
    source file, leaving the directory structure and any interpreter/tooling
    on PATH intact.  A command that genuinely inspects the project will fail;
    a command that never looks at the project (``echo``, ``true``) will not
    notice, which is exactly the signal we are after.
    """
    world = workdir / "world"
    world.mkdir(parents=True, exist_ok=True)
    try:
        _copy_world(root, world)
    except (OSError, shutil.Error) as exc:
        # A single FIFO, socket or device node makes copytree abort, and the
        # half-built directory it leaves behind is an *empty* world -- which
        # every command then "passes in", so one stray build artefact would
        # turn the whole gate red with a false accusation.  There is no
        # meaningful world to build here, so say so instead of proceeding
        # with a sham.
        raise _ControlUnavailable(
            f"the project could not be copied into a control world ({exc})"
        )
    for path in sorted(world.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            continue
        rel = path.relative_to(world)
        if any(part in _SKIP_NAMES for part in rel.parts):
            continue
        # Keep the programs, remove the data.  A relative script (`./x.sh`)
        # is the command; deleting it would make the control unrunnable and
        # quietly inconclusive, which is the one outcome worse than a false
        # alarm.  What the script *reads* is the world we are emptying.
        if _is_executable_program(path):
            continue
        try:
            path.unlink()
        except OSError:
            pass
    return world


class _ControlUnavailable(Exception):
    """The control world could not be built, so no control can be run."""


def _is_executable_program(path: Path) -> bool:
    """Whether a copied file is a runnable program rather than data.

    Shebang scripts and files carrying the executable bit qualify.  Importable
    modules and libraries are data: a check that genuinely needs them should
    fail when they are gone, which is precisely the signal we are looking for.
    """
    if path.suffix in (".sh", ".bash", ".zsh", ".py", ".rb", ".pl", ".js"):
        first = b""
        try:
            with path.open("rb") as handle:
                first = handle.read(2)
        except OSError:
            return False
        if first == b"#!":
            return True
    try:
        return bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def _clean_env(root: Path) -> dict[str, str]:
    """A minimal environment for the control run.

    The real evaluation inherits everything; a control must not, or the two
    runs are not comparable -- ``PYTHONPATH`` pointing back at the real
    project, or ``HOME`` reaching a real cache, would let a command find the
    artefacts we just deleted.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "TMPDIR")
    }
    env["HOME"] = str(root)
    env["TMPDIR"] = str(root)
    env["GIT_CEILING_DIRECTORIES"] = str(root)
    # Neutralise the two variables most likely to hand the command a path
    # back to the world we just emptied.
    for leaky in ("PYTHONPATH", "VIRTUAL_ENV", "GIT_DIR", "GIT_WORK_TREE", "NODE_PATH"):
        env.pop(leaky, None)
    return env


def _command_runs(check: dict[str, Any], root: Path) -> CheckResult:
    return evaluate_check(check, root)


def _control_command(check: dict[str, Any], root: Path) -> tuple[ControlResult, CheckResult | None]:
    """Control for ``command`` checks: run the *same* command in an emptied world.

    The check must not be rewritten -- not its ``run``, not its
    ``expect_exit``.  The world is what moves.  We copy the project, delete
    every file in the copy, and run the byte-identical check there.  A command
    that inspects the project (``pytest``, ``make``, ``git diff --exit-code``,
    a script that imports the package) will notice the absence and answer
    differently.  A command that never looks at the world at all -- ``echo``,
    ``true``, ``sleep 0``, ``python3 -c "pass"`` -- will answer exactly as
    before, and that is what "constant-true" means.

    Earlier revisions of this control substituted a stand-in command with a
    different exit status.  That was wrong in a way worth recording: the
    stand-in's status was chosen to differ from the real one, the check
    compares statuses, so the two verdicts could never agree and the control
    returned "caught" unconditionally.  It was a tautology -- every vacuous
    command check sailed through while the suite stayed green, because the
    only command tests we had asserted the *opposite* property.

    The signal is graded, because "same answer twice" means two different
    things depending on the answer:

    * both VERIFIED -- a vacuous pass. The check certified work it never
      looked at.  This is FAIL_CONSTANT, the strongest signal.
    * both FAILED -- weak.  The check may simply be wrong, or may depend on
      something the emptied world also removed.  Calling that constant-true
      would condemn honest checks, so it is reported inconclusive.
    * either UNVERIFIED -- we could not evaluate one side, so we could not
      prove anything either.  Also inconclusive.

    Anything that would reach outside the sandbox is refused rather than run;
    see :data:`UNSAFE_FOR_CONTROL`.
    """
    name = str(check.get("name", "<unnamed>"))
    run = check.get("run")
    if not isinstance(run, str) or not run.strip():
        return ControlResult(name, "skipped", MUTATE_EMPTY_WORLD, "no run field"), None

    original = _command_runs(check, root)
    if original.verdict is Verdict.UNVERIFIED:
        return (
            ControlResult(
                name,
                "inconclusive",
                MUTATE_EMPTY_WORLD,
                f"original check is UNVERIFIED ({original.detail}); cannot build a control",
            ),
            original,
        )

    # A check that already fails is not evidence of anything: we have no
    # passing behaviour to compare against, and reporting it constant-true
    # would fire on every honest failing check.
    if original.verdict is Verdict.FAILED:
        return (
            ControlResult(
                name,
                "inconclusive",
                MUTATE_EMPTY_WORLD,
                "check already fails in the real world; nothing to discriminate against",
            ),
            original,
        )

    # No shortcut on "the command printed nothing".  A silent success is
    # what `test -s file`, `grep -q pattern file` and most well-behaved
    # checkers do *by design*; treating silence as evidence of a vacuous
    # check condemns the healthiest checks in any real spec.  Output volume
    # is not a proxy for whether a command looked at anything -- the emptied
    # world below is the only signal that actually distinguishes the two,
    # and it costs one subprocess.
    if UNSAFE_FOR_CONTROL.search(run):
        return (
            ControlResult(
                name,
                "inconclusive",
                MUTATE_EMPTY_WORLD,
                (
                    "not re-run: this command reaches outside the sandbox (network, absolute paths, "
                    "or a parent shell), so an emptied world would not be a fair test"
                ),
            ),
            original,
        )

    escape = _escapes_sandbox(run)
    if escape:
        return (
            ControlResult(
                name,
                "inconclusive",
                MUTATE_EMPTY_WORLD,
                f"not re-run: {escape}",
            ),
            original,
        )

    workdir = _temp_world()
    try:
        try:
            world = _emptied_copy(root, workdir)
        except _ControlUnavailable as exc:
            return (
                ControlResult(
                    name,
                    "inconclusive",
                    MUTATE_EMPTY_WORLD,
                    f"no control could be built: {exc}",
                ),
                original,
            )
        # Bound the control well below the check's own timeout so a slow
        # command does not triple the cost of every verify.
        control_check = dict(check)
        control_check["timeout"] = min(int(check.get("timeout", DEFAULT_TIMEOUT)), CONTROL_TIMEOUT_CAP)
        mutated = _run_command_in(check, world, control_check["timeout"])
        if mutated is Verdict.UNVERIFIED:
            return (
                ControlResult(
                    name,
                    "inconclusive",
                    MUTATE_EMPTY_WORLD,
                    "the emptied world could not be evaluated, so constancy is unproven",
                    mutated.value,
                ),
                original,
            )
        if mutated is Verdict.VERIFIED:
            return (
                ControlResult(
                    name,
                    "survived",
                    MUTATE_EMPTY_WORLD,
                    (
                        "command passed with the entire project deleted; it does not read the work "
                        "it is supposed to be checking"
                    ),
                    mutated.value,
                ),
                original,
            )
        return (
            ControlResult(
                name,
                "caught",
                MUTATE_EMPTY_WORLD,
                (
                    "check discriminates: verified with the project present vs failed with it removed"
                ),
                mutated.value,
            ),
            original,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_command_in(check: dict[str, Any], world: Path, timeout: int) -> Verdict:
    """Evaluate a ``command`` check with its process rooted at ``world``.

    The verdict logic is re-implemented here rather than borrowed from
    :class:`CommandCheck` so the control can substitute a scrubbed environment
    and a bounded timeout without teaching the main evaluator about controls.
    """
    import shlex
    import subprocess

    argv = shlex.split(str(check.get("run", "")))
    if not argv:
        return Verdict.UNVERIFIED
    # Resolve exactly the way the real evaluator does.  Using shutil.which
    # alone here looked stricter than the evaluator and was the difference
    # between a verdict and no verdict: `which("./run_tests.sh")` is always
    # None, so every project-relative script -- the single most common way a
    # real spec invokes a check -- came back UNVERIFIED and skipped detection
    # entirely.  An inconclusive control is not a safe default; it is an
    # unchecked check.
    program = argv[0]
    resolved = shutil.which(program)
    if resolved is None and os.sep in program:
        candidate = world / program
        if candidate.exists():
            resolved = str(candidate)
    if resolved is None:
        return Verdict.UNVERIFIED
    try:
        proc = subprocess.run(
            argv,
            cwd=str(world),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=_clean_env(world),
        )
    except (subprocess.TimeoutExpired, OSError):
        return Verdict.UNVERIFIED
    except UnicodeDecodeError:  # pragma: no cover - defensive; errors=replace
        return Verdict.UNVERIFIED
    return Verdict.VERIFIED if proc.returncode == check.get("expect_exit", 0) else Verdict.FAILED


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


def _control_marker() -> str:
    """A long token a real file is very unlikely to contain already.

    Used to perturb a control world for regex assertions, whose counterexample
    cannot be synthesised from the pattern itself.
    """
    return "truthgate-control-" + uuid.uuid4().hex


def _looks_negated(pattern: str) -> bool:
    """Whether a regex is shaped like a negative assertion.

    A conservative structural guess, used only to *decline* a verdict: when a
    pattern carries a negative lookahead/lookbehind its truth depends on what
    is absent from the text, and no amount of adding unrelated content will
    make it fail.  Erring towards "maybe negative" costs an inconclusive
    report; erring the other way convicts a good check.
    """
    return bool(re.search(r"\(\?!|\(\?<!", pattern))


def _control_file_contains(check: dict[str, Any], root: Path) -> tuple[ControlResult, CheckResult | None]:
    """Control for ``file_contains``: can the check tell two texts apart?

    Constant-ness means "the same answer whatever the file contains", so the
    question is whether the assertion is *sensitive to content at all*.  We
    build three worlds and read the check's answers:

        ``seeded``  the real file, plus a marker that satisfies most literals
        ``empty``   a file with nothing in it
        ``foreign`` text unrelated to anything the check mentions

    A content-sensitive check gives at least two different answers across
    those worlds; one that holds for all of them is constant-true.  This
    deliberately does not try to guess whether the assertion is positive
    ("must contain X") or inverted ("must not contain X").  Guessing was the
    previous bug: a fixed "satisfied" body that contained the needle happens
    to satisfy an inverted assertion too, so the two worlds agreed and a
    perfectly good `must not contain TODO` check was reported vacuous.
    Deriving the worlds from the file rather than from the needle covers both
    directions without having to tell them apart.

    The check itself is never rewritten.  Rewriting the needle tests a
    different assertion than the user wrote.
    """
    raw = check.get("path")
    if not isinstance(raw, str) or not raw:
        return ControlResult(check.get("name", "<unnamed>"), "skipped", MUTATE_NEGATE_CONTAINS, "no path field"), None
    original = evaluate_check(check, root)
    name = str(check.get("name", "<unnamed>"))
    needle = check.get("contains")
    regex = check.get("regex")
    if needle is None and regex is None:
        return ControlResult(name, "skipped", MUTATE_NEGATE_CONTAINS, "no contains/regex field"), None
    if original.verdict is Verdict.UNVERIFIED:
        return (
            ControlResult(
                name,
                "inconclusive",
                MUTATE_NEGATE_CONTAINS,
                f"original check is UNVERIFIED ({original.detail})",
            ),
            original,
        )
    if original.verdict is Verdict.FAILED:
        # Nothing passing to compare against; firing here would condemn every
        # honest failing check.
        return (
            ControlResult(
                name,
                "inconclusive",
                MUTATE_NEGATE_CONTAINS,
                "check already fails on the real file; there is no passing behaviour to compare",
            ),
            original,
        )

    src_real = root / raw
    real_body = ""
    if src_real.is_file():
        try:
            real_body = src_real.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ControlResult(name, "inconclusive", MUTATE_NEGATE_CONTAINS, f"cannot read {raw}: {exc}"), original

    # Two ordinary-looking texts, neither of which is degenerate.  An earlier
    # revision used an empty file and a "\x00\x01\x02" blob as the control
    # worlds, which quietly excused a whole family of vacuous assertions:
    # `^.` and `\S` are constant-true on any non-empty file, but they fail on
    # an empty one, so the "empty" world made them look discriminating.  Both
    # worlds here are plausible file contents, which is the bar a real
    # assertion has to clear.
    #
    # The seeded world has to contain something the assertion could be
    # anchored on, otherwise a check written as a *negative* assertion
    # ("this file must not contain TODO", expressed as a lookahead regex)
    # is satisfied by both worlds and looks vacuous.  Putting the needle in
    # the seeded world makes it a genuine counterexample for the negative
    # form while leaving the positive form discriminating as before.
    if isinstance(needle, str) and needle:
        seeded_body = f"truthgate-control-marker\n{needle}\n{real_body}" if real_body else f"truthgate-control-marker\n{needle}\n"
    elif isinstance(regex, str) and regex:
        # An arbitrary pattern cannot be inverted into matching text, so the
        # counterexample has to come from the file itself.  We seed with a
        # long random marker that the real file does not contain: if a regex
        # assertion still answers identically, it is holding for both, which
        # is the evidence we need.  A *negative* assertion ("must not contain
        # TODO") is the known limit here -- it is satisfied by both worlds
        # and is reported inconclusive below rather than wrongly convicted.
        if _looks_negated(regex):
            # A negative assertion -- "must not contain TODO" -- is satisfied
            # by every text that lacks the marker, so both control worlds
            # agree and the check would be convicted of being vacuous when it
            # is in fact discriminating.  Recognising the shape is enough to
            # *decline to judge*: we can neither build a counterexample nor
            # prove one, so the honest answer is that constancy is unproven.
            return (
                ControlResult(
                    name,
                    "inconclusive",
                    MUTATE_NEGATE_CONTAINS,
                    (
                        "regex looks like a negative assertion (it uses a lookahead/lookbehind); "
                        "such a check is satisfied by any text lacking the marker, so it cannot be "
                        "distinguished from a vacuous one by mutation alone"
                    ),
                ),
                original,
            )
        seeded_body = (
            f"{_control_marker()}\n{real_body}" if real_body else f"{_control_marker()}\n"
        )
    else:
        seeded_body = "truthgate-control-marker\n"
    foreign_body = "a plain line of ordinary text\nwith nothing special in it\n"

    mutant = _temp_world()
    try:
        worlds = {"seeded": seeded_body, "foreign": foreign_body}
        answers: dict[str, Verdict] = {}
        for label, body in worlds.items():
            target = mutant / f"{label}.txt"
            target.write_text(body, encoding="utf-8")
            spec = dict(check)
            spec["path"] = target.name
            answers[label] = evaluate_check(spec, mutant).verdict

        if any(v is Verdict.UNVERIFIED for v in answers.values()):
            return (
                ControlResult(
                    name,
                    "inconclusive",
                    MUTATE_NEGATE_CONTAINS,
                    "one of the control worlds could not be evaluated",
                    next(v for v in answers.values() if v is Verdict.UNVERIFIED).value,
                ),
                original,
            )

        distinct = {v.value for v in answers.values()}
        if len(distinct) == 1 and answers["seeded"] is Verdict.VERIFIED:
            return (
                ControlResult(
                    name,
                    "survived",
                    MUTATE_NEGATE_CONTAINS,
                    (
                        "check gave the same answer for two unrelated ordinary texts "
                        f"({answers['seeded'].value}); the assertion holds for any content"
                    ),
                    answers["foreign"].value,
                ),
                original,
            )
        return (
            ControlResult(
                name,
                "caught",
                MUTATE_NEGATE_CONTAINS,
                (
                    f"check discriminates by content: seeded={answers['seeded'].value}, "
                    f"unrelated={answers['foreign'].value}"
                ),
                answers["foreign"].value,
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
    for index, check in enumerate(checks):
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
                    index=index,
                )
            )
    return findings, results


__all__ = [
    "ConstantFinding",
    "ControlResult",
    "find_constant_checks",
    "run_control",
]
