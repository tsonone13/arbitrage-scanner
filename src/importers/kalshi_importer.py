"""Live, read-only importer for Kalshi's public market-data API.

No API key: verified directly against https://external-api.kalshi.com that
GET /events and the nested market objects it returns (yes/no bid/ask in
dollars) are public and unauthenticated. Nothing here places orders or
touches a private/signed endpoint.

Correctness note: a Kalshi "event" can bundle markets that are genuinely
mutually exclusive (e.g. "who wins the election") or markets that are merely
related but independent/correlated (e.g. "over 1.5 goals" and "over 2.5
goals" for the same game -- both can be true at once). Only the former is a
single real proposition split across outcomes; the latter are separate bets
that happen to share an event. Kalshi flags this for us via
`mutually_exclusive` on the event object, so we only tag a group as
"multi_outcome" when that flag is true and there's more than one market in
it; everything else is treated as its own standalone "binary" market so it
never gets incorrectly grouped with unrelated siblings.
"""

from dataclasses import replace
from datetime import datetime, timezone

import requests

from models import NormalizedPrice
from importers.base import VenueImporter
from normalizer import safe_float, validate_price
from ttl_cache import TTLCache

_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
_TIMEOUT_SECONDS = 15
_PAGE_SIZE = 200
_ACTIVE_STATUSES = {"active", "open"}

# Every field any file in this codebase ever reads off a raw Kalshi event/
# market dict (confirmed via grep across the whole tree, not guessed) --
# trimmed to just these the moment each page arrives, before it's
# accumulated or cached. Kalshi's real payload carries far more than this:
# full multi-paragraph rules_secondary text, settlement metadata, strike
# details, and dozens of other fields nothing here reads. Confirmed
# empirically (2026-08-20): 500 events / 3,466 nested markets, untrimmed,
# is ~15.5MB -- held for the full 90s cache TTL on top of whatever a scan
# is concurrently processing. Trimming shrinks both the cached catalog's
# standing memory and each request's transient peak, for free -- nothing
# downstream reads a field outside this list, so behavior is unchanged.
_EVENT_FIELDS = ("category", "mutually_exclusive", "event_ticker", "title", "sub_title")
_MARKET_FIELDS = (
    "status", "ticker", "yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars",
    "title", "volume", "volume_fp", "yes_sub_title", "close_time", "expected_expiration_time",
    "rules_primary", "yes_ask_size_fp", "yes_bid_size_fp",
)


def _trim_event(event: dict) -> dict:
    trimmed = {field: event.get(field) for field in _EVENT_FIELDS}
    trimmed["markets"] = [{field: m.get(field) for field in _MARKET_FIELDS} for m in event.get("markets", [])]
    return trimmed

# list_markets() is category-agnostic (see its own docstring) -- it always
# pages through the SAME up-to-max_events full catalog regardless of what
# category filter a caller will apply afterward. That made every one of
# this project's 8 website category tabs independently re-page through
# ~6000 events on every scan, even though "which events currently exist"
# barely changes between scans seconds apart -- confirmed the dominant
# cost empirically (~15-20s of a ~15-25s category scan). Cached here,
# keyed by max_events, so scanning several categories in a row only pays
# this cost once. Never applied to price/order-book calls (see
# ttl_cache.py's own docstring for why that split is safe).
_CATALOG_CACHE_TTL_SECONDS = 90
_catalog_cache = TTLCache(_CATALOG_CACHE_TTL_SECONDS, max_entries=2)

# Kalshi's exact per-market taker fee rate lives behind GET /margin/fee_tiers,
# which requires a signed API key (KALSHI-ACCESS-*) -- not used here, same as
# the orderbook-depth endpoint. 0.07 is the publicly documented default taker
# coefficient (Kalshi help center: "a transaction fee on the expected
# earnings on the contract", i.e. fee = rate * contracts * price * (1-price));
# some categories carry different published rates (e.g. index markets have
# been discounted to roughly half this), which this default won't capture.
# Treat any fee figure derived from this as an approximation, not the
# authoritative per-market rate -- see slippage.py.
_DEFAULT_TAKER_FEE_RATE = 0.07


