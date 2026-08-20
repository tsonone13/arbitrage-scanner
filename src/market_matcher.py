"""Groups normalized prices into MarketGroup objects.

Trusted matching is intentionally simple and exact: two NormalizedPrice rows
are only considered the same market if they share an identical
(canonical_market_name, market_type) key. That's a conservative choice on
purpose -- for live Kalshi/Polymarket data, canonical_market_name is
currently just each venue's own title (see kalshi_importer.py /
polymarket_importer.py), so match_markets() only merges markets across
venues when those titles happen to line up exactly. It will never silently
mismatch two different markets just because their titles look similar.

data/market_pairs.csv is a verified mapping, not just documentation:
load_market_pairs()/apply_market_pairs() override canonical_market_name (and
market_type) for specific (venue, market_id) rows that a human has actually
checked resolve on the same question, same date, same source on both venues
-- title text is never trusted on its own to merge two venues' markets into
something arb_engine.py treats as tradeable (see arb_engine.py's cross-venue
guard for why that specifically failed once already).

find_title_candidates() below is a different, lower-stakes job: surfacing
*leads* worth pricing and showing a human as explicitly unverified (never
fed into match_markets()/the crosswalk). Since nothing here ever grants
trust, it's fuzzy on purpose -- token-overlap similarity, not exact
equality -- so it can catch two venues asking the same real question in
different words (e.g. "Fed Decision Sep 2026 Meeting: Hike 50+ bps" vs.
"Will the Fed raise rates by 50+ bps at the September 2026 meeting?"),
which exact-title matching structurally cannot. A wider net here costs
nothing in trust (every result still goes through
opportunity_view.py's UNVERIFIED_MATCH/UNVERIFIED_NO_EDGE relabeling and
still faces arb_engine's own close-date guard), only in how many leads get
shown -- and improving future work using outcome names / resolution source
agreement / contract wording remains open, same as before.
"""

import csv
import re
from collections import defaultdict
from dataclasses import replace

from models import MarketGroup, NormalizedPrice


def match_markets(prices: list[NormalizedPrice]) -> list[MarketGroup]:
    """Group normalized prices by (canonical_market_name, market_type)."""
    groups: dict[tuple[str, str], list[NormalizedPrice]] = defaultdict(list)
    for price in prices:
        key = (price.canonical_market_name, price.market_type)
        groups[key].append(price)

    market_groups: list[MarketGroup] = []
    for (canonical_market_name, market_type), group_prices in groups.items():
        outcomes = sorted({p.outcome_name for p in group_prices})
        market_groups.append(MarketGroup(
            canonical_market_name=canonical_market_name,
            market_type=market_type,
            outcomes=outcomes,
            prices=group_prices,
            match_confidence=1.0,  # exact key match -- see module docstring
        ))
    return market_groups


def load_market_pairs(path: str) -> dict[tuple[str, str], tuple[str, str]]:
    """Load the hand-verified crosswalk: (venue, market_id) -> (canonical_market_name, market_type).

    Missing file means no crosswalk entries yet, not an error -- callers
    should treat that the same as an empty crosswalk.
    """
    pairs: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                venue = (row.get("venue") or "").strip()
                market_id = (row.get("market_id") or "").strip()
                canonical_market_name = (row.get("canonical_market_name") or "").strip()
                market_type = (row.get("market_type") or "binary").strip()
                if venue and market_id and canonical_market_name:
                    pairs[(venue, market_id)] = (canonical_market_name, market_type)
    except FileNotFoundError:
        pass
    return pairs


