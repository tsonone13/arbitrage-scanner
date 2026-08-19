"""Ranks detected opportunities so the best trades surface first."""

from models import ArbOpportunity

# Opportunities flagged "REVIEW" (see arb_engine.SUSPICIOUS_EDGE_RATIO) tend to
# show the largest raw net_edge of all -- that's exactly why they're flagged.
# Left unranked, they'd bury genuine PASS opportunities under noise instead of
# surfacing the best real trades. So status is the primary sort key; the
# spec's three criteria apply within each status group. "NO EDGE" (only ever
# shown when min_edge <= 0, e.g. --min-edge -1 for diagnostics) ranks last --
# those are known-unprofitable candidates, not opportunities.
_STATUS_RANK = {"PASS": 0, "REVIEW": 1, "NO EDGE": 2}


def rank_opportunities(opportunities: list[ArbOpportunity]) -> list[ArbOpportunity]:
    """PASS before REVIEW, then net_edge desc, then estimated_depth desc,
    then match_confidence desc.

    Unknown depth (None) sorts last within its status group -- it shouldn't
    outrank a measured one.
    """
    def sort_key(opp: ArbOpportunity) -> tuple[int, float, float, float]:
        depth = opp.estimated_depth if opp.estimated_depth is not None else float("-inf")
        return (
            _STATUS_RANK.get(opp.status, 99),
            -opp.net_edge,
            -depth,
            -opp.match_confidence,
        )

    return sorted(opportunities, key=sort_key)
