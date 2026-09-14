# PR #225 - Development Gateway Hardening - Changelog

## Added

- A Node-only Vite development gateway that injects the configured trusted
  principal only for exact allowlisted API route segments.
- Real HTTP adversarial coverage for hostile identity headers, ordinary and
  `Expect: 100-continue` requests, encoded path confusion, prefix/query/absolute
  request smuggling, Host/Origin misuse, and actual serve-versus-preview behavior.
- One shared exact-route contract consumed at runtime by the canonical browser
  API client and the Vite proxy. This is the security invariant: unlisted roots,
  encoded separators/hash bytes, ASCII controls, iterative traversal encodings,
  and untrusted absolute origins fail before transport. Literal fragments remain
  non-transport browser metadata.
- A conservative compiler-backed accidental-drift/change-control tripwire.
  Frontend source, build configuration, and pinned dependencies remain trusted;
  this gate is not an adversarial JavaScript sandbox or exhaustive behavior
  proof. It rejects raw transports, dynamic code/loading, client escapes,
  executable JS/JSX or Worker-query imports, unresolved Worker sources,
  extra/classic/inline HTML, and alternate Rollup, library, or SSR entries.
- Startup-fixture coverage distinguishing pre-allocation creation failure and
  proving allocated HTTP servers and file watchers close after listen or port-
  resolution failure.

## Changed

- The proxy fails before listening on blank identity/token configuration,
  contradictory global scope configuration, unsafe backend URL components, or
  an untrusted backend origin.
- The trusted proxy rechecks Vite's final resolved host after inline and CLI
  overrides, rejecting wildcard and non-loopback listeners before startup. It
  also rejects middleware mode because Vite cannot prove the outer listener is
  loopback-only.
- Loopback targets include canonical localhost, IPv6 loopback, and the IPv4
  `127.0.0.0/8` range. Non-loopback targets require HTTPS and an exact canonical
  origin allowlist match, including IDNA normalization.
- Gateway-controlled header roots and families are scrubbed before http-proxy
  copies request headers, with `proxyReq` replacement retained as defense in depth.
- The Google credential smoke runbook requires immutable numeric Secret Manager
  versions, audited credential registration before service-actor provisioning,
  and no public service-actor placeholder in `.env.example`.
- The Compose header now describes only the deployment assets present in this
  repository.

## Removed

- The mutable Secret Manager `latest` alias from operator registration examples.
- The active `.env.example` service-actor placeholder that Compose never forwards.
- Claims that this branch includes Helm or External Secrets Operator assets.

## Runtime and data impact

Trusted-header injection affects only Vite's local development server; build and
every preview mode remain proxy-free. The browser API client now enforces the
shared route contract in every build and rejects encoded path separators before
calling `fetch`. No backend schema, finance calculation, persisted JSON shape,
production authorization path, migration, or backfill changes.
