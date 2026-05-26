"""Typed error hierarchy for the B2 live Google connector.

Every error raised inside connectors/google or connectors/runs subclasses
GoogleConnectorError so the orchestrator's outer handler can catch the whole
family in one except clause and translate it into a connector_runs FAILED
row plus an audit event with error_class=<class name>.
"""
from __future__ import annotations


class GoogleConnectorError(Exception):
    """Root of all B2 typed errors."""


class UnsupportedSecretSchemeError(GoogleConnectorError):
    def __init__(self, *, scheme: str) -> None:
        super().__init__(f"unsupported secret scheme: {scheme}")
        self.scheme = scheme


class MalformedSecretUriError(GoogleConnectorError):
    def __init__(self, *, ref: str) -> None:
        super().__init__(f"malformed secret URI: {ref}")
        self.ref = ref


class SecretNotFoundError(GoogleConnectorError):
    def __init__(self, *, ref: str) -> None:
        super().__init__(f"secret not found: {ref}")
        self.ref = ref


class SecretFetchError(GoogleConnectorError):
    def __init__(self, *, ref: str, inner: Exception) -> None:
        super().__init__(f"secret fetch failed for {ref}: {type(inner).__name__}")
        self.ref = ref
        self.inner = inner


class MalformedSecretPayloadError(GoogleConnectorError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(f"malformed secret payload: {detail}")
        self.detail = detail


class OAuthRefreshError(GoogleConnectorError):
    def __init__(self, *, inner: Exception) -> None:
        super().__init__(f"oauth refresh failed: {type(inner).__name__}")
        self.inner = inner


class BlobUploadError(GoogleConnectorError):
    def __init__(self, *, storage_uri: str, inner: Exception) -> None:
        super().__init__(
            f"blob upload failed for {storage_uri}: {type(inner).__name__}"
        )
        self.storage_uri = storage_uri
        self.inner = inner


class BlobChecksumMismatchError(GoogleConnectorError):
    def __init__(self, *, storage_uri: str, computed: str, read: str) -> None:
        super().__init__(
            f"checksum mismatch at {storage_uri}: computed={computed} read={read}"
        )
        self.storage_uri = storage_uri
        self.computed = computed
        self.read = read


class RawFileLifecycleError(GoogleConnectorError):
    def __init__(self, *, raw_file_id: str, current: str, target: str) -> None:
        super().__init__(
            f"illegal raw_file lifecycle transition for {raw_file_id}: "
            f"{current} -> {target}"
        )
        self.raw_file_id = raw_file_id
        self.current = current
        self.target = target


class RawFileAlreadyParsedError(GoogleConnectorError):
    def __init__(self, *, raw_file_id: str) -> None:
        super().__init__(f"raw_file already parsed: {raw_file_id}")
        self.raw_file_id = raw_file_id
