"""Executable local validation gate for backend changes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PYTEST_ENV_OVERRIDES = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
)
PYTEST_DISABLE_PLUGIN_AUTOLOAD = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"
RUFF_ENTRYPOINT = "import runpy; runpy.run_module('ruff', run_name='__main__')"
PYTEST_POLICY_ENTRYPOINT = (
    "import sys; from pathlib import Path; "
    "sys.path.insert(0, str(Path.cwd() / 'backend')); "
    "from ums_smart_revenue.devtools.pytest_policy_gate import main; "
    "raise SystemExit(main())"
)
PYTEST_ENTRYPOINT = (
    "import sys; from pathlib import Path; "
    "sys.path.insert(0, str(Path.cwd() / 'backend')); "
    "from pytest import console_main; raise SystemExit(console_main())"
)


@dataclass(frozen=True)
class GateCommand:
    """A single command in the repository validation gate."""

    label: str
    command: tuple[str, ...]


def _resolve_bun() -> str:
    """Resolve a Bun executable. On Windows, Bun is bun.exe."""
    bun = shutil.which("bun") or shutil.which("bun.exe")
    if bun is None:
        raise RuntimeError("Bun not found on PATH; install Bun to run frontend tests.")
    return bun


# ============================================================================
# Purpose: Build the local validation gate required before upload, push, or
# review-readiness claims, including the gate wrapper script itself.
# Database/ORM: None.
# Standards: Backend-owned tooling, explicit command tuples, fail-fast execution.
# Blast Radius: Tests, lint gate, and diff hygiene.
# Connections:
#   - File: AGENTS.md -> Mirrors the required baseline validation commands.
#   - File: Docs/implementation/CODEX_STABILIZATION_PLAN.md -> Keeps Windows
#     pytest temp handling explicit for this workstation.
# ============================================================================
def build_gate_commands(*, python: str = sys.executable) -> tuple[GateCommand, ...]:
    """Return the ordered validation commands for local quality gates."""
    return (
        GateCommand(
            label="Ruff backend, tests, and scripts",
            command=(
                python,
                "-B",
                "-P",
                "-c",
                RUFF_ENTRYPOINT,
                "check",
                "backend",
                "tests",
                "scripts",
            ),
        ),
        *build_test_gate_commands(python=python),
        GateCommand(
            label="Frontend tests (Vitest)",
            command=(_resolve_bun(), "--cwd", "frontend", "run", "test"),
        ),
        GateCommand(
            label="Git diff whitespace check",
            command=("git", "diff", "--check"),
        ),
        GateCommand(
            label="Git staged diff whitespace check",
            command=("git", "diff", "--cached", "--check"),
        ),
    )


def build_test_gate_commands(*, python: str = sys.executable) -> tuple[GateCommand, ...]:
    """Return the test-only validation commands for local quality gates."""
    return (
        GateCommand(
            label="Pytest no skip or xfail policy",
            command=(python, "-B", "-P", "-c", PYTEST_POLICY_ENTRYPOINT),
        ),
        GateCommand(
            label="Pytest full suite",
            command=(
                python,
                "-B",
                "-P",
                "-c",
                PYTEST_ENTRYPOINT,
                "-q",
                "--strict-config",
                "--strict-markers",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                ".pytest-tmp",
            ),
        ),
    )


# ============================================================================
# Purpose: Execute the repository validation gate with a controlled subprocess
# environment so caller-local pytest overrides cannot weaken validation.
# Database/ORM: None.
# Standards: Fail-fast subprocess execution, explicit environment normalization,
# and safe stderr reporting without swallowing command failures.
# Blast Radius: Local validation tooling only.
# Connections:
#   - File: scripts/run_validation_gate.py -> CLI wrapper for this entry point.
#   - File: scripts/run_tests_gate.py -> Reuses this runner for pytest-only gates.
#   - File: tests/devtools/test_quality_gate.py -> Covers environment handling.
# ============================================================================
def run_gate(
    *,
    commands: Sequence[GateCommand] | None = None,
    repo_root: Path = PROJECT_ROOT,
    runner: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> int:
    """Run validation commands from the repo root and stop at the first failure."""
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # FIX: Remove caller-supplied pytest startup overrides before validation.
    for key in PYTEST_ENV_OVERRIDES:
        env.pop(key, None)
    env[PYTEST_DISABLE_PLUGIN_AUTOLOAD] = "1"
    existing_pythonpath = env.get("PYTHONPATH")
    backend_pythonpath = str(repo_root / "backend")
    env["PYTHONPATH"] = (
        backend_pythonpath
        if not existing_pythonpath
        else f"{backend_pythonpath}{os.pathsep}{existing_pythonpath}"
    )
    gate_commands = build_gate_commands() if commands is None else commands

    for gate_command in gate_commands:
        completed = runner(gate_command.command, cwd=repo_root, env=env)
        if completed.returncode != 0:
            print(
                f"Validation gate failed: {gate_command.label}",
                file=sys.stderr,
            )
            return completed.returncode
    return 0


def main() -> int:
    """CLI entry point for the local validation gate."""
    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
