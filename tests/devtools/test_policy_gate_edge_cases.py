from ums_smart_revenue.devtools.pytest_policy_gate import find_policy_violations


def write_test_file(root, name, content):
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_rejects_unittest_case_skip_decorators(tmp_path):
    write_test_file(
        tmp_path,
        "test_case_skip.py",
        """
import unittest.case


@unittest.case.skipIf(True, "not ready")
def test_revenue_contract_is_checked():
    assert False


@unittest.case.skip("not ready")
def test_payment_contract_is_checked():
    assert False
""",
    )
    violations = find_policy_violations(tmp_path)
    symbols = {v.symbol for v in violations}
    assert "unittest.case.skip" in symbols
    assert "unittest.case.skipIf" in symbols


def test_catches_import_unittest_case_as_alias(tmp_path):
    write_test_file(
        tmp_path,
        "test_import_case_as_alias.py",
        """
import unittest.case as uc


@uc.skipIf(True, "not ready")
def test_revenue_contract_is_checked():
    assert False
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "unittest.case.skipIf"


def test_resolves_destructured_assignment(tmp_path):
    write_test_file(
        tmp_path,
        "test_destructured.py",
        """
import pytest


skip_now, xfail_now = pytest.skip, pytest.xfail


def test_revenue_contract_is_checked():
    skip_now("not ready")


def test_payment_contract_is_checked():
    xfail_now("broken")
""",
    )
    violations = find_policy_violations(tmp_path)
    symbols = {v.symbol for v in violations}
    assert "pytest.skip" in symbols
    assert "pytest.xfail" in symbols
