#!/usr/bin/env bash
# Run a command after ensuring HOME points at a writable directory.
#
# Windows gnuwin32 make often launches sub-recipes with HOME=/ (or unset)
# even when the parent shell has a proper home. That breaks uv's
# ${HOME}/.cache lookups, bash's tilde expansion, and anything else that
# reads $HOME.
#
# We rebuild HOME from $USERPROFILE in pure bash. cygpath would be the
# natural choice but it returns "/" for every input under gnuwin32 make
# (its PATH-resolved binary is itself reached via the broken HOME), so
# we cannot rely on it here.
#
# Usage:
#   bash ci/scripts/with-home.sh <command> [args...]
set -Eeuo pipefail

if [ "${HOME:-/}" = "/" ] || [ -z "${HOME:-}" ]; then
  if [ -n "${USERPROFILE:-}" ]; then
    # USERPROFILE looks like "C:\Users\Mrmah". Convert to "/c/Users/Mrmah":
    #   1. drive letter lower-cased
    #   2. backslashes flipped to forward slashes
    drive_letter="$(printf '%s' "${USERPROFILE%%:*}" | tr 'A-Z' 'a-z')"
    rest="${USERPROFILE#*:}"
    rest="${rest//\\//}"
    HOME="/${drive_letter}${rest}"
    export HOME
  fi
fi

if [ -z "${HOME:-}" ] || [ "$HOME" = "/" ]; then
  echo "with-home: HOME is not set to a usable directory and USERPROFILE could not repair it." >&2
  exit 1
fi

mkdir -p "$HOME" 2>/dev/null || {
  echo "with-home: HOME is not creatable: $HOME" >&2
  exit 1
}

if [ ! -d "$HOME" ] || [ ! -w "$HOME" ]; then
  echo "with-home: HOME is not writable: $HOME" >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  echo "with-home: missing command" >&2
  exit 1
fi

exec "$@"
