#!/usr/bin/env bash
# Verify that every install instruction in the README is one that works.
#
# Why this exists: v0.2.0's README opened with `pip install truthgate` while
# the package had never been uploaded to PyPI. Every visitor's first command
# failed. The release check at the time only confirmed the wheel *built*,
# which is a different claim from "you can install it".
#
# A README is documentation, and documentation rots silently: nothing fails
# when an instruction stops working, the reader just gives up. So this runs
# the instructions rather than reading them.
#
# rc=0 every instruction in the README works
# rc=2 at least one does not
set -uo pipefail
# This script lives at the repository root, not in a subdirectory.
ROOT="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0

say() { printf '  %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; fails=$((fails + 1)); }

echo "README install-instruction check"

# --- 1. No instruction may reference a package index we have not published to.
if grep -qE '^pip install truthgate' "$ROOT/README.md" "$ROOT/README.zh-CN.md" 2>/dev/null; then
  if curl -fsS -m 10 https://pypi.org/pypi/truthgate/json >/dev/null 2>&1; then
    say "ok: PyPI package exists, the pip instruction is honest"
  else
    fail "README says 'pip install truthgate' but that package is not on PyPI."
    say "     Either upload the dist/, or replace the instruction with a source install."
  fi
fi

# --- 2. No placeholder org/repo URLs.
# `github.com/truthgate/truthgate` was in the README while the repository
# actually lives under the maintainer's account: a URL that 404s on copy-paste.
placeholder=$(grep -ohE 'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+' "$ROOT/README.md" "$ROOT/README.zh-CN.md" 2>/dev/null \
  | sort -u | grep -v 'kevindurant735rocket-creator/truthgate' || true)
if [ -n "$placeholder" ]; then
  fail "README points at a repository that is not this one:"
  printf '%s\n' "$placeholder" | sed 's/^/       /'
else
  say "ok: every repository URL points at the real repository"
fi

# --- 3. The clone-and-run path must actually run.
if git clone -q --depth 1 https://github.com/kevindurant735rocket-creator/truthgate "$TMP/clone" 2>/dev/null; then
  if (cd "$TMP/clone" && python3 -m truthgate.cli --help >/dev/null 2>&1); then
    say "ok: clone + 'python3 -m truthgate.cli --help' works"
  else
    fail "cloning the repository does not give a runnable CLI"
  fi
else
  fail "could not clone the repository (network, or the URL is wrong)"
fi

# --- 4. An installed console script must work with no PYTHONPATH help.
if [ -x "$TMP/clone/.venv/bin/truthgate" ]; then
  say "ok: virtualenv present"
else
  if python3 -m venv "$TMP/venv" 2>/dev/null && "$TMP/venv/bin/python" -m pip install -q "$TMP/clone" 2>/dev/null; then
    if (cd "$TMP" && env -u PYTHONPATH "$TMP/venv/bin/truthgate" --version >/dev/null 2>&1); then
      say "ok: 'pip install .' gives a truthgate command that works in a clean environment"
    else
      fail "installed console script does not run outside the source tree"
    fi
  else
    fail "pip install of the checkout failed"
  fi
fi

# --- 5. No duplicated install section (it happened: two blocks, one of them
# carrying a placeholder URL that nobody noticed).
# `grep -c` exits non-zero when there are zero matches, so under `set -e` this
# would abort; and an empty result is not the number zero -- feeding it to an
# arithmetic test is how a check ends up judging nothing and passing. Read it
# once, validate it, and treat "could not count" as a failure in its own right.
dupes=$(grep -cE '^## Install' "$ROOT/README.md" 2>/dev/null || true)
case "$dupes" in
  ''|*[!0-9]*) fail "could not count the Install sections (got '$dupes')" ;;
esac
if [ "$dupes" = "1" ]; then
  say "ok: exactly one Install section"
else
  fail "README has $dupes '## Install' sections; there should be one"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "INSTALL-CHECK FAILED: $fails problem(s)"
  exit 2
fi
echo "INSTALL-CHECK PASSED"
exit 0
