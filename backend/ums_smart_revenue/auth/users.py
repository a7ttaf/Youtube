import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import UserORM

logger = logging.getLogger(__name__)

USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"
USER_STATUS_SERVICE = "service"
USER_STATUSES = frozenset(
    {USER_STATUS_ACTIVE, USER_STATUS_DISABLED, USER_STATUS_SERVICE}
)
USER_EMAIL_MAX_LENGTH = 320
USER_DISPLAY_NAME_MAX_LENGTH = 200
_EMAIL_CONFLICT_SAMPLE_LIMIT = 2


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
        normalized_display_name = _normalize_bounded_string(
            display_name,
            "display_name",
            max_length=USER_DISPLAY_NAME_MAX_LENGTH,
        )
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
            if self._email_exists(normalized_email, excluding_user_id=user_uuid):
                raise UserAccountConflictError("User email already exists")
            row.email = normalized_email

        if display_name is not None:
            row.display_name = _normalize_bounded_string(
                display_name,
                "display_name",
                max_length=USER_DISPLAY_NAME_MAX_LENGTH,
            )

        if status is not None:
            normalized_status = _normalize_status(status)
            _require_compatible_status(row, normalized_status)
            row.status = normalized_status

        try:
            self._session.flush()
        except IntegrityError as exc:
            if email is not None and self._email_exists(
                _normalize_email(email), excluding_user_id=user_uuid
            ):
                raise UserAccountConflictError("User email already exists") from exc
            raise
        return self._to_entry(row)

    def _email_exists(
        self, email: str, *, excluding_user_id: UUID | None = None
    ) -> bool:
        criteria = [func.lower(UserORM.email) == email]
        if excluding_user_id is not None:
            criteria.append(UserORM.id != excluding_user_id)
        conflicts = self._session.scalars(
            select(UserORM.id).where(*criteria).limit(_EMAIL_CONFLICT_SAMPLE_LIMIT)
        ).all()
        if len(conflicts) > 1:
            logger.warning("Multiple existing users matched a normalized email lookup")
        return bool(conflicts)

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
    normalized = _normalize_bounded_string(
        value, "email", max_length=USER_EMAIL_MAX_LENGTH
    ).lower()
    if normalized.count("@") != 1:
        raise UserAccountValidationError("email must be a valid email address")
    local, domain = normalized.split("@", maxsplit=1)
    if (
        not local
        or not domain
        or any(character.isspace() for character in normalized)
        or domain.startswith(".")
        or domain.endswith(".")
        or "." not in domain
        or any(part == "" for part in domain.split("."))
    ):
        raise UserAccountValidationError("email must be a valid email address")
    return normalized


def _normalize_status(value: str) -> str:
    normalized = _normalize_required_string(value, "status").lower()
    if normalized not in USER_STATUSES:
        allowed = ", ".join(sorted(USER_STATUSES))
        raise UserAccountValidationError(
            f"Unknown status: {normalized}; allowed: {allowed}"
        )
    return normalized


def _require_compatible_status(row: UserORM, status: str) -> None:
    if status == USER_STATUS_SERVICE and not row.is_service_account:
        raise UserAccountValidationError(
            "service status requires a service account user"
        )
    if row.is_service_account and status == USER_STATUS_ACTIVE:
        raise UserAccountValidationError(
            "service accounts must use service or disabled status"
        )


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserAccountValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_bounded_string(value: str, field_name: str, *, max_length: int) -> str:
    normalized = _normalize_required_string(value, field_name)
    if len(normalized) > max_length:
        raise UserAccountValidationError(
            f"{field_name} must be at most {max_length} characters"
        )
    return normalized
