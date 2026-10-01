# truthgate

**A gate that proves its own checks can fail — and reports its own false-positive rate.**

Most verification tools tell you *whether* a task passed. Almost none tell you *how often they are
wrong*. truthgate does both: it runs each check against a world that should break it (catching
constant-true checks that pass no matter what), and it can score the gate itself against known
outcomes.

```
$ truthgate calibrate samples.json
Brier score       : 0.4000   (0 = perfect, 0.25 = always guess 0.5)  n=5
ECE               : 0.4000   (expected calibration error, 10 bins)
False positive    : 100.0%   (100.0% of known-fail samples were passed anyway)
Constant checks   : 5/5 (100.0%) cannot discriminate
```

That output is a real run: every check was `regex: ".*"`, which matches any file, so the gate said
"verified" to a sample it should have failed. A conventional gate would have reported 5/5 passing.
truthgate reports the gate as worthless — because it is.

**Zero dependencies. Python 3.9+. Nothing to install but the tool.**

---

## Why this exists

The AI-coding-tooling boom produced ~187,000 stars of evaluation and observability tooling, and
almost all of it scores a model with another model. The projects that tried to *fix* the scorer
total **608 stars across 9 repos**. Runtime provenance and audit tooling — the layer that would
prove *what actually happened* — totals **56 stars across 10 repos**. The most-starred tool in the
"deterministic verification" niche has **403 stars**.

The demand is written up constantly. The supply is nearly empty. truthgate is the part of that gap
that a small deterministic tool can honestly fill.

Concretely, three things it refuses to let a gate do:

| What a gate normally does | What truthgate does instead |
|---|---|
| Reports `pass` when a check silently did nothing | Derives a negative control per check; a check that can't fail is `FAIL_CONSTANT` (exit 4) |
| Folds "couldn't run it" into pass or fail | Three states — `unverified` is its own answer, exit 3, never 0 |
| Emits a boolean nobody has measured | `calibrate` reports Brier, ECE, false-positive rate, and constant rate |

## Install

```bash
pip install truthgate
```

From source (no dependencies either way):

```bash
git clone https://github.com/truthgate/truthgate && cd truthgate
python3 -m truthgate.cli --help
```

## Use

Write a check spec — plain YAML:

```yaml
# truthgate.yaml
checks:
  - name: unit tests pass
    type: command
    run: python3 -m pytest -q
    expect_exit: 0

  - name: package metadata present
    type: file_exists
    path: pyproject.toml
    non_empty: true

  - name: README documents the exit codes
    type: file_contains
    path: README.md
    contains: "exit codes"
```

Run it:

```bash
truthgate verify truthgate.yaml
```

```
truthgate 0.1.0  spec=truthgate.yaml  (3 check(s))
  verified       unit tests pass
  verified       package metadata present
  verified       README documents the exit codes

receipt appended: .truthgate/receipts.jsonl

PASS  every check verified (3 check(s))
```

### Check types

| `type` | Fields | Verified when |
|---|---|---|
| `command` | `run`, `expect_exit` (default `0`), `timeout` | the command's exit code equals `expect_exit` |
| `file_exists` | `path`, `non_empty` (optional) | the path exists (and is non-empty, if asked) |
| `file_contains` | `path` + `contains` **or** `regex` | the file's text contains the literal / matches the regex |

Every check also accepts `name` and `enabled: false`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | every check verified |
| `1` | usage or internal error (e.g. the spec declares zero checks) |
| `2` | a check failed |
| `3` | a check could not be evaluated — **not** a pass |
| `4` | a constant-true check was caught |

Drop it into CI as-is:

```yaml
- run: truthgate verify truthgate.yaml
```

## The three states

`unverified` exists because the alternative is worse. A check whose tool is missing, whose file
can't be read, or whose command times out has told you *nothing* about the work. Reporting it as
`verified` manufactures evidence; reporting it as `failed` trains you to ignore real failures. So it
gets its own state and its own exit code:

```bash
$ truthgate verify
  unverified     integration suite
                  command not found on PATH: 'pytest-integration'
...
UNVERIFIED  1 check(s) could not be evaluated. Exit 3 means 'not proven', which is not the same as passing.
```

An empty spec is refused outright (exit 1) rather than reported as "all verified" — zero checks
proves nothing, and defaulting to green there is the most dangerous thing this tool could do.
So is a spec whose every check is switched off with `enabled: false`; it is the same empty gate
wearing a disguise, and it is rejected the same way.

## Automatic negative controls

A check that always passes is a gate that always passes. The obvious fix — "please write a negative
control" — does not work; the one project that ships one documents why it can't be automated: a
control a user invents is exactly the fake check the tool exists to refuse.

So truthgate doesn't ask. It **derives** the control from the check, by mutating the *world* and
keeping the check byte-identical:

```
$ truthgate verify
  FAIL_CONSTANT  docs look sane
                  check reported failed for both the real file and an unrelated one;
                  the assertion does not depend on the file's contents
...
FAIL_CONSTANT  1 check(s) cannot discriminate; they pass regardless of the work.
```

The `FAIL_CONSTANT` above was a `file_contains` whose needle wasn't in the file — it fails in the
real world *and* in a world with unrelated content. A check with no dependency on what it claims
to measure is reporting a fact about nothing.

A `regex: ".*"` check — the single most common vacuous assertion — is caught the same way.

