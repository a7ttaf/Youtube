# PR #225 - Development Gateway Hardening - Changelog

## Added

- A Node-only Vite development gateway that injects the configured trusted
  principal only for exact allowlisted API route segments.
- Real HTTP adversarial coverage for hostile identity headers, ordinary and
  `Expect: 100-continue` requests, encoded path confusion, prefix/query/absolute
  request smuggling, Host/Origin misuse, and actual serve-versus-preview behavior.
- Compiler-AST route discovery with a deliberately narrow syntax contract:
  canonical `useApiClient()` results remain direct immutable bindings, calls
  remain on the six audited methods, and unsupported aliases, wrappers,
  containers, assignments, destructuring, constructors, getters, callbacks,
  or method escapes fail closed.
- Completeness tripwires for raw `fetch` references outside the audited client,
  future API-client method additions, `.mts`/`.cts` application files, and
  compiler-loaded frontend imports outside `src`.
- Startup-fixture coverage distinguishing pre-allocation creation failure and
  proving allocated HTTP servers and file watchers close after listen or port-
  resolution failure.

## Changed

- The proxy fails before listening on blank identity/token configuration,
  contradictory global scope configuration, unsafe backend URL components, or
  an untrusted backend origin.
- The trusted proxy rechecks Vite's final resolved host after inline and CLI
  overrides, rejecting wildcard and non-loopback listeners before startup.
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

The gateway changes affect only Vite's local development server. Build and every
preview mode remain proxy-free. No backend schema, finance calculation, persisted
JSON shape, production authorization path, migration, or backfill changes.
