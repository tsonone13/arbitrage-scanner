"""Pure arbitrage math.

No API calls, no venue-specific knowledge -- every function here only reads
NormalizedPrice / MarketGroup objects and returns ArbOpportunity objects.
That boundary is what makes it possible to trust this file unchanged as
more venues get wired in: it cannot know or care where a price came from.
"""

from itertools import combinations

from models import ArbOpportunity, MarketGroup, NormalizedPrice

GUARANTEED_PAYOUT_BINARY = 1.00

# A full-book basket's gross edge, as a fraction of its guaranteed payout, above
# which we no longer trust the "PASS" label. Live venue "mutually exclusive"
# flags (Kalshi) / "negRisk" flags (Polymarket) only promise that at most one
# listed outcome can win -- not that exactly one must. A real event can carry
# hidden probability mass outside the listed outcomes entirely: Kalshi's
# "What will be the 51st state" lists 8 candidate territories with no listed
# outcome for "no new state at all", even though a sibling Kalshi market prices
# that base case at ~87%. For a full YES basket that means most of the cost
# buys nothing (none of the 8 pay out if the hidden case happens) -- a real
# risk, not just a modeling nuance. A real, fully-covered arbitrage edge is
# almost always small (a few percent); an edge this large is far more likely
# to mean the outcome set is incomplete, so we flag it instead of asserting
# it's safe to trade.
SUSPICIOUS_EDGE_RATIO = 0.20


def _classify(net_edge: float, gross_edge: float, guaranteed_payout: float, base_note: str) -> tuple[str, str]:
    # Real profitability (net_edge > 0) first, independent of whatever
    # min_edge let this through -- a caller passing min_edge <= 0 for
    # diagnostics should never see a losing basket labeled "PASS" just
    # because it cleared a permissive filter.
    if net_edge <= 0:
        return "NO EDGE", (
            f"{base_note} -- not profitable after fees. Shown only because "
            "min_edge allowed it."
        )
    if guaranteed_payout > 0 and (gross_edge / guaranteed_payout) >= SUSPICIOUS_EDGE_RATIO:
        return "REVIEW", (
            f"{base_note} -- edge is unusually large. This usually means the "
            "listed outcome set is not actually exhaustive (there may be a "
            "hidden 'none of these' probability not broken out as its own "
            "market). Double-check the event's full resolution rules before "
            "trusting this size of edge."
        )
    return "PASS", base_note


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


def _cheapest_ask(
    prices: list[NormalizedPrice], field: str
) -> tuple[float | None, NormalizedPrice | None]:
    best_price: float | None = None
    best_row: NormalizedPrice | None = None
    for price in prices:
        ask = getattr(price, field)
        if ask is None:
            continue
        if best_price is None or ask < best_price:
            best_price = ask
            best_row = price
    return best_price, best_row


def check_full_book_yes_arbs(
    market_group: MarketGroup,
    fee_buffer: float = 0.003,
    min_edge: float = 0.005,
) -> list[ArbOpportunity]:
    """Buy the cheapest YES on every outcome; exactly one pays $1.

    Only valid on a complete, mutually-exclusive, exhaustive outcome set,
    so this is guarded by market_type == "multi_outcome" (set by
    market_matcher.py based on what the importers reported -- see the
    correctness notes in kalshi_importer.py / polymarket_importer.py for
    why that flag matters).
    """
    if market_group.market_type != "multi_outcome" or len(market_group.outcomes) < 2:
        return []

    by_outcome = _group_by_outcome(market_group)
    legs: list[dict] = []
    depth_sources: list[NormalizedPrice] = []
    total_cost = 0.0
    for outcome_name in market_group.outcomes:
        ask, row = _cheapest_ask(by_outcome.get(outcome_name, []), "yes_ask")
        if ask is None or row is None:
            return []  # incomplete basket -- can't guarantee the payout
        total_cost += ask
        legs.append(_leg("BUY", "YES", row, ask))
        depth_sources.append(row)

    guaranteed_payout = GUARANTEED_PAYOUT_BINARY
    gross_edge = guaranteed_payout - total_cost
    net_edge = gross_edge - fee_buffer
    if net_edge < min_edge:
        return []

    status, notes = _classify(net_edge, gross_edge, guaranteed_payout, f"{len(legs)}-outcome full YES basket")
    return [ArbOpportunity(
        market_name=market_group.canonical_market_name,
        outcome_name=None,
        arb_type="fullbook_yes",
        legs=legs,
        total_cost=total_cost,
        guaranteed_payout=guaranteed_payout,
        gross_edge=gross_edge,
        fee_buffer=fee_buffer,
        net_edge=net_edge,
        estimated_depth=_min_depth(*depth_sources),
        match_confidence=market_group.match_confidence,
        status=status,
        notes=notes,
    )]


def check_full_book_no_arbs(
    market_group: MarketGroup,
    fee_buffer: float = 0.003,
    min_edge: float = 0.005,
) -> list[ArbOpportunity]:
    """Buy the cheapest NO on every outcome; N-1 of the N contracts pay $1.

    Same completeness requirement and market_type guard as
    check_full_book_yes_arbs.
    """
    if market_group.market_type != "multi_outcome" or len(market_group.outcomes) < 2:
        return []

    by_outcome = _group_by_outcome(market_group)
    n_outcomes = len(market_group.outcomes)
    legs: list[dict] = []
    depth_sources: list[NormalizedPrice] = []
    total_cost = 0.0
    for outcome_name in market_group.outcomes:
        ask, row = _cheapest_ask(by_outcome.get(outcome_name, []), "no_ask")
        if ask is None or row is None:
            return []  # incomplete basket -- can't guarantee the payout
        total_cost += ask
        legs.append(_leg("BUY", "NO", row, ask))
        depth_sources.append(row)

    guaranteed_payout = float(n_outcomes - 1)
    gross_edge = guaranteed_payout - total_cost
    net_edge = gross_edge - fee_buffer
    if net_edge < min_edge:
        return []

    status, notes = _classify(
        net_edge, gross_edge, guaranteed_payout,
        f"{n_outcomes}-outcome full NO basket ({n_outcomes - 1} of {n_outcomes} pay out)",
    )
    return [ArbOpportunity(
        market_name=market_group.canonical_market_name,
        outcome_name=None,
        arb_type="fullbook_no",
        legs=legs,
        total_cost=total_cost,
        guaranteed_payout=guaranteed_payout,
        gross_edge=gross_edge,
        fee_buffer=fee_buffer,
        net_edge=net_edge,
        estimated_depth=_min_depth(*depth_sources),
        match_confidence=market_group.match_confidence,
        status=status,
        notes=notes,
    )]


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
