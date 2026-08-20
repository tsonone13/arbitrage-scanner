"""Live, read-only importer for Polymarket's public APIs.

No API key: verified directly that the Gamma API (https://gamma-api.polymarket.com,
market/event discovery) and the CLOB API (https://clob.polymarket.com, live
order books) both serve read requests with no authentication. Nothing here
signs or places orders.

Correctness note: a Gamma "event" can bundle markets that are genuinely
mutually exclusive (e.g. "who wins the nomination") or markets that are
merely related but independent (e.g. separate per-game winner markets in a
best-of-5 series -- both can resolve YES). Only the former is a single real
proposition split across outcomes; the latter are separate bets that happen
to share an event. Polymarket flags this for us via `negRisk` on the event
object -- it's the actual mechanism that lets capital move between truly
mutually-exclusive outcomes -- so we only tag a group "multi_outcome" when
that flag is true and there's more than one market in it.
"""

import json
from datetime import datetime, timezone

import requests

from models import NormalizedPrice
from importers.base import VenueImporter
from normalizer import safe_float, validate_price
from ttl_cache import TTLCache

_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
_CLOB_BASE_URL = "https://clob.polymarket.com"
_TIMEOUT_SECONDS = 15
_EVENTS_PAGE_SIZE = 100
_BOOKS_BATCH_SIZE = 500

# Same reasoning as KalshiImporter's _catalog_cache: list_markets() is a
# listing (slow-changing), not a price call (always fetched fresh
# elsewhere), so caching it briefly doesn't make any shown price stale --
# see ttl_cache.py. Keyed by (max_events, tag_slug) since, unlike Kalshi,
# tag_slug is a real server-side filter that changes what gets fetched.
#
# max_entries lowered from 3 to 2 (2026-08-20): confirmed on the real
# server that a realistic sequence -- a user just browsing several
# category tabs, no scan overlap needed -- accumulates standing cache
# memory the concurrency lock doesn't touch at all (that lock only
# serializes concurrent heavy work, not sequential caching). Scanning
# elections, politics, and sports back to back (this project's 3 heaviest
# by nested-market count: 6,444/5,533/5,104) left all three cached
# simultaneously at max_entries=3, and the server's RSS climbed to
# ~416MB standing -- before even counting a new scan's own transient
# processing on top. At max_entries=2, only the 2 most recently used
# categories' Polymarket catalogs stay warm; a 3rd, older one gets
# re-fetched (a real but small latency cost -- listings are cached
# specifically because they're slow-changing, not because a re-fetch is
# expensive in absolute terms) in exchange for real memory margin back.
_CATALOG_CACHE_TTL_SECONDS = 90
_catalog_cache = TTLCache(_CATALOG_CACHE_TTL_SECONDS, max_entries=2)


