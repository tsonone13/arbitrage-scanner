"""Command-line entry point: importers -> normalizer -> matcher -> arb engine
-> ranker -> terminal output.

Read-only. Nothing in this pipeline places an order, signs a transaction, or
touches a private API key.

Two live-scanning modes, both built around "match cheaply first, price
expensively second":
- No --category: the fast path. Only the markets already in the verified
  crosswalk (data/market_pairs.csv) get fetched at all, via direct
  batch-lookup-by-id calls -- a handful of requests instead of paginating
  through the whole catalog. This is what routine "is there a real arb
  right now" runs should use.
- --category <name>: the discovery path, for finding *new* crosswalk
  candidates. Scoped to one category on both venues. Kalshi's pricing is
  free (bundled in its /events response) so it's still fetched eagerly;
  Polymarket's is not (a separate CLOB call per market), so only markets
  that already matched something -- the crosswalk or a title-candidate --
  ever get a real order-book price. Everything else in the category is
  listed but never priced, since there's nothing to compare it to anyway.
"""

import argparse
import concurrent.futures
from pathlib import Path

import terminal_reporter
from arb_engine import check_binary_cross_venue_arbs, estimate_profit
from importers.csv_importer import CsvImporter
from importers.kalshi_importer import KalshiImporter
from importers.mock_importer import load_all_mock_prices
from importers.polymarket_importer import PolymarketImporter
from market_matcher import apply_market_pairs, find_title_candidates, load_market_pairs, match_markets
from models import ArbOpportunity, MarketGroup, NormalizedPrice
from opportunity_ranker import rank_opportunities
from slippage import opportunity_sizing

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# CLI-facing category name -> each venue's own filter value. Kalshi's site
# groups things differently than its API's `category` field (its own
# "Culture" tab is titled "Culture ... & Entertainment Odds", confirmed by
# reading the rendered page), and there's no server-side filter for it, so
# this is applied client-side in KalshiImporter. Polymarket's `pop-culture`
# tag_slug is a real server-side filter (its own label for tag id 596 is
# literally "Culture", confirmed via GET /tags/slug/pop-culture) applied at
# the API level, which is why category scans are much cheaper on that side.
#
# Every entry here was verified two ways: (1) Kalshi's category value is one
# of the literal strings its /events endpoint actually returns (sampled
# 4,000 open events and tabulated the distribution), and (2) Polymarket's
# tag_slug was checked to actually exist and return on-topic results via
# GET /tags/slug/<slug> and a sample /events?tag_slug=<slug> call -- not
# assumed from a plausible-looking name. Kalshi's "Mentions" and "Companies"
# categories, and Polymarket's "/mentions" nav page, are left out: Polymarket
# fragments "mentions" into dozens of specific tags (e.g. "Trump Speech
# Mentions") rather than one general one, so there's no single verified
# tag_slug to pair against them.
_CATEGORY_ALIASES: dict[str, dict[str, str]] = {
    "culture": {"kalshi_category": "Entertainment", "polymarket_tag_slug": "pop-culture"},
    "politics": {"kalshi_category": "Politics", "polymarket_tag_slug": "politics"},
    "elections": {"kalshi_category": "Elections", "polymarket_tag_slug": "elections"},
    "sports": {"kalshi_category": "Sports", "polymarket_tag_slug": "sports"},
    "financials": {"kalshi_category": "Financials", "polymarket_tag_slug": "finance"},
    "economics": {"kalshi_category": "Economics", "polymarket_tag_slug": "economy"},
    "tech": {"kalshi_category": "Science and Technology", "polymarket_tag_slug": "tech"},
    "climate": {"kalshi_category": "Climate and Weather", "polymarket_tag_slug": "weather"},
}

