"""Ingest tenant-scoped deduction-component evidence into finance storage."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
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
from ums_smart_revenue.finance.month_close import (
    get_month_close_status,
    get_or_create_month_close_row,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

INGESTION_SOURCES: tuple[str, ...] = ("source_rows", "bank", "gap")
_SOURCE_REPLACE_TABLES: dict[str, frozenset[str]] = {
    "source_rows": frozenset({"google_revenue_source_rows"}),
    "bank": frozenset({"bank_reconciliation_entries"}),
    "gap": frozenset({"adsense_payment_gap"}),
}
_MONTH_LENGTH = 7
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


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


@dataclass(frozen=True)
class DeductionComponentPage:
    """One page of deduction components plus the full-match total count."""

    total_count: int
    components: list[DeductionComponent]


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit, ambient, or default tenant UUID for repository scoping.

    Raises:
        DeductionComponentValidationError: If an explicit tenant id is invalid.
    """
    if isinstance(tenant_id, UUID):
        return tenant_id
    if tenant_id is None:
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.id
        return _DEFAULT_TENANT_UUID
    try:
        return UUID(str(tenant_id).strip())
    except ValueError as exc:
        raise DeductionComponentValidationError(f"invalid tenant_id: {tenant_id!r}") from exc


def _validate_month(month: str) -> None:
    """Validate YYYY-MM month input.

    Raises:
        DeductionComponentValidationError: If the month is malformed.
    """
    if len(month) != _MONTH_LENGTH or month[4] != "-":
        raise DeductionComponentValidationError("month must use YYYY-MM")
    year, sep, mm = month[:4], month[4], month[5:]
    if not (
        all("0" <= char <= "9" for char in year)
        and sep == "-"
        and all("0" <= char <= "9" for char in mm)
        and 1 <= int(mm) <= 12
    ):
        raise DeductionComponentValidationError("month must use YYYY-MM")


