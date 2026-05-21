from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.reports.raw_files import (
    MAX_RAW_REPORT_FILE_PAGE_SIZE,
    RawReportFileConflictError,
    RawReportFileEntry,
    RawReportFileNotFoundError,
    RawReportFileValidationError,
    SqlAlchemyRawReportFileRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
OTHER_TENANT_UUID = UUID("00000000-0000-0000-0000-000000071999")
ACTOR_USER_ID = "00000000-0000-0000-0000-000000071001"


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    return Session(engine)


def register_defaults(
    repo: SqlAlchemyRawReportFileRepository,
    *,
    source: str = "youtube_reporting",
    report_type: str = "YOUTUBE_CMS_REVENUE",
    report_month: str = "2026-03",
    storage_uri: str = "s3://ums-raw-reports/youtube/2026-03/cms.csv",
    checksum: str = "sha256:83f8b7d92d8a",
    parse_status: str = "DOWNLOADED",
    actor_user_id: str = ACTOR_USER_ID,
) -> RawReportFileEntry:
    return repo.register_file(
        source=source,
        report_type=report_type,
        report_month=report_month,
        storage_uri=storage_uri,
        checksum=checksum,
        parse_status=parse_status,
        actor_user_id=actor_user_id,
    )


# -----------------------------------------------------------------------------
# RawReportFileEntry.to_api
# -----------------------------------------------------------------------------


def test_entry_to_api_serializes_every_field_with_iso_datetime():
    downloaded_at = datetime(2026, 3, 1, 12, 30, 45, tzinfo=UTC)
    entry = RawReportFileEntry(
        id="11111111-1111-1111-1111-111111111111",
        source="youtube_reporting",
        report_type="YOUTUBE_CMS_REVENUE",
        report_month="2026-03",
        storage_uri="s3://ums-raw-reports/youtube/2026-03/cms.csv",
        checksum="sha256:abcd",
        parse_status="DOWNLOADED",
        downloaded_by="22222222-2222-2222-2222-222222222222",
        downloaded_at=downloaded_at,
    )

    assert entry.to_api() == {
        "id": "11111111-1111-1111-1111-111111111111",
        "source": "youtube_reporting",
        "report_type": "YOUTUBE_CMS_REVENUE",
        "report_month": "2026-03",
        "storage_uri": "s3://ums-raw-reports/youtube/2026-03/cms.csv",
        "checksum": "sha256:abcd",
        "parse_status": "DOWNLOADED",
        "downloaded_by": "22222222-2222-2222-2222-222222222222",
        "downloaded_at": "2026-03-01T12:30:45+00:00",
    }


def test_entry_to_api_emits_null_for_missing_downloaded_by():
    entry = RawReportFileEntry(
        id="11111111-1111-1111-1111-111111111111",
        source="youtube_reporting",
        report_type="YOUTUBE_CMS_REVENUE",
        report_month="2026-03",
        storage_uri="s3://ums/x.csv",
        checksum="sha256:abcd",
        parse_status="DOWNLOADED",
        downloaded_by=None,
        downloaded_at=datetime(2026, 3, 1, tzinfo=UTC),
    )

    assert entry.to_api()["downloaded_by"] is None


# -----------------------------------------------------------------------------
# register_file — happy path
# -----------------------------------------------------------------------------


def test_register_file_stamps_tenant_and_persists_all_fields():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        entry = register_defaults(repo)
        session.commit()

        rows = session.scalars(select(RawReportFileORM)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == DEFAULT_TENANT_UUID
        assert row.source == "youtube_reporting"
        assert row.report_type == "YOUTUBE_CMS_REVENUE"
        assert row.report_month == "2026-03"
        assert row.file_url == "s3://ums-raw-reports/youtube/2026-03/cms.csv"
        assert row.checksum == "sha256:83f8b7d92d8a"
        assert row.parse_status == "DOWNLOADED"
        assert row.downloaded_by == UUID(ACTOR_USER_ID)
        assert row.id == UUID(entry.id)
        assert entry.storage_uri == row.file_url


def test_register_file_strips_whitespace_from_normalized_fields():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        entry = repo.register_file(
            source="  youtube_reporting  ",
            report_type="  YOUTUBE_CMS_REVENUE  ",
            report_month="2026-03",
            storage_uri="  s3://ums/x.csv  ",
            checksum="  sha256:abcd  ",
            parse_status="  DOWNLOADED  ",
            actor_user_id=ACTOR_USER_ID,
        )
        session.commit()

        assert entry.source == "youtube_reporting"
        assert entry.report_type == "YOUTUBE_CMS_REVENUE"
        assert entry.storage_uri == "s3://ums/x.csv"
        assert entry.checksum == "sha256:abcd"
        assert entry.parse_status == "DOWNLOADED"


def test_register_file_accepts_each_allowed_parse_status():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        for index, status in enumerate(
            ["DOWNLOADED", "PARSED", "FAILED", "QUARANTINED"]
        ):
            entry = register_defaults(
                repo, checksum=f"sha256:status-{index}", parse_status=status
            )
            assert entry.parse_status == status
        session.commit()
        assert len(session.scalars(select(RawReportFileORM)).all()) == 4


def test_register_file_accepts_each_allowed_storage_prefix():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        for index, prefix in enumerate(
            ["s3://", "gs://", "azure://", "blob://", "file-store://"]
        ):
            entry = register_defaults(
                repo,
                checksum=f"sha256:prefix-{index}",
                storage_uri=f"{prefix}bucket/key-{index}.csv",
            )
            assert entry.storage_uri.startswith(prefix)
        session.commit()


# -----------------------------------------------------------------------------
# register_file — duplicate detection
# -----------------------------------------------------------------------------


def test_register_file_raises_conflict_on_same_tenant_duplicate_key():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(repo)
        session.commit()

        with pytest.raises(RawReportFileConflictError):
            register_defaults(repo)


def test_register_file_with_same_key_under_different_tenants_does_not_conflict():
    with build_session() as session:
        primary_repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(primary_repo)
        session.commit()

        other_repo = SqlAlchemyRawReportFileRepository(session)
        other_repo._tenant_id = OTHER_TENANT_UUID  # noqa: SLF001
        register_defaults(other_repo)
        session.commit()

        rows = session.scalars(
            select(RawReportFileORM).order_by(RawReportFileORM.tenant_id)
        ).all()
        assert {row.tenant_id for row in rows} == {
            DEFAULT_TENANT_UUID,
            OTHER_TENANT_UUID,
        }
        assert len(rows) == 2


# -----------------------------------------------------------------------------
# register_file — input validation
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value,message_fragment",
    [
        ("source", "", "source must not be blank"),
        ("source", "   ", "source must not be blank"),
        ("report_type", "", "report_type must not be blank"),
        ("checksum", "", "checksum must not be blank"),
        ("parse_status", "UNKNOWN", "Unknown raw report parse_status"),
        ("parse_status", "", "parse_status must not be blank"),
        ("actor_user_id", "", "actor_user_id must not be blank"),
    ],
)
def test_register_file_rejects_bad_string_inputs(field, bad_value, message_fragment):
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        kwargs = {
            "source": "youtube_reporting",
            "report_type": "YOUTUBE_CMS_REVENUE",
            "report_month": "2026-03",
            "storage_uri": "s3://ums/x.csv",
            "checksum": "sha256:abcd",
            "parse_status": "DOWNLOADED",
            "actor_user_id": ACTOR_USER_ID,
            field: bad_value,
        }
        with pytest.raises(RawReportFileValidationError, match=message_fragment):
            repo.register_file(**kwargs)


@pytest.mark.parametrize("bad_month", ["2026-13", "26-03", "2026/03", "", "2026-00"])
def test_register_file_rejects_bad_report_month(bad_month):
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        with pytest.raises(RawReportFileValidationError, match="report_month must use"):
            register_defaults(repo, report_month=bad_month)


@pytest.mark.parametrize(
    "bad_uri",
    [
        "https://example.com/x.csv",
        "file:///etc/passwd",
        "",
        "  ",
        "ftp://server/x.csv",
        "data:text/plain;base64,SGk=",
    ],
)
def test_register_file_rejects_storage_uri_outside_allowlist(bad_uri):
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        with pytest.raises(RawReportFileValidationError):
            register_defaults(repo, storage_uri=bad_uri)


def test_register_file_maps_non_uuid_gateway_actor_user_id():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        registered = register_defaults(
            repo, actor_user_id="gateway-raw-files-subject"
        )

        assert registered.downloaded_by == str(
            actor_identity_uuid("gateway-raw-files-subject")
        )


# -----------------------------------------------------------------------------
# get_file
# -----------------------------------------------------------------------------


def test_get_file_returns_entry_for_valid_id():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        registered = register_defaults(repo)
        session.commit()

        fetched = repo.get_file(registered.id)
        assert fetched == registered


def test_get_file_raises_not_found_for_unknown_id():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(repo)
        session.commit()

        with pytest.raises(RawReportFileNotFoundError):
            repo.get_file(str(uuid4()))


def test_get_file_raises_validation_for_malformed_uuid():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(repo)
        session.commit()

        with pytest.raises(
            RawReportFileValidationError,
            match="raw_report_file_id must be a valid UUID",
        ):
            repo.get_file("not-a-uuid")


def test_get_file_does_not_surface_other_tenants_row_via_id_lookup():
    with build_session() as session:
        other_repo = SqlAlchemyRawReportFileRepository(session)
        other_repo._tenant_id = OTHER_TENANT_UUID  # noqa: SLF001
        foreign = register_defaults(other_repo, checksum="sha256:foreign")
        session.commit()

        primary_repo = SqlAlchemyRawReportFileRepository(session)
        with pytest.raises(RawReportFileNotFoundError):
            primary_repo.get_file(foreign.id)


# -----------------------------------------------------------------------------
# list_files
# -----------------------------------------------------------------------------


def test_list_files_returns_all_tenant_rows_with_default_paging():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(repo, checksum="sha256:first")
        register_defaults(repo, checksum="sha256:second")
        register_defaults(repo, checksum="sha256:third")
        session.commit()

        page = repo.list_files()
        assert {entry.checksum for entry in page.items} == {
            "sha256:first",
            "sha256:second",
            "sha256:third",
        }
        assert page.limit == 50
        assert page.offset == 0
        assert page.has_more is False


def test_list_files_orders_by_downloaded_at_desc_then_id_desc():
    with build_session() as session:
        older = RawReportFileORM(
            id=UUID("00000000-0000-0000-0000-000000aa0001"),
            tenant_id=DEFAULT_TENANT_UUID,
            source="youtube_reporting",
            report_type="YOUTUBE_CMS_REVENUE",
            report_month="2026-03",
            file_url="s3://ums/older.csv",
            checksum="sha256:older",
            parse_status="DOWNLOADED",
            downloaded_by=UUID(ACTOR_USER_ID),
            downloaded_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
        )
        newer = RawReportFileORM(
            id=UUID("00000000-0000-0000-0000-000000aa0002"),
            tenant_id=DEFAULT_TENANT_UUID,
            source="youtube_reporting",
            report_type="YOUTUBE_CMS_REVENUE",
            report_month="2026-03",
            file_url="s3://ums/newer.csv",
            checksum="sha256:newer",
            parse_status="DOWNLOADED",
            downloaded_by=UUID(ACTOR_USER_ID),
            downloaded_at=datetime(2026, 3, 5, 9, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 3, 5, 9, 0, 0, tzinfo=UTC),
        )
        same_time_higher_id = RawReportFileORM(
            id=UUID("00000000-0000-0000-0000-000000aa0003"),
            tenant_id=DEFAULT_TENANT_UUID,
            source="youtube_reporting",
            report_type="YOUTUBE_CMS_REVENUE",
            report_month="2026-03",
            file_url="s3://ums/same.csv",
            checksum="sha256:same",
            parse_status="DOWNLOADED",
            downloaded_by=UUID(ACTOR_USER_ID),
            downloaded_at=datetime(2026, 3, 5, 9, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 3, 5, 9, 0, 0, tzinfo=UTC),
        )
        session.add_all([older, newer, same_time_higher_id])
        session.commit()

        repo = SqlAlchemyRawReportFileRepository(session)
        page = repo.list_files()
        assert [entry.checksum for entry in page.items] == [
            "sha256:same",
            "sha256:newer",
            "sha256:older",
        ]


def test_list_files_filters_by_source_report_type_and_month():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(
            repo,
            source="youtube_reporting",
            report_type="YOUTUBE_CMS_REVENUE",
            report_month="2026-03",
            checksum="sha256:a",
        )
        register_defaults(
            repo,
            source="youtube_reporting",
            report_type="YOUTUBE_ANALYTICS",
            report_month="2026-03",
            checksum="sha256:b",
        )
        register_defaults(
            repo,
            source="adsense",
            report_type="YOUTUBE_CMS_REVENUE",
            report_month="2026-03",
            checksum="sha256:c",
        )
        register_defaults(
            repo,
            source="youtube_reporting",
            report_type="YOUTUBE_CMS_REVENUE",
            report_month="2026-04",
            checksum="sha256:d",
        )
        session.commit()

        only_cms = repo.list_files(
            source="youtube_reporting", report_type="YOUTUBE_CMS_REVENUE"
        )
        assert {entry.checksum for entry in only_cms.items} == {"sha256:a", "sha256:d"}

        only_march = repo.list_files(report_month="2026-03")
        assert {entry.checksum for entry in only_march.items} == {
            "sha256:a",
            "sha256:b",
            "sha256:c",
        }

        only_adsense = repo.list_files(source="adsense")
        assert {entry.checksum for entry in only_adsense.items} == {"sha256:c"}


def test_list_files_paginates_with_has_more_flag():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        for index in range(5):
            register_defaults(repo, checksum=f"sha256:{index}")
        session.commit()

        first_page = repo.list_files(limit=2, offset=0)
        assert len(first_page.items) == 2
        assert first_page.has_more is True
        assert first_page.limit == 2
        assert first_page.offset == 0

        second_page = repo.list_files(limit=2, offset=2)
        assert len(second_page.items) == 2
        assert second_page.has_more is True

        third_page = repo.list_files(limit=2, offset=4)
        assert len(third_page.items) == 1
        assert third_page.has_more is False
        assert third_page.offset == 4


def test_list_files_rejects_bad_limit_and_offset():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        with pytest.raises(RawReportFileValidationError, match="limit must be between"):
            repo.list_files(limit=0)
        with pytest.raises(RawReportFileValidationError, match="limit must be between"):
            repo.list_files(limit=MAX_RAW_REPORT_FILE_PAGE_SIZE + 1)
        with pytest.raises(
            RawReportFileValidationError, match="offset must be greater than or equal"
        ):
            repo.list_files(offset=-1)


def test_list_files_validates_filter_month_format():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        with pytest.raises(RawReportFileValidationError, match="report_month must use"):
            repo.list_files(report_month="2026-13")


def test_list_files_does_not_surface_cross_tenant_rows():
    with build_session() as session:
        primary_repo = SqlAlchemyRawReportFileRepository(session)
        register_defaults(primary_repo, checksum="sha256:primary-1")
        register_defaults(primary_repo, checksum="sha256:primary-2")
        session.commit()

        other_repo = SqlAlchemyRawReportFileRepository(session)
        other_repo._tenant_id = OTHER_TENANT_UUID  # noqa: SLF001
        register_defaults(other_repo, checksum="sha256:foreign-1")
        register_defaults(other_repo, checksum="sha256:foreign-2")
        register_defaults(other_repo, checksum="sha256:foreign-3")
        session.commit()

        primary_page = primary_repo.list_files()
        assert {entry.checksum for entry in primary_page.items} == {
            "sha256:primary-1",
            "sha256:primary-2",
        }

        other_page = other_repo.list_files()
        assert {entry.checksum for entry in other_page.items} == {
            "sha256:foreign-1",
            "sha256:foreign-2",
            "sha256:foreign-3",
        }


def test_list_files_returns_empty_page_when_no_rows_match():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        page = repo.list_files()
        assert page.items == []
        assert page.has_more is False
        assert page.limit == 50
        assert page.offset == 0


# -----------------------------------------------------------------------------
# repository defaults
# -----------------------------------------------------------------------------


def test_repository_default_tenant_id_matches_constant():
    with build_session() as session:
        repo = SqlAlchemyRawReportFileRepository(session)
        assert repo._tenant_id == DEFAULT_TENANT_UUID  # noqa: SLF001
