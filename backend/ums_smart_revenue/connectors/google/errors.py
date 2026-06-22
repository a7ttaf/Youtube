"""Typed error hierarchy for the B2 live Google connector.

Every error raised inside connectors/google or connectors/runs subclasses
GoogleConnectorError so the orchestrator's outer handler can catch the whole
family in one except clause and translate it into a connector_runs FAILED
row plus an audit event with error_class=<class name>.
"""

from __future__ import annotations

from uuid import UUID


class GoogleConnectorError(Exception):
    """Root of all B2 typed errors."""


class ConnectorServicePrincipalUnavailableError(GoogleConnectorError):
    """Raised when the connector service actor is required but not provisioned.

    The ``build_connector_service_principal`` call inside the live run path
    fails closed with ``ValueError`` if ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID``
    is unset; the orchestrator converts that to this typed exception so the
    executor's ``except GoogleConnectorError`` branch treats it as a Bucket-A
    pre-start failure and writes a ``job_failed_before_start`` audit row
    (otherwise the ValueError would be swallowed by the catch-all log-only
    branch and operators would see a 202 with no run row, no failure audit,
    and no reason the job never started).

    Subclassing ``GoogleConnectorError`` keeps the error in the family the
    orchestrator already knows how to translate; subclassing is preferred
    over a stand-alone Exception so future Bucket-A additions (catching by
    the parent class) continue to handle this path.
    """

    def __init__(self, *, env_var: str) -> None:
        """Carry the offending ``env_var`` for downstream diagnostics."""
        super().__init__(
            f"connector service principal is unavailable; set {env_var} "
            "to a UUID before running connector jobs"
        )
        self.env_var = env_var


class UnsupportedSecretSchemeError(GoogleConnectorError):
    """Raised when a credential resolver is asked to handle an unknown scheme."""

    def __init__(self, *, scheme: str) -> None:
        """Carry the offending ``scheme`` for downstream diagnostics."""
        super().__init__(f"unsupported secret scheme: {scheme}")
        self.scheme = scheme


class ResolverAlreadyRegisteredError(GoogleConnectorError):
    """Raised when two resolvers race to register the same secret scheme."""

    def __init__(self, *, scheme: str) -> None:
        """Carry the duplicate ``scheme`` for downstream diagnostics."""
        super().__init__(f"secret resolver already registered: {scheme}")
        self.scheme = scheme


class ConnectorAlreadyRegisteredError(GoogleConnectorError):
    """Raised when two adapters race to register the same connector key."""

    def __init__(self, *, key: str) -> None:
        """Carry the duplicate connector ``key`` for downstream diagnostics."""
        super().__init__(f"connector already registered: {key}")
        self.key = key


def _redacted_secret_locator(ref: str) -> str:
    """Return a scheme-only locator (no path/version) safe to log for secret refs."""
    scheme, _, _rest = ref.partition("://")
    return f"{scheme or 'unknown'} secret"


class MalformedSecretUriError(GoogleConnectorError):
    """Raised when a credential row's secret URI cannot be parsed."""

    def __init__(self, *, ref: str) -> None:
        """Carry the redacted ``ref`` for downstream diagnostics (resource name elided)."""
        super().__init__(f"malformed secret URI: {_redacted_secret_locator(ref)}")
        self.ref = ref


class SecretNotFoundError(GoogleConnectorError):
    """Raised when the secret backend returns 404 / NOT_FOUND for a credential."""

    def __init__(self, *, ref: str) -> None:
        """Carry the redacted ``ref`` for downstream diagnostics."""
        super().__init__(f"secret not found: {_redacted_secret_locator(ref)}")
        self.ref = ref


class SecretFetchError(GoogleConnectorError):
    """Raised when the secret backend returns a non-404 error during fetch."""

    def __init__(self, *, ref: str, inner: Exception) -> None:
        """Carry the redacted ``ref`` and the original ``inner`` exception."""
        super().__init__(
            f"secret fetch failed for {_redacted_secret_locator(ref)}: {type(inner).__name__}"
        )
        self.ref = ref
        self.inner = inner


