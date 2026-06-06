"""Committed account-allocation write path (Phase 4 Spec 2b).

Persists a versioned, audited snapshot of the account-allocation compute
(gross / post-tax / company_level / no_allocation, allowlisted). Runs on the
shared request session and holds
the finance-month advisory lock across idempotency lookup, OPEN-month guard,
method validation, compute, reject-on-unallocated, version assignment, and the
row inserts. It NEVER opens or commits its own session/transaction — the FastAPI
session dependency commits after the route returns.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.finance_models import (
    CommittedAllocationLineORM,
    CommittedAllocationNoteORM,
    CommittedAllocationRunORM,
    CommittedAllocationUnallocatedORM,
)
from ums_smart_revenue.finance.allocation import (
    COMMITTABLE_ALLOCATION_METHODS,
    COMPANY_LEVEL_ALLOCATION_METHOD,
    NO_ALLOCATION_METHOD,
    AccountAllocationResult,
    AllocationValidationError,
)
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.manual_allocation import (
    MANUAL_ALLOCATION_METHOD,
    ManualAllocationInput,
    build_manual_account_allocation,
)
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)

# Service-layer allowlist. The proportional ENGINE allowlist
# (COMMITTABLE_ALLOCATION_METHODS) intentionally excludes 'manual': manual lines
# bypass the engine entirely via build_manual_account_allocation. This service
# set widens it so the commit route accepts 'manual' while the engine pin in
# tests/finance/test_allocation.py stays green.
SERVICE_COMMITTABLE_METHODS = COMMITTABLE_ALLOCATION_METHODS | {MANUAL_ALLOCATION_METHOD}


class CommittedAllocationValidationError(ValueError):
    """Unsupported method, unallocated components, or invalid actor/tenant (-> 422)."""


class CommittedAllocationLockedMonthError(RuntimeError):
    """The finance month is LOCKED; a new commit is rejected (-> 409)."""


class CommittedAllocationIdempotencyConflictError(RuntimeError):
    """The idempotency key was reused with a different request (-> 409)."""


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit, ambient, or default tenant UUID for repository scoping."""
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
        raise CommittedAllocationValidationError(
            f"invalid tenant_id: {tenant_id!r}"
        ) from exc


def _actor_identity_uuid(value: str) -> UUID:
    """Parse/derive the committed_by UUID (UUID literal or gateway subject)."""
    try:
        return actor_identity_uuid(value)
    except ValueError as exc:
        raise CommittedAllocationValidationError(str(exc)) from exc


@dataclass(frozen=True)
class CommitAllocationOutcome:
    """A committed run plus its child rows and a created/replayed flag."""

    run: CommittedAllocationRunORM
    lines: tuple[CommittedAllocationLineORM, ...]
    unallocated: tuple[CommittedAllocationUnallocatedORM, ...]
    notes: tuple[CommittedAllocationNoteORM, ...]
    created: bool


