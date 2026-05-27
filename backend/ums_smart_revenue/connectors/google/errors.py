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


class ResolverAlreadyRegisteredError(GoogleConnectorError):
    def __init__(self, *, scheme: str) -> None:
        super().__init__(f"secret resolver already registered: {scheme}")
        self.scheme = scheme


class ConnectorAlreadyRegisteredError(GoogleConnectorError):
    def __init__(self, *, key: str) -> None:
        super().__init__(f"connector already registered: {key}")
        self.key = key


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


class BlobDownloadError(GoogleConnectorError):
    def __init__(self, *, storage_uri: str, inner: Exception) -> None:
        super().__init__(
            f"blob download failed for {storage_uri}: {type(inner).__name__}"
        )
        self.storage_uri = storage_uri
        self.inner = inner


class BlobStorageConfigurationError(GoogleConnectorError):
    def __init__(self, *, backend: str, detail: str) -> None:
        super().__init__(f"blob storage backend {backend!r} is invalid: {detail}")
        self.backend = backend
        self.detail = detail


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


class CredentialNotFoundError(GoogleConnectorError):
    def __init__(self, *, connector_key: str, account_id: str) -> None:
        super().__init__(f"no credential for {connector_key}/{account_id}")
        self.connector_key = connector_key
        self.account_id = account_id


class InactiveCredentialError(GoogleConnectorError):
    def __init__(self, *, credential_id: str, status: str) -> None:
        super().__init__(f"credential {credential_id} is {status}, not active")
        self.credential_id = credential_id
        self.status = status


class _GoogleApiHttpError(GoogleConnectorError):
    def __init__(self, *, method: str, url: str, status: int, attempts: int = 1) -> None:
        if attempts > 1:
            msg = f"{method} {url}: HTTP {status} after {attempts} attempts"
        else:
            msg = f"{method} {url}: HTTP {status}"
        super().__init__(msg)
        self.method = method
        self.url = url
        self.status = status
        self.attempts = attempts


class GoogleApiAuthError(_GoogleApiHttpError):
    pass


class GoogleApiClientError(_GoogleApiHttpError):
    pass


class GoogleApiRateLimitError(_GoogleApiHttpError):
    pass


class GoogleApiServerError(_GoogleApiHttpError):
    pass


class GoogleApiResponseError(GoogleConnectorError):
    def __init__(self, *, url: str, reason: str) -> None:
        super().__init__(f"{url}: response schema invalid ({reason})")
        self.url = url
        self.reason = reason


class UnsupportedReportTypeError(GoogleConnectorError):
    def __init__(self, *, report_type_id: str) -> None:
        super().__init__(f"report_type_id {report_type_id} not in supported set")
        self.report_type_id = report_type_id