def _parse_json_list(value: object) -> list:
    """Gamma returns outcomes/clobTokenIds as JSON-encoded strings, not native arrays."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


# Every field any file in this codebase ever reads off a raw Gamma event/
# market dict (confirmed via grep across the whole tree, not guessed) --
# trimmed to just these the moment each page arrives, before it's
# accumulated or cached. A real Gamma market object carries ~86 fields;
# this project reads about 15 of them. Confirmed empirically (2026-08-20):
# a single market's untrimmed `description` field alone runs ~1.4KB, and
# across a 250-event/~5,000-nested-market sports fetch the full untrimmed
# payload is ~37MB -- held for the full 90s cache TTL on top of whatever a
# scan is concurrently processing. Trimming shrinks both the cached
# catalog's standing memory and each request's transient peak, for free --
# nothing downstream reads a field outside this list, so behavior is
# unchanged.
_EVENT_FIELDS = ("negRisk", "title")
_MARKET_FIELDS = (
    "id", "question", "slug", "feeSchedule", "feesEnabled", "conditionId", "volume", "endDate",
    "resolutionSource", "active", "closed", "enableOrderBook", "outcomes", "clobTokenIds", "groupItemTitle",
)


def _trim_event(event: dict) -> dict:
    trimmed = {field: event.get(field) for field in _EVENT_FIELDS}
    trimmed["markets"] = [{field: m.get(field) for field in _MARKET_FIELDS} for m in event.get("markets", [])]
    return trimmed


class PolymarketImporter(VenueImporter):
    """Pulls active Polymarket events/markets and prices them from the real CLOB order book."""

    def __init__(self, max_events: int = 500, tag_slug: str | None = None):
        """tag_slug, when set, is a real server-side filter (unlike Kalshi's
        category, which isn't) -- Polymarket's Gamma API supports it directly,
        e.g. tag_slug="pop-culture" (Polymarket's own label for that tag is
        literally "Culture"; verified via GET /tags/slug/pop-culture).
        """
        self._max_events = max_events
        self._tag_slug = tag_slug

    @property
    def venue_name(self) -> str:
        return "Polymarket"

    def list_markets(self) -> list[dict]:
        """Return Gamma event objects (trimmed to _EVENT_FIELDS/_MARKET_FIELDS
        -- see _trim_event), each with its nested markets list.

        Sorted by 24h volume descending: Polymarket has thousands of open
        events, most of them thin/illiquid, and max_events won't reach all
        of them. Prioritizing by volume means the events actually worth
        scanning (and most likely to have a real cross-venue counterpart)
        get covered first instead of being crowded out by long-tail markets.

        Cached (see _catalog_cache above), keyed by (max_events, tag_slug).
        """
        return _catalog_cache.get_or_fetch((self._max_events, self._tag_slug), self._fetch_markets)

    def _fetch_markets(self) -> list[dict]:
        events: list[dict] = []
        offset = 0
        while len(events) < self._max_events:
            params = {
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": min(_EVENTS_PAGE_SIZE, self._max_events - len(events)),
                "offset": offset,
            }
            if self._tag_slug:
                params["tag_slug"] = self._tag_slug
            try:
                resp = requests.get(f"{_GAMMA_BASE_URL}/events", params=params, timeout=_TIMEOUT_SECONDS)
                resp.raise_for_status()
            except requests.HTTPError as exc:
                if resp.status_code == 422 and offset > 0:
                    # A tag_slug-filtered query 422s once offset runs past that
                    # tag's actual result count (confirmed empirically) -- that's
                    # "no more results", not a real error, unlike a 422 on the
                    # first page (which would mean a bad tag_slug/params).
                    break
                raise RuntimeError(f"Polymarket Gamma /events request failed: {exc}") from exc
            except requests.RequestException as exc:
                raise RuntimeError(f"Polymarket Gamma /events request failed: {exc}") from exc

            batch = resp.json()
            if not batch:
                break
            events.extend(_trim_event(e) for e in batch)
            offset += len(batch)
        return events

    def get_orderbook(self, market_id: str) -> dict:
        """market_id here is a CLOB token_id. Returns the raw {bids, asks, ...} book."""
        try:
            resp = requests.get(f"{_CLOB_BASE_URL}/book", params={"token_id": market_id}, timeout=_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Polymarket CLOB /book request failed for {market_id}: {exc}") from exc
        return resp.json()

    def _fetch_books(self, token_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch order books for many tokens at once (CLOB caps a batch at 500)."""
        books: dict[str, dict] = {}
        for start in range(0, len(token_ids), _BOOKS_BATCH_SIZE):
            chunk = token_ids[start:start + _BOOKS_BATCH_SIZE]
            body = [{"token_id": token_id} for token_id in chunk]
            try:
                resp = requests.post(f"{_CLOB_BASE_URL}/books", json=body, timeout=_TIMEOUT_SECONDS)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(f"Polymarket CLOB /books batch request failed: {exc}") from exc
            for book in resp.json():
                asset_id = book.get("asset_id")
                if asset_id:
                    books[asset_id] = book
        return books

    @staticmethod
    def _parse_levels(levels: list[dict]) -> list[tuple[float, float]]:
        """Parse a book side into [(price, size), ...], valid entries only.

        CLOB book levels are not guaranteed to arrive sorted (confirmed
        empirically -- both ascending and descending orderings have been seen
        from the same endpoint), so nothing here assumes an order.
        """
        parsed = [
            (safe_float(lvl.get("price")), safe_float(lvl.get("size")))
            for lvl in levels
        ]
        return [(price, size) for price, size in parsed if price is not None and size is not None]

    @classmethod
    def _best_level(cls, levels: list[dict], pick_max: bool) -> tuple[float | None, float | None]:
        """Best (highest for bids, lowest for asks) (price, size) in a book side."""
        parsed = cls._parse_levels(levels)
        if not parsed:
            return None, None
        return max(parsed) if pick_max else min(parsed)

    @classmethod
    def _top_of_book(
        cls, book: dict | None
    ) -> tuple[float | None, float | None, float | None, list[tuple[float, float]] | None]:
        """Returns (best_bid_price, best_ask_price, size_at_best_ask, full_ask_book).

        full_ask_book is every ask level sorted cheapest-first -- kept
        around purely for slippage.py's optional level-by-level walk;
        arb_engine.py's detection math only ever uses the first three.
        """
        if not book:
            return None, None, None, None
        best_bid, _ = cls._best_level(book.get("bids") or [], pick_max=True)
        best_ask, ask_size = cls._best_level(book.get("asks") or [], pick_max=False)
        ask_book = sorted(cls._parse_levels(book.get("asks") or [])) or None
        return best_bid, best_ask, ask_size, ask_book

    @staticmethod
    def _market_question(market: dict) -> str:
        return market.get("question") or market.get("slug") or str(market.get("id"))

    def _build_price(
        self, market: dict, timestamp: str,
        yes_bid: float | None, yes_ask: float | None, no_bid: float | None, no_ask: float | None,
        depth: float | None,
        canonical_market_name: str | None = None, market_type: str | None = None, outcome_name: str | None = None,
        yes_ask_size: float | None = None, no_ask_size: float | None = None,
        yes_ask_book: list[tuple[float, float]] | None = None, no_ask_book: list[tuple[float, float]] | None = None,
    ) -> NormalizedPrice | None:
        """Shared row builder. canonical_market_name/market_type/outcome_name
        default to the market's own question/"binary"/"Yes" -- correct for
        the targeted-lookup paths below, where every row is either
        crosswalk-covered (overridden by apply_market_pairs() regardless) or
        metadata-only (no price to compare, so the grouping doesn't matter).
        The full event-based scan passes its own event-derived values instead.
        """
        if not all(validate_price(p) for p in (yes_bid, yes_ask, no_bid, no_ask)):
            return None
        market_question = self._market_question(market)
        # Real, per-market taker fee rate (Theta in fee = Theta * C * p *
        # (1-p)) straight from the market's own data -- confirmed against
        # Polymarket's official docs (docs.polymarket.com/trading/fees:
        # "See Market Details to read the fee parameters for a market"), and
        # cross-checked directly: this Fed-decision market's feeSchedule.rate
        # (0.05) matches the documented "economics" category rate exactly.
        # No hardcoded category table needed, and none would stay accurate --
        # Polymarket's docs note per-category rates can change by time of day.
        fee_schedule = market.get("feeSchedule") or {}
        # feesEnabled is explicitly False for genuinely fee-free markets (e.g.
        # geopolitics, per Polymarket's docs); treat missing/unset as "unknown,
        # use the rate if one is given" rather than silently assuming free --
        # understating a real fee is the wrong direction to be wrong in.
        taker_fee_rate = 0.0 if market.get("feesEnabled") is False else safe_float(fee_schedule.get("rate"))

        return NormalizedPrice(
            venue=self.venue_name,
            market_id=str(market.get("id") or market.get("conditionId") or ""),
            canonical_market_name=canonical_market_name or market_question,
            raw_market_name=market_question,
            outcome_name=outcome_name or "Yes",
            market_type=market_type or "binary",
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            depth=depth,
            volume=safe_float(market.get("volume")),
            close_time=market.get("endDate"),
            resolution_notes=market.get("resolutionSource") or None,
            timestamp=timestamp,
            yes_ask_size=yes_ask_size,
            no_ask_size=no_ask_size,
            yes_ask_book=yes_ask_book,
            no_ask_book=no_ask_book,
            taker_fee_rate=taker_fee_rate,
        )

    def get_normalized_prices(self) -> list[NormalizedPrice]:
        events = self.list_markets()

        # Gather every (event, market, yes_token, no_token) worth pricing and every
        # token id needed, so order books can be fetched in large efficient batches
        # instead of one request per market.
        rows: list[tuple[dict, dict, str, str]] = []
        all_token_ids: list[str] = []
        for event in events:
            for market in event.get("markets", []):
                if not market.get("active") or market.get("closed") or not market.get("enableOrderBook"):
                    continue
                outcomes = _parse_json_list(market.get("outcomes"))
                token_ids = _parse_json_list(market.get("clobTokenIds"))
                if len(outcomes) != len(token_ids) or "Yes" not in outcomes or "No" not in outcomes:
                    continue
                yes_token = token_ids[outcomes.index("Yes")]
                no_token = token_ids[outcomes.index("No")]
                rows.append((event, market, yes_token, no_token))
                all_token_ids.extend([yes_token, no_token])

        books = self._fetch_books(all_token_ids)
        timestamp = datetime.now(timezone.utc).isoformat()

        prices: list[NormalizedPrice] = []
        for event, market, yes_token, no_token in rows:
            yes_bid, yes_ask, yes_ask_size, yes_ask_book = self._top_of_book(books.get(yes_token))
            no_bid, no_ask, no_ask_size, no_ask_book = self._top_of_book(books.get(no_token))

            markets = event.get("markets", [])
            is_partition = bool(event.get("negRisk")) and len(markets) > 1
            event_title = event.get("title") or "Unknown Polymarket Event"
            market_question = self._market_question(market)
            sizes = [s for s in (yes_ask_size, no_ask_size) if s is not None]

            price = self._build_price(
                market, timestamp, yes_bid, yes_ask, no_bid, no_ask, min(sizes) if sizes else None,
                canonical_market_name=event_title if is_partition else market_question,
                market_type="multi_outcome" if is_partition else "binary",
                outcome_name=(market.get("groupItemTitle") or market_question) if is_partition else "Yes",
                yes_ask_size=yes_ask_size, no_ask_size=no_ask_size,
                yes_ask_book=yes_ask_book, no_ask_book=no_ask_book,
            )
            if price is not None:
                prices.append(price)
        return prices

    def get_market_metadata(self) -> list[NormalizedPrice]:
        """Same rows as get_normalized_prices(), same category/tag_slug
        scope, but skips CLOB pricing entirely (all price fields None) --
        for finding title-candidates cheaply before deciding what's actually
        worth the expense of fetching a real order book for.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        prices: list[NormalizedPrice] = []
        for event in self.list_markets():
            markets = event.get("markets", [])
            is_partition = bool(event.get("negRisk")) and len(markets) > 1
            event_title = event.get("title") or "Unknown Polymarket Event"
            for market in markets:
                if not market.get("active") or market.get("closed"):
                    continue
                market_question = self._market_question(market)
                prices.append(NormalizedPrice(
                    venue=self.venue_name,
                    market_id=str(market.get("id") or market.get("conditionId") or ""),
                    canonical_market_name=event_title if is_partition else market_question,
                    raw_market_name=market_question,
                    outcome_name=(market.get("groupItemTitle") or market_question) if is_partition else "Yes",
                    market_type="multi_outcome" if is_partition else "binary",
                    yes_bid=None, yes_ask=None, no_bid=None, no_ask=None,
                    depth=None,
                    volume=safe_float(market.get("volume")),
                    close_time=market.get("endDate"),
                    resolution_notes=market.get("resolutionSource") or None,
                    timestamp=timestamp,
                ))
        return prices

    def get_normalized_prices_for_ids(self, ids: list[str]) -> list[NormalizedPrice]:
        """Direct batch lookup by exact Gamma market id -- no event
        pagination. Used both for the crosswalk-only fast path and for
        pricing just the title-candidates found by a category scan, instead
        of eagerly pricing every market that happens to be in scope.

        Unlike Kalshi's batch ticker lookup, which silently ignores unknown
        tickers, Gamma's `id` filter 422s the *entire* request if any id in
        the batch isn't a valid market id (confirmed empirically) -- so one
        stale or mistyped id in data/market_pairs.csv would otherwise take
        down the whole batch. Market ids are always numeric, so non-numeric
        values are dropped here with a warning instead of being sent.
        """
        clean_ids = []
        for i in dict.fromkeys(ids):  # de-dupe, keep order
            if i and str(i).isdigit():
                clean_ids.append(i)
            elif i:
                print(f"[polymarket_importer] skipping malformed market id: {i!r}")
        ids = clean_ids
        if not ids:
            return []

        markets: list[dict] = []
        chunk_size = 100
        for start in range(0, len(ids), chunk_size):  # keep query strings reasonable
            chunk = ids[start:start + chunk_size]
            try:
                resp = requests.get(
                    f"{_GAMMA_BASE_URL}/markets",
                    # limit defaults to 20 server-side regardless of how many
                    # id= params are given (confirmed empirically: 25 valid
                    # ids with no limit param returned only 20 markets) --
                    # without this, any chunk with more than 20 real matches
                    # silently drops the rest, no error, no warning.
                    params=[("id", i) for i in chunk] + [("limit", chunk_size)],
                    timeout=_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(f"Polymarket Gamma /markets (batch id) request failed: {exc}") from exc
            batch = resp.json()
            if len(batch) < len(chunk):
                print(
                    f"[polymarket_importer] warning: requested {len(chunk)} ids in this batch, "
                    f"Gamma returned {len(batch)} -- some ids may no longer exist or be active."
                )
            markets.extend(batch)

        rows: list[tuple[dict, str, str]] = []
        all_token_ids: list[str] = []
        for market in markets:
            if not market.get("active") or market.get("closed") or not market.get("enableOrderBook"):
                continue
            outcomes = _parse_json_list(market.get("outcomes"))
            token_ids = _parse_json_list(market.get("clobTokenIds"))
            if len(outcomes) != len(token_ids) or "Yes" not in outcomes or "No" not in outcomes:
                continue
            yes_token = token_ids[outcomes.index("Yes")]
            no_token = token_ids[outcomes.index("No")]
            rows.append((market, yes_token, no_token))
            all_token_ids.extend([yes_token, no_token])

        books = self._fetch_books(all_token_ids)
        timestamp = datetime.now(timezone.utc).isoformat()

        prices: list[NormalizedPrice] = []
        for market, yes_token, no_token in rows:
            yes_bid, yes_ask, yes_ask_size, yes_ask_book = self._top_of_book(books.get(yes_token))
            no_bid, no_ask, no_ask_size, no_ask_book = self._top_of_book(books.get(no_token))
            sizes = [s for s in (yes_ask_size, no_ask_size) if s is not None]
            price = self._build_price(
                market, timestamp, yes_bid, yes_ask, no_bid, no_ask, min(sizes) if sizes else None,
                yes_ask_size=yes_ask_size, no_ask_size=no_ask_size,
                yes_ask_book=yes_ask_book, no_ask_book=no_ask_book,
            )
            if price is not None:
                prices.append(price)
        return prices