> **An honest limit.** Constant-true detection proves a check *responds to the world*. It cannot
> prove the thing being checked is honest. A check like `run: ./my-wrapper.sh, expect_exit: 0`
> passes its control even if `my-wrapper.sh` lies, because truthgate is verifying the check, not
> the work. Deeper semantic verification is a different tool. This one tells you which of your
> checks are load-bearing.

### What the controls refuse to judge

A control that cannot be built is reported **inconclusive**, never a pass. Concretely:

| Situation | Why | Result |
|---|---|---|
| The check already fails in the real world | There is no passing behaviour to compare against | `inconclusive` — firing here would condemn every honest failing check |
| The command reaches the network, uses `sh -c`, or an absolute path | A sandbox cannot isolate those, so an emptied world would be a sham | `inconclusive`, and the command is **not re-run** |
| A `regex:` assertion is shaped like a negative one (`(?!`, `(?<!`) | "must not contain TODO" is satisfied by any text lacking TODO, so no counterexample exists | `inconclusive` rather than a false accusation |
| One of the control worlds cannot be evaluated | We could not prove constancy either way | `inconclusive` |

Only a control that can be *proven* to survive turns into `FAIL_CONSTANT`. The sound, if
unexciting, position is that a check whose discrimination cannot be demonstrated has not been
shown to be worthless.

## Calibrating the gate

Give it samples with known outcomes and it scores itself:

```json
[
  {"check": {"type": "file_contains", "path": "pyproject.toml", "contains": "name = "}, "known_pass": true},
  {"check": {"type": "file_exists", "path": "README.md"}, "known_pass": false}
]
```

```bash
truthgate calibrate samples.json            # paths resolve next to samples.json
truthgate calibrate examples/samples.json --root .   # paths resolve against the repo root
truthgate calibrate samples.json --json     # machine-readable
```

It exits non-zero when the measured gate should not be used to block anything, so a CI job
running `truthgate calibrate` cannot come back green next to a 100% false-positive rate.

Numbers that were *not measured* say so. A rate with no samples in its denominator prints
`n/a`, not `0.0%` — a fabricated zero is indistinguishable from a perfect score — and a run with
fewer than three evaluated samples is labelled `LOW SAMPLE`.

It reports the numbers that matter when you are deciding whether to trust a gate:

- **Brier score** — squared error between the gate's confidence and the known outcome.
- **ECE** — how far confidence sits from observed pass rates, in bins.
- **False-positive rate** — how often the gate passed work that should have failed. The number that
  decides whether your gate is safe to block on.
- **False-negative rate** — how often the gate failed work that was actually fine. Loud, but it
  is the number that trains people to ignore failures.
- **Constant rate** — the share of your own checks that cannot fail.

## Receipts

Every run appends a hash-chained line to `.truthgate/receipts.jsonl` (name, exit code, UTC
timestamp, verdict, per-check detail). Each line carries the SHA-256 of the one before it, so an
edit to a past verdict breaks the chain and `truthgate receipts` says so:

```bash
$ truthgate receipts
.truthgate/receipts.jsonl: line 1 has been altered since it was written
```

This proves the log wasn't quietly rewritten **in the middle**. It cannot, on its own, prove
nothing was removed from the **end** — delete the last line and the remaining prefix is still a
perfectly valid chain. That is arithmetic, not a bug: the file is the only thing being checked,
so nothing inside it can testify about its own missing tail.

So pin the head somewhere else. `truthgate verify --anchor .truthgate/anchor.json` records
`{count, head_hash}`, and `truthgate receipts --anchor ...` then fails on a length or head
mismatch:

```
$ truthgate receipts --anchor .truthgate/anchor.json
chain length 1 does not match the anchored length 2; entries have been removed since the anchor was written
```

`--min-lines N` is the same idea without a second file. Keep the anchor out of the repository, or
make it append-only in CI, and truncating the log becomes detectable. Neither mechanism is a
signature scheme: neither stops someone with write access from rewriting both files.

A missing receipt file is a **failure**, not a pass. "No evidence" and "the evidence is sound"
are opposite answers, and an empty log must not certify itself.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # 75 tests
```

The suite includes **mutation-style self-checks**: real constant-true checks that the detector must
flag, and correct checks it must *not* flag. A control that never fires is worse than no control, so
a detector that can't be fooled by a `regex: ".*"` fails the build.

The command controls were written twice, both times because an independent adversarial pass found
the first version was a tautology. The original substituted a stand-in command whose exit status was
chosen to differ, so the two verdicts could never agree and it reported "caught" unconditionally —
while `echo`, `true` and `sleep 0` sailed through. The second pass then found that `sh -c "exit 0"`
and `./verify.sh` were equivalent escapes. Those regressions are now tests, and the reason the
command control runs the *real* command in a *copy of your project with the data deleted* is
exactly that: swapping the command, the expectation, or the path all turned out to be ways of not
testing anything.

## License

MIT — see [LICENSE](LICENSE).

## 中文说明

见 [README.zh-CN.md](README.zh-CN.md)。核心区别只有一句：**大多数验证工具告诉你任务过没过，
几乎没人告诉你它自己错得多频繁。** truthgate 两件事都做——先用变异世界证明每条判据真的会失败，
再用 `calibrate` 报出这个门自己的假阳性率。
