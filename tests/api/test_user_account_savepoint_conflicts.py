"""Savepoint-isolation guards for user-account IntegrityError diagnosis."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import ums_smart_revenue.auth.users as auth_users
from tests.api.test_user_accounts_api import DEFAULT_TENANT_ID, build_database_url, seed_database
from ums_smart_revenue.auth.users import (
    SqlAlchemyUserAccountRepository,
    UserAccountConflictError,
)
from ums_smart_revenue.db.security_models import UserORM


def test_user_repository_keeps_a_non_email_conflict_typed_after_a_real_failed_flush(
    tmp_path,
    monkeypatch,
):
    """A REAL non-email IntegrityError stays the typed conflict and keeps siblings.

    Codex P1 regression (3416d8d46): the storage-retry savepoint rework left the
    failed flush's deactivated transaction in place while ``create_user``'s
    diagnosis ran its ``_email_exists`` SELECT, so every IntegrityError the
    string matcher could not attribute to the email index escaped as
    ``PendingRollbackError`` and surfaced as ``UserAccountStorageError``
    ("storage unavailable") instead of the typed conflict. Unlike the simulated
    variants above, nothing here stubs ``_email_exists`` and the failure is a
    real database constraint — a planted primary-key collision — so this test
    is red whenever the diagnosis query cannot run after the failed write. The
    sibling account flushed EARLIER on the same session must survive the
    conflict: that preservation is the property the savepoint rework exists to
    protect, so this pins both halves at once.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    planted_id = UUID("00000000-0000-0000-0000-00000000c011")
    with Session(engine) as seed_session:
        seed_session.add(
            UserORM(id=planted_id, email="planted@example.com", display_name="Planted User")
        )
        seed_session.commit()

    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session, tenant_id=DEFAULT_TENANT_ID)
        sibling = repository.create_user(
            email="sibling@example.com",
            display_name="Sibling User",
            is_service_account=False,
        )
        # Only the id GENERATOR is forged, to make the next insert collide on
        # the primary key. The write, the failure, and the diagnosis all run
        # against the real database.
        monkeypatch.setattr(auth_users, "uuid4", lambda: planted_id)

        with pytest.raises(
            UserAccountConflictError,
            match="User account violates database constraints",
        ):
            repository.create_user(
                email="victim@example.com",
                display_name="Victim User",
                is_service_account=False,
            )

        monkeypatch.undo()
        session.commit()

    with Session(engine) as check_session:
        emails = set(check_session.scalars(select(UserORM.email)).all())
    assert sibling.email == "sibling@example.com"
    assert "sibling@example.com" in emails
    assert "victim@example.com" not in emails


def test_update_user_email_conflict_diagnosis_survives_a_real_failed_flush(
    tmp_path,
    monkeypatch,
):
    """update_user's ``_email_exists`` fallback still queries after a real failed UPDATE.

    The fallback only runs when neither the constraint name nor the error text
    identifies the email index — the driver-dependent case the string matcher
    exists to backstop — so the matcher is forged to MISS. The race window is
    simulated by letting only the FIRST ``_email_exists`` call (the pre-flush
    check) miss the planted winner, exactly as it would have before a
    concurrent commit; the UPDATE, the unique-index failure, and the diagnosis
    SELECT all run against the real database. Same Codex P1 regression class as
    the create path above: without the savepoint around the flush, the failed
    UPDATE leaves the transaction deactivated, the fallback SELECT raises
    ``PendingRollbackError``, and the typed email conflict surfaces as
    ``UserAccountStorageError``.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    subject_id = UUID("00000000-0000-0000-0000-00000000c012")
    with Session(engine) as seed_session:
        seed_session.add(
            UserORM(id=subject_id, email="subject@example.com", display_name="Subject User")
        )
        seed_session.add(
            UserORM(id=uuid4(), email="race-update@example.com", display_name="Race Winner")
        )
        seed_session.commit()

    monkeypatch.setattr(auth_users, "_is_email_constraint_violation", lambda exc: False)
    with Session(engine) as session:
        repository = SqlAlchemyUserAccountRepository(session, tenant_id=DEFAULT_TENANT_ID)
        real_email_exists = repository._email_exists
        email_exists_calls = {"n": 0}

        def email_exists_missing_the_race_winner(email: str, **kwargs: object) -> bool:
            """Miss the race winner once, then delegate to the real lookup."""
            email_exists_calls["n"] += 1
            if email_exists_calls["n"] == 1:
                # The TOCTOU window: the pre-flush check ran before the winner
                # committed. Every later call is the real query.
                return False
            return real_email_exists(email, **kwargs)

        monkeypatch.setattr(repository, "_email_exists", email_exists_missing_the_race_winner)

        with pytest.raises(UserAccountConflictError, match="User email already exists"):
            repository.update_user(user_id=str(subject_id), email="race-update@example.com")
    # The typed conflict must have come from the REAL diagnosis SELECT running
    # on the post-failure session, not from the forged pre-check.
    assert email_exists_calls["n"] >= 2