class KalshiImporter(VenueImporter):
    """Pulls open Kalshi events/markets and normalizes their top-of-book prices.

    Unlike Polymarket, Kalshi's /events endpoint has no working sort/order
    parameter (confirmed empirically -- passing sort=volume returns identical
    results to no sort at all), so there's no way to fetch the most-relevant
    events first. The catalog is dominated by a long tail of thin sports/combo
    markets ahead of higher-profile events in whatever default order Kalshi
    uses -- e.g. "Fed decision in Sep 2026?" sits ~5,200 events deep. A default
    that's too low silently misses exactly the events most likely to matter
    or have a real cross-venue counterpart, so this defaults higher than
    Polymarket's even though it makes each scan slower (~15s for 6,000
    events).
    """

    def __init__(self, max_events: int = 6000, category: str | None = None):
        """category, when set, must match Kalshi's own event-level `category`
        field exactly (e.g. "Entertainment") -- there's no server-side filter
        for it (confirmed empirically, same as the sort parameter), so this
        still pages through up to max_events and filters client-side.
        """
        self._max_events = max_events
        self._category = category

    @property
    def venue_name(self) -> str:
        return "Kalshi"

    def list_markets(self) -> list[dict]:
        """Return Kalshi event objects (trimmed to _EVENT_FIELDS/_MARKET_FIELDS
        -- see _trim_event), each with its nested markets list.

        Category-agnostic and cached (see _catalog_cache above) -- every
        KalshiImporter with the same max_events shares one fetch, filtered
        client-side per-instance afterward in get_normalized_prices().
        """
        return _catalog_cache.get_or_fetch(self._max_events, self._fetch_markets)

    def _fetch_markets(self) -> list[dict]:
        events: list[dict] = []
        cursor = None
        while len(events) < self._max_events:
            params = {
                "status": "open",
                "with_nested_markets": "true",
                "limit": min(_PAGE_SIZE, self._max_events - len(events)),
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = requests.get(f"{_BASE_URL}/events", params=params, timeout=_TIMEOUT_SECONDS)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(f"Kalshi /events request failed: {exc}") from exc

            payload = resp.json()
            batch = payload.get("events", [])
            events.extend(_trim_event(e) for e in batch)
            cursor = payload.get("cursor")
            if not cursor or not batch:
                break
        return events

    def get_orderbook(self, market_id: str) -> dict:
        """TODO: GET /markets/{ticker}/orderbook requires signed KALSHI-ACCESS-*
        auth headers even for reads (unlike /events, which does not). Top-of-book
        yes/no bid/ask -- everything this scanner needs -- is already available
        without auth via list_markets()/get_normalized_prices(). Wire up a real
        API key here later only if full order-book depth becomes necessary.
        """
        raise NotImplementedError(
            "Kalshi's per-ticker orderbook endpoint requires an authenticated API "
            "key; this importer intentionally only uses the public, keyless "
            "top-of-book fields returned by GET /events."
        )

    def _build_price(
        self, market: dict, timestamp: str,
        canonical_market_name: str | None = None, market_type: str | None = None,
        resolution_notes: str | None = None,
    ) -> NormalizedPrice | None:
        """Shared row builder. canonical_market_name/market_type default to
        the market's own title/"binary" -- correct for the targeted-lookup
        paths below, where every row is crosswalk-covered and gets
        overridden by apply_market_pairs() regardless; the full event-based
        scan passes its own event-derived values instead.
        """
        if market.get("status") not in _ACTIVE_STATUSES:
            return None
        ticker = market.get("ticker")
        if not ticker:
            return None

        yes_bid = safe_float(market.get("yes_bid_dollars"))
        yes_ask = safe_float(market.get("yes_ask_dollars"))
        no_bid = safe_float(market.get("no_bid_dollars"))
        no_ask = safe_float(market.get("no_ask_dollars"))
        if not all(validate_price(p) for p in (yes_bid, yes_ask, no_bid, no_ask)):
            print(f"[kalshi_importer] skipping {ticker}: price out of [0, 1] range")
            return None

        market_title = market.get("title") or ticker
        volume = market.get("volume")
        if volume is None:
            volume = market.get("volume_fp")

        return NormalizedPrice(
            venue=self.venue_name,
            market_id=ticker,
            canonical_market_name=canonical_market_name or market_title,
            raw_market_name=market_title,
            outcome_name=market.get("yes_sub_title") or "Yes",
            market_type=market_type or "binary",
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            depth=None,  # top-of-book only; real depth needs the authenticated orderbook endpoint
            volume=safe_float(volume),
            # expected_expiration_time, not close_time: close_time is an outer
            # settlement deadline that can include a multi-year buffer for
            # contested results (confirmed via a real market's rules_secondary:
            # "remains open until the rescheduled election or two years from
            # the original date"), so two venues covering the identical event
            # can show wildly different close_time while expected_expiration_time
            # -- when the market actually expects to resolve -- agrees almost
            # exactly with Polymarket's endDate. Falls back to close_time for
            # the rare market that doesn't set it.
            close_time=market.get("expected_expiration_time") or market.get("close_time"),
            resolution_notes=resolution_notes or market.get("rules_primary") or None,
            timestamp=timestamp,
            # Real top-of-book size, no auth needed. NO-ask liquidity is
            # yes_bid_size_fp, not a separate no_ask_size_fp field -- Kalshi
            # doesn't expose one, because a NO ask is mechanically the same
            # resting order as a YES bid (confirmed via Kalshi's own stated
            # convention: "a yes bid at 7c is the same as a no ask at 93c").
            # No deeper book levels are available without a signed API key.
            yes_ask_size=safe_float(market.get("yes_ask_size_fp")),
            no_ask_size=safe_float(market.get("yes_bid_size_fp")),
            taker_fee_rate=_DEFAULT_TAKER_FEE_RATE,
        )

    def get_normalized_prices(self) -> list[NormalizedPrice]:
        timestamp = datetime.now(timezone.utc).isoformat()
        prices: list[NormalizedPrice] = []

        for event in self.list_markets():
            if self._category is not None and event.get("category") != self._category:
                continue
            markets = event.get("markets", [])
            is_partition = bool(event.get("mutually_exclusive")) and len(markets) > 1
            event_title = event.get("title") or event.get("event_ticker") or "Unknown Kalshi Event"

            for market in markets:
                market_title = market.get("title") or market.get("ticker")
                if is_partition:
                    canonical_market_name = event_title
                    market_type = "multi_outcome"
                else:
                    canonical_market_name = market_title
                    market_type = "binary"

                price = self._build_price(
                    market, timestamp,
                    canonical_market_name=canonical_market_name,
                    market_type=market_type,
                    resolution_notes=event.get("sub_title"),
                )
                if price is not None:
                    if is_partition:
                        price = replace(price, outcome_name=market.get("yes_sub_title") or market_title)
                    prices.append(price)
        return prices

    def get_normalized_prices_for_tickers(self, tickers: list[str]) -> list[NormalizedPrice]:
        """Direct batch lookup by exact ticker -- no pagination, no category
        filtering, no parent-event fetch. Every row returned here is, by
        construction, something the caller already has a verified crosswalk
        entry for, so canonical_market_name/market_type get overridden by
        apply_market_pairs() regardless of what's derived here -- this just
        needs to be fast, not fully event-aware.
        """
        tickers = [t for t in dict.fromkeys(tickers) if t]  # de-dupe, keep order
        if not tickers:
            return []
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            resp = requests.get(
                f"{_BASE_URL}/markets", params={"tickers": ",".join(tickers)}, timeout=_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Kalshi /markets (batch tickers) request failed: {exc}") from exc

        prices = [self._build_price(m, timestamp) for m in resp.json().get("markets", [])]
        return [p for p in prices if p is not None]
