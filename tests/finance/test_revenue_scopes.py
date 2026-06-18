"""Unit tests for build_authorized_revenue_scopes (pure scope expansion).

These exercise the security-critical expansion/dedup/order logic in isolation
(no FastAPI, no DB): a global grant lists every active sector + company plus the
global option; a scoped grant lists ONLY the authorized scopes; a sector grant
expands to its companies (reverse company_sector walk); a company grant confers
only itself (never its sector); names fall back to the raw id when absent.
"""

from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.finance.revenue_scopes import (
    RevenueScopeOption,
    build_authorized_revenue_scopes,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry

# Two sectors, three companies. company_sector maps company_id -> sector_id.
SECTOR_TV = "sector-tv"
SECTOR_MUSIC = "sector-music"
COMPANY_TV_A = "company-tv-a"
COMPANY_TV_B = "company-tv-b"
COMPANY_MUSIC = "company-music"
CHANNEL_TV_A = "channel-tv-a"
CHANNEL_TV_B = "channel-tv-b"
CHANNEL_MUSIC = "channel-music"

ORG_INDEX = OrgAccessIndex(
    channel_company={
        CHANNEL_TV_A: COMPANY_TV_A,
        CHANNEL_TV_B: COMPANY_TV_B,
        CHANNEL_MUSIC: COMPANY_MUSIC,
    },
    channel_sector={
        CHANNEL_TV_A: SECTOR_TV,
        CHANNEL_TV_B: SECTOR_TV,
        CHANNEL_MUSIC: SECTOR_MUSIC,
    },
    company_sector={
        COMPANY_TV_A: SECTOR_TV,
        COMPANY_TV_B: SECTOR_TV,
        COMPANY_MUSIC: SECTOR_MUSIC,
    },
)
SECTOR_NAMES = {SECTOR_TV: "TV", SECTOR_MUSIC: "Music"}
COMPANY_NAMES = {
    COMPANY_TV_A: "TV A",
    COMPANY_TV_B: "TV B",
    COMPANY_MUSIC: "Music Co",
}


def _build(granted, *, groups=()):
    """Invoke the service with the shared org fixtures and given grants."""
    return build_authorized_revenue_scopes(
        granted=granted,
        org_index=ORG_INDEX,
        sector_names=SECTOR_NAMES,
        company_names=COMPANY_NAMES,
        groups=groups,
    )


def _group(
    group_id: str,
    name: str,
    channel_ids: tuple[str, ...],
    *,
    active: bool = True,
) -> ChannelGroupEntry:
    """Build an in-memory channel-group entry for pure scope tests."""
    return ChannelGroupEntry(
        id=group_id,
        name=name,
        group_type="CUSTOM_GROUP",
        active=active,
        channel_ids=channel_ids,
    )


def test_global_grant_emits_global_plus_all_sectors_and_companies():
    """A global VIEW_REVENUE grant lists global + every active sector + company."""
    options = _build((AccessScope.global_scope(),))

    assert options[0] == RevenueScopeOption(scope_type="global", scope_id=None, label="Global")
    by_type = {(o.scope_type, o.scope_id) for o in options}
    assert ("sector", SECTOR_TV) in by_type
    assert ("sector", SECTOR_MUSIC) in by_type
    assert ("company", COMPANY_TV_A) in by_type
    assert ("company", COMPANY_TV_B) in by_type
    assert ("company", COMPANY_MUSIC) in by_type
    assert len(options) == 6


def test_global_grant_orders_global_then_sectors_then_companies_by_name():
    """Deterministic order: global, sectors by name, then companies by name."""
    options = _build((AccessScope.global_scope(),))

    keys = [(o.scope_type, o.label) for o in options]
    assert keys == [
        ("global", "Global"),
        ("sector", "Music"),
        ("sector", "TV"),
        ("company", "Music Co"),
        ("company", "TV A"),
        ("company", "TV B"),
    ]


def test_sector_grant_expands_to_sector_and_its_companies_only():
    """A sector grant lists that sector + its companies, not other sectors'."""
    options = _build((AccessScope.sector(SECTOR_TV),))

    by_key = {(o.scope_type, o.scope_id) for o in options}
    assert by_key == {
        ("sector", SECTOR_TV),
        ("company", COMPANY_TV_A),
        ("company", COMPANY_TV_B),
    }
    # Music sector and its company must never appear for a TV-sector viewer.
    assert ("sector", SECTOR_MUSIC) not in by_key
    assert ("company", COMPANY_MUSIC) not in by_key
    # No global option without a global grant.
    assert all(o.scope_type != "global" for o in options)


def test_company_grant_confers_only_that_company_not_its_sector():
    """A company grant lists only the company; it does NOT confer the sector."""
    options = _build((AccessScope.company(COMPANY_TV_A),))

    assert [(o.scope_type, o.scope_id) for o in options] == [("company", COMPANY_TV_A)]
    assert all(o.scope_type != "sector" for o in options)
    assert all(o.scope_type != "global" for o in options)


def test_company_grant_excludes_sibling_company():
    """A grant for one company must not leak a sibling company in the sector."""
    options = _build((AccessScope.company(COMPANY_TV_A),))

    ids = {o.scope_id for o in options}
    assert COMPANY_TV_B not in ids
    assert COMPANY_MUSIC not in ids


def test_dedup_when_sector_and_member_company_both_granted():
    """Overlapping sector + member-company grants dedup by (type, id)."""
    options = _build(
        (
            AccessScope.sector(SECTOR_TV),
            AccessScope.company(COMPANY_TV_A),
        )
    )

    keys = [(o.scope_type, o.scope_id) for o in options]
    assert keys.count(("company", COMPANY_TV_A)) == 1
    assert set(keys) == {
        ("sector", SECTOR_TV),
        ("company", COMPANY_TV_A),
        ("company", COMPANY_TV_B),
    }


def test_global_grant_with_other_scoped_grants_still_lists_full_universe():
    """A global grant wins: full universe regardless of extra scoped grants."""
    options = _build(
        (
            AccessScope.company(COMPANY_TV_A),
            AccessScope.global_scope(),
        )
    )

    assert any(o.scope_type == "global" for o in options)
    assert len(options) == 6


def test_global_grant_emits_active_non_empty_groups():
    """A global grant lists active channel groups after org rollup options."""
    options = _build(
        (AccessScope.global_scope(),),
        groups=(
            _group("group-tv", "TV Group", (CHANNEL_TV_A, CHANNEL_TV_B)),
            _group("group-empty", "Empty Group", ()),
            _group("group-inactive", "Inactive Group", (CHANNEL_TV_A,), active=False),
        ),
    )

    keys = [(option.scope_type, option.scope_id) for option in options]
    assert ("group", "group-tv") in keys
    assert ("group", "group-empty") not in keys
    assert ("group", "group-inactive") not in keys
    assert keys[-1] == ("group", "group-tv")


def test_channel_grants_for_every_member_emit_group_option():
    """A group can be listed from channel grants only when every member is covered."""
    options = _build(
        (
            AccessScope.channel(CHANNEL_TV_A),
            AccessScope.channel(CHANNEL_MUSIC),
        ),
        groups=(
            _group("group-mixed", "Mixed Group", (CHANNEL_TV_A, CHANNEL_MUSIC)),
            _group("group-tv", "TV Group", (CHANNEL_TV_A, CHANNEL_TV_B)),
        ),
    )

    assert [(option.scope_type, option.scope_id) for option in options] == [
        ("group", "group-mixed")
    ]


def test_company_grant_excludes_group_with_foreign_member_channel():
    """A company grant does not authorize a group that includes another company."""
    options = _build(
        (AccessScope.company(COMPANY_TV_A),),
        groups=(
            _group("group-tv-a", "TV A Group", (CHANNEL_TV_A,)),
            _group("group-mixed", "Mixed Group", (CHANNEL_TV_A, CHANNEL_MUSIC)),
        ),
    )

    keys = {(option.scope_type, option.scope_id) for option in options}
    assert ("group", "group-tv-a") in keys
    assert ("group", "group-mixed") not in keys


def test_sector_grant_emits_group_when_all_members_are_in_sector():
    """A sector grant can authorize groups whose members are all in that sector."""
    options = _build(
        (AccessScope.sector(SECTOR_TV),),
        groups=(
            _group("group-tv", "TV Group", (CHANNEL_TV_A, CHANNEL_TV_B)),
            _group("group-mixed", "Mixed Group", (CHANNEL_TV_A, CHANNEL_MUSIC)),
        ),
    )

    keys = {(option.scope_type, option.scope_id) for option in options}
    assert ("group", "group-tv") in keys
    assert ("group", "group-mixed") not in keys


def test_raw_id_fallback_when_name_missing():
    """An active unit absent from the name maps falls back to its raw id as the label.

    Only units present in the active name maps are included — a grant for a
    deactivated sector/company is filtered out so the selector never offers a
    dead option that 403s on read.
    """
    options = build_authorized_revenue_scopes(
        granted=(AccessScope.sector(SECTOR_TV),),
        org_index=ORG_INDEX,
        sector_names={SECTOR_TV: SECTOR_TV},  # active but name == raw id
        company_names={COMPANY_TV_A: "TV A"},  # company-tv-b name missing
    )

    by_id = {o.scope_id: o.label for o in options}
    assert by_id[SECTOR_TV] == SECTOR_TV  # raw-id fallback for the sector
    assert by_id[COMPANY_TV_B] == COMPANY_TV_B  # raw-id fallback for the company
    assert by_id[COMPANY_TV_A] == "TV A"


def test_stale_grants_for_deactivated_units_are_filtered():
    """Grants for deactivated sectors/companies are excluded from the options.

    When a VIEW_REVENUE grant points at a sector or company that has since been
    deactivated (absent from the active name maps), the selector must NOT offer
    it — the read path would return an empty scope for that option.
    """
    options = build_authorized_revenue_scopes(
        granted=(AccessScope.sector(SECTOR_TV),),
        org_index=ORG_INDEX,
        sector_names={},  # sector deactivated
        company_names={COMPANY_TV_A: "TV A"},  # company-tv-b name missing
    )

    scope_ids = {o.scope_id for o in options}
    assert SECTOR_TV not in scope_ids  # deactivated sector filtered out
    assert COMPANY_TV_B not in scope_ids  # deactivated company filtered out
    assert COMPANY_TV_A not in scope_ids  # parent sector deactivated, children excluded too
    assert options == []  # no active grants remain


def test_no_grants_returns_empty_list():
    """No grants -> no options (the route turns this into a 403, not here)."""
    assert _build(()) == []


def test_covered_channel_ids_uses_set_containment():
    """Regression for the Qodo #122 performance fix.

    The precomputed covered-channel set is computed once and reused for every
    group; semantically equivalent to the prior per-channel/per-grant scan.
    """
    # FIX: a sector grant must cover every channel mapped to that sector via
    # the org_index, and a group whose members are a strict subset must be
    # listed exactly once (no per-channel duplication).
    options = _build(
        (AccessScope.sector(SECTOR_TV),),
        groups=(
            _group("group-tv", "TV Group", (CHANNEL_TV_A, CHANNEL_TV_B)),
            _group("group-tv-a-only", "TV A Only", (CHANNEL_TV_A,)),
            _group("group-foreign", "Foreign", (CHANNEL_MUSIC,)),
        ),
    )
    keys = [(option.scope_type, option.scope_id) for option in options]
    assert keys.count(("group", "group-tv")) == 1
    assert ("group", "group-tv-a-only") in keys
    assert ("group", "group-foreign") not in keys


def test_unsupported_scope_type_is_dropped():
    """Only global/sector/company become options; an unexpected granted type
    (channel/connector/finance-month, the HOLDING-equivalent here) is dropped so
    a malformed grant can never widen the authorized rollup surface.
    """
    options = build_authorized_revenue_scopes(
        granted=(
            AccessScope.channel("channel-1"),
            AccessScope.connector("conn-1"),
            AccessScope.finance_month("2026-03"),
        ),
        org_index=ORG_INDEX,
        sector_names=SECTOR_NAMES,
        company_names=COMPANY_NAMES,
    )
    assert options == []
