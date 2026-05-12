from datetime import date
from decimal import Decimal
from importlib import import_module
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase

USER_ID = UUID("00000000-0000-0000-0000-000000009901")


def test_bank_reconciliation_model_persists_finance_bank_receipt_metadata():
    module = import_module("ums_smart_revenue.db.finance_models")
    bank_model = module.BankReconciliationEntryORM
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            bank_model(
                id=uuid4(),
                month="2026-03",
                bank_reference="bank-transfer-2026-04-22",
                bank_received_date=date(2026, 4, 22),
                bank_received_amount=Decimal("928.50"),
                bank_received_currency="USD",
                bank_received_amount_usd=Decimal("928.50"),
                transfer_fee_usd=Decimal("1.50"),
                fx_difference_usd=Decimal("0.25"),
                notes="Bank transfer received",
                source_report_id="bank-statement-2026-04",
                recorded_by=USER_ID,
            )
        )
        session.commit()
        entry = session.scalars(select(bank_model)).one()

    assert entry.month == "2026-03"
    assert entry.bank_reference == "bank-transfer-2026-04-22"
    assert entry.bank_received_amount == Decimal("928.50")
    assert entry.bank_received_currency == "USD"
    assert entry.bank_received_amount_usd == Decimal("928.50")
    assert entry.transfer_fee_usd == Decimal("1.50")
    assert entry.fx_difference_usd == Decimal("0.25")
    assert entry.recorded_by == USER_ID
