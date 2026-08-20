"""Ranks detected opportunities so the best trades surface first."""

from models import ArbOpportunity

# Status is the primary sort key, then the criteria below apply within each
# status group. "NO EDGE" (only ever shown when min_edge <= 0, e.g.
# --min-edge -1 for diagnostics) ranks last -- those are known-unprofitable
# candidates, not opportunities.
_STATUS_RANK = {"PASS": 0, "NO EDGE": 1}


def rank_opportunities(opportunities: list[ArbOpportunity]) -> list[ArbOpportunity]:
    """PASS before NO EDGE, then net_edge desc, then estimated_depth desc,
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
