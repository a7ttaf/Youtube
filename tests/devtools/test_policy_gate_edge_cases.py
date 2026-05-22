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


def test_module_level_string_constant_for_getattr(tmp_path):
    write_test_file(
        tmp_path,
        "test_module_attr.py",
        """
import pytest as pt

attr_name = "xfail"

def test_revenue_contract_is_checked():
    getattr(pt, attr_name)("broken")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.xfail"


def test_function_local_variable_does_not_leak_to_module(tmp_path):
    write_test_file(
        tmp_path,
        "test_no_leak.py",
        """
import pytest as pt

def helper():
    attr_name = "xfail"

def test_revenue_contract_is_checked():
    getattr(pt, attr_name)("broken")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 0


def test_getattr_with_object_attribute(tmp_path):
    write_test_file(
        tmp_path,
        "test_obj_attr.py",
        """
import pytest

class Box:
    attr = "xfail"

box = Box()

def test_revenue_contract_is_checked():
    getattr(pytest, Box.attr)("broken")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.xfail"


def test_resolves_destructured_assignment_by_position(tmp_path):
    write_test_file(
        tmp_path,
        "test_destructured_by_position.py",
        """
import pytest


skip_now, harmless = pytest.skip, object()


def test_revenue_contract_is_checked():
    skip_now("not ready")


def test_payment_contract_is_checked():
    harmless()
""",
    )
    violations = find_policy_violations(tmp_path)
    assert any(
        violation.symbol == "pytest.skip" and violation.line > 5
        for violation in violations
    )


def test_catches_aliased_builtin_getattr(tmp_path):
    write_test_file(
        tmp_path,
        "test_aliased_builtin_getattr.py",
        """
from builtins import getattr as grab
import pytest as pt


def test_revenue_contract_is_checked():
    grab(pt, "xfail")("broken")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.xfail"


def test_resolves_destructured_getattr_attribute_names(tmp_path):
    write_test_file(
        tmp_path,
        "test_destructured_getattr_name.py",
        """
import pytest


blocked_attr, allowed_attr = "skip", "not_forbidden"


def test_revenue_contract_is_checked():
    getattr(pytest, blocked_attr)("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_scans_pytest_plugins_declared_in_conftest(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
pytest_plugins = ("tests.policy_plugin",)
""",
    )
    write_test_file(
        tmp_path,
        "policy_plugin.py",
        """
import pytest


pytest.skip("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].relative_path.as_posix() == "tests/policy_plugin.py"
    assert violations[0].symbol == "pytest.skip"


def test_scans_augmented_assignment_pytest_plugins(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
pytest_plugins = ()
pytest_plugins += ("tests.policy_plugin",)
""",
    )
    write_test_file(
        tmp_path,
        "policy_plugin.py",
        """
import pytest


pytest.skip("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].relative_path.as_posix() == "tests/policy_plugin.py"
    assert violations[0].symbol == "pytest.skip"


def test_scans_module_scope_control_flow_pytest_plugins(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
if True:
    pytest_plugins = ("tests.policy_plugin",)
""",
    )
    write_test_file(
        tmp_path,
        "policy_plugin.py",
        """
import pytest


pytest.skip("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].relative_path.as_posix() == "tests/policy_plugin.py"
    assert violations[0].symbol == "pytest.skip"


def test_ignores_function_local_pytest_plugins_declarations(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
def helper():
    pytest_plugins = ("tests.policy_plugin",)
    return pytest_plugins
""",
    )
    write_test_file(
        tmp_path,
        "policy_plugin.py",
        """
import pytest


pytest.skip("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 0
