from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

_TENANT_ID_DEFAULT = text(f"'{UMS_TENANT_ID}'")
_TENANT_ID_DEFAULT_VALUE = UUID(UMS_TENANT_ID)


class ConnectorRunORM(ReportBase):
    __tablename__ = "connector_runs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )
    connector_key: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    report_month: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'RUNNING'")
    )
    counts_json: Mapped[dict[str, int]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_connector_runs_tenant_id"),
        CheckConstraint(
            "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
            "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_connector_runs_report_month_format",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')",
            name="ck_connector_runs_status",
        ),
        CheckConstraint(
            "error_summary IS NULL OR length(error_summary) <= 500",
            name="ck_connector_runs_error_summary_len",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_connector_runs_finished_at_order",
        ),
        Index(
            "ix_connector_runs_tenant_connector_month",
            "tenant_id",
            "connector_key",
            "report_month",
        ),
        Index("ix_connector_runs_tenant_started", "tenant_id", started_at.desc()),
    )


class ConnectorRunRawFileORM(ReportBase):
    __tablename__ = "connector_run_raw_files"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )
    connector_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    raw_report_file_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ordering_index: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connector_run_id"],
            ["connector_runs.tenant_id", "connector_runs.id"],
            name="fk_connector_run_raw_files_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "raw_report_file_id"],
            ["raw_report_files.tenant_id", "raw_report_files.id"],
            name="fk_connector_run_raw_files_raw_file",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "connector_run_id",
            "raw_report_file_id",
            name="uq_connector_run_raw_files_run_file",
        ),
        Index(
            "ix_connector_run_raw_files_tenant_raw_file",
            "tenant_id",
            "raw_report_file_id",
        ),
    )
