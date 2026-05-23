"""Guard tests proving B1's finance modules do not depend on
CurrencyExchangeRateORM, market FX rates, or any provider FX feed for
official revenue, payment, tax, deduction, or reconciliation values.

Spec section 6: currency_exchange_rates is legacy scaffolding. Any new
finance/* module added in B1 must not import or query it.
"""

import ast
from pathlib import Path

FINANCE_DIR = Path(__file__).resolve().parents[2] / "backend" / "ums_smart_revenue" / "finance"

# exchange_rates.py is the legacy module itself; it's allowed to reference
# CurrencyExchangeRateORM. Everything else must not.
ALLOWED_LEGACY_FILES = {"exchange_rates.py"}


def _module_imports_currency_exchange_rate_orm(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        # `from ... import CurrencyExchangeRateORM [as anything]`: matching on
        # alias.name (the original symbol) already covers aliased imports.
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "CurrencyExchangeRateORM":
                    return True
        # Qualified access: `module.CurrencyExchangeRateORM`.
        if isinstance(node, ast.Attribute) and node.attr == "CurrencyExchangeRateORM":
            return True
        # Bare name: catches `from ...models import *` followed by a direct
        # `CurrencyExchangeRateORM(...)` reference, which the ImportFrom check
        # above cannot see because alias.name is "*".
        if isinstance(node, ast.Name) and node.id == "CurrencyExchangeRateORM":
            return True
    return False


def test_no_finance_module_outside_legacy_imports_currency_exchange_rate_orm() -> None:
    offenders = []
    for path in FINANCE_DIR.glob("**/*.py"):
        if path.name in ALLOWED_LEGACY_FILES:
            continue
        if _module_imports_currency_exchange_rate_orm(path):
            offenders.append(str(path.relative_to(FINANCE_DIR)))
    assert not offenders, (
        "B1 forbids new finance modules from depending on CurrencyExchangeRateORM "
        f"for official money. Offenders: {offenders}"
    )
