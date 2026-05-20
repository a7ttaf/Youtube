from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.user_roles import SqlAlchemyUserRoleAssignmentRepository
from ums_smart_revenue.db.security_models import AccessScopeORM, SecurityBase


def _build_db(tmp_path) -> str:
    url = f"sqlite+pysqlite:///{(tmp_path / 'repo.db').as_posix()}"
    engine = create_engine(url)
    SecurityBase.metadata.create_all(engine)
    return url


def test_get_or_create_scope_concurrent_insert_race_is_recovered(tmp_path):
    """When a concurrent writer inserts the same scope between our SELECT and INSERT,
    _get_or_create_scope must catch the IntegrityError and return the existing row
    instead of propagating an unhandled IntegrityError (which would cause a 500)."""
    url = _build_db(tmp_path)
    engine = create_engine(url)

    # A concurrent writer commits this scope before we try to insert it.
    with Session(engine) as setup:
        setup.add(
            AccessScopeORM(
                id=uuid4(),
                scope_type="company",
                scope_id="race-co",
                label="company:race-co",
            )
        )
        setup.commit()

    with Session(engine) as session:
        repo = SqlAlchemyUserRoleAssignmentRepository(session)

        original_scalars = session.scalars
        call_count = [0]

        def intercept(stmt, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate the race window: our existence check ran before
                # the concurrent commit, so we see no existing scope and
                # proceed to INSERT.
                mock = MagicMock()
                mock.one_or_none.return_value = None
                return mock
            return original_scalars(stmt, *args, **kwargs)

        with patch.object(session, "scalars", side_effect=intercept):
            result = repo._get_or_create_scope(scope_type="company", scope_id="race-co")

    assert result.scope_type == "company"
    assert result.scope_id == "race-co"
