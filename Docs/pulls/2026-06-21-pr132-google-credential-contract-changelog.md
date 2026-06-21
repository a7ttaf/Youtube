# PR #132 - Google Credential Contract Reconciliation - Changelog

## Added

- Google credential contract section in `Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md`.
- Standard PR report, changelog, and handoff artifacts for this documentation
  and wording cleanup.

## Changed

- Replaced active-doc credential blockers framed only as OAuth with
  owner-approved Google connector credential material.
- Clarified that API-key-only access is limited to Google-permitted YouTube Data
  API public metadata.
- Clarified that YouTube Reporting, YouTube Analytics private revenue/account
  data, and AdSense account/payment data require official Google authorization
  tokens/scopes.
- Reworded Connector Admin descriptions and one connector probe error detail to
  avoid implying direct Gmail account linking.

## Removed

Nothing.

## Runtime impact

No control-flow, schema, permission, or token-refresh path changed. The backend
still catches `OAuthRefreshError` for the existing Google token-refresh path.
However, two API-visible / admin-facing string values did change and are called
out explicitly so release notes are accurate:

- The `POST /connectors/credentials/{key}/{id}/test` 200 response `detail` for
  the `auth_failed` (`OAuthRefreshError`) case is now the canned message
  `"Google credential token refresh failed; check that the credential secret is current."`
  (previously the OAuth-prefixed wording). The status code (`200`) and
  machine-readable `status: "auth_failed"` field are unchanged.
- The `Connector Admin` role description (admin metadata surfaced via the roles
  API and seeded by `security_seed.sql`) is now `"Technical owner for Google/API
  connector credential configuration."` Existing seeded databases keep the prior
  description until the seed is rerun; this is label metadata only.
