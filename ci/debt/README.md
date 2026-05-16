# Known Debt Registry

`known-failures.yml` tracks pre-existing issues that are temporarily tolerated by the local CI gate.

Rules:
- Every debt item must have an owner, reason, and expiry date.
- Debt must not silently increase.
- Expired debt entries fail the CI gate.
- Remove entries once fixed.
- Required fields for each active entry: `id`, `type`, `command`, `owner`, `reason`, `allowed_until`, `signatures`.
- `signatures` is a `string[]` of identifying patterns used to match the known debt (for example regex-like code tokens, stable error substrings, or unique IDs/hashes). Example: `["ASYNC240"]` matches current output lines containing `ASYNC240` so the ratchet can detect increases or new codes.
- Optional ratchet fields: `must_not_increase`, `expected_count` (only needed when ratchet constraints are enabled). `expected_count` is a total-occurrence baseline (not per-line unique count). Example: if one log line contains `ASYNC240` twice, the counted value is `2`.

If there is no active debt, keep:

```yaml
version: 1
known_failures: []
```
