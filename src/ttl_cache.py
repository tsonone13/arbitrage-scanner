"""In-memory TTL cache used in two places:
- The two importers' *catalog listing* calls (Kalshi's full /events
  page-through -- no server-side category filter, so every category scan
  used to re-page through the same up-to-6000 events from scratch -- and
  Polymarket's per-tag /events page-through). Both change slowly (new
  markets appear over hours, not seconds), unlike the price/order-book
  calls elsewhere in this project (get_normalized_prices_for_ids/
  _for_tickers), which are never cached and always fetch fresh -- caching
  a *listing* briefly doesn't make any shown price stale, only "which
  markets exist" a few seconds behind.
- opportunity_view.build_category_scan_result(), which caches the whole
  scan result per category. That one is a real security/stability
  requirement, not just a speed optimization: POST /api/scan/{category}
  is public and unauthenticated, so without a server-side floor a client
  could script unbounded repeated calls and drive unlimited real network
  load against Kalshi/Polymarket.

Per-key locking (not a single global lock -- different categories must
never block each other): a cache miss acquires that key's lock, then
re-checks the cache before actually fetching. That re-check is what makes
this safe against a *burst* of near-simultaneous requests for the same
key, not just sequential ones -- without it, several requests could all
see a miss and independently kick off their own expensive fetch before
any of them finished caching a result, which is exactly the amplification
a cache in front of a public endpoint needs to prevent. Whichever request
gets the lock first does the real fetch; the rest wait for it and then
share its result.
"""

import threading
import time


class TTLCache:
    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._store: dict[object, tuple[float, object]] = {}
        self._locks: dict[object, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: object) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def get_or_fetch(self, key: object, fetch):
        value = self._fresh(key)
        if value is not _MISS:
            return value

        with self._lock_for(key):
            # Re-check after acquiring the lock: another thread may have
            # already fetched and cached this key while this one was
            # waiting -- without this, two callers that both missed the
            # cache before either held the lock would still both fetch.
            value = self._fresh(key)
            if value is not _MISS:
                return value
            value = fetch()
            self._store[key] = (time.time(), value)
            return value

    def _fresh(self, key: object):
        cached = self._store.get(key)
        if cached is None:
            return _MISS
        fetched_at, value = cached
        if time.time() - fetched_at >= self._ttl:
            return _MISS
        return value


_MISS = object()
