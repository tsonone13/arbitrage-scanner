"""Shared data model for the arbitrage scanner pipeline."""

from dataclasses import dataclass

# slots=True on all three: no subclassing or dynamic attribute assignment
# anywhere in this codebase (confirmed via grep), so this is a pure
# memory-layout change -- every field access, dataclasses.replace() call,
# and equality comparison behaves identically. NormalizedPrice is by far
# the highest-volume of the three (thousands of instances per category
# scan), so it's the one that matters: measured directly (2026-08-20)
# against this exact field shape, slots cuts per-instance memory from
# ~430 to ~374 bytes (13%). Modest in absolute terms next to a scan's
# real cost (the raw fetched catalog dominates, not these objects), but
# free and safe, so worth taking.


@dataclass(slots=True)
class NormalizedPrice:
    """One venue's executable bid/ask quote for a single market outcome.

    Prices are always decimal probabilities in [0, 1] (e.g. 96.4 cents -> 0.964),
    never chart probabilities, midpoints, or last-traded prices.
    """

    venue: str
    market_id: str
    canonical_market_name: str
    raw_market_name: str
    outcome_name: str
    market_type: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    depth: float | None
    volume: float | None
    close_time: str | None
    resolution_notes: str | None
    timestamp: str | None
    # Optional, venue-dependent liquidity detail used only by slippage.py --
    # arb_engine.py's detection logic never reads these. yes_ask_size/
    # no_ask_size are top-of-book size at yes_ask/no_ask (both venues can
    # provide this without an API key). yes_ask_book/no_ask_book are the
    # full multi-level ask book, cheapest first, when a venue exposes one
    # publicly (currently just Polymarket's CLOB) -- None elsewhere.
    yes_ask_size: float | None = None
    no_ask_size: float | None = None
    yes_ask_book: list[tuple[float, float]] | None = None
    no_ask_book: list[tuple[float, float]] | None = None
    # Taker fee coefficient (Theta) for fee = Theta * quantity * price * (1 -
    # price), the formula both venues confirm using (Kalshi's help center:
    # "a transaction fee on the expected earnings on the contract" = the same
    # p(1-p) shape; Polymarket's docs: "fee = C x feeRate x p x (1-p)").
    # Polymarket exposes the exact per-market rate publicly (market's own
    # feeSchedule.rate); Kalshi's precise per-market rate needs a signed API
    # key this project doesn't use, so KalshiImporter fills in the
    # publicly-documented default (0.07) rather than a verified per-market
    # figure -- see slippage.py for how that's surfaced to the user.
    taker_fee_rate: float | None = None


@dataclass(slots=True)
class MarketGroup:
    """A set of NormalizedPrice quotes believed to reference the same market."""

    canonical_market_name: str
    market_type: str
    outcomes: list[str]
    prices: list[NormalizedPrice]
    match_confidence: float


@dataclass(slots=True)
class ArbOpportunity:
    """A single detected arbitrage trade, ready for ranking and display."""

    market_name: str
    outcome_name: str | None
    arb_type: str
    legs: list[dict]
    total_cost: float
    guaranteed_payout: float
    gross_edge: float
    fee_buffer: float
    net_edge: float
    estimated_depth: float | None
    match_confidence: float
    status: str
    notes: str | None = None