def _dialect_insert(dialect_name: str):
    """Select the SQLAlchemy insert function for the current dialect.

    Raises:
        DeductionComponentValidationError: If the dialect is unsupported.
    """
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
        """Raise when the target finance month is locked.

        Raises:
            DeductionComponentLockedMonthError: If the finance month is LOCKED.
        """
        close = get_or_create_month_close_row(
            self._session, month, tenant_id=self._tenant_id, for_update=True
        )
        if close.status == "LOCKED":
            raise DeductionComponentLockedMonthError(
                "Finance month is locked for deduction-component ingestion"
            )

    def upsert_components(
        self, *,
        month: str,
        components: list[DeductionComponentInput],
        replace_source_tables: Collection[str] | None = None,
    ) -> list[DeductionComponent]:
        """Upsert validated deduction components for one finance month.

        Raises:
            DeductionComponentValidationError: For malformed month, unsupported
                dialect, or failed upsert verification.
            DeductionComponentLockedMonthError: If the finance month is LOCKED.
        """
        _validate_month(month)
        # FIX: refuse LOCKED months even for a zero-component run. Live ingestion
        # must fail closed BEFORE the empty-return (and before the service's audit
        # path), so the lock check precedes the empty-component short-circuit.
        self._require_month_open(month)
        live_component_keys = {component.component_key for component in components}
        if not components:
            self._delete_stale_month_components(
                month=month,
                live_component_keys=live_component_keys,
                replace_source_tables=replace_source_tables,
            )
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
        self._delete_stale_month_components(
            month=month,
            live_component_keys=live_component_keys,
            replace_source_tables=replace_source_tables,
        )
        return entries

    def _delete_stale_month_components(
        self, *,
        month: str,
        live_component_keys: set[str],
        replace_source_tables: Collection[str] | None = None,
    ) -> None:
        """Delete tenant/month rows that were not produced by this ingestion run."""
        statement = (
            delete(DeductionComponentORM)
            .where(DeductionComponentORM.tenant_id == self._tenant_id)
            .where(DeductionComponentORM.month == month)
        )
        if replace_source_tables is not None:
            if not replace_source_tables:
                return
            statement = statement.where(
                DeductionComponentORM.source_table.in_(tuple(replace_source_tables))
            )
        if live_component_keys:
            statement = statement.where(
                DeductionComponentORM.component_key.not_in(live_component_keys)
            )
        self._session.execute(statement)

    def list_month_components(
        self,
        *,
        month: str,
        youtube_channel_ids: set[str] | None = None,
    ) -> list[DeductionComponent]:
        """List persisted deduction components for one finance month.

        When youtube_channel_ids is provided, only CHANNEL-scoped components
        whose scope_id is in the set are returned. Pass None to return all
        components regardless of scope_id (global/admin path).

        Raises:
            DeductionComponentValidationError: If the month is malformed.
        """
        _validate_month(month)
        query = (
            select(DeductionComponentORM)
            .where(DeductionComponentORM.tenant_id == self._tenant_id)
            .where(DeductionComponentORM.month == month)
        )
        if youtube_channel_ids is not None:
            query = query.where(
                DeductionComponentORM.scope_id.in_(youtube_channel_ids)
            )
        rows = self._session.scalars(
            query.order_by(
                DeductionComponentORM.scope_kind,
                DeductionComponentORM.scope_id,
                DeductionComponentORM.component_kind,
                DeductionComponentORM.component_key,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    # ============================================================================
    # Purpose: Return a filtered, paginated page of components for one month,
    #   plus the full COUNT(*) over the filtered set (not a Python slice).
    #   Used exclusively by the read-only GET /revenue/months/{month}/deduction-components
    #   endpoint to avoid loading an entire month into memory.
    # Database/ORM: Reads deduction_components / DeductionComponentORM.
    # Standards: SQL-layer COUNT + LIMIT/OFFSET; deterministic ORDER BY mirrors
    #   list_month_components; no write path touched.
    # Blast Radius: Finance read only. No auth/Neo4j/ingestion impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_deduction_components.
    # ============================================================================
    def list_month_components_page(
        self, *,
        month: str,
        component_kind: str | None = None,
        scope_kind: str | None = None,
        limit: int,
        offset: int,
    ) -> DeductionComponentPage:
        """Return a filtered, paginated page of components + the full match count.

        Raises:
            DeductionComponentValidationError: If the month is malformed,
                limit is less than 1, or offset is negative.
        """
        _validate_month(month)
        if limit < 1:
            raise DeductionComponentValidationError("limit must be >= 1")
        if offset < 0:
            raise DeductionComponentValidationError("offset must be >= 0")
        filters = [
            DeductionComponentORM.tenant_id == self._tenant_id,
            DeductionComponentORM.month == month,
        ]
        if component_kind is not None:
            filters.append(DeductionComponentORM.component_kind == component_kind)
        if scope_kind is not None:
            filters.append(DeductionComponentORM.scope_kind == scope_kind)
        total_count = self._session.scalar(
            select(func.count()).select_from(DeductionComponentORM).where(*filters)
        )
        rows = self._session.scalars(
            select(DeductionComponentORM)
            .where(*filters)
            .order_by(
                DeductionComponentORM.scope_kind,
                DeductionComponentORM.scope_id,
                DeductionComponentORM.component_kind,
                DeductionComponentORM.component_key,
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return DeductionComponentPage(
            total_count=int(total_count or 0),
            components=[self._to_entry(row) for row in rows],
        )

    @staticmethod
    def _to_entry(row: DeductionComponentORM) -> DeductionComponent:
        """Convert an ORM row into a read-model dataclass."""
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
        """Ingest deduction evidence for one finance month.

        Reads the selected source-of-truth adapters, maps them to deduction
        components, upserts in live mode, and records one summary audit event.

        Raises:
            DeductionComponentValidationError: The month, source, tenant ID, or
                repository dialect/input contract is invalid.
            DeductionComponentLockedMonthError: The target finance month is
                locked and must not accept deduction-component ingestion.
        """
        _validate_month(month)
        if not reason.strip():
            raise DeductionComponentValidationError("reason must not be blank")
        if source is not None and source not in INGESTION_SOURCES:
            raise DeductionComponentValidationError(
                f"source must be one of {INGESTION_SOURCES} or None"
            )
        # FIX: Dry-run must report the same locked-month refusal as live
        # ingestion without creating an OPEN close row as a dry-run side effect.
        if (
            dry_run
            and get_month_close_status(
                self._session, month, tenant_id=self._tenant_id
            )
            == "LOCKED"
        ):
            raise DeductionComponentLockedMonthError(
                "Finance month is locked for deduction-component ingestion"
            )

        components: list[DeductionComponentInput] = []
        skipped_non_usd = 0
        source_rows = []
        # FIX: Source-filtered runs must read only the selected adapter set.
        # The AdSense gap mapper needs source rows + payments, never bank rows.
        if source in (None, "source_rows", "gap"):
            source_row_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)
            source_rows = source_row_repo.list(self._tenant_id, report_month=month)
        if source in (None, "source_rows"):
            mapped, skipped = map_source_rows_to_components(source_rows)
            components.extend(mapped)
            skipped_non_usd += skipped
        if source in (None, "bank"):
            bank_repo = SqlAlchemyBankReconciliationRepository(
                self._session, tenant_id=self._tenant_id
            )
            bank_entries = bank_repo.list_month_entries(month=month)
            mapped, skipped = map_bank_entries_to_components(bank_entries, month=month)
            components.extend(mapped)
            skipped_non_usd += skipped
        if source in (None, "gap"):
            payment_repo = SqlAlchemyAdSensePaymentRepository(
                self._session, tenant_id=self._tenant_id
            )
            payments = payment_repo.list_month_payments(month=month)
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
            # FIX: Scoped ingestion replaces only the selected source family; a
            # bank-only rerun must not delete source-row or gap evidence.
            replace_source_tables = (
                None if source is None else _SOURCE_REPLACE_TABLES[source]
            )
            self._repository.upsert_components(
                month=month,
                components=components,
                replace_source_tables=replace_source_tables,
            )
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
