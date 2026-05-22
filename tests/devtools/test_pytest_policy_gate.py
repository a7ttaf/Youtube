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


def test_policy_gate_scans_default_pytest_file_patterns(tmp_path):
    write_test_file(
        tmp_path,
        "runtime_skip_test.py",
        """
import pytest


def test_revenue_contract_is_checked():
    pytest.skip("not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].relative_path == Path("tests/runtime_skip_test.py")
    assert violations[0].symbol == "pytest.skip"


def test_policy_gate_scans_conftest_files(tmp_path):
    write_test_file(
        tmp_path,
        "tenant/conftest.py",
        """
import pytest


def pytest_collection_modifyitems(items):
    pytest.xfail("disabled from hook")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].relative_path == Path("tests/tenant/conftest.py")
    assert violations[0].symbol == "pytest.xfail"


def test_policy_gate_rejects_pytest_importorskip(tmp_path):
    write_test_file(
        tmp_path,
        "test_importorskip.py",
        """
import pytest


def test_revenue_contract_is_checked():
    pytest.importorskip("missing_dependency")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "pytest.importorskip"


def test_policy_gate_resolves_imported_skip_aliases(tmp_path):
    write_test_file(
        tmp_path,
        "test_aliases.py",
        """
from pytest import mark
from unittest import skipIf


@mark.skip(reason="not ready")
def test_revenue_contract_is_checked():
    assert False


@skipIf(True, "not ready")
def test_payment_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    assert [violation.symbol for violation in violations] == [
        "pytest.mark.skip",
        "unittest.skipIf",
    ]


def test_policy_gate_rejects_marker_objects_passed_as_values(tmp_path):
    write_test_file(
        tmp_path,
        "test_param_marks.py",
        """
import pytest


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param(1, marks=pytest.mark.xfail),
        pytest.param(2, marks=[pytest.mark.skip]),
    ],
)
def test_revenue_contract_is_checked(amount):
    assert amount > 0
""",
    )

    violations = find_policy_violations(tmp_path)

    assert {violation.symbol for violation in violations} == {
        "pytest.mark.skip",
        "pytest.mark.xfail",
    }


def test_policy_gate_resolves_local_aliases_assigned_from_forbidden_symbols(
    tmp_path,
):
    write_test_file(
        tmp_path,
        "test_local_aliases.py",
        """
import pytest

skip_now = pytest.skip
xfail_marker = pytest.mark.xfail


def test_revenue_contract_is_checked():
    skip_now("not ready")


@xfail_marker(reason="broken")
def test_payment_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)
    symbols_by_line = {
        (violation.line, violation.symbol) for violation in violations
    }

    assert (9, "pytest.skip") in symbols_by_line
    assert (12, "pytest.mark.xfail") in symbols_by_line


def test_policy_gate_rejects_unittest_expected_failure(tmp_path):
    write_test_file(
        tmp_path,
        "test_expected_failure.py",
        """
import unittest


@unittest.expectedFailure
def test_revenue_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.expectedFailure"


def test_policy_gate_scans_project_root_conftest(tmp_path):
    write_test_file(
        tmp_path,
        "test_allowed.py",
        """
def test_revenue_contract_is_checked():
    assert True
""",
    )
    (tmp_path / "conftest.py").write_text(
        """
import pytest

pytest.xfail("disabled from root conftest")
""",
        encoding="utf-8",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].relative_path == Path("conftest.py")
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
