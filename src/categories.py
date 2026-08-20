"""Loads the category tag for each verified crosswalk market.

Category is purely a display/organization concern for the website --
arb_engine.py's matching and detection logic has no notion of it. Read
independently here, keyed by canonical_market_name (1:1 with a verified
pair), rather than folded into market_matcher.py's load_market_pairs()/
apply_market_pairs(), which have an established contract this doesn't
need to touch.
"""

import csv

# The 8 categories this project has verified Kalshi/Polymarket filter
# aliases for (see main.py's _CATEGORY_ALIASES) -- fixed display order and
# label, independent of how many crosswalk pairs currently fall in each.
CATEGORY_ORDER: list[str] = [
    "culture", "politics", "elections", "sports",
    "financials", "economics", "tech", "climate",
]
CATEGORY_LABELS: dict[str, str] = {
    "culture": "Culture",
    "politics": "Politics",
    "elections": "Elections",
    "sports": "Sports",
    "financials": "Financials",
    "economics": "Economics",
    "tech": "Tech",
    "climate": "Climate",
}


def load_market_categories(path: str) -> dict[str, str]:
    """canonical_market_name -> category slug, from market_pairs.csv's
    `category` column. Missing file or missing column means no category
    data yet, not an error -- callers should treat that the same as an
    empty mapping (matches load_market_pairs()'s own defensive style).
    """
    categories: dict[str, str] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("canonical_market_name") or "").strip()
                category = (row.get("category") or "").strip()
                if name and category:
                    categories[name] = category
    except FileNotFoundError:
        pass
    return categories
