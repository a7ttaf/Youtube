import os
import subprocess
import sys
from pathlib import Path

from ums_smart_revenue.devtools.quality_gate import (
    GateCommand,
    build_gate_commands,
    build_test_gate_commands,
    run_gate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_test_gate_commands_are_strict_and_test_only():
    commands = build_test_gate_commands(python=sys.executable)

    assert commands == (
        GateCommand(
            label="Pytest no skip or xfail policy",
            command=(
                sys.executable,
                "-m",
                "ums_smart_revenue.devtools.pytest_policy_gate",
            ),
        ),
        GateCommand(
            label="Pytest full suite",
            command=(
                sys.executable,
                "-B",
                "-m",
                "pytest",
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


def test_gate_commands_cover_required_local_validation_contract():
    commands = build_gate_commands(python=sys.executable)

    assert commands == (
        GateCommand(
            label="Ruff backend, tests, and scripts",
            command=(
                sys.executable,
                "-m",
                "ruff",
                "check",
                "backend",
                "tests",
                "scripts",
            ),
        ),
        GateCommand(
            label="Pytest no skip or xfail policy",
            command=(
                sys.executable,
                "-m",
                "ums_smart_revenue.devtools.pytest_policy_gate",
            ),
        ),
        GateCommand(
            label="Pytest full suite",
            command=(
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "-q",
                "--strict-config",
                "--strict-markers",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                ".pytest-tmp",
            ),
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


def test_run_gate_uses_repo_root_and_disables_bytecode(monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    observed_calls = []

    def runner(command, *, cwd, env):
        observed_calls.append(
            (
                tuple(command),
                cwd,
                env["PYTHONDONTWRITEBYTECODE"],
                env["PYTHONPATH"].split(os.pathsep)[0],
            )
        )
        return subprocess.CompletedProcess(command, 0)

    exit_code = run_gate(
        commands=(GateCommand("one", ("tool", "arg")),),
        repo_root=PROJECT_ROOT,
        runner=runner,
    )

    assert exit_code == 0
    assert observed_calls == [
        (("tool", "arg"), PROJECT_ROOT, "1", str(PROJECT_ROOT / "backend"))
    ]
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "0"


def test_run_gate_stops_at_first_failed_gate(capsys):
    calls = []

    def runner(command, *, cwd, env):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 7 if "pytest" in command else 0)

    exit_code = run_gate(
        commands=(
            GateCommand("ruff", ("python", "-m", "ruff")),
            GateCommand("pytest", ("python", "-m", "pytest")),
            GateCommand("diff", ("git", "diff", "--check")),
        ),
        repo_root=PROJECT_ROOT,
        runner=runner,
    )

    captured = capsys.readouterr()

    assert exit_code == 7
    assert calls == [
        ("python", "-m", "ruff"),
        ("python", "-m", "pytest"),
    ]
    assert "Validation gate failed: pytest" in captured.err