_LIVE_SOURCES = ("kalshi", "polymarket", "live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prediction-market arbitrage scanner (read-only).")
    parser.add_argument(
        "--source",
        choices=["mock", "csv", "kalshi", "polymarket", "live"],
        default="mock",
        help="Where to load prices from. 'live' = Kalshi + Polymarket combined. Default: mock",
    )
    parser.add_argument(
        "--category",
        choices=sorted(_CATEGORY_ALIASES) + ["all"],
        default=None,
        help=(
            "Discovery mode: scope a kalshi/polymarket/live scan to one "
            "category (or 'all' of them at once) and search for new "
            "title-match candidates. Slower than the default fast path "
            "(crosswalk-only) since it has to list the catalog first -- "
            "'all' is the slowest of all, typically 30-60s+. "
            "Default: off (fast path)."
        ),
    )
    parser.add_argument("--min-edge", type=float, default=0.005, help="Minimum net edge to report. Default: 0.005")
    parser.add_argument("--fee-buffer", type=float, default=0.003, help="Flat fee/slippage buffer. Default: 0.003")
    parser.add_argument("--top", type=int, default=20, help="Max opportunities to print. Default: 20")
    parser.add_argument("--bankroll", type=float, default=1000, help="Bankroll for profit estimates. Default: 1000")
    return parser.parse_args()


def _load_from_venue(label: str, loader) -> list[NormalizedPrice]:
    try:
        return loader()
    except Exception as exc:
        terminal_reporter.console.print(f"[yellow]Warning: {label} load failed, skipping: {exc}[/yellow]")
        return []


def _crosswalk_ids(pairs: dict[tuple[str, str], tuple[str, str]], venue: str) -> list[str]:
    return [market_id for (v, market_id) in pairs if v == venue]


def load_prices_fast(
    source: str, pairs: dict[tuple[str, str], tuple[str, str]]
) -> list[NormalizedPrice]:
    """The default live path: fetch only what's already verified. A handful
    of direct lookups instead of paginating the whole catalog.
    """
    prices: list[NormalizedPrice] = []
    if source in ("kalshi", "live"):
        tickers = _crosswalk_ids(pairs, "Kalshi")
        prices += _load_from_venue("Kalshi", lambda: KalshiImporter().get_normalized_prices_for_tickers(tickers))
    if source in ("polymarket", "live"):
        ids = _crosswalk_ids(pairs, "Polymarket")
        prices += _load_from_venue(
            "Polymarket", lambda: PolymarketImporter().get_normalized_prices_for_ids(ids)
        )
    return prices


# Confirmed live (2026-08-20) on a real 512MB deployment (Render free
# tier): a single category scan at the old defaults (Kalshi's own 6000,
# Polymarket 10000) was enough to get the instance OOM-killed. max_events
# caps EVENTS fetched, not the resulting NormalizedPrice row count -- each
# Kalshi/Polymarket "event" can nest many nested markets, and the ratio
# varies hugely by category. Measured directly against the worst real
# case (sports, Polymarket's own sports events average ~16 nested markets
# each): 1000/1000 events produced 810 Kalshi + 15,932 Polymarket rows at
# ~400MB RSS for the fetch step alone, before fuzzy-matching/pricing even
# runs. 500/250 produced 662 + 4,221 rows at ~223MB RSS for the same
# step -- confirmed (see build_category_scan_result's own memory check)
# this leaves real headroom for the rest of the pipeline on top of the
# baseline FastAPI/uvicorn process. These are deliberately asymmetric
# (Polymarket much lower) because Polymarket's events nest far more
# markets per event than Kalshi's for this project's categories -- cutting
# both by the same fraction would have left Polymarket dominating memory
# anyway. This only reduces how deep into each venue's long tail of
# thin/illiquid markets a scan reaches, not whether the scan runs at all;
# load_prices_for_all_categories() (the CLI-only `--category all` path,
# which runs on the user's own machine, not this memory-constrained
# server) is untouched.
_CATEGORY_SCAN_KALSHI_MAX_EVENTS = 500
_CATEGORY_SCAN_POLYMARKET_MAX_EVENTS = 250


