#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: python lane"

HAS_PYTHON_PROJECT=0
if [ -f pyproject.toml ] || [ -f requirements.txt ] || [ -f pytest.ini ] || [ -f ruff.toml ] || [ -f setup.py ]; then
  HAS_PYTHON_PROJECT=1
fi

if [ "$HAS_PYTHON_PROJECT" -eq 0 ]; then
  echo "No Python project configuration found. Skipping Python lane."
  exit "$CI_RESULT_PASS"
fi

if ! ci::common::command_exists python3; then
  echo "Python project detected but python3 is missing."
  exit "$CI_RESULT_FAIL_INFRA"
fi

if [ -f requirements.txt ]; then
  SKIP_INSTALL=0
  if [ -d ".venv" ] && [ -f ".ci-gate/venv.hash" ]; then
    CURRENT_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
    CACHED_HASH="$(cat .ci-gate/venv.hash)"
    if [ "$CURRENT_HASH" = "$CACHED_HASH" ]; then
      echo ".venv up to date. Skipping install."
      SKIP_INSTALL=1
    fi
  fi

  if [ "$SKIP_INSTALL" = "0" ]; then
    if [ -x .venv/bin/python3 ]; then
      echo "Installing Python requirements with .venv/bin/python3"
      .venv/bin/python3 -m pip install --quiet -r requirements.txt
    elif [ -x .venv/bin/python ]; then
      echo "Installing Python requirements with .venv/bin/python"
      .venv/bin/python -m pip install --quiet -r requirements.txt
    else
      echo "requirements.txt detected but .venv is missing. Refusing global install."
      exit "$CI_RESULT_FAIL_INFRA"
    fi
    mkdir -p .ci-gate
    sha256sum requirements.txt | cut -d' ' -f1 > .ci-gate/venv.hash
  fi
fi

has_ruff_config=0
if [ -f ruff.toml ]; then
  has_ruff_config=1
elif [ -f pyproject.toml ] && grep -Eq '^\[tool\.ruff(\.|])' pyproject.toml; then
  has_ruff_config=1
fi

RUFF_BIN=""
if [ -x .venv/bin/ruff ]; then
  RUFF_BIN=".venv/bin/ruff"
elif ci::common::command_exists ruff; then
  RUFF_BIN="ruff"
fi

if [ "$has_ruff_config" -eq 1 ]; then
  if [ -n "$RUFF_BIN" ]; then
    ruff_failed=0
    echo "Running: $RUFF_BIN check ."
    if ! "$RUFF_BIN" check .; then
      ruff_failed=1
    fi
    if "$RUFF_BIN" format --help >/dev/null 2>&1; then
      echo "Running: $RUFF_BIN format --check ."
      if ! "$RUFF_BIN" format --check .; then
        ruff_failed=1
      fi
    fi
    if [ "$ruff_failed" -eq 1 ]; then
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  else
    echo "Ruff configuration detected (ruff.toml or [tool.ruff] in pyproject.toml) but ruff binary is missing."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi

has_pytest_indicators=0
if [ -f pytest.ini ] || [ -f conftest.py ]; then
  has_pytest_indicators=1
elif [ -f pyproject.toml ] && grep -Eq '^\[tool\.pytest\.ini_options\]' pyproject.toml; then
  has_pytest_indicators=1
elif [ -f tox.ini ] && grep -Eq '^\[pytest\]' tox.ini; then
  has_pytest_indicators=1
elif [ -f setup.cfg ] && grep -Eq '^\[tool:pytest\]' setup.cfg; then
  has_pytest_indicators=1
fi

PYTEST_BIN=""
if [ -x .venv/bin/pytest ]; then
  PYTEST_BIN=".venv/bin/pytest"
elif ci::common::command_exists pytest; then
  PYTEST_BIN="pytest"
fi

if [ -n "$PYTEST_BIN" ]; then
  echo "Running: $PYTEST_BIN"
  set +e
  "$PYTEST_BIN"
  pytest_rc=$?
  set -e
  if [ "$pytest_rc" -eq 5 ]; then
    if [ "$has_pytest_indicators" -eq 1 ] || [ -d tests ] || [ -d test ]; then
      echo "pytest exited with code 5 (no tests collected) despite pytest config/files being present." >&2
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    else
      echo "Warning: pytest exited with code 5 (no tests collected)." >&2
    fi
  fi
  if [ "$pytest_rc" -ne 0 ] && [ "$pytest_rc" -ne 5 ]; then
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
elif [ "$has_pytest_indicators" -eq 1 ]; then
  echo "Pytest indicators detected but pytest binary is missing."
  exit "$CI_RESULT_FAIL_INFRA"
fi

echo "Running: python3 -m compileall ."
python3 -m compileall -x '\.venv|__pycache__|\.mypy_cache|\.git|node_modules' . 1>/dev/null

echo "Python lane passed."
exit "$CI_RESULT_PASS"
