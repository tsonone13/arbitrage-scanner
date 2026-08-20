"""Hand-computed tests for opportunity_view._split_scan_results -- the
profitable-vs-near-miss split and top-N cap applied to a category scan's
results.

Product rule being tested: a scan shows at most the top 3 profitable
(PASS) candidates by net edge, out of however many were actually found
(profitable_total); when there are zero profitable candidates, it shows
only the top 3 near-misses (anything not PASS -- FEE_ADJUSTED_NO_EDGE or
NO_EDGE -- closest to profitable by net edge) instead of dumping the whole
list. Scan candidates use the exact same PASS/FEE_ADJUSTED_NO_EDGE/NO_EDGE
vocabulary as crosswalk markets (there is no separate "unverified" status
tier -- removed by deliberate product decision, see opportunity_view.py's
build_category_scan_result docstring). This is a pure display-volume
decision -- it does not change route-level status, so it's tested in
isolation from arb_engine/slippage entirely, using hand-built fixture
dicts shaped like what build_category_scan_result already produces.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opportunity_view import _split_scan_results  # noqa: E402


def make_scored(name, status, net_edge):
    return {
        "kalshi_title": name,
        "polymarket_title": f"{name}?",
        "best_status": status,
        "_best_net_edge": net_edge,
        "routes": [],
    }


class TestSplitScanResults(unittest.TestCase):
    def test_profitable_markets_sorted_best_first_no_near_miss(self):
        """Two profitable markets (0.02, 0.05) plus two near-misses in the
        same batch -- profitable_markets must contain both (under the cap
        of 3), sorted highest edge first (B=0.05 before A=0.02),
        profitable_total=2, and near_miss_markets must stay empty since
        near-misses are only ever shown when nothing profitable was found.
        """
        a = make_scored("A", "PASS", 0.02)
        b = make_scored("B", "PASS", 0.05)
        c = make_scored("C", "NO_EDGE", -0.01)
        d = make_scored("D", "NO_EDGE", -0.005)

        profitable, profitable_total, near_miss, near_miss_total = _split_scan_results([a, b, c, d])

        self.assertEqual([m["kalshi_title"] for m in profitable], ["B", "A"])
        self.assertEqual(profitable_total, 2)
        self.assertEqual(near_miss, [])
        self.assertEqual(near_miss_total, 0)

    def test_more_than_three_profitable_capped_to_top_3(self):
        """Four profitable markets, net edges 0.01 (P4), 0.05 (P2), 0.02
        (P3), 0.08 (P1). Best-first means P1, P2, P3 -- P4 (the worst) must
        be excluded from the shown list but still counted in
        profitable_total.
        """
        p1 = make_scored("P1", "PASS", 0.08)
        p2 = make_scored("P2", "PASS", 0.05)
        p3 = make_scored("P3", "PASS", 0.02)
        p4 = make_scored("P4", "PASS", 0.01)

        profitable, profitable_total, near_miss, near_miss_total = _split_scan_results([p4, p2, p3, p1])

        self.assertEqual([m["kalshi_title"] for m in profitable], ["P1", "P2", "P3"])
        self.assertEqual(profitable_total, 4)
        self.assertEqual(near_miss, [])
        self.assertEqual(near_miss_total, 0)

    def test_no_profitable_more_than_three_near_misses_capped_to_top_3(self):
        """Four near-misses, net edges -0.001 (F), -0.005 (D), -0.01 (C),
        -0.02 (E). Closest-to-profitable first means F, D, C -- E (the
        worst) must be excluded from the shown list but still counted in
        near_miss_total.
        """
        c = make_scored("C", "NO_EDGE", -0.01)
        d = make_scored("D", "NO_EDGE", -0.005)
        e = make_scored("E", "NO_EDGE", -0.02)
        f = make_scored("F", "NO_EDGE", -0.001)

        profitable, profitable_total, near_miss, near_miss_total = _split_scan_results([c, d, e, f])

        self.assertEqual(profitable, [])
        self.assertEqual(profitable_total, 0)
        self.assertEqual([m["kalshi_title"] for m in near_miss], ["F", "D", "C"])
        self.assertEqual(near_miss_total, 4)

    def test_fee_adjusted_no_edge_counts_as_near_miss_too(self):
        """A FEE_ADJUSTED_NO_EDGE market (flat-buffer positive, real fees
        erase it) and a plain NO_EDGE market are both "not PASS" -- both
        must be treated as near-misses, sorted together by net edge.
        """
        fee_adjusted = make_scored("FeeAdjusted", "FEE_ADJUSTED_NO_EDGE", 0.001)
        no_edge = make_scored("NoEdge", "NO_EDGE", -0.02)

        profitable, profitable_total, near_miss, near_miss_total = _split_scan_results([fee_adjusted, no_edge])

        self.assertEqual(profitable, [])
        self.assertEqual(profitable_total, 0)
        self.assertEqual([m["kalshi_title"] for m in near_miss], ["FeeAdjusted", "NoEdge"])
        self.assertEqual(near_miss_total, 2)

    def test_no_profitable_two_near_misses_shows_both_uncapped(self):
        c = make_scored("C", "NO_EDGE", -0.01)
        d = make_scored("D", "NO_EDGE", -0.005)

        profitable, profitable_total, near_miss, near_miss_total = _split_scan_results([c, d])

        self.assertEqual(profitable, [])
        self.assertEqual(profitable_total, 0)
        self.assertEqual([m["kalshi_title"] for m in near_miss], ["D", "C"])
        self.assertEqual(near_miss_total, 2)

    def test_empty_input_returns_all_empty(self):
        profitable, profitable_total, near_miss, near_miss_total = _split_scan_results([])
        self.assertEqual(profitable, [])
        self.assertEqual(profitable_total, 0)
        self.assertEqual(near_miss, [])
        self.assertEqual(near_miss_total, 0)

    def test_internal_sort_key_stripped_from_every_market_not_just_returned_ones(self):
        """_best_net_edge must never reach the frontend -- including on
        markets that got cut by the top-3 cap (on either list) and never
        appear in either returned list, since scored_markets (the caller's
        full list) is what actually gets mutated.
        """
        p1 = make_scored("P1", "PASS", 0.08)
        p2 = make_scored("P2", "PASS", 0.05)
        p3 = make_scored("P3", "PASS", 0.02)
        p4 = make_scored("P4", "PASS", 0.01)
        all_markets = [p1, p2, p3, p4]

        _split_scan_results(all_markets)

        for m in all_markets:
            self.assertNotIn("_best_net_edge", m)


if __name__ == "__main__":
    unittest.main()
