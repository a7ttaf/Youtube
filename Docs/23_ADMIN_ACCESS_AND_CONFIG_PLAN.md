# 23 — Admin, Access & Configuration Plan

*Written 2026-08-27, scoped against the merged integration tree (main `d8418cea2` + the
#211–#217 historical draft band + the former #209/#210 P0 stack). Every "exists today" claim below was
verified against code, not recalled. Gap-patched 2026-08-31 (live-thread recertification).*

The operator's question that produced this plan: **"From where do I add the other users?
From where the permissions? From where can I make full config?"**

The short answer: **the backend for all of it already exists and is audited — what is
missing is the user interface.** Nothing in the current beta plan (Docs/21) builds one.
This document is the honest inventory of what works today, the plan for the missing
surface, and the tripwires that keep it safe.

### Prerequisites & related plans (program bundle)

| Doc / where | Role |
| --- | --- |
| [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) / [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md) | Parent beta audit/plan (snapshot; living status on P0 split PRs) |
| [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) | Execution DAG |
| P0 split PRs (P0-a…P0-e; #221–#225) on `main` | **Hard prerequisite** for A1/A5 (bootstrap, seed migration, `/users` proxy) |
| [`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md) | Sibling finance program (no overlap) |

> ⚠️ **Hard dependency on P0 split PRs.** `scripts/bootstrap_operator.py`, Alembic migration
> `20260825_0001` (roles/permissions seed), and Vite proxy `/users` (plus `/org-units`)
> land on **P0-c/#223 / P0-e/#225** — **not on bare `main`**. Do not start A1 or A5
> until P0-c merges (or cherry-picks). After P0-e/#225, A2 still owns adding
> `/security` to `TENANT_SCOPED_ROUTES`. PR #210 is historical: it merged on 2026-08-29
> into the non-main `docs/deployment-readiness-audit` branch and is not living source
> of truth on `main`; current successors are #221–#225.
>
> **Consolidation:** Docs/20/21/23/24/25 ship together in `docs/program-plans-consolidated`
> (supersedes closed drafts #209 / #218 / #219).

---

## 1 — What exists TODAY (no UI, fully functional)

### Users

| Capability | Surface | Gate |
| --- | --- | --- |
| List users | `GET /users` | `users.manage` |
| Create user (human or service account) | `POST /users` | `users.manage` |
| Update user (status, display name) | `PATCH /users/{id}` | `users.manage` |
| Effective access profile | `GET /users/{id}/access` | `users.manage` |
| Assign role (scoped, audited reason required) | `POST /users/{id}/roles` | `roles.assign` |
| Revoke role | `POST /users/{id}/roles/{assignment_id}/revoke` | `roles.assign` |
| Direct permission grant (scoped) | `POST /users/{id}/permissions` | `roles.assign` |
| Revoke grant | `POST /users/{id}/permissions/{grant_id}/revoke` | `roles.assign` |

All in `backend/ums_smart_revenue/api/users.py`. There is **no `DELETE /users`** —
lifecycle is activate/deactivate via `PATCH` status. Every write takes a **required
audit reason** and lands in `audit_logs` through the same sink as finance writes.
Grants and role assignments carry a `scope_type` (global / sector / company / channel /
finance-month), so scoped access is already expressible.

**Who holds the gates** (from the seeded catalog, 16 roles × 26 permissions):
`users.manage` → **super_owner, corporate_admin**. `roles.assign` → super_owner,
corporate_admin, **finance_admin**. Note the deliberate split: finance_admin can assign
roles to existing users but cannot create users.

### The two ways to add a user right now

1. **CLI (first admin / repeatable):** `scripts/bootstrap_operator.py`
   `--email … --display-name … --role …` (repeatable per user), plus `--org-skeleton`
   for the sector/company tree. This is how the first `corporate_admin` comes to exist.
   **Ships on P0-c** — absent from bare `main`.
2. **API (from the dev dashboard environment):** the Vite dev proxy forwards `/users`
   with the trusted-gateway headers once W0.2 / P0-e lands, so `POST /users` works
   from the browser dev setup — **but only when the dev gateway role holds
   `users.manage`**. The recommended finance-demo role `finance_admin` does NOT; user
   creation from the dev proxy needs `VITE_DEV_GATEWAY_ROLE=corporate_admin` for the
   session doing it. On bare `main`, `/users` is also missing from
   `TENANT_SCOPED_ROUTES` (same class of gap as `/org-units`).

### Permissions and roles catalog

- `GET /security/roles` and `GET /security/permissions` (`api/security.py`) expose the
  catalog read-only (currently gated by `audit.view`).
- The catalog itself is **code-owned and migration-seeded** (`auth/roles.py`,
  `auth/permissions.py`, `auth/seed.py`, migration `20260825_0001` on **P0-c**). There
  is no API to create a role or permission, on purpose — see tripwire T1. On bare
  `main`, `db/security_seed.sql` exists but is not wired into Alembic first-run.

### Identity vs accounts — the part that surprises everyone

**UMS has no login of its own.** Identity arrives from the trusted gateway
(`X-User-ID` / `X-Role` / gateway token headers); in dev, the Vite proxy plays gateway.
The user rows managed above are **accounts and access records**, consumed when
`UMS_AUTHZ_SOURCE=database` — the real multi-user mode, where the gateway supplies only
identity and the database supplies roles/permissions. In today's `headers` mode the
role comes straight from the header, and the in-app role dropdown is a **display-only
preview** (it changes labels, never capabilities). The switch to `database` mode is
already a planned P2 item in Docs/21; band A5 below is its operator story.

### Configuration

All configuration is environment variables read fail-fast at boot
(`config/settings.py`; the README env matrix is the reference). There is no runtime
config API and no config UI. Secrets (gateway token, DB password, Google credential
refs) must never gain a display surface.

---

## 2 — The gap

Eight views exist (Command / Registry / Groups / Close / Trace / Exports / Connectors /
Audit). **None of them is an Admin view.** Consequences the operator already hit:

- Adding a user requires a CLI session or a hand-rolled API call.
- "Which role can do what" is answerable only by reading `auth/seed.py`.
- A 403 in the UI (e.g. Connectors under finance_admin) is honest but unexplainable
  in-app — nothing shows the viewer's own effective access.
- Deployment configuration (authz mode, tenant currency, connector flags) is invisible
  unless you read the environment.

---

## 3 — The program

All bands are frontend-dominant (the APIs exist), independently draft-PR-able from
main, and none blocks the beta data path. Hours assume the established view patterns
(useAsync hooks, permission-gated panels, honest empty states).

### A1 — Admin view MVP: Users & Roles — **10–15h** ⭐ the operator's actual ask

**Not frontend-only.** The assignment drawer needs a scoped user list endpoint. Today
`GET /users` requires `users.manage`; `finance_admin` holds `roles.assign` but **not**
`users.manage`, so the drawer cannot populate assignable users without either:

1. **Preferred:** add **`users.read_scoped`** — list/access read without `users.manage`
   (scoped to the actor's subtree post-A6; global for HQ until then); **or**
2. Remove `finance_admin` from the assignment drawer until that endpoint exists.

A ninth view, nav-gated when the session reports **either** `can_manage_users` or
`can_assign_roles` (decision D-A1). Its controls are independently gated:

- User list (`GET /users` or scoped read): email, display name, status, service-account badge.
- Create user: email + display name + human/service toggle (`POST /users`) — visible
  only with `can_manage_users`.
- Activate / deactivate (`PATCH /users/{id}`) — not a hard delete; visible only with a
  separate global-only `can_manage_user_lifecycle` capability. In A1 that derives from
  the current global `users.manage` gate; A6 must keep it global even when user creation
  becomes subtree-scoped.
- Per-user drawer: current roles + assign/revoke with role picker, scope picker, and
  the **required reason field** (the API refuses blank reasons — surface that, don't
  fight it) — visible with `can_assign_roles` plus successful scoped user/access reads.
- Access profile panel (`GET /users/{id}/access`): the user's effective permissions.

**Session capability hole (A1 sub-item):** `GET /session/me` /
`SessionCapabilities` today has no `can_manage_users`, `can_assign_roles`, or
`can_manage_user_lifecycle`. The SPA
cannot fail-closed hide the Admin nav from session alone without inventing client-side
permission parsing. **Preferred:** extend `/session/me` with those two boolean
nav capabilities plus the global-only lifecycle boolean (same pattern as
`can_view_audit` / `can_manage_groups`). Alternative:
document reading raw permission lists from the session payload — weaker and easier to
drift.

New backend work: **`users.read_scoped`** (or equivalent) for the assignment drawer;
session-capability booleans. The six user write endpoints already exist. New wiring:
Admin view in `ViewRouter`/nav + hooks. Do not start until **P0-c** merges.

**Acceptance criteria (A1):**
- [ ] Admin nav visible iff session reports `can_manage_users || can_assign_roles`; hidden when both are false
- [ ] `finance_admin` can open the assignment-only surface when `users.read_scoped` returns 200 and `can_assign_roles=true`
- [ ] `finance_admin` never sees create controls without `can_manage_users`, or
  activate/deactivate controls without global-only `can_manage_user_lifecycle`
- [ ] Create/assign/revoke flows require audit reason; blank reason → 422 surfaced in UI
- [ ] No client-side permission widening beyond session capabilities

> ⚠️ **A1 must not present today's role-assignment policy as safe for delegation.**
> Family ceilings on **role** assign fence `super_owner` + finance roles only —
> anyone with `roles.assign` can still assign `connector_admin` / `corporate_admin`
> (see T9 / A6). HQ-only until A6.

### A2 — Access matrix & "who am I" — **4–6h**

- Read-only role × permission matrix from `GET /security/roles` + `/permissions`.
- A "Your access" panel: the resolved principal, role, scope, and capability list the
  session already carries (`GET /session/me`) — kills the "why is this button dead"
  confusion at the root.
- **Proxy residual:** on bare `main`, `/users` and `/security` are both missing from
  `TENANT_SCOPED_ROUTES`. P0-e/#225 adds `/users` (and `/org-units`); **A2 owns adding
  `/security`**, or the matrix 404s in dev. Decide whether `audit.view` is the right
  gate for catalog reads or whether they should move under `users.manage`
  (decision D-A2).

**Acceptance criteria (A2):**
- [ ] `/security` proxied in dev; matrix loads without 404
- [ ] "Your access" panel matches session principal and effective permissions
- [ ] Catalog reads fail-closed for principals lacking the chosen gate

### A3 — Scoped grants UI — **6–10h**

Direct permission grants with real scope pickers (sector/company/channel pulled from
the registry/org APIs, finance-month from the month window), revoke, and the audit
reason. This is the "give this analyst March-only bank-reconciliation view" story.
Defer until A1 proves the patterns; the API is ready.

### A4 — Deployment status panel — **4–8h** (needs one small backend endpoint)

Read-only "About this deployment": authz source, tenant slug + **declared primary
currency** (the EGP flip made visible!), connector/scheduler flags, log level, alembic
head, app version. Requires a new `GET /system/status` returning a **hand-picked
allowlist** — never a settings dump (tripwire T3). Supports the EGP program: the panel
is where the operator confirms the flip actually took effect in a deployment.

### A5 — Database-authz cutover runbook — docs, **2–4h**

Docs/21 W0 now requires the minimal single-operator database-authz cutover so one
principal can hold `finance_admin@global` plus a separate
`connectors.run_jobs@connector:manual-upload` grant. A5 owns the hardened external-
access version: bootstrap the first corporate_admin (CLI) → copy/verify its exact UUID
via `GET /users/{id}/access` → exercise unknown/disabled/storage-failure cases → flip
the environment → prove the gateway supplies identity only → rehearse rollback. A5
does not weaken or postpone the smaller P0-c beta cutover.

**Suggested order: A1 → A2 → (beta ships) → A6 → A7 + A5 → A3 → A4.** A1+A2 are worth
doing right after the P1 band merges — they are the same "stop looking like a mockup"
story applied to administration. A6 (delegated administration, §4b) comes before A3
because the scoped-grants UI should be built on the delegation model's ceiling, not
retrofitted under it. A7 (Google sign-in, §4c) pairs with A5 — together they are the
external-access milestone; the first competitor account waits for A5+A6+A7 all green.

---

## 4 — Tripwires

- **T1 — No role/permission editor, ever, in this program.** The catalog is code +
  migration owned; the anti-drift tests bind the DB to the code registries. An editing
  UI would fight the migration model and reopen the exact drift class the frozen
  catalog work closed. Editing = a code change + migration, by design.
- **T2 — The UI must never widen access.** The Admin nav uses
  `can_manage_users || can_assign_roles`; each subtree then keeps its narrower gate.
  No `can_manage_users` means no create controls; no global-only
  `can_manage_user_lifecycle` means no account-status controls; and no
  `can_assign_roles`/successful scoped read means no assignment drawer. A 403 still
  renders as a 403. Client-side checks are display sugar only.
- **T3 — No secret ever reaches a status panel.** The A4 endpoint is allowlist-only;
  gateway token, DSNs, credential references are permanently out.
- **T4 — Reasons are load-bearing.** Every admin write API requires an audit reason;
  the UI surfaces the field as required and never auto-fills it.
- **T5 — Keep the `users.manage` / `roles.assign` split.** finance_admin's
  assign-but-not-create shape is seeded policy; the UI reflects it rather than
  papering over it.
- **T6 — The no-amplification ceiling is a mutation-tested invariant, not a code
  review promise.** Every A6 policy test must go RED when its guard is deleted:
  a delegated admin attempting (a) a role above their layer, (b) a grant outside
  their scope subtree, (c) an upward self-modification, and (d) a revoke of an
  assignment they could not have made — all four refused, all four proven by
  reject→accept matrices, per the standing gate-flip discipline.
- **T7 — Until A6 lands, delegation is a policy decision, not a feature.** Do not
  grant `users.manage` / `roles.assign` beyond HQ trust; the current gates act
  tenant-wide. **Release gate:** no sub-company / competitor account until A6 is green.
- **T8 — No password store, permanently.** UMS never stores, hashes, prompts for, or
  resets a password. Sign-in is Google OIDC behind the operator's allowlist, and the
  no-password property holds by construction — there is nothing to phish, leak, or
  "save". Any future feature that needs a credential field is designed wrong.
- **T9 — Role-family ceiling hole (today).** `_require_role_assignment_policy`
  (`api/users.py`) fences `super_owner` + finance **roles** only. Permission grants
  have connector/finance/super-owner belts, but **role** assign does not stop
  `finance_admin` / `corporate_admin` from assigning `connector_admin` or
  `corporate_admin`. A1 must not paper over this; A6's subset-of-permissions ceiling
  closes it. Mutation-test the reject matrix when A6 lands.

## 4b — A6: Delegated administration with a hard ceiling (operator-required, 2026-08-27)

The operator's requirement, added after reading the first version of this plan, in his
own words: *"CEO of some sub-company, it will never see others; some users only see the
views; I can give CEO to add some users; GM add X to co. X for permission X — fully
config for permissions, but no one can take higher layer I give."*

That is a precise specification of **scope-bounded delegated administration with a
no-amplification invariant**, and it is the most security-sensitive band in this
program. What the code enforces today was verified line by line
(`api/users.py:651-737`):

**Exists today (keep, but not sufficient):**

- **Family ceilings (partial).** super_owner assignments require super_owner; finance
  roles require finance_admin or super_owner; super-owner-only / finance / connector
  **permission grants** each require the matching admin role. Service-account lifecycle
  is super_owner-only. **Gap (T9):** there is no matching fence preventing assignment of
  `connector_admin` / `corporate_admin` roles to lateral peers — A6 must close this.
- **Scoped assignments.** Role assignments and grants already CARRY a scope
  (global / sector / company / channel / finance-month), and the read-side authz layer
  checks permissions **on a scope** — the data model for "company CEO" exists.

**Missing (the A6 work):**

0. **Home scope at birth.** Delegated user creation must never leave a tenant-wide
   principal without an org anchor. Require persisted **`home_org_unit_id`** on user
   creation **or** an atomic **`create_user + scoped_role_assignment`** in one
   transaction. Acceptance: an unassigned tenant-wide user **never** exists after
   delegated create flows.

1. **Scoped admin gates.** `_require_role_assignment_permission` and
   `_require_user_management_permission` demand the permission at **GLOBAL scope
   only** — bounded delegation is impossible today: authority to add users or assign
   roles cannot be granted "for company X only". A6 makes both gates scope-aware.
2. **Scope containment.** A delegated admin may act only on users and assignments
   inside their own scope subtree; users they create are born inside it; revocations
   are limited to assignments they could have made.
   **Account status is different:** `users.status` is account-global while one user may
   carry assignments in several company/sector subtrees. Therefore delegated admins
   do **not** activate/deactivate accounts in A6. Account-global lifecycle stays with a
   global `users.manage` principal, exposed to the SPA through the distinct global-only
   `can_manage_user_lifecycle` capability; delegated admins may revoke only assignments/
   grants inside their own subtree. If delegated lifecycle is ever added, it needs one
   transaction that locks the target plus every active assignment/grant and rejects
   any global, unknown, or out-of-subtree authority, with a concurrency test against
   an HQ grant racing the status change.
3. **The no-amplification invariant** — the operator's "no one can take higher layer":
   an actor may assign a (role, scope) pair or grant a (permission, scope) pair **only
   if its effective permission set at that scope is a subset of the actor's own
   effective set there**, with self-modification upward always refused. This
   generalizes the family lists (which stay as a second belt). Subset-of-permissions is
   the recommended ceiling test — it is checkable mechanically and never needs a
   hand-maintained rank ladder (decision D-A6).
4. **Read-isolation proof — competitor grade.** The operator's clarification
   (2026-08-27, verbatim): *"sub-companies as same competitors whose data should not be
   viewed … can't see any data for others, only see the X (his company only or sectors
   — multiple channels)."* Sub-companies are **competitors to each other**; a
   cross-company leak is a confidentiality breach between competitors, not a UX bug.
   Consequences:
   - The matrix is a **release gate**: no sub-company account is issued until it is
     green. A company-scoped principal is driven through every view — channels /
     revenue / close / exports / audit / groups / connectors — and every response is
     proven to contain only their company's (or their sector's) rows.
   - **Aggregates count as data.** Row filtering is not enough: holdings-wide panels
     (the Command view's "all scopes" gap narrative, tenant-wide rankings and totals,
     tenant-wide audit trails) embed competitors' numbers in the aggregate. The
     enforcement pattern for this already exists and fails closed —
     `require_permission(user, VIEW_REVENUE, target_scope, org_index)`
     (`api/revenue.py:571`), and the holdings-wide gap surfaces demand global scope
     (`api/revenue.py:13`) — so a company-scoped CEO is refused; the matrix proves it
     endpoint by endpoint AND the UI renders those refusals as absent panels, not
     error cards.
   - Exports are scope-bound: a company-scoped principal can request exports only at
     or below their own scope. Sector scope ("multiple channels") is the same
     mechanism one level up.
   Tenant isolation is RLS-enforced; THIS is org-scope isolation inside one tenant,
   enforced at the app layer, and it has never been systematically proven. Whatever
   leaks, A6 fixes before any competitor account exists.
5. **"Add by email"** is the existing create flow (email is the account key). Actual
   invitation email is out of scope: UMS has no mailer and no login of its own —
   identity arrives from the gateway. When a real IdP/login lands (post-beta), invites
   ride it.

**Estimate: 24–44h** (policy rework + mutation-tested ceiling 12–20h; read-isolation
matrix + fixes 8–16h, honest unknown until measured; delegated mode in the A1 UI
4–8h). Sequence: **after A1** — A1 serves the global admin (the operator) immediately;
A6 is what makes it safe to hand pieces of it to sub-company people.

**Acceptance criteria (A6):**
- [ ] Delegated create always sets `home_org_unit_id` or atomically assigns scoped role
- [ ] Mutation tests RED when home-scope guard removed
- [ ] Competitor read-isolation matrix green across all views/exports
- [ ] No-amplification invariant proven for role assign, grant, revoke, self-modification
- [ ] Only global `users.manage` can change account status; delegated admins can revoke only in-subtree authority
- [ ] `can_manage_user_lifecycle` stays false for every merely sector/company-scoped admin
- [ ] A multi-scope target (company X + company Y, or any global grant) cannot be disabled by an X-scoped admin

## 4c — A7: Google-only sign-in with an operator-owned domain allowlist

The operator's final requirement, verbatim: *"I don't need someone have user and
password … login will be by Google only, as per domains I will add — without those
domains no one can login."*

**The architecture is already shaped for this.** UMS deliberately has no login and no
password store: identity arrives from a **trusted gateway** that injects
`X-User-ID` / `X-User-Email` + the gateway token. **`X-User-ID` must remain the
internal UMS UUID** — Google `sub` and email are **not** valid principal IDs.

**Required persistence (A7 backend):** an **`external_identities`** revision table with
a surrogate id, `(tenant_id, provider_issuer, provider_subject)`, `user_id`, normalized
verified email, created/revoked timestamps, and actor/reason provenance. Two PostgreSQL
partial unique indexes apply only where `revoked_at IS NULL`: one prevents a provider
subject from binding to two users; the other prevents one UMS user from carrying two
active Google identities for the same issuer. Revoked rows remain as history, so relink
does not overwrite evidence. The gateway adapter resolves Google OIDC claims → UMS UUID
**before** trusted headers reach FastAPI (`dependencies.py` UUID validation), but only
after the gateway has established a trusted tenant binding such as a configured
hostname, callback state, or operator-owned tenant selection record. A browser-supplied
tenant header is not an authentication boundary; absent or ambiguous tenant binding
fails closed before the external-identity lookup.

**Enrollment is explicit; account creation alone is not enough.** Add an audited
operator action such as
`POST /users/{user_id}/external-identities/google/enrollments` (a CLI may wrap the same
service) that requires `users.manage`, an existing active human account, an allowed
Workspace domain or exact-email entry, and a non-blank reason. It creates a short-lived,
single-use cryptographically random challenge returned once and persisted only as a
hash in an `external_identity_enrollments` revision (target user/issuer/email, expiry,
consumed/revoked timestamps, actor, and reason). On first Google sign-in, the
gateway supplies validated issuer/audience, `sub`, `email_verified=true`, normalized
email, and that challenge to a transaction which:

1. locks the enrollment and target user plus any existing matching identity rows; the
   active-only unique indexes close the absent-row race and map conflicts fail-closed;
2. re-checks expiry/unused state, tenant, domain/exact-email allowlist, and exact target
   email match;
3. refuses disabled/service/ambiguous/already-bound accounts and any subject collision;
4. inserts `external_identities`, consumes the challenge, and writes an audit event in
   one commit without logging the raw token or Google subject.

The operator delivers the one-time code out of band. The intended user submits it in a
gateway form whose POST body exchanges it for an HttpOnly, SameSite gateway state cookie
before the OIDC redirect; it never appears in a URL, reverse-proxy access log, UMS log,
or audit detail.

Unknown identities or a login without a valid pre-authorized enrollment still receive
the same clean 403; no user account is auto-provisioned and no account-existence detail
is leaked. Recovery/relink is a revoke-plus-new-enrollment operation, never an
overwrite of the old subject binding.

Google SSO is that gateway made real — an OIDC front (Google sign-in) standing where
the dev proxy stands today, injecting the same headers. Zero change to UMS's internal
auth model; the no-password property is preserved by construction, because there is
nothing to store.

**The gate is two layers, both fail-closed:**

1. **Domain gate (login at all).** After Google authenticates, the gateway checks the
   verified identity against the operator's allowlist. ⚠️ The one subtlety that needs
   a ruling (D-A7): Google only asserts a verifiable hosted domain (`hd` claim) for
   **Workspace domains** (`ceo@companyx.com`). A bare `@gmail.com` account has no
   domain of its own — allowlisting `gmail.com` would admit every Gmail user in the
   world. Safe model: **allowlist Workspace domains, plus exact email addresses for
   individuals on plain Gmail; never a public mail domain wholesale.**
2. **Account + enrollment gate (access).** Passing the domain gate authenticates, it
   does not authorize: the verified email must match an existing active UMS account
   and the Google subject must already be bound, or the request must carry the valid
   one-time enrollment above. Unknown/unapproved email from an allowed domain → clean
   403, no account auto-provisioning. "I add X for sub-company X" stays literal: no
   account and explicit enrollment, no access, regardless of domain.

**Build shape:** an off-the-shelf OIDC proxy (e.g. oauth2-proxy) in compose with a
thin adapter mapping its verified-identity headers onto UMS's trusted-header contract,
or a small dedicated auth service if the mapping fights the proxy. Allowlist lives in
gateway config first (deploy-audited); managing it from the Admin UI is a later A1
extension (D-A8). **Estimate: re-cost from the former 10–18h**: the durable enrollment,
revoke/relink, transaction/race tests, and audit contract are required work, not a
manual database precondition hidden outside the estimate.

**Acceptance criteria (A7):**
- [ ] `external_identities` migration + repository; Google `sub` maps to UMS UUID
- [ ] Audited, expiring, single-use enrollment binds only an existing active human account with exact verified email
- [ ] Active-only uniqueness permits audited revoke/relink history but never two live subject/user bindings
- [ ] Duplicate subject/user bindings, expired/replayed enrollment, disabled/service users, and storage races fail closed
- [ ] Revoke/relink preserves the old binding and reason; raw challenge/subject never appears in logs or audit details
- [ ] Gateway never forwards raw Google subject as `X-User-ID`
- [ ] Allowlisted unknown email → 403; known mapped user → session with DB authz (post-A5)

**The external-access milestone.** Handing the first sub-company account to a real
competitor requires ALL THREE: **A5** (database-authz: DB owns roles) + **A6**
(ceiling + isolation proof green) + **A7** (Google-only login behind the domain
gate). None of the three is optional for that step, and the beta needs none of them.

> ⚠️ **Interim rule, effective now:** until A6 lands, `users.manage` and `roles.assign`
> must not be granted to anyone outside HQ trust. Today's gates would let a delegated
> admin act tenant-wide and assign laterally (e.g. connector_admin) beyond their own
> layer. The family ceilings protect super_owner and finance only.

## 5 — Decisions for the operator

| # | Decision | Recommendation |
| --- | --- | --- |
| D-A1 | Which roles see the Admin nav | `can_manage_users \|\| can_assign_roles`; finance_admin gets assignment-only UI only after `users.read_scoped`; account status additionally requires global-only `can_manage_user_lifecycle` |
| D-A2 | Gate for the catalog reads (`/security/*`) | Move to `users.manage` alongside the matrix UI; `audit.view` was a placeholder |
| D-A3 | A4 status endpoint scope | Read-only allowlist, `platform.manage_settings` gate |
| D-A4 | When A1 lands | Immediately after the P1 band merges, before beta polish |
| D-A5 | A6 ceiling mechanism | **Subset-of-effective-permissions at the scope** (mechanical, no rank ladder to maintain); family lists stay as a second belt |
| D-A6 | Where a delegated admin's new users land | Born inside the delegator's scope subtree, always; only a global admin can move them |
| D-A7 | Allowlist semantics for Google sign-in | **Workspace domains + exact emails for plain-Gmail individuals; never a public mail domain wholesale** |
| D-A8 | Where the allowlist lives | Gateway config first (deploy-audited); Admin-UI management as a later A1 extension |

## 6 — Ideas added to the project (operator-invited)

- **Service-actor workflow in A1**: a "create connector service account" shortcut —
  `is_service_account=true` + `connectors.run_jobs` grant + copy-the-UUID affordance
  for `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`. Directly closes the Docs/19 Step-5 trap
  (reusing the human bootstrap UUID misattributes the connector audit trail).
- **Audit cross-link**: from a user's access profile to the Audit view filtered to
  their actions — the reviewer's "what did this account actually do" in one click.
- **One-command operator onboarding**: document the single bootstrap invocation that
  creates a named user with a role, so adding operator #2 is copy-paste.
- **EGP-flip visibility**: A4's currency line shows the *declared* tenant currency and
  which mode (headers setting vs tenants row) is authoritative — the program's Phase-3
  flip gets a place where the operator can SEE it took effect.

---

## 7 — Migration and blast-radius statement

- **Confirmed migration required (A1):** add/seed `users.read_scoped` if that new
  permission key is selected; update the frozen role/permission catalog and anti-drift
  tests. No existing grant is widened implicitly.
- **Confirmed migration required (A6):** add the persisted user home-scope relation
  (or the chosen atomic-equivalent schema) with tenant FK/RLS and an explicit backfill
  decision before delegated administration is enabled. Account `status` remains global;
  the lifecycle fence is policy/test work, not a status-column migration.
- **Confirmed migration required (A7):** add `external_identities` and hashed,
  expiring enrollment persistence with tenant RLS, active-only subject/user uniqueness,
  revoke/history, and audit provenance. Existing users are **not** auto-bound or
  backfilled.
- PostgreSQL remains the sole source of truth. No finance totals, close state, or
  withholding calculations change in Docs/23.

---

*Relationship to Docs/21 (snapshot in this PR; living status on P0 split PRs): this
program is additive and currency-neutral. A1/A5 assume **P0-c merged**. A1 adds scoped
user reads/session capabilities; A6 adds home-scope and delegation policy; A7 adds
identity/enrollment persistence and gateway integration; A4 remains an allowlisted
read-only status endpoint. Independent of
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).*
