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


def test_forbids_importorskip_and_unittest_skip_equivalents(tmp_path):
    write_test_file(
        tmp_path,
        "test_unittest_skip_equivalents.py",
        """
import pytest
import unittest
import unittest.case


pytest.importorskip("missing_package")


@unittest.expectedFailure
def test_unittest_expected_failure():
    assert False


@unittest.case.expectedFailure
def test_unittest_case_expected_failure():
    assert False


@unittest.case.skipUnless(False, "not ready")
def test_unittest_case_skip_unless():
    assert False


class RevenueContractTests(unittest.TestCase):
    @unittest.skip("not ready")
    def test_decorator_skip(self):
        assert False

    def test_self_skip(self):
        self.skipTest("not ready")

    def test_direct_class_skip(self):
        unittest.TestCase.skipTest(self, "not ready")

    def test_super_skip(self):
        super().skipTest("not ready")

    def test_raise_skip(self):
        raise unittest.SkipTest("not ready")

    def test_raise_case_skip(self):
        raise unittest.case.SkipTest("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    symbols = {violation.symbol for violation in violations}
    assert {
        "pytest.importorskip",
        "self.skipTest",
        "super.skipTest",
        "unittest.SkipTest",
        "unittest.case.SkipTest",
        "unittest.case.expectedFailure",
        "unittest.case.skipUnless",
        "unittest.TestCase.skipTest",
        "unittest.expectedFailure",
        "unittest.skip",
    } <= symbols


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
    pytest_plugins = ("external.policy_plugin",)
    return pytest_plugins
""",
    )
    plugin_path = tmp_path / "external" / "policy_plugin.py"
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_path.write_text(
        """
import pytest


pytest.skip("not ready")
""",
        encoding="utf-8",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 0


def test_catches_getattr_assigned_from_builtins_attribute(tmp_path):
    write_test_file(
        tmp_path,
        "test_builtins_getattr_alias.py",
        """
import builtins
import pytest


grab = builtins.getattr


def test_revenue_contract_is_checked():
    grab(pytest, "skip")("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_local_getattr_shadow_does_not_trigger_policy_gate(tmp_path):
    write_test_file(
        tmp_path,
        "test_local_getattr_shadow.py",
        """
import pytest


def getattr(module, name):
    return lambda *_args, **_kwargs: None


def test_revenue_contract_is_checked():
    getattr(pytest, "skip")("local helper")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert violations == ()


def test_module_control_flow_string_constant_for_getattr(tmp_path):
    write_test_file(
        tmp_path,
        "test_control_flow_attr.py",
        """
import pytest


if True:
    attr_name = "skip"


def test_revenue_contract_is_checked():
    getattr(pytest, attr_name)("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_function_local_string_constant_for_getattr(tmp_path):
    write_test_file(
        tmp_path,
        "test_function_attr.py",
        """
import pytest


def test_revenue_contract_is_checked():
    attr_name = "xfail"
    getattr(pytest, attr_name)("broken")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.xfail"


def test_function_local_string_constants_do_not_leak_between_functions(tmp_path):
    write_test_file(
        tmp_path,
        "test_function_attr_leak.py",
        """
import pytest


def helper():
    attr_name = "xfail"
    return attr_name


def test_revenue_contract_is_checked():
    attr_name = "plain"
    getattr(pytest, attr_name)("local helper")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert violations == ()


def test_symbol_aliases_do_not_leak_between_functions(tmp_path):
    write_test_file(
        tmp_path,
        "test_symbol_alias_leak.py",
        """
import pytest


def helper():
    skip_now = pytest.skip
    return skip_now


def test_revenue_contract_is_checked():
    def skip_now(message):
        return message

    assert skip_now("local helper") == "local helper"
""",
    )
    violations = find_policy_violations(tmp_path)
    assert all(violation.line < 10 for violation in violations)


def test_getattr_starred_arguments_are_checked(tmp_path):
    write_test_file(
        tmp_path,
        "test_starred_getattr.py",
        """
import pytest


args = (pytest, "skip")


def test_revenue_contract_is_checked():
    getattr(*args)("not ready")
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].symbol == "pytest.skip"


def test_scans_helper_modules_under_tests(tmp_path):
    write_test_file(
        tmp_path,
        "helpers.py",
        """
import pytest


pytest.skip("not ready")
""",
    )
    write_test_file(
        tmp_path,
        "test_revenue_contract.py",
        """
def test_revenue_contract_is_checked():
    assert True
""",
    )
    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].relative_path.as_posix() == "tests/helpers.py"
    assert violations[0].symbol == "pytest.skip"


def test_resolves_string_aliases_in_pytest_plugins_declarations(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
plugin_name = "tests.policy_plugin"
pytest_plugins = (plugin_name,)
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


def test_resolves_sequence_aliases_in_pytest_plugins_declarations(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
plugin_modules = [
    "ums_smart_revenue.testing.policy_plugin",
    "ums_smart_revenue.testing.second_policy_plugin",
]
pytest_plugins = plugin_modules
""",
    )
    plugin_root = tmp_path / "backend" / "ums_smart_revenue" / "testing"
    plugin_root.mkdir(parents=True)
    (plugin_root / "policy_plugin.py").write_text(
        """
import pytest


pytest.skip("not ready")
""",
        encoding="utf-8",
    )
    (plugin_root / "second_policy_plugin.py").write_text(
        """
import pytest


pytest.xfail("not ready")
""",
        encoding="utf-8",
    )
    violations = find_policy_violations(tmp_path)
    assert {
        (violation.relative_path.as_posix(), violation.symbol)
        for violation in violations
    } == {
        ("backend/ums_smart_revenue/testing/policy_plugin.py", "pytest.skip"),
        (
            "backend/ums_smart_revenue/testing/second_policy_plugin.py",
            "pytest.xfail",
        ),
    }


def test_resolves_mutating_pytest_plugins_declarations(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
plugin_name = "ums_smart_revenue.testing.policy_plugin"
plugin_group = ["ums_smart_revenue.testing.second_policy_plugin"]
pytest_plugins = []
pytest_plugins.append(plugin_name)
pytest_plugins.extend(plugin_group)
""",
    )
    plugin_root = tmp_path / "backend" / "ums_smart_revenue" / "testing"
    plugin_root.mkdir(parents=True)
    (plugin_root / "policy_plugin.py").write_text(
        """
import pytest


pytest.skip("not ready")
""",
        encoding="utf-8",
    )
    (plugin_root / "second_policy_plugin.py").write_text(
        """
import pytest


pytest.xfail("not ready")
""",
        encoding="utf-8",
    )
    violations = find_policy_violations(tmp_path)
    assert {
        (violation.relative_path.as_posix(), violation.symbol)
        for violation in violations
    } == {
        ("backend/ums_smart_revenue/testing/policy_plugin.py", "pytest.skip"),
        (
            "backend/ums_smart_revenue/testing/second_policy_plugin.py",
            "pytest.xfail",
        ),
    }


def test_resolves_backend_pytest_plugin_modules(tmp_path):
    write_test_file(
        tmp_path,
        "conftest.py",
        """
pytest_plugins = ("ums_smart_revenue.testing.policy_plugin",)
""",
    )
    plugin_path = (
        tmp_path
        / "backend"
        / "ums_smart_revenue"
        / "testing"
        / "policy_plugin.py"
    )
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_path.write_text(
        """
import pytest


pytest.skip("not ready")
""",
        encoding="utf-8",
    )

    violations = find_policy_violations(tmp_path)
    assert len(violations) == 1
    assert (
        violations[0].relative_path.as_posix()
        == "backend/ums_smart_revenue/testing/policy_plugin.py"
    )
    assert violations[0].symbol == "pytest.skip"
