"""Shapes a live fast-path scan into the JSON contract the website's
GET /api/opportunities endpoint returns. No FastAPI import here -- kept
independently testable, the same way slippage.py is tested without any
web-framework dependency.

Reuses the existing pipeline unmodified: load_prices_fast (main.py),
apply_market_pairs/match_markets (market_matcher.py),
check_binary_cross_venue_arbs (arb_engine.py), rank_opportunities
(opportunity_ranker.py), opportunity_sizing (slippage.py). This module
only shapes their output for display -- it adds no new detection logic
and never touches arb_engine.py's math.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from arb_engine import ArbOpportunity, _close_dates_conflict, check_binary_cross_venue_arbs
from categories import CATEGORY_LABELS, CATEGORY_ORDER, load_market_categories
from main import load_prices_fast, load_prices_for_category
from market_matcher import apply_market_pairs, load_market_pairs, match_markets
from models import MarketGroup, NormalizedPrice
from opportunity_ranker import rank_opportunities
from slippage import opportunity_sizing
from ttl_cache import TTLCache

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_FEE_BUFFER = 0.003
# Guaranteed to never filter out a comparison: net_edge = (1 - total_cost) -
# fee_buffer, and total_cost is in [0, 2] (two asks each in [0, 1]), so
# net_edge can't go below -1 - fee_buffer. -2 clears that with room to
# spare, so "PASS or NO EDGE, always see the full compared set" is exact,
# not approximate.
_MIN_EDGE = -2.0
_ACTIVE_VENUES = ("Kalshi", "Polymarket")
_STATUS_RANK = {"PASS": 0, "FEE_ADJUSTED_NO_EDGE": 1, "NO_EDGE": 2}


def _has_any_quote(price: NormalizedPrice) -> bool:
    return price.yes_ask is not None or price.no_ask is not None


def _uncompared_reason(rows: list[NormalizedPrice]) -> tuple[str, str | None]:
    """Why a crosswalk-covered market produced zero routes this scan.

    Two genuinely different situations were getting conflated here during
    testing: a venue not returning a usable price at all, versus BOTH
    venues quoting fine but arb_engine's own close-date guard blocking the
    comparison (confirmed live: the FIDE Chess series' Kalshi/Polymarket
    close dates disagree at the calendar-day level -- exactly the kind of
    case check_binary_cross_venue_arbs's _close_dates_conflict guard exists
    to catch, even though these specific pairs were human-verified into the
    crosswalk anyway; README documents the same thing for Big Brother).
    Reporting the second case as "no quote" would be actively misleading,
    so it gets its own reason instead of being folded in.
    """
    by_venue = {r.venue: r for r in rows if _has_any_quote(r)}
    if set(_ACTIVE_VENUES) - set(by_venue):
        return "no_live_quote", None
    kalshi, poly = by_venue.get("Kalshi"), by_venue.get("Polymarket")
    if kalshi and poly and _close_dates_conflict(kalshi, poly):
        return (
            "close_date_mismatch",
            f"Kalshi closes {kalshi.close_time}, Polymarket closes {poly.close_time} "
            "-- both quoted, but the engine won't compare mismatched close dates.",
        )
    return "not_directly_comparable", None


def _route_label(opp: ArbOpportunity) -> str:
    yes_leg = next(leg for leg in opp.legs if leg["side"] == "YES")
    no_leg = next(leg for leg in opp.legs if leg["side"] == "NO")
    return f"Buy YES on {yes_leg['venue']} / NO on {no_leg['venue']}"


def _shape_sizing(opp: ArbOpportunity, prices_by_key: dict) -> dict | None:
    sizing = opportunity_sizing(opp, prices_by_key)
    if sizing is None:
        return None
    legs_fees = []
    for leg in opp.legs:
        side = leg["side"].lower()
        rate = sizing.get(f"{side}_fee_rate")
        fee = sizing.get(f"{side}_fees")
        if rate is None or fee is None:
            continue
        legs_fees.append({
            "venue": leg["venue"],
            "side": leg["side"],
            "fee_amount": round(fee, 4),
            "fee_rate": rate,
            # Per-leg, not the aggregate fee_rate_is_estimate flag -- mirrors
            # terminal_reporter._sizing_lines' own per-leg check exactly.
            "fee_rate_is_estimate": leg["venue"] == "Kalshi",
        })
    return {
        "max_units": sizing["optimal_units"],
        "avg_cost_per_unit": sizing["avg_cost_per_unit"],
        "total_fees": round(sizing["total_fees"], 4),
        "estimated_profit_after_fees": round(sizing["estimated_profit"], 4),
        "limiting_factor": sizing["limiting_factor"],
        "legs_fees": legs_fees,
    }


def _real_fee_status(raw_status: str, sizing: dict | None) -> str:
    """Downgrade a flat-buffer PASS when real, per-venue trading fees --
    not the flat fee_buffer approximation arb_engine.py uses -- would erase
    the entire edge before a single unit is tradeable.

    Confirmed on live data (2026-08-19): 4 of 6 markets the flat 0.3% buffer
    classified PASS had sizing["estimated_profit"] == 0 -- max_profitable_units
    (slippage.py) found real fees ate the edge at the very first book level,
    because Kalshi's real taker coefficient (~0.07) and Polymarket's own
    posted per-market rate can each individually exceed the flat 0.3% buffer.
    A flat buffer is a simplification (arb_engine.py has no venue-specific
    fee knowledge by design); PASS/"ARB FOUND" must mean a real, executable
    profit, not just "cleared the approximation." sizing is None only if the
    2-leg lookup genuinely fails (shouldn't happen for a freshly-detected
    opportunity) -- treated the same as "can't confirm profit," since
    unverifiable is not the same as verified profitable.
    """
    if raw_status != "PASS":
        return "NO_EDGE"
    if sizing is None or sizing["estimated_profit_after_fees"] <= 0:
        return "FEE_ADJUSTED_NO_EDGE"
    return "PASS"


def _shape_route(opp: ArbOpportunity, prices_by_key: dict) -> dict:
    net_edge_pct = round(opp.net_edge * 100, 2)
    # Computed once, for every route regardless of status (matches what
    # terminal_reporter.py already does for the CLI today) -- it's pure
    # in-memory math, no network I/O -- and reused below for both the
    # status determination and the sizing payload, so this isn't computed
    # twice.
    sizing = _shape_sizing(opp, prices_by_key)
    return {
        "route_label": _route_label(opp),
        "status": _real_fee_status(opp.status, sizing),
        "legs": [
            {
                "action": leg["action"], "side": leg["side"], "venue": leg["venue"],
                "outcome": leg["outcome"], "price": leg["price"], "market_id": leg["market_id"],
            }
            for leg in opp.legs
        ],
        "total_cost": round(opp.total_cost, 4),
        "guaranteed_payout": opp.guaranteed_payout,
        "gross_edge": round(opp.gross_edge, 4),
        "net_edge": round(opp.net_edge, 4),
        "net_edge_pct": net_edge_pct,
        "profit_per_100": net_edge_pct,
        # opp.estimated_depth is deliberately NOT surfaced here: it's
        # arb_engine._min_depth(), which silently drops any leg with
        # depth=None -- and KalshiImporter always sets depth=None (Kalshi's
        # public API only exposes top-of-book *size*, not a separate
        # "depth" figure; see kalshi_importer.py). On every Kalshi-vs-
        # Polymarket route -- i.e. every route this site ever shows -- that
        # collapses to "Polymarket's own top-of-book minimum," completely
        # blind to Kalshi's side. sizing.max_units below is the correct,
        # both-legs-aware, real-fee-aware answer to the same question
        # ("how much can actually be traded here"), so it's the only size
        # figure exposed to the site now -- confirmed live (2026-08-19) on
        # the Avengers: Doomsday market: estimated_depth reported 5.0
        # units while max_units correctly reported 1.09, limited by
        # Kalshi's real NO-side liquidity, which estimated_depth couldn't
        # see at all. Two contradictory size numbers on one card is worse
        # than one correct one.
        "sizing": sizing,
    }


def build_scan_result() -> dict:
    pairs = load_market_pairs(str(_DATA_DIR / "market_pairs.csv"))
    categories_by_name = load_market_categories(str(_DATA_DIR / "market_pairs.csv"))

    raw_prices: list[NormalizedPrice] = load_prices_fast("live", pairs)

    venues_loaded: dict[str, int] = {}
    for price in raw_prices:
        venues_loaded[price.venue] = venues_loaded.get(price.venue, 0) + 1
    warnings = [
        f"{venue}: no prices loaded this scan (fetch may have failed -- check server console)."
        for venue in _ACTIVE_VENUES
        if venues_loaded.get(venue, 0) == 0
    ]

    prices = apply_market_pairs(raw_prices, pairs)
    prices_by_key = {(p.venue, p.market_id): p for p in prices}
    rows_by_market_name: dict[str, list[NormalizedPrice]] = {}
    for p in prices:
        rows_by_market_name.setdefault(p.canonical_market_name, []).append(p)

    groups = match_markets(prices)
    opportunities: list[ArbOpportunity] = []
    for group in groups:
        opportunities.extend(check_binary_cross_venue_arbs(group, _FEE_BUFFER, _MIN_EDGE))
    opportunities = rank_opportunities(opportunities)

    routes_by_market: dict[str, list[ArbOpportunity]] = {}
    for opp in opportunities:
        routes_by_market.setdefault(opp.market_name, []).append(opp)

    # Every canonical market name the crosswalk promises, grouped by category.
    # De-duplicated since each pair contributes one row per venue.
    crosswalk_names_by_category: dict[str, list[str]] = {cat: [] for cat in CATEGORY_ORDER}
    seen_names: set[str] = set()
    for canonical_name, _market_type in pairs.values():
        if canonical_name in seen_names:
            continue
        seen_names.add(canonical_name)
        category = categories_by_name.get(canonical_name)
        if category in crosswalk_names_by_category:
            crosswalk_names_by_category[category].append(canonical_name)

    categories_payload: dict[str, dict] = {}
    for category in CATEGORY_ORDER:
        market_entries: list[tuple[int, str, dict]] = []
        uncompared_entries: list[dict] = []
        pass_count = no_edge_count = uncompared_count = fee_adjusted_count = 0

        for name in crosswalk_names_by_category[category]:
            opps = routes_by_market.get(name)
            if opps:
                routes = [_shape_route(opp, prices_by_key) for opp in opps]
                if any(r["status"] == "PASS" for r in routes):
                    best_status = "PASS"
                    pass_count += 1
                elif any(r["status"] == "FEE_ADJUSTED_NO_EDGE" for r in routes):
                    best_status = "FEE_ADJUSTED_NO_EDGE"
                    fee_adjusted_count += 1
                else:
                    best_status = "NO_EDGE"
                    no_edge_count += 1
                market_entries.append((
                    _STATUS_RANK[best_status], name,
                    {"market_name": name, "best_status": best_status, "routes": routes},
                ))
            else:
                uncompared_count += 1
                rows = rows_by_market_name.get(name, [])
                with_quote = sorted({r.venue for r in rows if _has_any_quote(r)})
                missing = sorted(set(_ACTIVE_VENUES) - set(with_quote))
                reason, detail = _uncompared_reason(rows)
                uncompared_entries.append({
                    "market_name": name,
                    "venues_with_quote": with_quote,
                    "venues_missing_quote": missing,
                    "reason": reason,
                    "detail": detail,
                })

        market_entries.sort(key=lambda entry: (entry[0], entry[1]))
        uncompared_entries.sort(key=lambda entry: entry["market_name"])

        categories_payload[category] = {
            "label": CATEGORY_LABELS[category],
            "market_count": len(crosswalk_names_by_category[category]),
            "pass_count": pass_count,
            "fee_adjusted_count": fee_adjusted_count,
            "no_edge_count": no_edge_count,
            "uncompared_count": uncompared_count,
            "markets": [entry[2] for entry in market_entries],
            "uncompared_markets": uncompared_entries,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fee_buffer": _FEE_BUFFER,
        "venues_loaded": venues_loaded,
        "warnings": warnings,
        "category_order": CATEGORY_ORDER,
        "categories": categories_payload,
    }


def _score_candidate(
    kalshi_row: NormalizedPrice, poly_row: NormalizedPrice
) -> list[ArbOpportunity]:
    """Run a title-matched candidate pair through the real, unmodified
    arb_engine math -- the exact same math every crosswalk market on the
    site uses.

    Builds a MarketGroup that exists only in memory for this one
    computation (never written to data/market_pairs.csv, never cached):
    same canonical_market_name and outcome_name="Yes" standardization
    apply_market_pairs() uses for real crosswalk pairs, so
    check_binary_cross_venue_arbs() sees the identical shape it always
    does.
    """
    shared_name = kalshi_row.raw_market_name
    synth_kalshi = replace(kalshi_row, canonical_market_name=shared_name, outcome_name="Yes")
    synth_poly = replace(poly_row, canonical_market_name=shared_name, outcome_name="Yes")
    group = MarketGroup(
        canonical_market_name=shared_name,
        market_type="binary",
        outcomes=["Yes"],
        prices=[synth_kalshi, synth_poly],
        match_confidence=0.0,
    )
    return check_binary_cross_venue_arbs(group, _FEE_BUFFER, _MIN_EDGE)


_MAX_SCAN_CARDS_SHOWN = 3

# POST /api/scan/{category} has no auth and no client-side-only protection
# is trustworthy (the frontend disables its own button while a scan is in
# flight, but that's cosmetic -- nothing stops a direct request straight to
# the API, bypassing the browser entirely). Without a server-side floor, a
# public visitor scripting repeated calls to the same category could cause
# unbounded, uncapped real network load against Kalshi/Polymarket for every
# single request. Caching the full result per category closes that off
# categorically: however fast or slow a client calls this, the venues
# themselves are hit at most once per _SCAN_RESULT_CACHE_TTL_SECONDS per
# category. Short on purpose -- long enough to blunt rapid-fire repeats,
# short enough that a deliberate, spaced-out RESCAN still gets live numbers.
_SCAN_RESULT_CACHE_TTL_SECONDS = 10
_scan_result_cache = TTLCache(_SCAN_RESULT_CACHE_TTL_SECONDS, max_entries=4)


def build_category_scan_result(category: str) -> dict:
    return _scan_result_cache.get_or_fetch(category, lambda: _build_category_scan_result_uncached(category))


def _build_category_scan_result_uncached(category: str) -> dict:
    """Discovery-mode scan of exactly one category -- the expensive,
    explicit-trigger-only counterpart to build_scan_result()'s always-free
    crosswalk pass. Reuses load_prices_for_category() (main.py) unchanged:
    it already lists the category on both venues, finds title-match
    candidates via find_title_candidates(), and returns real live pricing
    for both the crosswalk-covered markets and every newly-matched
    candidate.

    Each candidate is priced through the exact same unmodified arb_engine
    math as every crosswalk market on the site and labeled with the same
    PASS / FEE_ADJUSTED_NO_EDGE / NO_EDGE vocabulary -- there is no
    separate "unverified" trust tier here (there used to be one, gated on
    reading each venue's actual resolution rules by hand; removed by
    deliberate product decision, since this project has no realistic path
    to verifying every candidate a scan can find, and isn't relied on by
    third parties trading real money off it -- see the sitewide disclaimer
    the frontend shows instead). Results are still capped to
    _MAX_SCAN_CARDS_SHOWN cards so one scan can't dump an unbounded list:
    profitable_markets is the best-net-edge PASS candidates found, out of
    profitable_total; near_miss_markets, only shown when nothing passed,
    is the closest-to-profitable candidates otherwise, out of
    near_miss_total.
    """
    pairs = load_market_pairs(str(_DATA_DIR / "market_pairs.csv"))
    priced_prices, total_listed, candidates = load_prices_for_category("live", category, pairs)
    prices_by_key = {(p.venue, p.market_id): p for p in priced_prices}

    scored_markets: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for a, b in candidates:
        kalshi_stub = a if a.venue == "Kalshi" else b
        poly_stub = a if a.venue == "Polymarket" else b
        pair_key = (kalshi_stub.market_id, poly_stub.market_id)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # Score using the REAL priced rows (full order book / size data),
        # not the metadata-only stubs find_title_candidates() bucketed on.
        priced_kalshi = prices_by_key.get(("Kalshi", kalshi_stub.market_id))
        priced_poly = prices_by_key.get(("Polymarket", poly_stub.market_id))
        if priced_kalshi is None or priced_poly is None:
            continue  # no live price for one side this scan -- skip, don't guess

        opps = _score_candidate(priced_kalshi, priced_poly)
        if not opps:
            continue  # e.g. close-date guard blocked it, or a leg had no ask

        routes = [_shape_route(opp, prices_by_key) for opp in opps]
        best_status = min((r["status"] for r in routes), key=lambda s: _STATUS_RANK[s])
        scored_markets.append({
            "kalshi_title": kalshi_stub.raw_market_name,
            "polymarket_title": poly_stub.raw_market_name,
            "best_status": best_status,
            # Internal sort key only -- popped before this dict is returned,
            # same reason match_confidence never reaches the frontend.
            "_best_net_edge": max(r["net_edge"] for r in routes),
            "routes": routes,
        })

    profitable_markets, profitable_total, near_miss_markets, near_miss_total = _split_scan_results(scored_markets)

    return {
        "category": category,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_listed": total_listed,
        "candidate_count": len(candidates),
        "priced_count": len(scored_markets),
        "profitable_markets": profitable_markets,
        "profitable_total": profitable_total,
        "near_miss_markets": near_miss_markets,
        "near_miss_total": near_miss_total,
    }


def _split_scan_results(scored_markets: list[dict]) -> tuple[list[dict], int, list[dict], int]:
    """Pure split/sort/cap step, isolated from build_category_scan_result's
    I/O so it's independently testable with hand-built fixture dicts.

    Returns (profitable_markets, profitable_total, near_miss_markets,
    near_miss_total). profitable_markets is the _MAX_SCAN_CARDS_SHOWN
    PASS markets with the best net edge, out of profitable_total found --
    capped so one category scan can never dump an unbounded card list
    regardless of how many genuinely profitable-looking candidates a scan
    turns up. near_miss_markets is only ever populated when
    profitable_markets is empty -- the _MAX_SCAN_CARDS_SHOWN
    FEE_ADJUSTED_NO_EDGE/NO_EDGE markets closest to profitable (highest net
    edge), out of near_miss_total that existed. Mutates nothing; pops the
    internal "_best_net_edge" sort key from every dict in scored_markets
    before returning (never reaches the frontend).
    """
    profitable_markets = [m for m in scored_markets if m["best_status"] == "PASS"]
    profitable_markets.sort(key=lambda m: (-m["_best_net_edge"], m["kalshi_title"]))
    profitable_total = len(profitable_markets)
    profitable_markets = profitable_markets[:_MAX_SCAN_CARDS_SHOWN]

    near_miss_markets: list[dict] = []
    near_miss_total = 0
    if not profitable_total:
        near_misses = [m for m in scored_markets if m["best_status"] != "PASS"]
        near_misses.sort(key=lambda m: (-m["_best_net_edge"], m["kalshi_title"]))
        near_miss_total = len(near_misses)
        near_miss_markets = near_misses[:_MAX_SCAN_CARDS_SHOWN]

    for m in scored_markets:
        m.pop("_best_net_edge", None)

    return profitable_markets, profitable_total, near_miss_markets, near_miss_total
