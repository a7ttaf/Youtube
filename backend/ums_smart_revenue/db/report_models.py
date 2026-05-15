from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ReportBase(DeclarativeBase):
    pass


class RawReportFileORM(ReportBase):
    __tablename__ = "raw_report_files"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    report_month: Mapped[str] = mapped_column(Text, nullable=False)
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'DOWNLOADED'")
    )
    downloaded_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
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
    )


class ExportJobORM(ReportBase):
    __tablename__ = "export_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    export_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ====================================================================
    # Purpose: Snapshot of YouTube channel IDs resolved at job creation so
    #   re-reads and re-downloads return the same data even if the source
    #   group/sector/company membership changes afterward. NULL means the
    #   job covers "all channels" (global) or pre-snapshot legacy rows.
    # Database/ORM: export_jobs.scope_channel_ids JSONB.
    # Standards: Finance numbers must be deterministic per export id.
    # Blast Radius: Finance and analytics export reads.
    # ====================================================================
    scope_channel_ids: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=True,
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'USD'")
    )
    requested_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'QUEUED'")
    )
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    artifact_checksum_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    month_lock_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'OPEN'")
    )
    include_confidence_notes: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    include_manual_override_notes: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "export_type IN ('FINANCE_EXCEL', 'EXECUTIVE_PDF', "
            "'BRANDED_SLIDE_PACK', 'ANALYTICS_SUMMARY_CSV')",
            name="ck_export_jobs_export_type",
        ),
        CheckConstraint(
            "scope_type IN ('global', 'sector', 'company', 'channel', 'group')",
            name="ck_export_jobs_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id IS NULL) OR "
            "(scope_type <> 'global' AND scope_id IS NOT NULL)",
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
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_export_jobs_status",
        ),
        CheckConstraint(
            "artifact_byte_size IS NULL OR artifact_byte_size >= 0",
            name="ck_export_jobs_artifact_byte_size",
        ),
        CheckConstraint(
            "artifact_checksum_sha256 IS NULL OR length(artifact_checksum_sha256) = 64",
            name="ck_export_jobs_artifact_checksum_sha256",
        ),
        CheckConstraint(
            "(artifact_filename IS NULL "
            "AND artifact_content_type IS NULL "
            "AND artifact_byte_size IS NULL "
            "AND artifact_checksum_sha256 IS NULL) "
            "OR (artifact_filename IS NOT NULL "
            "AND artifact_content_type IS NOT NULL "
            "AND artifact_byte_size IS NOT NULL "
            "AND artifact_checksum_sha256 IS NOT NULL)",
            name="ck_export_jobs_artifact_all_or_none",
        ),
        CheckConstraint(
            "month_lock_status IN ('OPEN', 'LOCKED')",
            name="ck_export_jobs_month_lock_status",
        ),
        Index("ix_export_jobs_requested_by_created", "requested_by", "created_at"),
        Index("ix_export_jobs_scope_month", "scope_type", "scope_id", "month"),
        # Mirror the migration 0006 GIN index on scope_channel_ids so ORM-built
        # schemas (e.g. ReportBase.metadata.create_all in tests) include the
        # PostgreSQL index. On SQLite postgresql_using is silently ignored and
        # a plain b-tree index is created.
        Index(
            "ix_export_jobs_scope_channel_ids",
            "scope_channel_ids",
            postgresql_using="gin",
        ),
    )


# Mirror the PostgreSQL-only CHECK from migration 0006 so ORM-built schemas on
# Postgres enforce the same shape + non-empty rule. The DDL is registered with
# execute_if(dialect="postgresql") so SQLite-backed create_all() — used by
# tests/db/test_export_job_models.py — silently skips it (jsonb_typeof and
# jsonb_array_length are PostgreSQL functions). Production uses migrations, so
# this DDL is a parity guarantee for dev/test PostgreSQL parity, not the
# authoritative constraint source.
event.listen(
    ExportJobORM.__table__,
    "after_create",
    DDL(
        "ALTER TABLE export_jobs ADD CONSTRAINT "
        "ck_export_jobs_scope_channel_ids_is_array "
        "CHECK (scope_channel_ids IS NULL OR ("
        "jsonb_typeof(scope_channel_ids) = 'array' "
        "AND jsonb_array_length(scope_channel_ids) > 0"
        "))"
    ).execute_if(dialect="postgresql"),
)