def apply_market_pairs(
    prices: list[NormalizedPrice], pairs: dict[tuple[str, str], tuple[str, str]]
) -> list[NormalizedPrice]:
    """Override canonical_market_name/market_type/outcome_name for rows with a
    verified crosswalk entry.

    Keyed by (venue, market_id) -- a stable venue-assigned identifier -- not
    by title text, which is exactly what drifted/collided before. outcome_name
    also gets standardized to "Yes": _group_by_outcome() in arb_engine.py
    buckets by outcome_name *before* anything else runs, so two venues'
    free-text outcome labels for the same real proposition (Kalshi's "Fed
    maintains rate" vs Polymarket's "No change") would otherwise land in
    different buckets and never even reach the venue/date checks -- silently
    matching nothing despite a correct canonical_market_name override. Each
    binary crosswalk entry already names one specific proposition, so "Yes"
    is the only outcome there is.
    """
    if not pairs:
        return prices
    result: list[NormalizedPrice] = []
    for price in prices:
        override = pairs.get((price.venue, price.market_id))
        if override:
            canonical_market_name, market_type = override
            result.append(replace(
                price,
                canonical_market_name=canonical_market_name,
                market_type=market_type,
                outcome_name="Yes",
            ))
        else:
            result.append(price)
    return result


# Universal English function words only -- no domain words (e.g. "win"),
# since those still carry real signal and hand-tuning a domain stoplist
# risks quietly cutting precision for no clearly justified gain.
_STOPWORDS = frozenset({
    "will", "the", "a", "an", "of", "in", "on", "to", "be", "by", "at", "for",
    "and", "or", "is", "are", "this", "that", "it", "as", "with",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A pair needs at least this many shared significant words, *and* to clear
# _MIN_SIMILARITY below -- the count alone stops two long, mostly-unrelated
# titles that happen to share one common word from slipping through on a
# technicality. Raised from 2 to 3 after a real false positive on live
# sports data (2026-08-20): a two-word proper noun alone -- a city name --
# was enough to clear both gates when the other title was short. "Will the
# San Francisco Pro Football team be announced as the host for the 2031
# Pro Football Championship?" matched Polymarket's "Spread: San Francisco
# Giants (-2.5)": {san, francisco} = 2 shared tokens (exactly the old
# floor) and 2 of the Polymarket title's 4 tokens = 0.5 similarity (exactly
# _MIN_SIMILARITY) -- a football hosting-announcement question and an
# unrelated baseball point-spread bet, sharing nothing but the city. Same
# pattern hit Tampa Bay/Los Angeles pairs in the same scan and separately
# in the culture category (Tom Holland: Bond casting vs. Spider-Man
# casting; two different actors "as" two different unrelated characters).
# Requiring 3 forces at least one word of real content beyond a shared
# name. Checked empirically against four live categories before raising
# this: removed 22/22 examined sports false positives and 19/24 culture
# false positives, while every genuine match found in politics (1/1) and
# elections (16/16) -- including same-question-different-country pairs
# that only have a 2-word name plus one country/context word -- survived
# unchanged. (Tried 4: cuts recall sharply for no real precision gain --
# most genuine "[name] + [1 context word]" pairs only clear 3, so 4 drops
# elections to 3/16 and loses the one politics match entirely.) Doesn't
# catch every case of this failure mode -- "Taylor Swift perform at Sphere
# 2027?" vs "Taylor Swift pregnant before 2027?" still shares 3 tokens
# (taylor, swift, 2027) and survives -- but 3 is the value that removes
# the bulk of it without cutting real matches; a full fix would need to
# distinguish proper nouns from regular words, which this token-overlap
# approach has no way to do.
_MIN_SHARED_TOKENS = 3
# Overlap coefficient (|shared| / smaller title's token count), not Jaccard
# (|shared| / union) -- confirmed by hand on a real pair still in this
# category ("Fed Decision Sep 2026 Meeting: Hike 50+ bps" vs "Will the Fed
# raise rates by 50+ bps at the September 2026 meeting?"): Jaccard scores
# that genuine match at 0.45 (5 shared of 11 union) because Polymarket's
# phrasing is longer, which a naive 0.5 Jaccard cutoff would have missed
# entirely; overlap coefficient scores the same pair 0.625 (5 of the
# shorter title's 8 tokens) and still separates it from a same-length,
# different-month decoy ("Will the Fed cut rates in December 2026?", 0.4)
# by a healthy margin.
_MIN_SIMILARITY = 0.5
# A token this common *within one scan's batch* is dropped from blocking
# AND from the similarity score's NUMERATOR (shared-token count) -- see
# _token_overlap's docstring for why the DENOMINATOR still uses each
# title's full, unfiltered length. Confirmed necessary, not just
# theoretical, on live sports data (2026-08-19): titles built around a long
# shared event/franchise phrase ("...the 2038 Men's FIFA World Cup?" vs
# "...host the final of the 2030 FIFA World Cup?") scored 0.5 overlap from
# "fifa"/"world"/"cup"/"host" alone, across genuinely different
# propositions (different countries, different years a full 8 years apart)
# -- 119,229 raw candidates on that one category scan. Raising
# _MIN_SIMILARITY globally can't fix this cleanly: the same live data's
# genuine Fed-meeting rewording (see _MIN_SIMILARITY's own comment) scores
# 0.625, uncomfortably close to that false positive's 0.5 -- no single flat
# cutoff separates them. Dropping pathologically common tokens from the
# numerator does: it directly targets *why* the false positive scored high
# (a repeated boilerplate phrase, not real content overlap) without
# touching genuinely rare, distinguishing words like "fed"/"bps".
_MAX_COMMON_TOKEN_DF = 50


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(w for w in _TOKEN_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1)


def _token_overlap(a: frozenset[str], b: frozenset[str], common_tokens: frozenset[str] = frozenset()) -> float:
    """(shared tokens, excluding common_tokens) / size of the SMALLER
    title's FULL token set (including common_tokens) -- see
    _MIN_SIMILARITY's comment for why overlap coefficient, not Jaccard.
    0.0 (not an error) if either title tokenized to nothing.

    The denominator deliberately keeps common words in the count even
    though the numerator excludes them from counting as "shared." Using
    the post-filter, common-word-free length for BOTH was the first
    version of this fix and still had a real bug, confirmed on live data:
    a short title that's built almost entirely from common/boilerplate
    words (e.g. "Will Kirk Cousins announce his retirement before the
    2026-27 NFL season?" -- everything but the athlete's name is common
    within a sports-category batch) reduces to just that one rare name
    after filtering, which then scores a trivial 1.0 against ANY other
    title naming the same athlete, regardless of what it's actually
    asking (a completely different real question -- "be the Raiders' Week
    1 starting QB?" -- scored a perfect 1.0 under the filtered-denominator
    version). Keeping the full original length as the denominator instead
    means a title that's mostly boilerplate can only ever contribute a
    small fraction of "real," specific overlap, however little content
    happens to remain after filtering -- confirmed this correctly drops
    that pair's score to 0.33, and two more real false positives found the
    same way (Pete Buttigieg: DNC chair vs. presidential run, scored 1.0 ->
    0.29; Goldman Sachs: next CEO vs. will it fail, scored 0.67 -> 0.40),
    while the genuine Fed-meeting rewording is unaffected (0.625 either
    way -- nothing in that pair was common enough to filter).
    """
    if not a or not b:
        return 0.0
    shared = len((a - common_tokens) & (b - common_tokens))
    return shared / min(len(a), len(b))


def find_title_candidates(
    prices: list[NormalizedPrice],
    pairs: dict[tuple[str, str], tuple[str, str]] | None = None,
    min_similarity: float = _MIN_SIMILARITY,
    min_shared_tokens: int = _MIN_SHARED_TOKENS,
) -> list[tuple[NormalizedPrice, NormalizedPrice]]:
    """Surface possible cross-venue matches by fuzzy title similarity, for a
    human (or the website's scan feature) to review -- a lead, never a
    verified match, and never fed to match_markets()/arb_engine directly.
    See the module docstring for why fuzzy is safe here even though it's
    deliberately avoided for trusted matching.

    min_shared_tokens defaults to the module constant (3 -- see its own
    comment for the false-positive history that value fixes), but is a
    real parameter, not just a hardcoded wall: confirmed directly
    (2026-08-20) that 3 has a real recall cost of its own, not just a
    precision win -- "When will Anthropic officially announce an IPO?"
    vs. "Will Anthropic IPO by September 15, 2026?" is a genuine match
    that shares only 2 tokens ("anthropic", "ipo"), the rest of each
    title being phrasing/date filler, not new subject matter. No
    reliable way was found to tell that case apart from a real false
    positive (a shared two-word proper noun, e.g. "san"/"francisco",
    across two genuinely unrelated propositions) using token overlap
    alone -- both are "2 shared tokens, rest of each title differs." A
    lower value here is a deliberate, manual override for someone who
    wants to search more permissively and is prepared to review more
    false positives to find it, not a new default.

    Two scale-driven steps happen before any pair is scored, both keyed off
    each token's document frequency within THIS batch (i.e. this one scan,
    not a fixed global list -- what's "pathologically common" depends on
    the category being scanned):
    - Blocked via an inverted token index (Kalshi row -> shares a token
      with which Polymarket rows) rather than comparing every Kalshi title
      against every Polymarket title -- needed at this project's real
      scale (a single category listing can be tens of thousands of rows
      per venue; a full cross product would be infeasible).
    - Tokens with document frequency over _MAX_COMMON_TOKEN_DF are treated
      as batch-local stopwords -- excluded from blocking (would fan out to
      a near-full cross product for near-zero signal) and from counting as
      "shared" in the similarity score (see _MAX_COMMON_TOKEN_DF's and
      _token_overlap's comments for why this is required, not just an
      optimization).

    A surviving pair still must share at least _MIN_SHARED_TOKENS
    non-common tokens AND clear min_similarity (see _token_overlap for its
    exact formula) to become a candidate.

    Pairs where both sides already have an independent crosswalk entry are
    excluded, same as before -- they're already handled, not new leads.
    """
    pairs = pairs or {}
    kalshi_rows = [(p, _tokenize(p.raw_market_name)) for p in prices if p.venue == "Kalshi"]
    poly_rows = [(p, _tokenize(p.raw_market_name)) for p in prices if p.venue == "Polymarket"]

    doc_freq: dict[str, int] = defaultdict(int)
    for _price, tokens in kalshi_rows:
        for tok in tokens:
            doc_freq[tok] += 1
    for _price, tokens in poly_rows:
        for tok in tokens:
            doc_freq[tok] += 1
    common_tokens = frozenset(tok for tok, count in doc_freq.items() if count > _MAX_COMMON_TOKEN_DF)

    def significant(tokens: frozenset[str]) -> frozenset[str]:
        return tokens - common_tokens if common_tokens else tokens

    poly_significant = [significant(tokens) for _price, tokens in poly_rows]
    poly_index: dict[str, list[int]] = defaultdict(list)
    for i, tokens in enumerate(poly_significant):
        for tok in tokens:
            poly_index[tok].append(i)

    candidates: list[tuple[NormalizedPrice, NormalizedPrice]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for kalshi_price, k_tokens in kalshi_rows:
        k_sig = significant(k_tokens)
        blocked_poly_idxs: set[int] = set()
        for tok in k_sig:
            blocked_poly_idxs.update(poly_index.get(tok, ()))

        for i in blocked_poly_idxs:
            poly_price, p_tokens = poly_rows[i]
            pair_key = (kalshi_price.market_id, poly_price.market_id)
            if pair_key in seen_pairs:
                continue
            if (kalshi_price.venue, kalshi_price.market_id) in pairs and (poly_price.venue, poly_price.market_id) in pairs:
                continue
            # Shared-count gate uses the significant (common-word-free)
            # sets; the ratio passed to _token_overlap uses the FULL
            # original token sets -- see that function's docstring for why
            # the denominator must not be the already-filtered length.
            shared = len(k_sig & poly_significant[i])
            if shared < min_shared_tokens:
                continue
            if _token_overlap(k_tokens, p_tokens, common_tokens) < min_similarity:
                continue
            seen_pairs.add(pair_key)
            candidates.append((kalshi_price, poly_price))
    return candidates
