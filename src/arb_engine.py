"""Pure arbitrage math.

No API calls, no venue-specific knowledge -- every function here only reads
NormalizedPrice / MarketGroup objects and returns ArbOpportunity objects.
That boundary is what makes it possible to trust this file unchanged as
more venues get wired in: it cannot know or care where a price came from.
"""

from itertools import combinations

from models import ArbOpportunity, MarketGroup, NormalizedPrice

GUARANTEED_PAYOUT_BINARY = 1.00


def _leg(action: str, side: str, price: NormalizedPrice, ask: float) -> dict:
    return {
        "action": action,
        "side": side,
        "venue": price.venue,
        "outcome": price.outcome_name,
        "price": ask,
        # Identifier only, for slippage.py to look the source row back up by
        # (venue, market_id) -- not read anywhere in this file's own math.
        "market_id": price.market_id,
    }


def _min_depth(*prices: NormalizedPrice) -> float | None:
    depths = [p.depth for p in prices if p.depth is not None]
    return min(depths) if depths else None


def _group_by_outcome(market_group: MarketGroup) -> dict[str, list[NormalizedPrice]]:
    by_outcome: dict[str, list[NormalizedPrice]] = {}
    for price in market_group.prices:
        by_outcome.setdefault(price.outcome_name, []).append(price)
    return by_outcome


def _close_dates_conflict(a: NormalizedPrice, b: NormalizedPrice) -> bool:
    """True only if both rows report a close_time and the calendar dates disagree.

    A cheap extra check on top of matching canonical_market_name/outcome_name:
    two rows that are genuinely the same bet should resolve on the same date.
    If either side is missing close_time we can't tell, so we don't block --
    this only catches a *disagreement* we can actually see, e.g. one leg
    closing in 2027 and the other in 2029, which is a decisive sign they are
    different propositions in a date ladder, not a hedge of each other.
    """
    if not a.close_time or not b.close_time:
        return False
    return a.close_time[:10] != b.close_time[:10]


def check_binary_cross_venue_arbs(
    market_group: MarketGroup,
    fee_buffer: float = 0.003,
    min_edge: float = 0.005,
) -> list[ArbOpportunity]:
    """For every outcome in the group, compare every *cross-venue* pair's YES/NO asks.

    Deliberately cross-venue only. On a single, well-functioning venue, YES
    and NO for the same proposition are two sides of one order book (a NO
    ask is mechanically ~1 - a YES bid), so they structurally can't cross --
    a same-venue "match" here almost always means the matcher grouped two
    different propositions together, not a real edge. That's not
    theoretical: canonical_market_name currently comes straight from each
    venue's own market title (see kalshi_importer.py / polymarket_importer.py),
    and Kalshi's own title field has been observed to be wrong -- ticker
    KXRECOGROC-29 is genuinely about the Republic of China's recognition
    (see its rules_primary) but its title field reads "Will Trump recognize
    Somaliland?", identical to the real Somaliland market's title. Without
    this guard, that venue-side data error alone was enough to produce a
    fake "arbitrage" between two unrelated bets. Real arbitrage is supposed
    to come from two *independent* order books genuinely disagreeing, which
    same-venue comparisons can't demonstrate anyway.
    """
    opportunities: list[ArbOpportunity] = []

    for outcome_name, outcome_prices in _group_by_outcome(market_group).items():
        for a, b in combinations(outcome_prices, 2):
            if a.venue == b.venue:
                continue
            if _close_dates_conflict(a, b):
                continue
            for yes_side, no_side in ((a, b), (b, a)):
                if yes_side.yes_ask is None or no_side.no_ask is None:
                    continue
                total_cost = yes_side.yes_ask + no_side.no_ask
                gross_edge = GUARANTEED_PAYOUT_BINARY - total_cost
                net_edge = gross_edge - fee_buffer
                if net_edge < min_edge:
                    continue
                # status reflects real profitability (net_edge > 0), not just
                # "cleared min_edge" -- those are the same thing at the sane
                # default (0.005) but diverge the moment min_edge <= 0 (e.g.
                # --min-edge -1 for diagnostics), where a trade that costs
                # more than its guaranteed payout would otherwise still get
                # labeled "PASS" purely because it cleared a permissive
                # filter, not because it's a real opportunity.
                if net_edge > 0:
                    status, notes = "PASS", None
                else:
                    status = "NO EDGE"
                    notes = (
                        f"total_cost {total_cost:.4f} vs guaranteed_payout "
                        f"{GUARANTEED_PAYOUT_BINARY:.4f} -- not profitable "
                        "after fees. Shown only because --min-edge allowed it."
                    )
                opportunities.append(ArbOpportunity(
                    market_name=market_group.canonical_market_name,
                    outcome_name=outcome_name,
                    arb_type="binary",
                    legs=[
                        _leg("BUY", "YES", yes_side, yes_side.yes_ask),
                        _leg("BUY", "NO", no_side, no_side.no_ask),
                    ],
                    total_cost=total_cost,
                    guaranteed_payout=GUARANTEED_PAYOUT_BINARY,
                    gross_edge=gross_edge,
                    fee_buffer=fee_buffer,
                    net_edge=net_edge,
                    estimated_depth=_min_depth(yes_side, no_side),
                    match_confidence=market_group.match_confidence,
                    status=status,
                    notes=notes,
                ))
    return opportunities


def estimate_profit(bankroll: float, total_cost: float, guaranteed_payout: float) -> dict[str, float]:
    """Units of the basket buyable with `bankroll`, and the resulting payout/profit."""
    if total_cost <= 0:
        raise ValueError(f"total_cost must be positive, got {total_cost}")

    units = bankroll / total_cost
    payout = units * guaranteed_payout
    profit = payout - bankroll
    return_pct = (profit / bankroll) * 100 if bankroll else 0.0

    return {
        "units": units,
        "payout": payout,
        "profit": profit,
        "return_pct": return_pct,
    }