class SqlAlchemyCommittedAllocationRepository:
    """Persist committed allocation runs on the shared request session."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None) -> None:
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    @property
    def tenant_id(self) -> UUID:
        """The tenant UUID this repository is scoped to (read-only)."""
        return self._tenant_id

    # ========================================================================
    # Purpose: Commit a versioned snapshot of the account-allocation compute
    #   (gross / post-tax / company_level / no_allocation / manual, allowlisted)
    #   for one month, under the finance-month advisory lock. company_level
    #   requires the caller-resolved channel_company map; no_allocation snapshots
    #   its full intentional-withhold set instead of rejecting on unallocated;
    #   manual bypasses the proportional engine and persists the operator-asserted
    #   per-channel split (validated fail-closed by build_manual_account_allocation).
    # Database/ORM: committed_allocation_runs/_lines/_unallocated/_notes;
    #   reads FinanceMonthCloseORM (lock) via month_close helpers.
    # Standards: shared request session (no commit here); typed errors -> route
    #   422/409; method-before-compute; reject-on-unallocated.
    # Blast Radius: Finance write; first allocation persistence. No reader change.
    # ========================================================================
    def commit_allocation(
        self,
        *,
        month: str,
        allocation_method: str,
        idempotency_key: str,
        request_fingerprint: str,
        reason: str,
        committed_by: str,
        deduction_repository: object,
        revenue_repository: object,
        link_repository: object,
        channel_company: Mapping[str, str] | None = None,
        manual_lines: tuple[ManualAllocationInput, ...] | None = None,
    ) -> CommitAllocationOutcome:
        """Compute + persist a committed run, or replay an idempotent retry."""
        committed_by_uuid = _actor_identity_uuid(committed_by)
        # Hold the finance-month advisory lock + close-row FOR UPDATE for the
        # whole unit (the lock is transaction-scoped on Postgres; no-op on SQLite).
        close_row = get_or_create_month_close_row(
            self._session, month, tenant_id=self._tenant_id, for_update=True
        )

        existing = self._session.scalars(
            select(CommittedAllocationRunORM).where(
                CommittedAllocationRunORM.tenant_id == self._tenant_id,
                CommittedAllocationRunORM.month == month,
                CommittedAllocationRunORM.idempotency_key == idempotency_key,
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise CommittedAllocationIdempotencyConflictError(
                    "idempotency key reused with a different request"
                )
            return self._replay(existing)

        if close_row.status == "LOCKED":
            raise CommittedAllocationLockedMonthError(f"Finance month is locked: {month}")

        if allocation_method not in SERVICE_COMMITTABLE_METHODS:
            raise CommittedAllocationValidationError(
                f"unsupported allocation method: {allocation_method}"
            )
        # Service-layer mirror of the engine's company_level precondition so the
        # route gets a typed 422 (the engine's AllocationValidationError would
        # surface as a 500 from this path).
        if (
            allocation_method == COMPANY_LEVEL_ALLOCATION_METHOD
            and channel_company is None
        ):
            raise CommittedAllocationValidationError(
                "channel_company mapping is required for company_level"
            )

        result = self._compute_result(
            month=month,
            allocation_method=allocation_method,
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
            channel_company=channel_company,
            manual_lines=manual_lines,
        )
        # no_allocation withholds EVERY component by design — its snapshot
        # persists the full unallocated set as evidence instead of rejecting.
        if result.unallocated and allocation_method != NO_ALLOCATION_METHOD:
            raise CommittedAllocationValidationError(
                f"cannot commit: {len(result.unallocated)} unallocated component(s)"
            )

        next_version = (
            self._session.scalars(
                select(CommittedAllocationRunORM.commit_version).where(
                    CommittedAllocationRunORM.tenant_id == self._tenant_id,
                    CommittedAllocationRunORM.month == month,
                ).order_by(CommittedAllocationRunORM.commit_version.desc())
            ).first()
            or 0
        ) + 1

        run = CommittedAllocationRunORM(
            tenant_id=self._tenant_id, month=month, commit_version=next_version,
            allocation_method=result.allocation_method,
            idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
            component_count=result.summary.component_count,
            allocated_component_count=result.summary.allocated_component_count,
            unallocated_component_count=result.summary.unallocated_component_count,
            allocated_total_usd=result.summary.allocated_total_usd,
            unallocated_total_usd=result.summary.unallocated_total_usd,
            net_applicable_total_usd=result.summary.net_applicable_total_usd,
            reconciliation_total_usd=result.summary.reconciliation_total_usd,
            committed_by=committed_by_uuid, reason=reason,
        )
        self._session.add(run)
        self._session.flush()  # assign run.id

        lines = tuple(
            CommittedAllocationLineORM(
                run_id=run.id, adsense_account_id=ln.adsense_account_id,
                youtube_channel_id=ln.youtube_channel_id,
                component_kind=ln.component_kind, source_system=ln.source_system,
                component_key=ln.component_key, basis_source_kind=ln.basis_source_kind,
                basis_amount_usd=ln.basis_amount_usd, basis_share=ln.basis_share,
                allocated_amount_usd=ln.allocated_amount_usd,
                net_applicable=ln.net_applicable,
            )
            for ln in result.lines
        )
        notes = tuple(
            CommittedAllocationNoteORM(
                run_id=run.id, note_code=note.note_code,
                youtube_channel_id=note.youtube_channel_id, detail=note.detail,
            )
            for note in result.notes
        )
        # result.unallocated is empty here for every method except
        # no_allocation (reject-on-unallocated above); a no_allocation run
        # snapshots its full INTENTIONAL_NO_ALLOCATION set as evidence.
        unallocated = tuple(
            CommittedAllocationUnallocatedORM(
                run_id=run.id, scope_id=iss.scope_id, component_kind=iss.component_kind,
                component_key=iss.component_key, amount_usd=iss.amount_usd,
                issue_code=iss.issue_code, detail=iss.detail,
            )
            for iss in result.unallocated
        )
        for child in (*lines, *notes, *unallocated):
            self._session.add(child)
        self._session.flush()
        return CommitAllocationOutcome(
            run=run, lines=lines, unallocated=unallocated, notes=notes, created=True
        )

    # ========================================================================
    # Purpose: Resolve the AccountAllocationResult to persist for one commit.
    #   manual bypasses the proportional engine: it reads the month's ACCOUNT
    #   components + the verified account->channel map and feeds them, with the
    #   operator-asserted lines, to build_manual_account_allocation (fail-closed,
    #   exact per-component conservation). Every other method delegates to the
    #   shared compute_month_account_allocation engine and rejects stray
    #   manual_lines so they are never silently ignored.
    # Database/ORM: reads deduction_components (list_account_components) and the
    #   verified map (list_verified_adsense_account_channels) for manual; the
    #   engine path reads via compute_month_account_allocation.
    # Standards: typed AllocationValidationError -> CommittedAllocationValidationError
    #   (route 422); no writes here; no session/lock interaction.
    # Blast Radius: Finance read-model + compute only. No persistence, no Neo4j.
    # ========================================================================
    def _compute_result(
        self,
        *,
        month: str,
        allocation_method: str,
        deduction_repository: object,
        revenue_repository: object,
        link_repository: object,
        channel_company: Mapping[str, str] | None,
        manual_lines: tuple[ManualAllocationInput, ...] | None,
    ) -> AccountAllocationResult:
        """Build the allocation result for one commit (manual or engine path)."""
        if allocation_method == MANUAL_ALLOCATION_METHOD:
            if not manual_lines:
                raise CommittedAllocationValidationError(
                    "manual_lines is required for allocation_method=manual"
                )
            components = deduction_repository.list_account_components(month=month)
            tenant_id = link_repository.tenant_id
            accounts = sorted({component.scope_id for component in components})
            verified_channels = {
                account: link_repository.list_verified_adsense_account_channels(
                    tenant_id=tenant_id, month=month, adsense_account_id=account
                )
                for account in accounts
            }
            try:
                return build_manual_account_allocation(
                    month=month,
                    components=components,
                    verified_channels=verified_channels,
                    manual_lines=manual_lines,
                )
            except AllocationValidationError as exc:
                raise CommittedAllocationValidationError(str(exc)) from exc
        if manual_lines:
            raise CommittedAllocationValidationError(
                "manual_lines is only valid for allocation_method=manual"
            )
        return compute_month_account_allocation(
            month=month,
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
            allocation_method=allocation_method,
            channel_company=channel_company,
        )

    # ========================================================================
    # Purpose: Load an existing run's child rows for an idempotent replay or a
    #   read-switch reconstruction. A stable ORDER BY on each child select makes
    #   reconstructed reads deterministic: a SQL SELECT without ORDER BY may
    #   return rows in any order (a PG seq-scan after updates/vacuum can reorder),
    #   so two successive reads of the same snapshot could otherwise differ.
    # Database/ORM: reads committed_allocation_lines/_unallocated/_notes.
    # Standards: deterministic read ordering; reads never write.
    # Blast Radius: Finance read-model only (snapshot reconstruction). No money
    #   change — the lines/issues/notes set is identical; only emission order is
    #   pinned. No auth/Neo4j.
    # ========================================================================
    def _replay(self, run: CommittedAllocationRunORM) -> CommitAllocationOutcome:
        """Load an existing run's children (deterministically ordered) for replay."""
        lines = tuple(self._session.scalars(
            select(CommittedAllocationLineORM).where(
                CommittedAllocationLineORM.run_id == run.id
            ).order_by(
                CommittedAllocationLineORM.adsense_account_id,
                CommittedAllocationLineORM.youtube_channel_id,
                CommittedAllocationLineORM.component_key,
            )
        ).all())
        unallocated = tuple(self._session.scalars(
            select(CommittedAllocationUnallocatedORM).where(
                CommittedAllocationUnallocatedORM.run_id == run.id
            ).order_by(
                CommittedAllocationUnallocatedORM.scope_id,
                CommittedAllocationUnallocatedORM.component_key,
            )
        ).all())
        notes = tuple(self._session.scalars(
            select(CommittedAllocationNoteORM).where(
                CommittedAllocationNoteORM.run_id == run.id
            ).order_by(CommittedAllocationNoteORM.youtube_channel_id)
        ).all())
        return CommitAllocationOutcome(
            run=run, lines=lines, unallocated=unallocated, notes=notes, created=False
        )

    def get_latest_run(self, month: str) -> CommittedAllocationRunORM | None:
        """Return the highest-version run for a month (NOT wired into readers)."""
        return self._session.scalars(
            select(CommittedAllocationRunORM).where(
                CommittedAllocationRunORM.tenant_id == self._tenant_id,
                CommittedAllocationRunORM.month == month,
            ).order_by(CommittedAllocationRunORM.commit_version.desc())
        ).first()

    def get_run_by_idempotency_key(
        self, month: str, idempotency_key: str
    ) -> CommittedAllocationRunORM | None:
        """Return the run for a month-scoped idempotency key, if any."""
        return self._session.scalars(
            select(CommittedAllocationRunORM).where(
                CommittedAllocationRunORM.tenant_id == self._tenant_id,
                CommittedAllocationRunORM.month == month,
                CommittedAllocationRunORM.idempotency_key == idempotency_key,
            )
        ).one_or_none()

    def get_latest_committed(self, month: str) -> CommitAllocationOutcome | None:
        """Return the highest-version committed run + its child rows for a month, or None."""
        run = self.get_latest_run(month)
        return None if run is None else self._replay(run)
