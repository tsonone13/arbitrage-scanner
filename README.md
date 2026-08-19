# Arbitrage Scanner

A terminal-only, read-only prediction-market arbitrage scanner. It scans
prices across multiple venues (currently live Kalshi and Polymarket, plus
mock/CSV fixtures), normalizes them to a common bid/ask format, and prints
trades where a guaranteed $1 payout can be bought for less than $1.

This is v1. It does **not** place trades, does **not** use any API key, and
does **not** have a web UI — it prints ranked opportunities to your terminal
and stops there. Right now it only runs the **binary cross-venue** check
(buy YES on one venue, NO on another) — full-book multi-outcome baskets
exist in the code but are disabled pending more matching safeguards; see
"Full-book checks: built but currently disabled" below.

## What the tool does

1. **Importers** pull raw prices from each venue (or from mock/CSV data).
2. The **normalizer** guarantees every price is a decimal probability in
   `[0, 1]` — never cents, never a display-rounded percentage.
3. The **market matcher** groups prices that represent the same market
   across venues — using a hand-verified crosswalk (`data/market_pairs.csv`)
   for specific markets a human has actually checked, plus exact-title
   matching for everything else (see "Market matching" below for why both
   pieces exist and what each one is trusted for).
4. The **arb engine** does pure math on those groups: is the total cost to
   buy a guaranteed payout less than the payout itself?
5. The **opportunity ranker** sorts what it found.
6. The **terminal reporter** prints the top N, using `rich` for readable
   output — plus a scan summary (prices loaded per venue, groups formed) so
   you can see the pipeline actually read real data even on a run where
   nothing clears the arb filters.

## How binary arbitrage works

For a single yes/no proposition, buying YES on one venue and NO on another
locks in $1 no matter what happens — you hold one full unit of either side.
If:

```
YES ask (venue A) + NO ask (venue B) + fee buffer < 1.00
```

...you've locked in `1.00 - total_cost` for less than a dollar. The engine
checks both directions (YES on A / NO on B, and YES on B / NO on A) for
every *cross-venue* pair and every outcome in a market group.

**Cross-venue only, deliberately.** `check_binary_cross_venue_arbs` skips
any pair on the same venue. On one well-functioning order book, YES and NO
for the same proposition are two sides of the same book (a NO ask is
mechanically ~`1 - a YES bid`), so they structurally can't cross — a
same-venue "match" almost always means the matcher grouped two different
propositions together, not a real edge. That's not a theoretical concern:
see the incident below, which is exactly what happened.

A second guard rejects any pair whose close dates disagree (comparing
`close_time`/`endDate` at the calendar-day level, when both sides report
one). Two rows that are genuinely the same bet should resolve on the same
day; a mismatch is a decisive sign they're actually different propositions,
e.g. one leg of a date ladder ("before 2027") paired against another
("before 2029").

### The bug that made both guards necessary