def load_prices_for_category(
    source: str, category: str, pairs: dict[tuple[str, str], tuple[str, str]]
) -> tuple[list[NormalizedPrice], int, list[tuple[NormalizedPrice, NormalizedPrice]]]:
    """Discovery path: list one category on both venues, find title-match
    candidates from metadata alone, then price only the crosswalk + matched
    subset. Returns (priced_prices, total_markets_listed, candidates).

    The Kalshi and Polymarket listing fetches are independent network calls
    (different hosts, no data dependency between them), so for source="live"
    they run concurrently in a thread pool instead of one after another --
    both are synchronous `requests` calls under the hood, so this overlaps
    their I/O without needing an async rewrite of the importers. Combined
    with list_markets() now being cached across categories (see
    kalshi_importer.py / polymarket_importer.py's _catalog_cache), a
    category scan that isn't the first one in a session is usually
    dominated by pricing the matched subset below, not listing.
    """
    alias = _CATEGORY_ALIASES[category]
    kalshi_prices: list[NormalizedPrice] = []
    poly_metadata: list[NormalizedPrice] = []

    kalshi_fetch = (
        (lambda: KalshiImporter(
            max_events=_CATEGORY_SCAN_KALSHI_MAX_EVENTS, category=alias["kalshi_category"]
        ).get_normalized_prices())
        if source in ("kalshi", "live") else None
    )
    # Metadata-only, no CLOB calls -- cheap enough to ask for more than the
    # fast-path default (500) and just let it stop naturally once a tag
    # runs out of results (list_markets() handles that) -- see the module
    # comment above for why this is no longer as high as it used to be.
    poly_fetch = (
        (lambda: PolymarketImporter(
            max_events=_CATEGORY_SCAN_POLYMARKET_MAX_EVENTS, tag_slug=alias["polymarket_tag_slug"]
        ).get_market_metadata())
        if source in ("polymarket", "live") else None
    )

    if kalshi_fetch and poly_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            kalshi_future = pool.submit(_load_from_venue, "Kalshi", kalshi_fetch)
            poly_future = pool.submit(_load_from_venue, "Polymarket", poly_fetch)
            kalshi_prices = kalshi_future.result()
            poly_metadata = poly_future.result()
    elif kalshi_fetch:
        kalshi_prices = _load_from_venue("Kalshi", kalshi_fetch)
    elif poly_fetch:
        poly_metadata = _load_from_venue("Polymarket", poly_fetch)

    total_listed = len(kalshi_prices) + len(poly_metadata)
    candidates = find_title_candidates(kalshi_prices + poly_metadata, pairs) if source == "live" else []

    worth_pricing: set[str] = set(_crosswalk_ids(pairs, "Polymarket"))
    for a, b in candidates:
        poly_row = a if a.venue == "Polymarket" else b
        worth_pricing.add(poly_row.market_id)
    # Category-scoped Polymarket ids only -- fetching a crosswalk id that
    # isn't even in this category would be pricing something out of scope.
    in_category_poly_ids = {p.market_id for p in poly_metadata}
    worth_pricing &= in_category_poly_ids

    poly_priced: list[NormalizedPrice] = []
    if worth_pricing:
        poly_priced = _load_from_venue(
            "Polymarket (pricing matches)",
            lambda: PolymarketImporter().get_normalized_prices_for_ids(list(worth_pricing)),
        )

    return kalshi_prices + poly_priced, total_listed, candidates


def load_prices_for_all_categories(
    source: str, pairs: dict[tuple[str, str], tuple[str, str]]
) -> tuple[list[NormalizedPrice], int, list[tuple[NormalizedPrice, NormalizedPrice]]]:
    """Discovery path across every verified category at once. Same
    match-then-price idea as load_prices_for_category(), scaled up --
    deliberately NOT 8 sequential calls to that function, since Kalshi has
    no server-side category filter and would end up re-paginating its
    entire catalog once per category for no reason. Instead: one unfiltered
    Kalshi fetch (its own category field just rides along on each row,
    unused for filtering here), one metadata-only Polymarket fetch per
    verified tag_slug (each is cheap -- no CLOB calls), then a single
    batched pricing call across whatever matched anywhere.
    """
    kalshi_prices: list[NormalizedPrice] = []
    poly_metadata: list[NormalizedPrice] = []

    if source in ("kalshi", "live"):
        kalshi_prices = _load_from_venue(
            "Kalshi", lambda: KalshiImporter(max_events=8000).get_normalized_prices()
        )
    if source in ("polymarket", "live"):
        seen_market_ids: set[str] = set()
        for alias in _CATEGORY_ALIASES.values():
            batch = _load_from_venue(
                f"Polymarket ({alias['polymarket_tag_slug']})",
                lambda a=alias: PolymarketImporter(
                    max_events=10000, tag_slug=a["polymarket_tag_slug"]
                ).get_market_metadata(),
            )
            # Polymarket tags aren't mutually exclusive -- a market tagged
            # both "elections" and "politics" would otherwise show up once
            # per matching tag_slug fetch and get double-counted below.
            for row in batch:
                if row.market_id not in seen_market_ids:
                    seen_market_ids.add(row.market_id)
                    poly_metadata.append(row)

    total_listed = len(kalshi_prices) + len(poly_metadata)
    candidates = find_title_candidates(kalshi_prices + poly_metadata, pairs) if source == "live" else []

    worth_pricing: set[str] = set(_crosswalk_ids(pairs, "Polymarket"))
    for a, b in candidates:
        poly_row = a if a.venue == "Polymarket" else b
        worth_pricing.add(poly_row.market_id)
    worth_pricing &= {p.market_id for p in poly_metadata}

    poly_priced: list[NormalizedPrice] = []
    if worth_pricing:
        poly_priced = _load_from_venue(
            "Polymarket (pricing matches)",
            lambda: PolymarketImporter().get_normalized_prices_for_ids(list(worth_pricing)),
        )

    return kalshi_prices + poly_priced, total_listed, candidates


