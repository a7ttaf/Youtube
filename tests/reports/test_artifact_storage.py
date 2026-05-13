import hashlib
from uuid import uuid4

import pytest

from ums_smart_revenue.reports.artifact_storage import (
    ExportArtifactStorageError,
    FileSystemExportArtifactStore,
)


def test_file_system_export_artifact_store_persists_bytes(tmp_path):
    export_id = str(uuid4())
    content = b"export-bytes"
    store = FileSystemExportArtifactStore(tmp_path)

    artifact = store.save(
        export_id=export_id,
        filename="finance.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=content,
    )

    assert artifact.file_url == f"file-store://exports/{export_id}/finance.xlsx"
    assert artifact.filename == "finance.xlsx"
    assert artifact.byte_size == len(content)
    assert artifact.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert (tmp_path / "exports" / export_id / "finance.xlsx").read_bytes() == content


def test_file_system_export_artifact_store_rejects_unsafe_filename(tmp_path):
    store = FileSystemExportArtifactStore(tmp_path)

    with pytest.raises(ExportArtifactStorageError):
        store.save(
            export_id=str(uuid4()),
            filename="../finance.xlsx",
            content_type="application/octet-stream",
            content=b"export-bytes",
        )
