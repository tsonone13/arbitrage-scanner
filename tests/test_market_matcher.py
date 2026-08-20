"""Hand-computed tests for market_matcher's fuzzy candidate discovery
(_tokenize, _token_overlap, find_title_candidates).

This is deliberately the ONLY fuzzy step in the whole pipeline -- it feeds
find_title_candidates() only, never match_markets()/apply_market_pairs()
(the trusted crosswalk path, still exact-key-only and untouched here). Every
expected token set / overlap score below was worked out by hand from the
stopword list and formula in market_matcher.py, not by running the function
and copying its output.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from market_matcher import _tokenize, _token_overlap, find_title_candidates  # noqa: E402
from models import NormalizedPrice  # noqa: E402


def make_price(venue, market_id, raw_market_name):
    return NormalizedPrice(
        venue=venue,
        market_id=market_id,
        canonical_market_name=raw_market_name,
        raw_market_name=raw_market_name,
        outcome_name="Yes",
        market_type="binary",
        yes_bid=0.5, yes_ask=0.5, no_bid=0.5, no_ask=0.5,
        depth=None, volume=None, close_time=None, resolution_notes=None, timestamp=None,
    )


class TestTokenize(unittest.TestCase):
    def test_strips_stopwords_and_punctuation_keeps_numbers(self):
        # "Will the Fed cut rates in September 2026?" -- will/the/in are
        # stopwords, "?" isn't a word character, "2026" is a kept number.
        self.assertEqual(
            _tokenize("Will the Fed cut rates in September 2026?"),
            frozenset({"fed", "cut", "rates", "september", "2026"}),
        )

    def test_all_stopwords_returns_empty(self):
        self.assertEqual(_tokenize("Will the a of in"), frozenset())

    def test_single_character_words_dropped(self):
        # "x" is length 1 (dropped by the len>1 filter); "corp"/"fed"/"rate"
        # are genuine 3+ char words and neither is a stopword, so kept.
        self.assertEqual(_tokenize("X Corp Fed rate"), frozenset({"corp", "fed", "rate"}))

    def test_case_insensitive(self):
        self.assertEqual(_tokenize("FED DECISION"), frozenset({"fed", "decision"}))


class TestTokenOverlap(unittest.TestCase):
    def test_hand_computed_above_threshold_match(self):
        # A = {team, alpha, win, 2026, championship} (5)
        # B = {team, alpha, championship, 2026, winner} (5)
        # shared = {team, alpha, championship, 2026} = 4; min(5,5)=5 -> 4/5 = 0.8
        a = _tokenize("Will Team Alpha win the 2026 Championship?")
        b = _tokenize("Team Alpha Championship 2026 Winner")
        self.assertEqual(a, frozenset({"team", "alpha", "win", "2026", "championship"}))
        self.assertEqual(b, frozenset({"team", "alpha", "championship", "2026", "winner"}))
        self.assertAlmostEqual(_token_overlap(a, b), 0.8)

    def test_hand_computed_unrelated_titles_score_zero(self):
        a = _tokenize("Will Team Alpha win the 2026 Championship?")
        b = _tokenize("Will it rain in Chicago tomorrow?")
        self.assertEqual(_token_overlap(a, b), 0.0)

    def test_hand_computed_exactly_at_threshold_boundary(self):
        # A = {team, alpha, win, 2026, championship} (5)
        # E = {2026, championship, merchandise, sale} (4)
        # shared = {2026, championship} = 2; min(5,4)=4 -> 2/4 = 0.5 (== _MIN_SIMILARITY)
        a = _tokenize("Will Team Alpha win the 2026 Championship?")
        e = _tokenize("2026 Championship Merchandise Sale")
        self.assertAlmostEqual(_token_overlap(a, e), 0.5)

    def test_hand_computed_just_below_threshold_boundary(self):
        # A = {team, alpha, win, 2026, championship} (5)
        # F = {2026, championship, parking, merchandise, ticket, sale} (6)
        # shared = {2026, championship} = 2; min(5,6)=5 -> 2/5 = 0.4 (< 0.5)
        a = _tokenize("Will Team Alpha win the 2026 Championship?")
        f = _tokenize("2026 Championship Parking Merchandise Ticket Sale")
        self.assertAlmostEqual(_token_overlap(a, f), 0.4)

    def test_empty_set_returns_zero_not_division_error(self):
        self.assertEqual(_token_overlap(frozenset(), frozenset({"fed"})), 0.0)
        self.assertEqual(_token_overlap(frozenset({"fed"}), frozenset()), 0.0)


class TestFindTitleCandidates(unittest.TestCase):
    def test_fuzzy_cross_venue_match_above_threshold_returned(self):
        k = make_price("Kalshi", "K1", "Will Team Alpha win the 2026 Championship?")
        p = make_price("Polymarket", "P1", "Team Alpha Championship 2026 Winner")
        result = find_title_candidates([k, p])
        self.assertEqual(result, [(k, p)])

    def test_same_venue_pair_never_returned_even_if_identical(self):
        k1 = make_price("Kalshi", "K1", "Will Team Alpha win the 2026 Championship?")
        k2 = make_price("Kalshi", "K2", "Will Team Alpha win the 2026 Championship?")
        result = find_title_candidates([k1, k2])
        self.assertEqual(result, [])

    def test_below_threshold_pair_not_returned(self):
        k = make_price("Kalshi", "K1", "Will Team Alpha win the 2026 Championship?")
        p = make_price("Polymarket", "P1", "2026 Championship Parking Merchandise Ticket Sale")
        result = find_title_candidates([k, p])
        self.assertEqual(result, [])

    def test_below_min_shared_tokens_excluded_even_if_ratio_would_pass(self):
        # "Fed Meeting" vs "Fed Party": shared={fed}=1 (< _MIN_SHARED_TOKENS
        # of 2) even though the ratio alone (1/2=0.5) would clear
        # min_similarity -- the count gate must still reject it.
        k = make_price("Kalshi", "K1", "Fed Meeting")
        p = make_price("Polymarket", "P1", "Fed Party")
        result = find_title_candidates([k, p])
        self.assertEqual(result, [])

    def test_both_sides_already_crosswalked_excluded(self):
        k = make_price("Kalshi", "K1", "Will Team Alpha win the 2026 Championship?")
        p = make_price("Polymarket", "P1", "Team Alpha Championship 2026 Winner")
        pairs = {
            ("Kalshi", "K1"): ("Team Alpha Wins 2026", "binary"),
            ("Polymarket", "P1"): ("Team Alpha Wins 2026", "binary"),
        }
        result = find_title_candidates([k, p], pairs=pairs)
        self.assertEqual(result, [])

    def test_only_one_side_crosswalked_still_returned(self):
        k = make_price("Kalshi", "K1", "Will Team Alpha win the 2026 Championship?")
        p = make_price("Polymarket", "P1", "Team Alpha Championship 2026 Winner")
        pairs = {("Kalshi", "K1"): ("Team Alpha Wins 2026", "binary")}
        result = find_title_candidates([k, p], pairs=pairs)
        self.assertEqual(result, [(k, p)])

    def test_no_duplicate_pair_even_with_multiple_shared_blocking_tokens(self):
        # Every token in "team alpha 2026 championship" is shared, so the
        # pair is reachable via 4 different blocking-index entries -- must
        # still be returned exactly once.
        k = make_price("Kalshi", "K1", "Team Alpha 2026 Championship")
        p = make_price("Polymarket", "P1", "Team Alpha 2026 Championship")
        result = find_title_candidates([k, p])
        self.assertEqual(result, [(k, p)])

    def test_overly_common_token_dropped_from_blocking_does_not_alone_produce_candidates(self):
        # 51 Polymarket rows all contain "generic" (> _MAX_COMMON_TOKEN_DF
        # of 50), so that token alone must not be used for blocking. A
        # Kalshi row sharing ONLY "generic" plus one other non-overlapping
        # word with each of them should therefore match none of them.
        poly_rows = [
            make_price("Polymarket", f"P{i}", f"Generic Event Number {i} Happens")
            for i in range(51)
        ]
        k = make_price("Kalshi", "K1", "Generic Something Else Entirely")
        result = find_title_candidates([k, *poly_rows])
        self.assertEqual(result, [])

    def test_common_franchise_phrase_excluded_from_similarity_not_just_blocking(self):
        """Regression test for the real false-positive pattern found on live
        sports data: many genuinely different propositions (different
        countries, different years) share one long boilerplate event phrase
        ("Fifa World Cup Host"). Once that phrase's document frequency
        exceeds _MAX_COMMON_TOKEN_DF, it must be dropped from the
        SIMILARITY score too -- not just the blocking index -- or two
        clearly-unrelated markets that only share the boilerplate phrase
        still cross min_similarity purely on its account.
        """
        noise = []
        for i in range(51):
            noise.append(make_price("Kalshi", f"NK{i}", f"Fifa World Cup Host Trivia Question {i}"))
            noise.append(make_price("Polymarket", f"NP{i}", f"Fifa World Cup Host Trivia Question {i}"))

        k = make_price("Kalshi", "K1", "Will Germany host the Fifa World Cup?")
        p = make_price("Polymarket", "P1", "Will Barcelona host the Fifa World Cup final?")
        # Raw tokens (pre-fix) would have shared {fifa, world, cup, host} = 4
        # of K's 5 -- an 0.8 overlap, comfortably over threshold. After
        # dropping the now-common fifa/world/cup/host, K_sig={germany},
        # P_sig={barcelona, final} -- zero overlap, correctly rejected.
        result = find_title_candidates(noise + [k, p])
        self.assertNotIn((k, p), result)

    def test_custom_min_similarity_threshold_is_honored(self):
        # k = {team, alpha, beta, gamma, win, 2026, championship} (7)
        # p = {2026, championship, team, delta, epsilon, zeta, parking, sale} (8)
        # shared = {2026, championship, team} = 3 (>= _MIN_SHARED_TOKENS, so
        # the count gate doesn't confound this -- deliberately not reusing
        # the 2-shared-token pair from the boundary tests above, since that
        # would now be excluded by the count gate before min_similarity is
        # even reached). ratio = 3/min(7,8) = 3/7 ~= 0.4286: excluded at the
        # default 0.5 threshold, included when the caller explicitly lowers
        # min_similarity to 0.4.
        k = make_price("Kalshi", "K1", "Will Team Alpha Beta Gamma win the 2026 Championship?")
        p = make_price("Polymarket", "P1", "2026 Championship Team Delta Epsilon Zeta Parking Sale")
        self.assertEqual(find_title_candidates([k, p]), [])
        self.assertEqual(find_title_candidates([k, p], min_similarity=0.4), [(k, p)])

    def test_custom_min_shared_tokens_threshold_is_honored(self):
        # k = {zeta, corp, announce, merger} (4)
        # p = {zeta, corp, q3, earnings, report} (5)
        # shared = {zeta, corp} = 2 (< default _MIN_SHARED_TOKENS of 3, so
        # excluded by default). ratio = 2/min(4,5) = 0.5, which already
        # clears the default min_similarity on its own -- deliberately
        # constructed this way so lowering min_shared_tokens is the ONLY
        # thing that changes the outcome, not a side effect of also
        # needing a looser min_similarity.
        k = make_price("Kalshi", "K1", "Will Zeta Corp announce merger?")
        p = make_price("Polymarket", "P1", "Zeta Corp Q3 Earnings Report")
        self.assertEqual(find_title_candidates([k, p]), [])
        self.assertEqual(find_title_candidates([k, p], min_shared_tokens=2), [(k, p)])

    def test_real_ipo_pair_now_found_by_default_via_strong_match(self):
        """Regression test for a real false negative found 2026-08-20:
        "When will Anthropic officially announce an IPO?" (Kalshi) and
        "Will Anthropic IPO by September 15, 2026?" (Polymarket) are the
        same real question, but share only 2 tokens ("anthropic", "ipo")
        -- below the default _MIN_SHARED_TOKENS of 3, and their overlap
        ratio (0.4) is also below the default _MIN_SIMILARITY (0.5). Fixed
        by _STRONG_MATCH_TOKENS -- "ipo" is specific and unambiguous enough
        that 2 shared tokens including it is trustworthy on its own, unlike
        a bare shared proper noun (see the San Francisco test above). Now
        found with the plain default call, no overrides needed.
        """
        k = make_price("Kalshi", "K1", "When will Anthropic officially announce an IPO?")
        p = make_price("Polymarket", "P1", "Will Anthropic IPO by September 15, 2026?")
        self.assertEqual(find_title_candidates([k, p]), [(k, p)])

    def test_strong_match_does_not_bridge_a_different_sub_question(self):
        """The real collision risk _STRONG_MATCH_TOKENS' own comment
        describes: "Which bank will lead Anthropic's IPO?" (a question
        about the underwriter) shares the identical {anthropic, ipo} / 0.4
        shape as the genuine timing question above -- token overlap alone
        can't tell them apart. The interrogative-lead guard
        (_starts_with_interrogative) is what actually blocks this one.
        """
        k = make_price("Kalshi", "K1", "Which bank will lead Anthropic's IPO?")
        p = make_price("Polymarket", "P1", "Will Anthropic IPO by September 15, 2026?")
        self.assertEqual(find_title_candidates([k, p]), [])

    def test_shared_proper_noun_alone_does_not_produce_a_candidate(self):
        """Regression test for a real false positive found on live sports
        data (2026-08-20): a football hosting-announcement question and an
        unrelated baseball point-spread bet, sharing nothing but the city
        name. {san, francisco} = 2 shared tokens (the old _MIN_SHARED_TOKENS
        floor) and 2 of the Polymarket title's 4 tokens = 0.5 similarity
        (exactly _MIN_SIMILARITY) -- both gates passed by coincidence, not
        real content overlap. See _MIN_SHARED_TOKENS's own comment for the
        full incident and why 3, not some other value, was chosen.
        """
        k = make_price(
            "Kalshi", "K1",
            "Will the San Francisco Pro Football team be announced as the "
            "host for the 2031 Pro Football Championship?",
        )
        p = make_price("Polymarket", "P1", "Spread: San Francisco Giants (-2.5)")
        result = find_title_candidates([k, p])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