def load_prices_flat(source: str) -> list[NormalizedPrice]:
    """mock/csv only -- no crosswalk, no categories, just load everything."""
    if source == "mock":
        return load_all_mock_prices()
    if source == "csv":
        csv_path = _DATA_DIR / "sample_prices.csv"
        return CsvImporter(str(csv_path)).get_normalized_prices()
    raise ValueError(f"Unknown flat source: {source}")


def run_checks(
    groups: list[MarketGroup], fee_buffer: float, min_edge: float
) -> list[ArbOpportunity]:
    opportunities: list[ArbOpportunity] = []
    for group in groups:
        opportunities.extend(check_binary_cross_venue_arbs(group, fee_buffer, min_edge))
    return opportunities


def main() -> None:
    args = parse_args()
    if args.top <= 0:
        raise SystemExit("--top must be a positive integer")
    if args.bankroll <= 0:
        raise SystemExit("--bankroll must be a positive number")

    terminal_reporter.print_header(args.source)
    pairs = load_market_pairs(str(_DATA_DIR / "market_pairs.csv"))

    total_listed: int | None = None
    candidates: list[tuple[NormalizedPrice, NormalizedPrice]] = []

    try:
        if args.source not in _LIVE_SOURCES:
            prices = load_prices_flat(args.source)
        elif args.category == "all":
            prices, total_listed, candidates = load_prices_for_all_categories(args.source, pairs)
        elif args.category:
            prices, total_listed, candidates = load_prices_for_category(args.source, args.category, pairs)
        else:
            prices = load_prices_fast(args.source, pairs)
    except Exception as exc:
        terminal_reporter.console.print(f"[bold red]Failed to load prices (source={args.source}): {exc}[/bold red]")
        raise SystemExit(1) from exc

    if args.source in _LIVE_SOURCES and not args.category and not pairs:
        terminal_reporter.console.print(
            "[yellow]data/market_pairs.csv has no verified pairs yet, so there's nothing to fetch on the "
            "fast path. Run with --category <name> to search for new candidates to verify.[/yellow]\n"
        )

    prices_by_venue: dict[str, int] = {}
    for price in prices:
        prices_by_venue[price.venue] = prices_by_venue.get(price.venue, 0) + 1

    prices = apply_market_pairs(prices, pairs)
    prices_by_key = {(p.venue, p.market_id): p for p in prices}
    groups = match_markets(prices)
    opportunities = run_checks(groups, args.fee_buffer, args.min_edge)
    ranked = rank_opportunities(opportunities)

    terminal_reporter.print_scan_summary(prices_by_venue, len(groups), len(ranked), total_listed=total_listed)

    if not ranked:
        terminal_reporter.print_no_opportunities()
    else:
        top = ranked[: args.top]
        for opp in top:
            estimate = estimate_profit(args.bankroll, opp.total_cost, opp.guaranteed_payout)
            sizing = opportunity_sizing(opp, prices_by_key)
            terminal_reporter.print_opportunity(opp, args.bankroll, estimate, sizing)
        terminal_reporter.print_top_n_notice(len(top), len(ranked))

    terminal_reporter.print_candidate_matches(candidates)


if __name__ == "__main__":
    main()
