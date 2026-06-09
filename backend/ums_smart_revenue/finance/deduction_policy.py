"""Shared net-revenue deduction policy constants (Phase 4 Spec 2b PR-2).

Neutral leaf module: holds the two constants both net_revenue and allocation
need, so neither has to import the other (breaks the net_revenue <-> allocation
import cycle). Imports nothing from finance.* compute modules.
"""

# ============================================================================
# Purpose: Map a Google source_system to the RevenueFactSourceKind it backs, so
#   a deduction component is only applied to a net derived from the SAME source
#   (no cross-source mixing). NET_APPLICABLE_COMPONENT_KINDS is the closed set
#   of component kinds that reduce a component-derived net.
# Database/ORM: None.
# Standards: explicit, closed maps; unknown source_system -> no match -> ignored.
# Blast Radius: Finance net-revenue derivation + account allocation net_applicable.
# ============================================================================
SOURCE_SYSTEM_TO_SOURCE_KIND: dict[str, str] = {
    "adsense_management": "ADSENSE",
    # FIX: adsense_payment_gap resolves to ADSENSE; allocation._basis_source_kind
    # already special-cased this, but _applicable_account_allocations and
    # _applicable_deduction_components use this map directly and silently dropped it.
    "adsense_payment_gap": "ADSENSE",
    "reconciliation": "ALLOCATION",
    "youtube_reporting": "YOUTUBE_CMS",
    "youtube_analytics": "YOUTUBE_ANALYTICS",
}
# Only blind, source-labeled reductions reduce a component-derived net; signed
# FX_VARIANCE / TRANSFER_FEE / UNRESOLVED_PAYMENT_GAP kinds never reduce net.
NET_APPLICABLE_COMPONENT_KINDS: frozenset[str] = frozenset({"TAX", "DEDUCTION"})