Kalshi's own `title` field for ticker `KXRECOGROC-29` reads *"Will Trump
recognize Somaliland?"* — but that market's `rules_primary` shows it's
actually about the Republic of China's recognition, unrelated to the real
Somaliland market (`KXRECOGSOMALI-29`). Both tickers are on Kalshi, so
before the cross-venue guard existed, the matcher (which builds
`canonical_market_name` from each venue's own title text) merged them, and
the binary check happily "arbitraged" between two unrelated bets, showing a
large fake edge, because a venue's own metadata was wrong. Restricting the
binary check to cross-venue pairs makes this whole failure class impossible
regardless of *why* a title collided — same-venue comparisons can't
demonstrate real arbitrage in the first place, so there's no reason to
allow them.

## Full-book checks: built but currently disabled

`check_full_book_yes_arbs` and `check_full_book_no_arbs` still exist in
`arb_engine.py`, fully implemented and tested against mock/CSV data, but
`main.py` no longer calls them — `--type` only accepts `binary` right now.
They were pulled out after the checks above turned up real problems in live
multi-outcome data (see below); re-enabling them is future work, not a
`--type` flag away.

For a market with N mutually exclusive outcomes where exactly one pays $1,
buying the single cheapest YES ask for *every* outcome guarantees you hold
the winning YES no matter which outcome happens (full-book NO is the
mirror: buy the cheapest NO on every outcome, `N - 1` of them pay $1,
guaranteed payout is `N - 1`). Both require a **complete, exhaustive**
outcome set — if even one outcome is missing, "guaranteed payout" is a lie.

That completeness requirement is where these checks turned out to be
fragile on live data. Kalshi's `mutually_exclusive` flag (and Polymarket's
`negRisk`) only promise that *at most one* listed outcome can win — not
that one of them *must*. Kalshi's own "What will be the 51st state in
Trump's term?" event lists 8 candidate territories with no listed outcome
for "no new state at all," even though a sibling Kalshi market prices that
base case at roughly 87%. Buying every listed YES there isn't arbitrage —
it's an uncovered bet that something on the list happens at all, and most
of the time it wouldn't pay out. `arb_engine.py` has a guard for this
(`SUSPICIOUS_EDGE_RATIO`: any full-book basket whose gross edge is ≥20% of
the guaranteed payout gets `status = "REVIEW"` instead of `"PASS"`, shown
in a yellow panel instead of green), which is a real mitigation but not a
proof of correctness — it's a heuristic on top of a fundamentally
unverifiable assumption (that the listed outcomes really are the whole
picture). Given that, and given the binary path had its own real bug, the
call for now is to keep the surface area small and trustworthy rather than
show a check with a known, only-partially-mitigated failure mode.

## Market matching: a verified crosswalk, plus exact-title fallback

Kalshi and Polymarket phrase the same real-world bet differently, and
nudging that with fuzzy text similarity is genuinely risky, not just noisy
— two titles can look nearly identical while differing in resolution date,
threshold, or resolution source, and a false match recommends a trade that
isn't actually the same bet. So there's no fuzzy matching here. Two things
happen instead:

1. **`data/market_pairs.csv` is a real, live lookup table**, not just
   documentation. It maps `(venue, market_id)` — a stable ID, never title
   text — to a shared `canonical_market_name`/`market_type`.
   `load_market_pairs()`/`apply_market_pairs()` in `market_matcher.py` apply
   it before grouping. Every row in that file should represent a market a
   human has actually checked against both venues' real resolution rules
   and dates, not just matched on similar-looking titles — that's exactly
   the mistake that caused the Somaliland/ROC incident above. As of this
   writing it holds 90 verified pairs across four series, each confirmed by
   reading both venues' actual rules text (Kalshi's `rules_primary`,
   Polymarket's `description`), not by title or date matching alone:
   - Fed decision at the September 2026 FOMC meeting (5 buckets)
   - Ottawa 2026 mayoral election, Vancouver 2026 mayoral election, São
     Paulo 2026 gubernatorial election, Minas Gerais 2026 gubernatorial
     election (25 candidates total)
   - Movie delay markets: Shrek 5, Dune: Part Three, Avengers: Doomsday
   - Big Brother Season 28 winner (18 contestants)
   - 46th FIDE Chess Olympiad Open Tournament winner (40 countries)
2. **Everything else still falls back to exact `(canonical_market_name,
   market_type)` matching** with `match_confidence = 1.0` for an exact key
   match. Since Kalshi and Polymarket essentially never phrase the same
   question identically, this fallback alone finds ~0 cross-venue matches
   on live data — which is expected, not a bug (see below).

Adding a new verified pair means: find a market on both venues, read (not
skim) both platforms' actual resolution rules and dates, confirm they
really are the same proposition, then add one row per venue to
`data/market_pairs.csv` with the venue's `market_id` (the Kalshi ticker, or
the Polymarket market `id`) and a shared `canonical_market_name`. Two
lessons from doing this across categories, both worth knowing before
verifying more:

