from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import DeductionComponentORM
from ums_smart_revenue.finance.adsense_payments import SqlAlchemyAdSensePaymentRepository
from ums_smart_revenue.finance.bank_reconciliation import (
    SqlAlchemyBankReconciliationRepository,
)
from ums_smart_revenue.finance.deduction_components import (
    DeductionComponent,
    DeductionComponentInput,
    map_adsense_gap_to_components,
    map_bank_entries_to_components,
    map_source_rows_to_components,
)
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

INGESTION_SOURCES: tuple[str, ...] = ("source_rows", "bank", "gap")
_MONTH_LENGTH = 7
"""
Module for idempotent, tenant-scoped ingestion of deduction components into the finance system.
"""


class DeductionComponentError(ValueError):
    """Base error for deduction-component ingestion."""


class DeductionComponentValidationError(DeductionComponentError):
    """Raised for invalid ingestion input."""


class DeductionComponentLockedMonthError(DeductionComponentError):
    """Raised when the target finance month is locked."""


@dataclass(frozen=True)
class DeductionIngestionResult:
    """Summary of one ingestion run (counts only — no amounts)."""

    month: str
    total_upserted: int
    by_kind: dict[str, int]
    skipped_non_usd: int
    dry_run: bool


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Convert the given tenant_id to a UUID, using the default if None or validating string input."""
    if tenant_id is None:
        return UUID(UMS_TENANT_ID)
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(str(tenant_id))
    except ValueError as exc:
        raise DeductionComponentValidationError(f"invalid tenant_id: {tenant_id!r}") from exc


def _validate_month(month: str) -> None:
    """Ensure the month string matches 'YYYY-MM' format and represents a valid calendar month."""
    if len(month) != _MONTH_LENGTH or month[4] != "-":
        raise DeductionComponentValidationError("month must use YYYY-MM")
    year, sep, mm = month[:4], month[4], month[5:]
    if not (year.isdigit() and sep == "-" and mm.isdigit() and 1 <= int(mm) <= 12):
        raise DeductionComponentValidationError("month must use YYYY-MM")


def _dialect_insert(dialect_name: str):
    """Select the appropriate SQLAlchemy insert function based on the database dialect."""
    if dialect_name == "sqlite":
        return sqlite_insert
    if dialect_name == "postgresql":
        return postgresql_insert
    raise DeductionComponentValidationError(
        f"Unsupported database dialect for deduction component upsert: {dialect_name}"
    )


class SqlAlchemyDeductionComponentRepository:
    """Idempotent, tenant-scoped storage for deduction components."""

    # ========================================================================
    # Purpose: Upsert deduction components by (tenant_id, component_key) and
    #   read a month's components. Refuses writes to LOCKED finance months.
    # Database/ORM: deduction_components / DeductionComponentORM.
    # Standards: idempotent ON CONFLICT DO UPDATE; month-lock advisory gate via
    #   get_or_create_month_close_row(for_update=True), mirroring sibling repos.
    # Blast Radius: Finance source-of-truth writes (new table). No auth/Neo4j.
    # ========================================================================
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def _require_month_open(self, month: str) -> None:
        """Check if the given month is open for ingestion and raise an error if it is locked."""
        close = get_or_create_month_close_row(
            self._session, month, tenant_id=self._tenant_id, for_update=True
        )
        if close.status == "LOCKED":
            raise DeductionComponentLockedMonthError(
                "Finance month is locked for deduction-component ingestion"
            )

    def upsert_components(
        self, *, month: str, components: list[DeductionComponentInput]
    ) -> list[DeductionComponent]:
        """Insert or update deduction components for a given month, validating inputs, enforcing month locks, and returning the list of processed components."""
        _validate_month(month)
        # FIX: refuse LOCKED months even for a zero-component run. Live ingestion
        # must fail closed BEFORE the empty-return (and before the service's audit
        # path), so the lock check precedes the empty-component short-circuit.
        self._require_month_open(month)
        if not components:
            return []
        insert_builder = _dialect_insert(self._session.get_bind().dialect.name)
        entries: list[DeductionComponent] = []
        now = datetime.now(UTC)
        for component in components:
            statement = insert_builder(DeductionComponentORM).values(
                id=uuid4(),
                tenant_id=self._tenant_id,
                month=month,
                component_kind=component.component_kind,
                scope_kind=component.scope_kind,
                scope_id=component.scope_id,
                amount_usd=component.amount_usd,
                amount_native=component.amount_native,
                currency_code=component.currency_code,
                source_system=component.source_system,
                source_table=component.source_table,
                source_id=component.source_id,
                source_key=component.source_key,
                source_report_id=component.source_report_id,
                raw_payload=dict(component.raw_payload),
                component_key=component.component_key,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=[
                    DeductionComponentORM.tenant_id,
                    DeductionComponentORM.component_key,
                ],
                set_={
                    "month": month,
                    "component_kind": component.component_kind,
                    "scope_kind": component.scope_kind,
                    "scope_id": component.scope_id,
                    "amount_usd": component.amount_usd,
                    "amount_native": component.amount_native,
                    "currency_code": component.currency_code,
                    "source_system": component.source_system,
                    "source_table": component.source_table,
                    "source_id": component.source_id,
                    "source_key": component.source_key,
                    "source_report_id": component.source_report_id,
                    "raw_payload": dict(component.raw_payload),
                    "updated_at": now,
                },
            ).returning(DeductionComponentORM.id)
            row_id = self._session.execute(statement).scalar_one()
            row = self._session.get(DeductionComponentORM, row_id)
            if row is None:
                raise DeductionComponentValidationError("deduction component upsert failed")
            self._session.refresh(row)
            entries.append(self._to_entry(row))
        return entries

    def list_month_components(self, *, month: str) -> list[DeductionComponent]:
        """Return a list of DeductionComponent entries for the specified month by querying the database."""
        _validate_month(month)
        rows = self._session.scalars(
            select(DeductionComponentORM)
            .where(DeductionComponentORM.tenant_id == self._tenant_id)
            .where(DeductionComponentORM.month == month)
            .order_by(
                DeductionComponentORM.scope_kind,
                DeductionComponentORM.scope_id,
                DeductionComponentORM.component_kind,
                DeductionComponentORM.component_key,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    @staticmethod
    def _to_entry(row: DeductionComponentORM) -> DeductionComponent:
        """Convert a DeductionComponentORM instance into a DeductionComponent data model."""
        return DeductionComponent(
            id=str(row.id),
            month=row.month,
            component_kind=row.component_kind,
            scope_kind=row.scope_kind,
            scope_id=row.scope_id,
            amount_usd=row.amount_usd,
            amount_native=row.amount_native,
            currency_code=row.currency_code,
            source_system=row.source_system,
            source_table=row.source_table,
            source_id=row.source_id,
            source_key=row.source_key,
            source_report_id=row.source_report_id,
            raw_payload=dict(row.raw_payload or {}),
            component_key=row.component_key,
        )


class DeductionIngestionService:
    """Read source-of-truth tables, map to components, upsert + audit."""

    # ========================================================================
    # Purpose: Orchestrate deduction-evidence ingestion for one tenant+month:
    #   read source rows / bank entries / AdSense payments, run the pure
    #   mappers, idempotently upsert, and record one summary-count-only
    #   DEDUCTION_COMPONENTS_INGESTED audit event. No allocation, no net math.
    # Database/ORM: reads google_revenue_source_rows, bank_reconciliation_entries,
    #   adsense_payments; writes deduction_components (via the repository).
    # Standards: USD-only (non-USD skipped+counted); month-lock-gated; audit
    #   details carry ONLY counts (no amounts/payloads).
    # Blast Radius: Finance source-of-truth writes + one audit event.
    # ========================================================================
    def __init__(
        self, session: Session, *, audit_sink: AuditSink,
        tenant_id: UUID | str | None = None,
    ):
        self._session = session
        self._audit_sink = audit_sink
        self._tenant_id = _resolve_tenant_id(tenant_id)
        self._repository = SqlAlchemyDeductionComponentRepository(
            session, tenant_id=self._tenant_id
        )

    def ingest(
        self, *, month: str, actor: UserPrincipal, reason: str,
        source: str | None = None, dry_run: bool = False,
    ) -> DeductionIngestionResult:
        """Ingest deduction components for a given month by fetching source rows, bank entries, and AdSense payments, mapping them to components, upserting in the repository, and recording an audit summary event."""
        _validate_month(month)
        if source is not None and source not in INGESTION_SOURCES:
            raise DeductionComponentValidationError(
                f"source must be one of {INGESTION_SOURCES} or None"
            )
        payment_repo = SqlAlchemyAdSensePaymentRepository(
            self._session, tenant_id=self._tenant_id
        )
        bank_repo = SqlAlchemyBankReconciliationRepository(
            self._session, tenant_id=self._tenant_id
        )
        source_row_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)

        payments = payment_repo.list_month_payments(month=month)
        bank_entries = bank_repo.list_month_entries(month=month)
        source_rows = source_row_repo.list(self._tenant_id, report_month=month)

        components: list[DeductionComponentInput] = []
        skipped_non_usd = 0
        if source in (None, "source_rows"):
            mapped, skipped = map_source_rows_to_components(source_rows)
            components.extend(mapped)
            skipped_non_usd += skipped
        if source in (None, "bank"):
            mapped, skipped = map_bank_entries_to_components(bank_entries, month=month)
            components.extend(mapped)
            skipped_non_usd += skipped
        if source in (None, "gap"):
            mapped, skipped = map_adsense_gap_to_components(
                month=month, source_rows=source_rows, payments=payments
            )
            components.extend(mapped)
            skipped_non_usd += skipped

        by_kind: dict[str, int] = {}
        for component in components:
            by_kind[component.component_kind] = by_kind.get(component.component_kind, 0) + 1
        total = len(components)

        if not dry_run:
            self._repository.upsert_components(month=month, components=components)
            record_audit_event(
                sink=self._audit_sink,
                actor=actor,
                event_type=AuditEventType.DEDUCTION_COMPONENTS_INGESTED,
                entity_type="deduction_components",
                entity_id=month,
                scope=AccessScope.finance_month(month),
                reason=reason,
                details={
                    "month": month,
                    "total_upserted": total,
                    "by_kind": by_kind,
                    "skipped_non_usd": skipped_non_usd,
                },
            )
        return DeductionIngestionResult(
            month=month, total_upserted=total, by_kind=by_kind,
            skipped_non_usd=skipped_non_usd, dry_run=dry_run,
        )