class MalformedSecretPayloadError(GoogleConnectorError):
    """Raised when a fetched secret cannot be parsed into the expected schema."""

    def __init__(self, *, detail: str) -> None:
        """Carry the schema-violation ``detail`` for downstream diagnostics."""
        super().__init__(f"malformed secret payload: {detail}")
        self.detail = detail


class OAuthRefreshError(GoogleConnectorError):
    """Raised when refreshing an OAuth access token fails irrecoverably."""

    def __init__(self, *, inner: Exception) -> None:
        """Carry the original ``inner`` exception (e.g. revoked grant)."""
        super().__init__(f"oauth refresh failed: {type(inner).__name__}")
        self.inner = inner


class BlobUploadError(GoogleConnectorError):
    """Raised when writing a raw report payload to blob storage fails."""

    def __init__(self, *, storage_uri: str, inner: Exception) -> None:
        """Carry the target ``storage_uri`` and the original ``inner`` exception."""
        super().__init__(f"blob upload failed for {storage_uri}: {type(inner).__name__}")
        self.storage_uri = storage_uri
        self.inner = inner


class BlobDownloadError(GoogleConnectorError):
    """Raised when reading a raw report payload back from blob storage fails."""

    def __init__(self, *, storage_uri: str, inner: Exception) -> None:
        """Carry the source ``storage_uri`` and the original ``inner`` exception."""
        super().__init__(f"blob download failed for {storage_uri}: {type(inner).__name__}")
        self.storage_uri = storage_uri
        self.inner = inner


class BlobStorageConfigurationError(GoogleConnectorError):
    """Raised when a configured blob backend's identity/credentials are invalid."""

    def __init__(self, *, backend: str, detail: str) -> None:
        """Carry the ``backend`` name and a configuration-issue ``detail``."""
        super().__init__(f"blob storage backend {backend!r} is invalid: {detail}")
        self.backend = backend
        self.detail = detail


class BlobChecksumMismatchError(GoogleConnectorError):
    """Raised when a downloaded blob's checksum does not match the recorded one."""

    def __init__(self, *, storage_uri: str, computed: str, read: str) -> None:
        """Carry the ``storage_uri`` and both ``computed``/``read`` digests."""
        super().__init__(f"checksum mismatch at {storage_uri}: computed={computed} read={read}")
        self.storage_uri = storage_uri
        self.computed = computed
        self.read = read


class RawFileLifecycleError(GoogleConnectorError):
    """Raised when a raw_file's status would transition outside the allowed graph."""

    def __init__(self, *, raw_file_id: str, current: str, target: str) -> None:
        """Carry the ``raw_file_id`` and the illegal ``current`` → ``target`` pair."""
        super().__init__(
            f"illegal raw_file lifecycle transition for {raw_file_id}: {current} -> {target}"
        )
        self.raw_file_id = raw_file_id
        self.current = current
        self.target = target


class RawFileAlreadyParsedError(GoogleConnectorError):
    """Raised when a parser is asked to reparse a raw_file already in PARSED."""

    def __init__(self, *, raw_file_id: str) -> None:
        """Carry the offending ``raw_file_id`` for downstream diagnostics."""
        super().__init__(f"raw_file already parsed: {raw_file_id}")
        self.raw_file_id = raw_file_id


class CredentialNotFoundError(GoogleConnectorError):
    """Raised when no credential row matches the requested connector + account."""

    def __init__(self, *, connector_key: str, account_id: str) -> None:
        """Carry the looked-up ``connector_key`` and ``account_id``."""
        super().__init__(f"no credential for {connector_key}/{account_id}")
        self.connector_key = connector_key
        self.account_id = account_id


