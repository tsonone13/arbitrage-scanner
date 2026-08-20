"""Hand-computed tests for ttl_cache.TTLCache -- the shared cache behind
both importers' list_markets() (see their _catalog_cache usage) and
opportunity_view.build_category_scan_result()'s per-category cache, which
is what actually bounds worst-case load against Kalshi/Polymarket from
POST /api/scan/{category} (public, unauthenticated) -- see that module's
comment for why this is a security/stability requirement, not just speed.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ttl_cache import TTLCache  # noqa: E402


class TestTTLCache(unittest.TestCase):
    def test_second_call_within_ttl_reuses_cached_value_no_refetch(self):
        cache = TTLCache(ttl_seconds=90)
        fetch = mock.Mock(return_value=["event1", "event2"])

        first = cache.get_or_fetch("key", fetch)
        second = cache.get_or_fetch("key", fetch)

        self.assertEqual(first, ["event1", "event2"])
        self.assertEqual(second, ["event1", "event2"])
        fetch.assert_called_once()

    def test_call_after_ttl_expires_refetches(self):
        cache = TTLCache(ttl_seconds=90)
        fetch = mock.Mock(side_effect=[["first-batch"], ["second-batch"]])

        with mock.patch("ttl_cache.time.time", return_value=1000.0):
            first = cache.get_or_fetch("key", fetch)
        # 1000.0 + 91 = 1091.0, which is 91s later -- past the 90s TTL.
        with mock.patch("ttl_cache.time.time", return_value=1091.0):
            second = cache.get_or_fetch("key", fetch)

        self.assertEqual(first, ["first-batch"])
        self.assertEqual(second, ["second-batch"])
        self.assertEqual(fetch.call_count, 2)

    def test_call_just_under_ttl_still_reuses_cached_value(self):
        cache = TTLCache(ttl_seconds=90)
        fetch = mock.Mock(return_value=["only-batch"])

        with mock.patch("ttl_cache.time.time", return_value=1000.0):
            cache.get_or_fetch("key", fetch)
        # 1000.0 + 89.9 = 1089.9, 89.9s later -- just inside the 90s TTL.
        with mock.patch("ttl_cache.time.time", return_value=1089.9):
            result = cache.get_or_fetch("key", fetch)

        self.assertEqual(result, ["only-batch"])
        fetch.assert_called_once()

    def test_different_keys_cached_independently(self):
        cache = TTLCache(ttl_seconds=90)
        fetch_a = mock.Mock(return_value="value-a")
        fetch_b = mock.Mock(return_value="value-b")

        result_a = cache.get_or_fetch("a", fetch_a)
        result_b = cache.get_or_fetch("b", fetch_b)

        self.assertEqual(result_a, "value-a")
        self.assertEqual(result_b, "value-b")
        fetch_a.assert_called_once()
        fetch_b.assert_called_once()

    def test_concurrent_calls_for_same_key_only_fetch_once(self):
        """The actual scenario this cache exists to prevent for a public
        endpoint: a burst of near-simultaneous requests for the same key,
        none of which have a cached result yet. Without per-key locking +
        a re-check after acquiring it, every one of these 10 threads would
        see a miss and independently call fetch -- 10x the real network
        load a single client's rapid clicking (or a script) could cause.
        fetch sleeps briefly specifically to widen the race window so
        concurrent callers actually overlap instead of running one after
        another by accident on a fast machine.
        """
        cache = TTLCache(ttl_seconds=90)
        call_count = 0
        call_count_lock = threading.Lock()

        def slow_fetch():
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            time.sleep(0.05)
            return "the-value"

        results = []
        results_lock = threading.Lock()

        def worker():
            result = cache.get_or_fetch("shared-key", slow_fetch)
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(call_count, 1)
        self.assertEqual(results, ["the-value"] * 10)

    def test_concurrent_calls_for_different_keys_do_not_block_each_other(self):
        """Per-key locking, not one global lock -- two different categories
        being scanned at the same time must not serialize behind each
        other just because they share one TTLCache instance.
        """
        cache = TTLCache(ttl_seconds=90)
        start_barrier = threading.Barrier(2)
        call_order = []
        call_order_lock = threading.Lock()

        def fetch_for(key):
            start_barrier.wait(timeout=2)  # both threads must be mid-fetch together
            with call_order_lock:
                call_order.append(f"start-{key}")
            time.sleep(0.05)
            with call_order_lock:
                call_order.append(f"end-{key}")
            return key

        results = {}

        def worker(key):
            results[key] = cache.get_or_fetch(key, lambda: fetch_for(key))

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results, {"a": "a", "b": "b"})
        # If the two keys serialized behind one shared lock, "end-a" would
        # have to appear before "start-b" (or vice versa) -- the barrier
        # only releases once both threads are already inside their fetch,
        # so reaching it at all proves neither waited on the other's lock.
        self.assertIn("start-a", call_order)
        self.assertIn("start-b", call_order)


if __name__ == "__main__":
    unittest.main()
