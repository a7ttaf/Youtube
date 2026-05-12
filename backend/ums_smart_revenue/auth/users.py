from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import UserORM


USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"
USER_STATUS_SERVICE = "service"
USER_STATUSES = frozenset({USER_STATUS_ACTIVE, USER_STATUS_DISABLED, USER_STATUS_SERVICE})


@dataclass(frozen=True)
class UserAccountEntry:
    id: str
    email: str
    display_name: str
    status: str
    is_service_account: bool
    created_at: datetime
    updated_at: datetime

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "status": self.status,
            "is_service_account": self.is_service_account,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class UserAccountError(ValueError):
    pass


class UserAccountConflictError(UserAccountError):
    pass


class UserAccountNotFoundError(UserAccountError):
    pass


class UserAccountValidationError(UserAccountError):
    pass


class SqlAlchemyUserAccountRepository:
    def __init__(self, session: Session):
        self._session = session

    def create_user(
        self,
        *,
        email: str,
        display_name: str,
        is_service_account: bool,
    ) -> UserAccountEntry:
        normalized_email = _normalize_email(email)
        normalized_display_name = _normalize_required_string(display_name, "display_name")
        if self._email_exists(normalized_email):
            raise UserAccountConflictError("User email already exists")

        row = UserORM(
            id=uuid4(),
            email=normalized_email,
            display_name=normalized_display_name,
            status=USER_STATUS_SERVICE if is_service_account else USER_STATUS_ACTIVE,
            is_service_account=is_service_account,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            if self._email_exists(normalized_email):
                raise UserAccountConflictError("User email already exists") from exc
            raise
        return self._to_entry(row)

    def get_user(self, *, user_id: str) -> UserAccountEntry:
        user_uuid = _parse_uuid(user_id, field_name="user_id")
        row = self._session.get(UserORM, user_uuid)
        if row is None:
            raise UserAccountNotFoundError("User not found")
        return self._to_entry(row)

    def update_user(
        self,
        *,
        user_id: str,
        email: str | None = None,
        display_name: str | None = None,
        status: str | None = None,
    ) -> UserAccountEntry:
        user_uuid = _parse_uuid(user_id, field_name="user_id")
        row = self._session.get(UserORM, user_uuid)
        if row is None:
            raise UserAccountNotFoundError("User not found")

        if email is not None:
            normalized_email = _normalize_email(email)
            existing = self._session.scalars(
                select(UserORM).where(
                    func.lower(UserORM.email) == normalized_email,
                    UserORM.id != user_uuid,
                )
            ).one_or_none()
            if existing is not None:
                raise UserAccountConflictError("User email already exists")
            row.email = normalized_email

        if display_name is not None:
            row.display_name = _normalize_required_string(display_name, "display_name")

        if status is not None:
            normalized_status = _normalize_status(status)
            _require_compatible_status(row, normalized_status)
            row.status = normalized_status

        try:
            self._session.flush()
        except IntegrityError as exc:
            if email is not None and self._email_exists(_normalize_email(email), excluding_user_id=user_uuid):
                raise UserAccountConflictError("User email already exists") from exc
            raise
        return self._to_entry(row)

    def _email_exists(self, email: str, *, excluding_user_id: UUID | None = None) -> bool:
        criteria = [func.lower(UserORM.email) == email]
        if excluding_user_id is not None:
            criteria.append(UserORM.id != excluding_user_id)
        return self._session.scalars(select(UserORM.id).where(*criteria)).one_or_none() is not None

    @staticmethod
    def _to_entry(row: UserORM) -> UserAccountEntry:
        return UserAccountEntry(
            id=str(row.id),
            email=row.email,
            display_name=row.display_name,
            status=row.status,
            is_service_account=row.is_service_account,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _parse_uuid(value: str, *, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise UserAccountValidationError(f"{field_name} must be a valid UUID") from exc


def _normalize_email(value: str) -> str:
    normalized = _normalize_required_string(value, "email").lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise UserAccountValidationError("email must be a valid email address")
    return normalized


def _normalize_status(value: str) -> str:
    normalized = _normalize_required_string(value, "status").lower()
    if normalized not in USER_STATUSES:
        allowed = ", ".join(sorted(USER_STATUSES))
        raise UserAccountValidationError(f"Unknown status: {normalized}; allowed: {allowed}")
    return normalized


def _require_compatible_status(row: UserORM, status: str) -> None:
    if status == USER_STATUS_SERVICE and not row.is_service_account:
        raise UserAccountValidationError("service status requires a service account user")
    if row.is_service_account and status == USER_STATUS_ACTIVE:
        raise UserAccountValidationError("service accounts must use service or disabled status")


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserAccountValidationError(f"{field_name} must not be blank")
    return normalized
