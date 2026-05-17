from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID


class ReportBase(DeclarativeBase):
    pass


# Shared server_default for the tenant_id column added in migration
# 20260517_0001. See backend/ums_smart_revenue/db/security_models.py for
# the rationale (single source of truth for the UMS tenant id).
_TENANT_ID_DEFAULT = text(f"'{UMS_TENANT_ID}'")


class RawReportFileORM(ReportBase):
    __tablename__ = "raw_report_files"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    report_month: Mapped[str] = mapped_column(Text, nullable=False)
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'DOWNLOADED'"))
    downloaded_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, server_default=_TENANT_ID_DEFAULT
    )

    __table_args__ = (
        UniqueConstraint(
            "source",
            "report_type",
            "report_month",
            "checksum",
            name="uq_raw_report_files_source_type_month_checksum",
        ),
        CheckConstraint(
            "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
            "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_raw_report_files_report_month_format",
        ),
        CheckConstraint(
            "parse_status IN ('DOWNLOADED', 'PARSED', 'FAILED', 'QUARANTINED')",
            name="ck_raw_report_files_parse_status",
        ),
        Index("ix_raw_report_files_source_month", "source", "report_month"),
        Index("ix_raw_report_files_report_type_month", "report_type", "report_month"),
        Index("ix_raw_report_files_tenant_id", "tenant_id"),
    )


class ExportJobORM(ReportBase):
    __tablename__ = "export_jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    export_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    month: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    requested_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'QUEUED'"))
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    month_lock_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'OPEN'"))
    include_confidence_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    include_manual_override_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, server_default=_TENANT_ID_DEFAULT
    )

    __table_args__ = (
        CheckConstraint(
            "export_type IN ('FINANCE_EXCEL', 'EXECUTIVE_PDF', 'BRANDED_SLIDE_PACK', 'ANALYTICS_SUMMARY_CSV')",
            name="ck_export_jobs_export_type",
        ),
        CheckConstraint(
            "scope_type IN ('global', 'sector', 'company', 'channel', 'group')",
            name="ck_export_jobs_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id IS NULL) OR (scope_type <> 'global' AND scope_id IS NOT NULL)",
            name="ck_export_jobs_scope",
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_export_jobs_month_format",
        ),
        CheckConstraint("currency = 'USD'", name="ck_export_jobs_currency_usd"),
        CheckConstraint("status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')", name="ck_export_jobs_status"),
        CheckConstraint("month_lock_status IN ('OPEN', 'LOCKED')", name="ck_export_jobs_month_lock_status"),
        Index("ix_export_jobs_requested_by_created", "requested_by", "created_at"),
        Index("ix_export_jobs_scope_month", "scope_type", "scope_id", "month"),
        Index("ix_export_jobs_tenant_id", "tenant_id"),
    )