class CredentialSmokeRequiredError(GoogleConnectorError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class TenantLifecycleError(GoogleConnectorError):
    """Raised when a connector run targets a tenant in a non-ACTIVE lifecycle.

    Connector runs (CLI / future executor) run outside the FastAPI request
    path that ``TenantResolverMiddleware`` guards, so the lifecycle check
    the resolver enforces (ACTIVE only; SUSPENDED -> 423, ARCHIVED -> 410)
    must be reapplied at the connector entry point. Otherwise a UUID that
    points at a suspended/archived tenant would still authorize the run
    on the tenant lane and read/mutate the suspended tenant's rows.
    """

    def __init__(self, *, tenant_id: UUID, status: str | None) -> None:
        """Carry the looked-up ``tenant_id`` and the offending ``status``.

        ``status`` is ``None`` when the tenant row was not found at all
        (the ``tenants`` table returned no row for the supplied id).
        """
        if status is None:
            super().__init__(f"tenant {tenant_id} not found")
        else:
            super().__init__(f"tenant {tenant_id} is {status}; connector runs require ACTIVE")
        self.tenant_id = tenant_id
        self.status = status


class InactiveCredentialError(GoogleConnectorError):
    """Raised when a matched credential row is not in the ``ACTIVE`` status."""

    def __init__(self, *, credential_id: str, status: str) -> None:
        """Carry the ``credential_id`` and the non-active ``status``."""
        super().__init__(f"credential {credential_id} is {status}, not active")
        self.credential_id = credential_id
        self.status = status


class _GoogleApiHttpError(GoogleConnectorError):
    """Base for all Google HTTP failures; carries request shape + retry count."""

    def __init__(self, *, method: str, url: str, status: int, attempts: int = 1) -> None:
        """Carry the failing ``method``/``url``/``status`` and the retry count."""
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
    """Raised on 401/403 from a Google API after authenticated retries."""


class GoogleApiClientError(_GoogleApiHttpError):
    """Raised on a 4xx (excluding 401/403/429) from a Google API."""


class GoogleApiRateLimitError(_GoogleApiHttpError):
    """Raised when a Google API keeps returning 429 across the retry budget."""


class GoogleApiServerError(_GoogleApiHttpError):
    """Raised when a Google API keeps returning 5xx across the retry budget."""


class GoogleApiResponseError(GoogleConnectorError):
    """Raised when a 2xx Google response body fails schema validation."""

    def __init__(self, *, url: str, reason: str) -> None:
        """Carry the ``url`` and a free-text ``reason`` describing the schema gap."""
        super().__init__(f"{url}: response schema invalid ({reason})")
        self.url = url
        self.reason = reason


class UnsupportedReportTypeError(GoogleConnectorError):
    """Raised when the Reporting API returns a report_type not on the allowlist."""

    def __init__(self, *, report_type_id: str) -> None:
        """Carry the offending ``report_type_id`` for downstream diagnostics."""
        super().__init__(f"report_type_id {report_type_id} not in supported set")
        self.report_type_id = report_type_id


class MalformedReportMonthError(GoogleConnectorError):
    """Raised when a report_month input is not a valid `YYYY-MM` string."""

    def __init__(self, *, report_month: str) -> None:
        """Carry the offending ``report_month`` for downstream diagnostics."""
        super().__init__(
            f"report_month {report_month!r} must be YYYY-MM with a calendar month 01-12"
        )
        self.report_month = report_month


class MalformedAdsenseAccountIdError(GoogleConnectorError):
    """Raised when an AdSense account id is empty or whitespace-padded."""

    def __init__(self, *, account_id: str) -> None:
        """Carry the offending ``account_id`` for downstream diagnostics."""
        super().__init__(
            f"adsense account_id must be a non-empty string without surrounding "
            f"whitespace, got {account_id!r}"
        )
        self.account_id = account_id


class MalformedAnalyticsSelectorError(GoogleConnectorError):
    """Raised when an analytics selector id is empty / whitespace-only.

    Lets ``_build_query_request`` fail closed before constructing an invalid
    ``ids=contentOwner==`` / ``filters=channel==`` string that the YouTube
    Analytics API would otherwise reject with an opaque 400 (or worse, silently
    persist a row whose ``source_account_id`` cannot be tied back to a real
    owner or channel).
    """

    def __init__(self, *, field_name: str, value: str) -> None:
        """Carry the offending ``field_name`` / ``value`` for downstream diagnostics."""
        super().__init__(f"{field_name} must be a non-empty string, got {value!r}")
        self.field_name = field_name
        self.value = value