- **Don't compare Kalshi's `close_time` directly.** It's an outer
  settlement deadline that can include a multi-year buffer for contested
  results (confirmed via a real market's `rules_secondary`: "remains open
  until the rescheduled election or two years from the original date"), so
  two venues covering the identical event can show close dates months or
  years apart. `KalshiImporter` now populates `close_time` from
  `expected_expiration_time` instead, which tracks much closer to
  Polymarket's `endDate` for genuinely-matching markets — but even that
  isn't absolute (Big Brother's dates still disagreed by ~3 months after
  the fix; reading the actual rules confirmed it was the same market
  anyway, with Polymarket's `endDate` just being a rolling estimate). Dates
  are a fast pre-filter, never the final word.
- **A title match is not a resolution-rules match, even when everything
  else lines up.** One Brazil-presidential-election candidate had a
  Polymarket `question` field ("Will Renan Santos win...") that flatly
  contradicted its own `description` field, which described an unrelated
  vote-margin-bracket market — a Polymarket-side data inconsistency in the
  same spirit as the Kalshi ROC/Somaliland mislabeling. That candidate was
  excluded. A climate candidate ("Will 2026 be the hottest year on
  record?") was excluded for a subtler reason: Kalshi's rule requires 2026
  to beat both 2025's specific recorded value *and* a stated 1.28°C
  threshold, while Polymarket ranks 2026 against all years by the same
  underlying NASA GISS data series — plausibly equivalent if 1.28°C happens
  to be 2025's actual value, but not confirmable without an external number
  neither venue's text supplies, so it was left out rather than assumed.

### Why a live binary scan may still show few candidates outside a verified series

Exact-title matching alone finds ~0 cross-venue matches on live data across
most categories — confirmed directly: politics, financials, economics, and
tech all returned zero title-candidates even scanning their full catalogs
(13,000+ markets listed for politics alone). Elections, culture, and sports
are the exception, because "Will [name] win [event]?" and "Will [movie] be
delayed?" are template phrasings both venues happen to converge on; most
other categories don't share a phrasing convention at all. That is the
honest, structural shape of this problem, not a bug — growing real coverage
means growing the verified crosswalk (or eventually building the NLP-assisted
matching described in "Future versions"), not loosening the matching logic
itself.

### Category-scoped scanning, and finding new candidates to verify

`--category <name>` scopes a kalshi/polymarket/live scan to one category
instead of the whole catalog. Each side is filtered differently: Kalshi's
own `category` field, filtered client-side (there's no server-side filter
for it, confirmed empirically the same way the missing sort parameter was),
and Polymarket's tag system, filtered server-side via `tag_slug` (a real
filter, unlike Kalshi's). The two venues don't share a taxonomy, so every
entry in `main.py`'s `_CATEGORY_ALIASES` table is a hand-verified pair of
filter values, not a guess: Kalshi's value is one of the literal strings
its `/events` endpoint actually returns (sampled 4,000 open events and
tabulated the distribution), and Polymarket's `tag_slug` was confirmed to
exist and return on-topic results via `GET /tags/slug/<slug>` before being
added. Currently verified:

| `--category` | Kalshi `category` | Polymarket `tag_slug` |
|---|---|---|
| `culture` | Entertainment | pop-culture |
| `politics` | Politics | politics |
| `elections` | Elections | elections |
| `sports` | Sports | sports |
| `financials` | Financials | finance |
| `economics` | Economics | economy |
| `tech` | Science and Technology | tech |
| `climate` | Climate and Weather | weather |

Kalshi's "Mentions" and "Companies" categories are deliberately left out:
Polymarket has no single equivalent tag for "Mentions" (it fragments into
dozens of specific tags like "Trump Speech Mentions" instead of one general
one), so there's nothing to verify a pairing against yet.

Narrowing scope like this also makes it practical to search for *new*
crosswalk candidates instead of only checking the verified crosswalk:
`market_matcher.find_title_candidates()` buckets same-category prices by a
lightly normalized title (lowercase, whitespace/punctuation only — no fuzzy
or semantic matching) and surfaces cross-venue pairs that land in the same
bucket. This is the "match cheaply first, then price" idea in practice:
candidates are found from metadata alone, and only a verified pair ever
gets treated as a real, priced opportunity. A title match here is a **lead,
not a verified pair** — it's printed in a separate, deliberately plain
table (never green "ARB FOUND"), and still needs the same manual check
(read both venues' actual resolution rules and dates) before it's safe to
add to `data/market_pairs.csv`. First real runs found leads like "Will
Shrek 5 be delayed?" and nineteen individual "Will [contestant] win Big
Brother Season 28?" markets under `--category culture` (differing only in
capitalization), and 111 candidates under `--category elections` — election
markets are phrased far more consistently across venues than most
categories, for obvious reasons.

### Two speeds, on purpose

Fetching a venue's full catalog is expensive in two different ways: Kalshi
has no server-side relevance filter, so reaching any specific market means
paging through thousands of events (~15s for 6,000); Polymarket's event
listing is cheap, but pricing a market means a *separate* CLOB order-book
call per market, and most of a catalog will never match anything on the
other venue anyway. Paying either cost on every routine run doesn't scale,
so there are two distinct modes instead of one:

- **No `--category` (default).** The fast path. Only markets already in
  `data/market_pairs.csv` get fetched at all, by exact ticker/id
  (`KalshiImporter.get_normalized_prices_for_tickers()` /
  `PolymarketImporter.get_normalized_prices_for_ids()`) — a handful of
  direct requests instead of paginating a whole catalog. A `--source live`
  run against the current one-event crosswalk takes about a second. This is
  what routine "is there a real arb right now" runs should use, and it's
  also why growing `data/market_pairs.csv` is the thing that actually grows
  what a plain `--source live` run can find.
- **`--category <name>`.** The discovery path, for finding new candidates.
  Kalshi is still fetched eagerly within the category (its pricing is free,
  bundled into the same response as the listing), but Polymarket is
  fetched as metadata only first (`get_market_metadata()`, no CLOB calls),
  title-candidates are found from that metadata, and only the
  crosswalk-covered and newly-candidate-matched markets ever get priced via
  `get_normalized_prices_for_ids()`. A `--category elections` run lists
  ~14,900 markets across both venues but only ever prices the ~100 that
  matched something — the **Scan Summary** shows both numbers so it's clear
  "priced" doesn't mean "everything else was ignored," it means everything
  else had nothing to compare it to.

## Why importers are separate from the arb engine

`arb_engine.py` never imports `requests`, never knows a venue's name, and
never makes a network call — it only takes `NormalizedPrice` /
`MarketGroup` objects and returns `ArbOpportunity` objects. That means:

- The math can be tested against mock data with confidence it will behave
  identically against live data.
- Adding a new venue (PredictIt, ForecastEx, anything else) means writing
  one new `VenueImporter` subclass — nothing about matching, math, ranking,
  or display has to change.
- A bug in a venue's API integration can never corrupt the arbitrage math,
  and a bug in the math can never accidentally reach out to the network.

## Why we use executable ask prices only

Chart probabilities, last-traded prices, midpoints, and UI-rounded
percentages are not prices you can actually transact at. An "arbitrage"
computed from any of those is fiction — the only prices that matter are the
best executable ask on each side, which is what you'd actually pay to buy
in right now. Every importer in this project is required to return real
ask (and bid) prices, not display values.

## Live data: Kalshi and Polymarket, no API key

This version pulls real markets from Kalshi and Polymarket over their
public, unauthenticated REST endpoints — verified directly against both
APIs before writing the importers:

- **Kalshi** (`external-api.kalshi.com/trade-api/v2/events`): market
  listings and top-of-book yes/no bid/ask are public. Kalshi's separate
  per-ticker orderbook endpoint (full depth) does require a signed API key,
  so this version deliberately doesn't use it — top-of-book is all the arb
  math needs. There's no working sort/relevance parameter on this endpoint
  (confirmed empirically: `sort=volume` returns identical results to no
  sort), and the catalog is dominated by a long tail of thin sports/combo
  markets — e.g. the Fed decision event used in the crosswalk sits about
  5,200 events deep in the default ordering. `KalshiImporter` defaults to
  pulling 6,000 events (~15s) specifically so events like that are actually
  reachable; a lower default would silently miss exactly the markets most
  likely to matter.
- **Polymarket**: the Gamma API (`gamma-api.polymarket.com`) is used for
  market/event discovery and is fully public — and does support sorting, so
  `PolymarketImporter` requests events ordered by 24h volume descending, to
  prioritize markets actually worth scanning. The CLOB API
  (`clob.polymarket.com`) is used for the actual executable order book
  (`/books`) and is public for reads — only order placement requires
  signing.

No account, key, or secret is required to run `--source kalshi`,
`--source polymarket`, or `--source live`. That's intentional, not just a
v1 shortcut: a future public-facing version of this tool (e.g. a website)
can't depend on one person's private trading credentials, so keeping the
read path fully keyless now means there's nothing sensitive to strip out
later.

## How to run the terminal scanner

```bash
cd "$HOME/Desktop/Quant Project Work/Arbitrage Scanner"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/main.py --source mock
python src/main.py --source mock --min-edge 0.005 --top 10 --bankroll 10000
python src/main.py --source csv
python src/main.py --source live --min-edge -1
python src/main.py --source live --category elections --min-edge -1
```

The first four are near-instant (well under a second for mock/csv/live, live
included — see "Two speeds, on purpose" above for why plain `--source live`
is fast). Only `--category` runs take real time, since they have to list an
entire category before pricing anything: `--category elections` takes
roughly 15-20s, mostly Kalshi's pagination cost, which no category filter
avoids.

`--min-edge -1` is worth using on any live run: it shows every candidate
trade actually compared, not just ones clearing a threshold, which is the
fastest way to confirm the pipeline is reading and comparing real data (see
"Why a live binary scan may show zero candidates" above for why the count
may still be small on the fast path specifically). The **Scan Summary**
table prints regardless — it shows exactly how many prices were read from
each venue and how many market groups they formed, which is the first thing
to check: it proves the read pipeline worked even when the arb filters find
nothing to show.

## What the CLI arguments mean

| Flag | Choices / type | Default | Meaning |
|---|---|---|---|
| `--source` | mock, csv, kalshi, polymarket, live | mock | Where prices come from. `live` = Kalshi + Polymarket combined. |
| `--category` | culture, politics, elections, sports, financials, economics, tech, climate | none | Discovery mode: scope a scan to one category and search for new candidates. Slower than the default fast path. See "Category-scoped scanning" below. |
| `--type` | binary | binary | Which arb check(s) to run. Full-book checks exist in `arb_engine.py` but are not wired up right now — see above. |
| `--min-edge` | float | 0.005 | Minimum net edge (after fee buffer) required to report a trade. |
| `--fee-buffer` | float | 0.003 | Flat cost/slippage buffer subtracted from gross edge before filtering. |
| `--top` | int | 20 | Max opportunities printed (the engine still scans everything internally). |
| `--bankroll` | float | 1000 | Bankroll used for the "estimated profit" display on each opportunity. |

## What future versions will add

- Re-enabling full-book YES/NO checks once there's a better answer to the
  "is this outcome set really exhaustive" problem than a heuristic edge-size
  cutoff
- Deeper Kalshi order-book depth (via an authenticated key) instead of
  top-of-book only
- Other prediction-market venue importers (PredictIt, ForecastEx, and
  beyond) — both are already stubbed out as `VenueImporter` subclasses
- Growing `data/market_pairs.csv` well beyond one verified event
- Real NLP-assisted market matching: structured extraction of each market's
  subject, resolution condition, threshold, and resolution date/source,
  used as the actual match signal, with text/embedding similarity as a
  cheap candidate generator rather than the final answer — and given real
  money is on the line, a human-reviewable confirmation step before a new
  cross-venue mapping is trusted, with confirmed pairs promoted into
  `data/market_pairs.csv` the same way the Fed decision pair was added by
  hand
- Historical opportunity tracking
- Alerts
- Paper trading
- A dashboard/UI
- Manual trade execution support

None of that is built yet. This version is the terminal scanner only.
