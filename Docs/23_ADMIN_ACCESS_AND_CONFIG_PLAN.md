# 23 — Admin, Access & Configuration Plan

*Written 2026-08-27, scoped against the merged integration tree (main `d8418cea2` + the
#211–#217 draft band + the #209/#210 P0 stack). Every "exists today" claim below was
verified against code, not recalled.*

The operator's question that produced this plan: **"From where do I add the other users?
From where the permissions? From where can I make full config?"**

The short answer: **the backend for all of it already exists and is audited — what is
missing is the user interface.** Nothing in the current beta plan (Docs/21) builds one.
This document is the honest inventory of what works today, the plan for the missing
surface, and the tripwires that keep it safe.

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

All in `backend/ums_smart_revenue/api/users.py`. Every write takes a **required audit
reason** and lands in `audit_logs` through the same sink as finance writes. Grants and
role assignments carry a `scope_type` (global / sector / company / channel /
finance-month), so scoped access is already expressible.

**Who holds the gates** (from the seeded catalog, 16 roles × 26 permissions):
`users.manage` → **super_owner, corporate_admin**. `roles.assign` → super_owner,
corporate_admin, **finance_admin**. Note the deliberate split: finance_admin can assign
roles to existing users but cannot create users.

### The two ways to add a user right now

1. **CLI (first admin / repeatable):** `scripts/bootstrap_operator.py`
   `--email … --display-name … --role …` (repeatable per user), plus `--org-skeleton`
   for the sector/company tree. This is how the first `corporate_admin` comes to exist.
2. **API (from the dev dashboard environment):** the Vite dev proxy already forwards
   `/users` with the trusted-gateway headers (W0.2), so `POST /users` works from the
   browser dev setup — **but only when the dev gateway role holds `users.manage`**.
   The recommended dev role `finance_admin` does NOT; user creation from the dev proxy
   needs `VITE_DEV_GATEWAY_ROLE=corporate_admin` for the session doing it.

### Permissions and roles catalog

- `GET /security/roles` and `GET /security/permissions` (`api/security.py`) expose the
  catalog read-only (currently gated by `audit.view`).
- The catalog itself is **code-owned and migration-seeded** (`auth/roles.py`,
  `auth/permissions.py`, `auth/seed.py`, migration `20260825_0001`). There is no API to
  create a role or permission, on purpose — see tripwire T1.

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

A ninth view, nav-gated to principals holding `users.manage` (decision D-A1):

- User list (`GET /users`): email, display name, status, service-account badge.
- Create user: email + display name + human/service toggle (`POST /users`).
- Activate / deactivate (`PATCH /users/{id}`).
- Per-user drawer: current roles + assign/revoke with role picker, scope picker, and
  the **required reason field** (the API refuses blank reasons — surface that, don't
  fight it).
- Access profile panel (`GET /users/{id}/access`): the user's effective permissions.

New backend work: none. New wiring: add the Admin view to `ViewRouter`/nav; hooks for
the six endpoints.

### A2 — Access matrix & "who am I" — **4–6h**

- Read-only role × permission matrix from `GET /security/roles` + `/permissions`.
- A "Your access" panel: the resolved principal, role, scope, and capability list the
  session already carries (`GET /session/me`) — kills the "why is this button dead"
  confusion at the root.
- Wiring item found during the audit: `/security` is **not** in the dev proxy's
  `TENANT_SCOPED_ROUTES` (`frontend/vite.config.ts`) — add it, or the matrix 404s in
  dev. Decide whether `audit.view` is the right gate for catalog reads or whether they
  should move under `users.manage` (decision D-A2).

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

The operator story for `UMS_AUTHZ_SOURCE=database`: bootstrap the first
corporate_admin (CLI) → verify via `GET /users/{id}/access` → flip the env → the
gateway now supplies identity only. Cross-references Docs/21 P2 — this band documents,
it does not re-scope.

**Suggested order: A1 → A2 → (beta ships) → A6 → A3 → A4 → A5.** A1+A2 are worth
doing right after the P1 band merges — they are the same "stop looking like a mockup"
story applied to administration. A6 (delegated administration, §4b) comes before A3
because the scoped-grants UI should be built on the delegation model's ceiling, not
retrofitted under it. A3–A5 sit naturally with P2.

---

## 4 — Tripwires

- **T1 — No role/permission editor, ever, in this program.** The catalog is code +
  migration owned; the anti-drift tests bind the DB to the code registries. An editing
  UI would fight the migration model and reopen the exact drift class the frozen
  catalog work closed. Editing = a code change + migration, by design.
- **T2 — The UI must never widen access.** Admin surfaces render inside the same
  fail-closed gates as everything else: no `users.manage` → no Admin nav item, and a
  403 still renders as a 403. Client-side checks are display sugar only.
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
  tenant-wide.

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

- **Family ceilings.** super_owner assignments require super_owner; finance roles
  require finance_admin or super_owner; super-owner-only / finance / connector
  permission grants each require the matching admin role. Service-account lifecycle is
  super_owner-only.
- **Scoped assignments.** Role assignments and grants already CARRY a scope
  (global / sector / company / channel / finance-month), and the read-side authz layer
  checks permissions **on a scope** — the data model for "company CEO" exists.

**Missing (the A6 work):**

1. **Scoped admin gates.** `_require_role_assignment_permission` and
   `_require_user_management_permission` demand the permission at **GLOBAL scope
   only** — bounded delegation is impossible today: authority to add users or assign
   roles cannot be granted "for company X only". A6 makes both gates scope-aware.
2. **Scope containment.** A delegated admin may act only on users and assignments
   inside their own scope subtree; users they create are born inside it; revocations
   are limited to assignments they could have made.
3. **The no-amplification invariant** — the operator's "no one can take higher layer":
   an actor may assign a (role, scope) pair or grant a (permission, scope) pair **only
   if its effective permission set at that scope is a subset of the actor's own
   effective set there**, with self-modification upward always refused. This
   generalizes the family lists (which stay as a second belt). Subset-of-permissions is
   the recommended ceiling test — it is checkable mechanically and never needs a
   hand-maintained rank ladder (decision D-A6).
4. **Read-isolation proof.** "It will never see others" must be **proven per view**,
   not assumed: a test matrix where a company-scoped principal exercises channels /
   revenue / close / exports / audit / groups / connectors and every response is
   verified to contain only their company's rows. Tenant isolation is RLS-enforced;
   THIS is org-scope isolation inside one tenant, enforced at the app layer, and it has
   never been systematically proven. Whatever leaks, A6 fixes.
5. **"Add by email"** is the existing create flow (email is the account key). Actual
   invitation email is out of scope: UMS has no mailer and no login of its own —
   identity arrives from the gateway. When a real IdP/login lands (post-beta), invites
   ride it.

**Estimate: 24–44h** (policy rework + mutation-tested ceiling 12–20h; read-isolation
matrix + fixes 8–16h, honest unknown until measured; delegated mode in the A1 UI
4–8h). Sequence: **after A1** — A1 serves the global admin (the operator) immediately;
A6 is what makes it safe to hand pieces of it to sub-company people.

> ⚠️ **Interim rule, effective now:** until A6 lands, `users.manage` and `roles.assign`
> must not be granted to anyone outside HQ trust. Today's gates would let a delegated
> admin act tenant-wide and assign laterally (e.g. connector_admin) beyond their own
> layer. The family ceilings protect super_owner and finance only.

## 5 — Decisions for the operator

| # | Decision | Recommendation |
| --- | --- | --- |
| D-A1 | Which roles see the Admin nav | Principals holding `users.manage` (super_owner, corporate_admin); finance_admin sees only the role-assignment drawer via `roles.assign` if we want the split visible |
| D-A2 | Gate for the catalog reads (`/security/*`) | Move to `users.manage` alongside the matrix UI; `audit.view` was a placeholder |
| D-A3 | A4 status endpoint scope | Read-only allowlist, `platform.manage_settings` gate |
| D-A4 | When A1 lands | Immediately after the P1 band merges, before beta polish |
| D-A5 | A6 ceiling mechanism | **Subset-of-effective-permissions at the scope** (mechanical, no rank ladder to maintain); family lists stay as a second belt |
| D-A6 | Where a delegated admin's new users land | Born inside the delegator's scope subtree, always; only a global admin can move them |

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

*Relationship to Docs/21: this program is additive and currency-neutral; it does not
touch the P0/P1 bands or the EGP phases. Only A4's status endpoint adds backend
surface, and it is read-only.*
