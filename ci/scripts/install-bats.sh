#!/usr/bin/env bash
# ci/scripts/install-bats.sh – provision the bats runner this repository's
# `tests-shell` lane requires, into the worktree rather than onto the machine.
#
# Why this exists: `tests-shell` is a *blocker*, and correctly so — the suites
# under ci/tests/ are what guard the layout, node and changeset lanes, and a lane
# that reports PASS without running them is worse than no lane. But
# ci/checks/tests-shell.sh refuses with FAIL_INFRA when bats is missing, and
# neither `uv sync` nor `ci/install-hooks.sh` used to put it anywhere. On a fresh
# clone of a supported machine that meant every push touching ci/ was blocked
# with no in-repo way to unblock it.
#
# Installed under .ci-gate/ (already git-ignored) and not globally: no sudo, no
# package manager, nothing outside the worktree, and removing the directory
# undoes it completely. The gate adds that directory to PATH itself when it is
# present, so nothing has to be exported by hand.
#
# Pinned, because an unpinned test runner is the same class of problem as an
# unpinned Vitest: the version that runs the suites decides what "passing" means.
# Change BATS_VERSION deliberately, not incidentally.
set -Eeuo pipefail

BATS_VERSION="${BATS_VERSION:-v1.11.1}"
BATS_REPO="${BATS_REPO:-https://github.com/bats-core/bats-core.git}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

DEST="${ROOT_DIR}/.ci-gate/bats"
BIN="${DEST}/bin/bats"

# Already provisioned is not already *current*.
#
# This branch used to exit on the file existing, which made BATS_VERSION a
# comment for everyone who had ever run this script: bump the pin, re-run, and
# the stale runner stays. The whole point of pinning is that the version which
# runs the suites decides what "passing" means, and the setup and the handoff
# both describe the worktree copy as pinned -- so a developer would be running
# semantics the repository does not claim while being told otherwise. `FORCE=1`
# could not reach the reinstall either, because it was only consulted further
# down.
#
# Compared against the tag rather than recorded separately: `bats --version`
# prints `Bats <semver>` and BATS_VERSION is a `v`-prefixed tag, so the `v` comes
# off before the comparison. A version that cannot be read at all is treated as a
# mismatch -- reinstalling is cheap and recoverable, and running an unidentified
# runner is the thing this is here to prevent.
REPLACING=0
if [ -x "$BIN" ]; then
  INSTALLED="$("$BIN" --version 2>/dev/null | awk '{ print $2 }')" || INSTALLED=""
  WANT="${BATS_VERSION#v}"
  if [ -n "$INSTALLED" ] && [ "$INSTALLED" = "$WANT" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "bats ${INSTALLED} is already provisioned at ${BIN}"
    exit 0
  fi
  REPLACING=1
  if [ -z "$INSTALLED" ]; then
    echo "The copy at ${BIN} does not report a version; replacing it."
  elif [ "$INSTALLED" != "$WANT" ]; then
    echo "Provisioned bats is ${INSTALLED}, this repository pins ${WANT}; replacing it."
  else
    echo "FORCE=1: reinstalling bats ${WANT}."
  fi
fi

# Guarded on REPLACING, and that guard is the point rather than a tidiness.
# `.ci-gate/bats/bin` is on PATH inside the lane and can be on a developer's, and
# a system-wide bats puts one there too -- so without this, a stale worktree copy
# that the branch above had just decided to replace would find *some* bats here
# and exit 0, leaving the stale one in place. That is the same bug this commit
# fixes, one branch further down.
if [ "$REPLACING" -eq 0 ] && command -v bats >/dev/null 2>&1; then
  echo "bats is already on PATH: $(command -v bats)"
  bats --version || true
  echo "Nothing to do. Provision into .ci-gate/ anyway with:  FORCE=1 $0"
  [ "${FORCE:-0}" = "1" ] || exit 0
fi

command -v git >/dev/null 2>&1 || {
  echo "git is required to fetch bats and is not on PATH." >&2
  exit 1
}

echo "Installing bats ${BATS_VERSION} into ${DEST} ..."
# Templated, because BSD `mktemp` -- which is what macOS ships -- requires a
# template and fails without one. Under `set -e` that aborts here, so the one
# command this repository offers for provisioning bats would fail on the
# platform whose Bash 3.2 the gate is written for.
SRC="$(mktemp -d "${TMPDIR:-/tmp}/ums-bats.XXXXXX")"
trap 'rm -rf "$SRC"' EXIT
# --depth 1 against the tag: this is a tool checkout, not history anyone reads.
git clone --quiet --depth 1 --branch "$BATS_VERSION" "$BATS_REPO" "$SRC/bats-core"
rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
# bats-core's own installer lays out bin/, libexec/ and lib/ under a prefix.
bash "$SRC/bats-core/install.sh" "$DEST"

[ -x "$BIN" ] || {
  echo "bats did not install as expected: ${BIN} is not executable." >&2
  exit 1
}
echo ""
"$BIN" --version
echo ""
echo "Done. ci/checks/tests-shell.sh finds this automatically."
echo "To use it in your own shell:  export PATH=\"${DEST}/bin:\$PATH\""
