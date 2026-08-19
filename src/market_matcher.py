"""Groups normalized prices into MarketGroup objects.

v1 matching is intentionally simple and exact: two NormalizedPrice rows are
only considered the same market if they share an identical
(canonical_market_name, market_type) key. That's a conservative choice on
purpose -- for live Kalshi/Polymarket data, canonical_market_name is
currently just each venue's own title (see kalshi_importer.py /
polymarket_importer.py), so this only merges markets across venues when
those titles happen to line up exactly. It will never silently mismatch two
different markets just because their titles look similar.

Future versions should improve matching using:
- market title similarity (embeddings / fuzzy text as a candidate generator,
  not a final decision -- short prediction-market titles are exactly the
  case where "looks similar" and "is the same bet" diverge)
- outcome names
- close time / event date proximity
- resolution source agreement
- contract wording and settlement rules
and should assign match_confidence < 1.0 for anything short of a verified,
ideally human-reviewed, cross-venue mapping.

data/market_pairs.csv is that verified mapping today, not just documentation:
load_market_pairs()/apply_market_pairs() override canonical_market_name (and
market_type) for specific (venue, market_id) rows that a human has actually
checked resolve on the same question, same date, same source on both venues
-- title text is never trusted on its own to merge two venues' markets (see
arb_engine.py's cross-venue guard for why that specifically failed once
already).
"""

import csv
from collections import defaultdict
from dataclasses import replace
from itertools import combinations

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


def _normalize_title(text: str) -> str:
    return " ".join(text.lower().strip().rstrip("?.!").split())


def find_title_candidates(
    prices: list[NormalizedPrice], pairs: dict[tuple[str, str], tuple[str, str]] | None = None
) -> list[tuple[NormalizedPrice, NormalizedPrice]]:
    """Surface possible cross-venue matches by normalized-title equality, for
    a human to review -- a lead, never a verified match, and never fed to
    arb_engine directly.

    Deliberately shallow normalization (lowercase, strip whitespace/trailing
    punctuation): enough to tolerate formatting differences, not fuzzy or
    semantic matching -- exactly the kind of "looks similar" similarity this
    project has already been burned by trusting (see the module docstring).
    Pairs already in the verified crosswalk are excluded since they're
    already handled, not new leads.

    Meant to run on a small, scoped set of prices (e.g. one category) --
    it's O(n) to bucket, but the point is to find candidates worth manually
    verifying before fetching full pricing for them, not to replace the
    crosswalk.
    """
    pairs = pairs or {}
    by_title: dict[str, list[NormalizedPrice]] = defaultdict(list)
    for price in prices:
        by_title[_normalize_title(price.raw_market_name)].append(price)

    candidates: list[tuple[NormalizedPrice, NormalizedPrice]] = []
    for rows in by_title.values():
        if len({r.venue for r in rows}) < 2:
            continue
        for a, b in combinations(rows, 2):
            if a.venue == b.venue:
                continue
            if (a.venue, a.market_id) in pairs and (b.venue, b.market_id) in pairs:
                continue
            candidates.append((a, b))
    return candidates
