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

max_entries (optional): confirmed necessary, not just theoretical, on a
real 512MB deployment (Render free tier, 2026-08-20): each cached catalog
can hold thousands of raw event objects, and the Polymarket catalog cache
is keyed by (max_events, tag_slug) -- one distinct key per category. With
no eviction, scanning several categories in a row (very plausible normal
use -- clicking through tabs) accumulated a separate multi-thousand-row
catalog in memory per category *simultaneously* for the whole TTL window,
on top of each request's own peak usage while processing -- confirmed as
a real contributor to an out-of-memory crash, not a hypothetical one. When
set, the least-recently-used entry is evicted once the cache would exceed
this many entries -- bounds total retained memory regardless of how many
distinct keys (categories) get scanned in a session.
"""

import threading
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int | None = None):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: OrderedDict[object, tuple[float, object]] = OrderedDict()
        # Guards all direct access to _store (including from the lock-free
        # fast path in _fresh) -- eviction and move-to-end are compound
        # operations on a shared structure, unsafe under concurrent callers
        # without this. Cheap and in-memory only (no I/O under this lock),
        # so it doesn't reintroduce the cross-key blocking _lock_for exists
        # to avoid -- that's specifically about not blocking on a slow
        # network fetch for an unrelated key.
        self._store_lock = threading.Lock()
        # Deliberately never pruned, unlike _store -- a key's Lock has to
        # keep meaning the same object to every caller for as long as
        # anyone might be waiting on it; deleting an entry while another
        # thread holds a reference to the old Lock (blocked, waiting to
        # acquire it) would let a later caller for the same key get handed
        # a *different* Lock object, silently breaking the mutual exclusion
        # this whole cache exists for. Audited instead of "fixed" (2026-08-20):
        # every real key space this project ever calls this with is small
        # and fixed for the life of the process (Kalshi: 1-2 distinct
        # max_events values; Polymarket: (max_events, tag_slug), <=8 tag
        # slugs; opportunity_view's scan cache: 8 categories) -- worst case
        # is on the order of 17 Lock objects total, ever, which is a couple
        # KB, not a real contributor to the OOM crash this project actually
        # hit. Correctness risk from pruning outweighs a non-existent gain.
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
            with self._store_lock:
                self._store[key] = (time.time(), value)
                self._store.move_to_end(key)
                if self._max_entries is not None:
                    while len(self._store) > self._max_entries:
                        self._store.popitem(last=False)  # evict least-recently-used
            return value

    def _fresh(self, key: object):
        with self._store_lock:
            cached = self._store.get(key)
            if cached is None:
                return _MISS
            fetched_at, value = cached
            if time.time() - fetched_at >= self._ttl:
                del self._store[key]  # expired -- drop it now, don't wait for eviction to free the memory
                return _MISS
            self._store.move_to_end(key)  # touch on read too: true LRU, not just insertion order
            return value


_MISS = object()
