# Channel `content_owner_id` write path — design

**Date:** 2026-06-17 · **Branch:** `feat/channel-content-owner-write-path` (off `main` @ `23df6937`)
**Source finding:** `UMS_Smart_Revenue_GAPS_STATUS.md` → "Code-level items still open", HIGH (latent).

## Problem

`content_owner_id` is the operator-set key that joins a YouTube channel to its
Google CMS content owner. It is the bridge of the API-driven channels + AdSense
model:

- **AdSense side (has a write API):** `finance/channel_account_links.py` models
  operator-verified `adsense_account_id ↔ content_owner_id` and derived
  `content_owner_id ↔ youtube_channel_id`.
- **Channels side (the gap):** `youtube_channels.content_owner_id` drives
  `list_target_channels` (`content_owner_id == account_id` + `INSIDE_CMS`,
  `active`, `revenue_required`) — i.e. which channels the Analytics pull targets.

This field has **no write path** at any layer:

- `ChannelCreateRequest` (`api/channels.py`) omits it.
- `SqlAlchemyChannelRegistry.create_channel` hardcodes `content_owner_id=None`.
- `update_mapping` only re-parents the company; nothing sets the content owner.
- The `ChannelRegistryStore` Protocol and in-memory `ChannelRegistry` omit it.

Consequence: every channel keeps `content_owner_id=None`, so once the connector
runs against real Google API credentials, `list_target_channels` matches **zero**
channels and the run reports SUCCEEDED while ingesting nothing — a silent-zero.
(Not "blocked on OAuth": auth is stored server-to-server API credentials, not an
interactive flow. This is the missing *channels-API controller surface*.)

The column `youtube_channels.content_owner_id` (`Text`, nullable) already exists
→ **no migration**.

## Design

### 1. Create path (optional field)
- Add `content_owner_id: str | None = None` to `ChannelCreateRequest`, stripped;
  a present-but-blank value → 422 (explicit, not silently coerced to null).
- Thread it through `ChannelRegistryStore.create_channel` (Protocol) → both
  registry implementations (SQL stops hardcoding `None`) → the ORM row.
- `CHANNEL_CREATED` audit `details` gains `content_owner_id`.

### 2. Update path (new dedicated route)
The operationally important half — existing/bootstrapped channels are all
`content_owner_id=None` today and must be configurable.

- `PATCH /channels/{youtube_channel_id}/content-owner`, body
  `{content_owner_id: str | None, reason: str}` (`content_owner_id` key required,
  value nullable so `null` clears; blank string → 422; `reason` required).
- Permission: `MANAGE_CHANNELS` at `AccessScope.channel(youtube_channel_id)` —
  this is ingestion configuration, deliberately **not** the mapping route's
  `MANAGE_ORG_MAPPING` (org re-parenting). Authorize before existence check.
- New `update_content_owner(*, youtube_channel_id, content_owner_id)` on the
  Protocol + both registries. SQL raises `KeyError` when the row is absent →
  route maps to 404.
- **No locked-month guard** (unlike `update_mapping`): changing
  `content_owner_id` never rewrites a closed month's company/sector attribution;
  it only retargets *future* ingestion. Documented in the method.
- No-op suppression: when the normalized new value equals the current value,
  return 200 with `audit_event: null` and emit nothing (mirrors the mapping
  route's idempotency contract).
- Otherwise emit `CHANNEL_UPDATED` with
  `details {old_content_owner_id, new_content_owner_id}` + `reason`.

### 3. Validation
`content_owner_id` is a free-form Google CMS string (compared to `account_id:
str`), **not** a UUID. Normalize: `None` → `None`; otherwise strip and reject a
present-but-blank value at the API boundary; the registry treats stripped-empty
defensively as `None`.

## Out of scope (YAGNI)
- No migration (column exists).
- No change to `list_target_channels` eligibility logic.
- No new writability for `cms_status` / `revenue_required`.
- No AdSense-linkage changes (`channel_account_links` already has its API).
- No frontend wiring (separate Registry-Phase-3 concern).

## Testing (TDD, red→green)
- **In-memory registry:** `create_channel(content_owner_id=…)` persists;
  `update_content_owner` sets / changes / clears (`None`); unknown id raises.
- **SQL registry:** `create_channel` persists `content_owner_id` to the ORM row;
  `update_content_owner` updates it; missing row → `KeyError`; blank → `None`.
- **API:** create with `content_owner_id` (in response + `CHANNEL_CREATED`
  audit); create without (back-compat `None`); present-but-blank → 422;
  `PATCH …/content-owner`: set → 200 + `CHANNEL_UPDATED {old,new}`; change;
  clear (`null`); no-op → 200 + `audit_event: null`, no audit record; 404 after
  authz; 403 without `MANAGE_CHANNELS` (authz-before-404).
- **Closed-loop regression (the payoff):** a channel created/updated with
  `content_owner_id == account_id`, `INSIDE_CMS`, `active`, `revenue_required`
  now appears in `list_target_channels(account_id)`; with `content_owner_id`
  unset it does not. Proves the silent-zero loop is closed.

## Blast radius
Exactly three files reference `create_channel` (Protocol + 2 impls + API) and one
route is added. No finance math, no migration, no AdSense path, no exports.
