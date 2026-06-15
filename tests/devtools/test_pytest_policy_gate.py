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
    symbols_by_line = {(violation.line, violation.symbol) for violation in violations}

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


def test_policy_gate_rejects_unittest_skip_test_exception(tmp_path):
    write_test_file(
        tmp_path,
        "test_unittest_skip_test.py",
        """
import unittest


def test_revenue_contract_is_checked():
    raise unittest.SkipTest("not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.SkipTest"


def test_policy_gate_catches_wildcard_pytest_import(tmp_path):
    write_test_file(
        tmp_path,
        "test_wildcard.py",
        """
from pytest import *


skip("not ready")
xfail("broken")


@mark.skip(reason="not ready")
def test_mark_skip_is_blocked():
    assert False


@mark.xfail(reason="broken")
def test_mark_xfail_is_blocked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    symbols = {violation.symbol for violation in violations}
    assert "pytest.skip" in symbols
    assert "pytest.xfail" in symbols
    assert "pytest.mark.skip" in symbols
    assert "pytest.mark.xfail" in symbols


def test_policy_gate_catches_wildcard_unittest_import(tmp_path):
    write_test_file(
        tmp_path,
        "test_wildcard_unittest.py",
        """
from unittest import *


@skipIf(True, "not ready")
def test_payment_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.skipIf"


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


def test_policy_gate_rejects_self_skip_test(tmp_path):
    write_test_file(
        tmp_path,
        "test_self_skip.py",
        """
import unittest


class TestRevenue(unittest.TestCase):
    def test_revenue_contract_is_checked(self):
        self.skipTest("not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "self.skipTest"


def test_policy_gate_catches_unittest_submodule_import(tmp_path):
    write_test_file(
        tmp_path,
        "test_submodule_import.py",
        """
from unittest.case import skipIf


@skipIf(True, "not ready")
def test_revenue_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.case.skipIf"


def test_policy_gate_catches_wildcard_submodule_unittest_import(tmp_path):
    write_test_file(
        tmp_path,
        "test_wildcard_submodule.py",
        """
from unittest.case import *


@skipIf(True, "not ready")
def test_payment_contract_is_checked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.case.skipIf"


def test_policy_gate_scans_init_py_in_tests(tmp_path):
    (tmp_path / "tests" / "tenancy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "tenancy" / "__init__.py").write_text(
        "import pytest\npytest.skip('not ready')\n",
        encoding="utf-8",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].relative_path == Path("tests/tenancy/__init__.py")
    assert violations[0].symbol == "pytest.skip"


def test_policy_gate_catches_getattr_indirection(tmp_path):
    write_test_file(
        tmp_path,
        "test_getattr_bypass.py",
        """
import unittest


def test_revenue_contract_is_checked():
    getattr(unittest, "skip")(True, "not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.skip"


def test_policy_gate_catches_getattr_indirection_with_alias(tmp_path):
    write_test_file(
        tmp_path,
        "test_getattr_alias.py",
        """
import pytest as pt


def test_revenue_contract_is_checked():
    getattr(pt, "xfail")("broken")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "pytest.xfail"


def test_policy_gate_catches_wildcard_mark_decorator(tmp_path):
    write_test_file(
        tmp_path,
        "test_wildcard_mark.py",
        """
from pytest import mark


@mark.skip(reason="not ready")
def test_mark_skip_is_blocked():
    assert False


@mark.xfail(reason="broken")
def test_mark_xfail_is_blocked():
    assert False
""",
    )

    violations = find_policy_violations(tmp_path)

    symbols = {violation.symbol for violation in violations}
    assert "pytest.mark.skip" in symbols
    assert "pytest.mark.xfail" in symbols


def test_policy_gate_catches_super_skip_test(tmp_path):
    write_test_file(
        tmp_path,
        "test_super_skip.py",
        """
import unittest


class TestRevenue(unittest.TestCase):
    def test_revenue_contract_is_checked(self):
        super().skipTest("not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "super.skipTest"


def test_policy_gate_catches_unittest_testcase_skip_test(tmp_path):
    write_test_file(
        tmp_path,
        "test_testcase_skip.py",
        """
import unittest


class TestRevenue(unittest.TestCase):
    def test_revenue_contract_is_checked(self):
        unittest.TestCase.skipTest(self, "not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.TestCase.skipTest"


def test_policy_gate_catches_aliased_getattr_builtins_qual(tmp_path):
    write_test_file(
        tmp_path,
        "test_builtins_getattr.py",
        """
import builtins
import pytest

def test_revenue_contract_is_checked():
    builtins.getattr(pytest, "skip")(True, "not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_policy_gate_catches_aliased_getattr_from_import(tmp_path):
    write_test_file(
        tmp_path,
        "test_getattr_alias_import.py",
        """
from builtins import getattr as g
import unittest

def test_revenue_contract_is_checked():
    g(unittest, "skip")(True, "not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.skip"


def test_policy_gate_scans_root_level_conftest_with_plugins(tmp_path):
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "conftest.py").write_text(
        """
pytest_plugins = ("tests.policy_plugin",)
""",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "policy_plugin.py").write_text(
        """
import pytest

pytest.skip("not ready")
""",
        encoding="utf-8",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_policy_gate_catches_string_constant_via_getattr(tmp_path):
    write_test_file(
        tmp_path,
        "test_string_via_getattr.py",
        """
import pytest as pt

attr_name = "skip"

def test_revenue_contract_is_checked():
    getattr(pt, attr_name)(True, "not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_policy_gate_catches_unittest_case_skip_test_via_getattr(tmp_path):
    write_test_file(
        tmp_path,
        "test_case_skipTest.py",
        """
import unittest.case


def test_revenue_contract_is_checked():
    raise unittest.case.SkipTest("not ready")
""",
    )

    violations = find_policy_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].symbol == "unittest.case.SkipTest"
