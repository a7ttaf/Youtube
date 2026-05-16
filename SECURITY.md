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

Open a private GitHub security advisory for this repository when the "Report a vulnerability" flow is available. If the UI is unavailable, contact a CODEOWNERS maintainer through GitHub and request a private encrypted intake channel before sharing exploit details. Maintainers monitor vulnerability-intake notifications on an ongoing basis to meet the response SLAs below.

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
- The frontend (`frontend/`) when it exists.
- Deployment assets (`deploy/helm/`, `Dockerfile`, `docker-compose.yml`).
- Spec documents that describe authorization, audit, or data handling.

Out of scope:

- Third-party dependencies — please report upstream first. We do triage transitive risk via Dependabot + `pip-audit` + Trivy.
- Social-engineering attacks against staff.
- Denial-of-service requiring sustained traffic the service is not provisioned to receive.
- Issues in unsupported branches.

### Safe-harbour

Good-faith research conducted under this policy will not be subject to legal action by the maintainers. **Stop, document, and report** as soon as you confirm a vulnerability — do not exfiltrate financial data or pivot beyond proof-of-concept.

## Hardening posture

- Role-based access control with 14 roles + 34 permissions; scope containment (channel / company / sector / finance-month / connector / global).
- 24 audited event types; sensitive payload masking unless `audit.view_sensitive_payloads` is granted.
- Postgres SERIALIZABLE isolation for principal reads; retryable failure handling; 256-role / 512-grant caps.
- Locked-month immutability; manual overrides require a different-person approver.
- Secrets via HashiCorp Vault / External Secrets Operator (no secrets in repo).
- TLS 1.3 only on ingress; encrypted PV; KMS-wrapped secrets.

### Planned automation

- Pre-commit: secret scanning, `ruff`, `mypy`, and security linters once declared in repository-managed hook config.
- CI: dependency audit, SAST, image scan, signing, and SBOM checks once workflows are added under `.github/workflows/`.

## Hall of fame

Once we have our first valid report, contributors will be acknowledged here (with their consent).
