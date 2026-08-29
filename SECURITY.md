# Security Policy

We take the security of the UMS Smart Revenue Control Center seriously. The system handles financial data for a portfolio of YouTube channels — confidentiality, integrity, and auditability are non-negotiable.

## Supported Versions

| Version | Supported |
|---|---|
| `main` (pre-1.0) | ✅ Security fixes accepted |
| Tagged releases | ✅ Latest minor + one previous minor receive security fixes |
| Older releases | ❌ Upgrade required |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security problems.**

### How to report

Open a private GitHub security advisory for this repository when the "Report a vulnerability" flow is available. If the UI is unavailable before CODEOWNERS teams are replaced with live org handles, contact the repository owner `@XGenerationy` through GitHub and request a private encrypted intake channel before sharing exploit details. Maintainers monitor vulnerability-intake notifications on an ongoing basis to meet the response SLAs below.

Include:

- A description of the issue and the impact you've assessed.
- Steps to reproduce — minimal, deterministic, with example payloads if applicable.
- Affected versions, deployments, or branches.
- Your name and contact for follow-up. Anonymous reports are accepted but slow down clarification.

For encrypted reports, ask the responding maintainer for their current PGP fingerprint inside the private advisory or maintainer channel before sending credentials, tokens, customer data, or proof-of-concept material.

### Our commitments

| Stage | SLA |
|---|---|
| Initial acknowledgment | 2 business days |
| Triage decision (accept / decline / need-more-info) | 5 business days |
| Fix for **Critical / High** severity | 14 calendar days target |
| Fix for **Medium** severity | 60 calendar days target |
| Fix for **Low** severity | next scheduled release |
| Public disclosure | coordinated with reporter after a patch is available |

Severity is judged by our maintainers against CVSS 3.1, weighted toward real-world exploitability in the deployed configuration.

### Scope

In scope:

- The backend service (`backend/ums_smart_revenue/`).
- The frontend (`frontend/`).
- Deployment assets: `Dockerfile` and `docker-compose.yml`. *(This list previously
  named `deploy/helm/`. That directory has never existed in this repository —
  `git log --all -- deploy` returns zero commits — so the reference has been removed
  rather than left implying a deployment surface that could be reviewed.)*
- Operational scripts under `scripts/`, including the backup and restore CLIs.
- Spec documents that describe authorization, audit, or data handling.

Out of scope:

- Third-party dependencies — report them through this policy as well; we triage deployed impact, coordinate upstream disclosure when needed, and track mitigation via Dependabot + `pip-audit` + Trivy.
- Social-engineering attacks against staff.
- Denial-of-service requiring sustained traffic the service is not provisioned to receive.
- Issues in unsupported branches.

### Safe-harbour

Good-faith research conducted under this policy will not be subject to legal action by the maintainers. **Stop, document, and report** as soon as you confirm a vulnerability — do not exfiltrate financial data or pivot beyond proof-of-concept.

## Hardening posture

**Implemented in this repository, today:**

- Role-based access control with 16 roles + 26 permissions; scope containment (channel / company / sector / finance-month / connector / global).
- 30 audited event types; sensitive payload masking unless `audit.view_sensitive_payloads` is granted.
- Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` on the tenant-scoped tables, enforced through the `app_tenant` / `app_platform` roles.
- Postgres SERIALIZABLE isolation for principal reads; retryable failure handling; 256-role / 512-grant caps.
- Locked-month immutability; manual overrides require a different-person approver.
- No secrets in the repository, and no secret material in the database — connector credentials are stored as *references only*.

**Not implemented — aspirational, and listed here so nobody assumes otherwise:**

- **Secrets via HashiCorp Vault / External Secrets Operator.** Neither is wired up. Secrets come from the operator's untracked `.env` (compose) or the process environment. The only implemented connector-secret backend is GCP Secret Manager; every other accepted URI scheme fails closed.
- **TLS 1.3 on ingress; encrypted PV; KMS-wrapped secrets.** There is no ingress. The single deployment shape is `docker compose` on one machine with every published port bound to `127.0.0.1`, and there is no Helm chart or `deploy/` directory to attach these controls to.
- **An authentication front door.** UMS has no login of its own — no password, session, cookie, or token login exists. Identity arrives as gateway-asserted headers behind one shared secret, and the compose stack ships no gateway. In the default `headers` authz mode the caller's `X-Role` *is* their role. This is acceptable only for the single-operator localhost beta it is scoped to, and only because of the port binding. See [`Docs/20_DEPLOYMENT_READINESS_AUDIT.md`](Docs/20_DEPLOYMENT_READINESS_AUDIT.md), blockers B1/B2.

*This section previously listed all six bullets without distinction. The split above is
a correction, not a change in posture: the unimplemented items were never implemented.*

### Planned automation

- Pre-commit: secret scanning, `ruff`, `mypy`, and security linters once declared in repository-managed hook config.
- CI: dependency audit, SAST, image scan, signing, and SBOM checks once workflows are added under `.github/workflows/`.

## Hall of fame

Once we have our first valid report, contributors will be acknowledged here (with their consent).
