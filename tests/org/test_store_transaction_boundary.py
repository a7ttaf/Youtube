"""Store-level pins for the transaction boundary (review #184, C2).

The bulk import wraps its two passes in ``registry.transaction()`` +
``groups.transaction()``; these tests pin the boundary's own contract on the
in-memory adapters, independent of the import: own writes are undone on raise,
a FOREIGN write interleaved mid-boundary survives (a committed SQL row
survives our rollback, and the in-memory idiom for "another transaction" is a
direct dict mutation), success keeps everything, and the boundary refuses to
nest. The SQL adapters delegate to the request's session transaction, whose
end-to-end proof lives in tests/api/test_channels_import_postgres.py.
"""

import dataclasses

import pytest

from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
SECOND_CHANNEL_ID = "UC3Dci3BzZXDo4jw4dU8KqWg"


class _BoomError(Exception):
    """A failure staged by the tests below, never a real domain error."""


def _seeded_channel(name: str = "Old Name") -> ChannelRegistryEntry:
    return ChannelRegistryEntry(
        youtube_channel_id=CHANNEL_ID,
        channel_name=name,
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
        content_owner_id="TestOwnerAAAAAAAAAAAAA",
    )


def test_registry_boundary_undoes_creates_and_restores_updates_on_raise() -> None:
    """Every write THIS store performed inside the boundary is taken back."""
    registry = ChannelRegistry([_seeded_channel()])

    with pytest.raises(_BoomError), registry.transaction():
        registry.create_channel(
            youtube_channel_id=SECOND_CHANNEL_ID,
            channel_name="Minted Inside",
            primary_company_id=None,
            cms_status="INSIDE_CMS",
            revenue_required=True,
        )
        registry.update_inventory(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Renamed Inside",
            cms_status="INSIDE_CMS",
            content_owner_id="TestOwnerAAAAAAAAAAAAA",
            revenue_required=True,
        )
        raise _BoomError()

    assert registry.get_channel(SECOND_CHANNEL_ID) is None
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Old Name"


def test_registry_boundary_leaves_a_foreign_write_standing() -> None:
    """The undo scope is this store's OWN writes, never "the world at enter".

    The direct dict mutation below is the established in-memory idiom for a
    concurrent writer's COMMITTED change (the import's race tests stage theirs
    the same way): SQL rollback cannot revert another transaction's committed
    rows, so the journal must not either.
    """
    registry = ChannelRegistry([_seeded_channel()])

    with pytest.raises(_BoomError), registry.transaction():
        registry.update_inventory(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Renamed Inside",
            cms_status="INSIDE_CMS",
            content_owner_id="TestOwnerAAAAAAAAAAAAA",
            revenue_required=True,
        )
        # The foreign writer lands AFTER our journaled write; its value is
        # what the store holds when the boundary unwinds.
        current = registry._channels[CHANNEL_ID]
        registry._channels[CHANNEL_ID] = dataclasses.replace(
            current, content_owner_id=None
        )
        raise _BoomError()

    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None
    # Our rename was undone back to the journaled pre-image — which restores
    # the whole entry as this store last wrote it, exactly as a SQL rollback
    # returns OUR row version. The foreign clear rode on top of our
    # uncommitted write, a state SQL's row lock makes unrepresentable, so the
    # pre-image is the honest restore target.
    assert stored.channel_name == "Old Name"


def test_registry_boundary_keeps_writes_on_success_and_journals_nothing_outside() -> None:
    """A clean exit keeps every write; writes outside a boundary never journal."""
    registry = ChannelRegistry([])
    registry.create_channel(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Outside Any Boundary",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
    )

    with registry.transaction():
        registry.update_inventory(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Committed Inside",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )

    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Committed Inside"
    # A later failing boundary must not resurrect anything from before it.
    with pytest.raises(_BoomError), registry.transaction():
        raise _BoomError()
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Committed Inside"


def test_registry_boundary_does_not_nest() -> None:
    """One enter per logical operation; nesting is a different contract.

    One compound ``with`` on purpose: contexts enter left to right, so the
    second ``transaction()`` raises INSIDE the first's scope and
    ``pytest.raises`` absorbs it — the same shape as the nested spelling,
    conformed to the analyzer's collapsible-with rule (PTC-W0062).
    """
    registry = ChannelRegistry([])

    with (
        registry.transaction(),
        pytest.raises(RuntimeError, match="does not nest"),
        registry.transaction(),
    ):
        pass


def test_groups_boundary_undoes_own_writes_and_keeps_a_foreign_group() -> None:
    """Mirror of the registry pins, over the group store's write methods."""
    groups = ChannelGroupRegistry()
    groups.create_group(
        name="seeded", group_type="SECTOR", channel_ids=[], cms_group_id="cms-seeded"
    )

    with pytest.raises(_BoomError), groups.transaction():
        minted = groups.create_group(
            name="minted", group_type="SECTOR", channel_ids=[], cms_group_id="cms-minted"
        )
        seeded = groups.get_group_by_cms_id("cms-seeded")
        assert seeded is not None
        groups.add_members(group_id=seeded.id, channel_ids=[CHANNEL_ID])
        # A concurrent writer's committed group, staged as the race tests
        # stage theirs: directly, invisible to the journal.
        foreign = ChannelGroupEntry(
            id="foreign-group",
            name="foreign",
            group_type="SECTOR",
            active=True,
            channel_ids=(),
            cms_group_id="cms-foreign",
        )
        groups._groups[foreign.id] = foreign
        assert minted.cms_group_id == "cms-minted"
        raise _BoomError()

    assert groups.get_group_by_cms_id("cms-minted") is None
    seeded = groups.get_group_by_cms_id("cms-seeded")
    assert seeded is not None and seeded.channel_ids == ()
    foreign_stored = groups.get_group_by_cms_id("cms-foreign")
    assert foreign_stored is not None and foreign_stored.name == "foreign"


def test_groups_boundary_does_not_nest() -> None:
    """Same compound-with shape as the registry's nest test, same reason."""
    groups = ChannelGroupRegistry()

    with (
        groups.transaction(),
        pytest.raises(RuntimeError, match="does not nest"),
        groups.transaction(),
    ):
        pass
