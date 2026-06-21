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

No runtime behavior changed. The backend still catches `OAuthRefreshError` for
the existing Google token-refresh path; this PR changes only operator-facing
wording and active documentation contracts.
