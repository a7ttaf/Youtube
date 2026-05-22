from pathlib import Path

from ums_smart_revenue.devtools.pytest_policy_gate import (
    find_policy_violations,
    run_policy_gate,
)


def write_test_file(root: Path, name: str, content: str) -> None:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_policy_gate_allows_normal_tests(tmp_path):
    write_test_file(
        tmp_path,
        "test_allowed.py",
        """
def test_revenue_contract_is_checked():
    assert True
""",
    )

    assert find_policy_violations(tmp_path) == ()
    assert run_policy_gate(project_root=tmp_path) == 0


def test_policy_gate_rejects_pytest_mark_skip(tmp_path):
    write_test_file(
        tmp_path,
        "test_skip_marker.py",
        """
import pytest


@pytest.mark.skip(reason="not ready")
def test_revenue_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].relative_path == Path("tests/test_skip_marker.py")
    assert violations[0].symbol == "pytest.mark.skip"


def test_policy_gate_rejects_runtime_pytest_xfail(tmp_path):
    write_test_file(
        tmp_path,
        "test_runtime_xfail.py",
        """
import pytest


def test_revenue_contract_is_checked():
    pytest.xfail("broken")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].relative_path == Path("tests/test_runtime_xfail.py")
    assert violations[0].symbol == "pytest.xfail"


def test_policy_gate_reports_failure_summary(tmp_path, capsys):
    write_test_file(
        tmp_path,
        "test_skip.py",
        """
import pytest


@pytest.mark.xfail(reason="broken")
def test_revenue_contract_is_checked():
    assert False
""",
    )

    exit_code = run_policy_gate(project_root=tmp_path)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Forbidden pytest skip/xfail policy violations: 1" in captured.err
    assert "tests/test_skip.py" in captured.err
