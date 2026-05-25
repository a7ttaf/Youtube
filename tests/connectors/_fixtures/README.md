# Fixture provenance

Every payload in this tree is **synthetic**. None of the account IDs, channel IDs, content owner IDs, OAuth tokens, or money figures correspond to real Google/YouTube/AdSense data.

Naming convention: `sample_<report_type>_<YYYY_MM>.json` plus a `_rerun.json` sibling that is byte-identical to the first file (used to assert parser/repository idempotency).

Each fixture mirrors the structural shape Google publicly documents for that report type, with field names matching the upstream API, but the values are invented for B1 testing. Channel IDs follow the `UC_test_<n>` convention. Account IDs follow the `pub-test-<n>` convention for AdSense and `cms-test-<n>` for content owners. Money values are small decimal amounts (e.g. `123.456789`) chosen to verify Decimal preservation, not to reflect production amounts.

Do not replace any fixture with real data without operator approval and a separate audit-logged commit.
