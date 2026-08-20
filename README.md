# Arbitrage Engine for Prediction Markets

A terminal-and-website, read-only prediction-market arbitrage engine (Arbitrage Engine for short). It scans
prices across multiple venues (currently live Kalshi and Polymarket, plus
mock/CSV fixtures), normalizes them to a common bid/ask format, and surfaces
trades where a guaranteed $1 payout can be bought for less than $1. You can
run it from your terminal, or through a read-only website (FastAPI backend,
static HTML/JS frontend, same underlying pipeline).

This is v1. It does **not** place trades and does **not** use any API key
for its price data. Kalshi and Polymarket are both read over public,
unauthenticated endpoints (see "Live data" below). It only runs the
**binary cross-venue** check: buy YES on one venue, NO on another. A
multi-outcome full-book basket check (buying the cheapest YES/NO across
every outcome in a mutually-exclusive set) was built and tested, then
removed. It turned out to be fragile on live data (see "Market matching"
below for the same lesson on the matching side), and this project doesn't
need it.

## What the tool does

1. **Importers** pull raw prices from each venue (or from mock/CSV data).
2. The **normalizer** guarantees every price is a decimal probability in
   `[0, 1]`, never cents and never a display-rounded percentage.
3. The **market matcher** groups prices that represent the same market
   across venues. It uses a hand-verified crosswalk (`data/market_pairs.csv`)
   for specific markets a human has actually checked, plus exact-title
   matching for everything else (see "Market matching" below for why both
   pieces exist and what each one is trusted for).
4. The **arb engine** does pure math on those groups: is the total cost to
   buy a guaranteed payout less than the payout itself?
5. The **opportunity ranker** sorts what it found.
6. The **terminal reporter** prints the top N, using `rich` for readable
   output. It also prints a scan summary (prices loaded per venue, groups
   formed), so you can see the pipeline actually read real data even on a
   run where nothing clears the arb filters.

## How binary arbitrage works

For a single yes/no proposition, buying YES on one venue and NO on another
locks in $1 no matter what happens. You end up holding one full unit of
either side. If:

```
YES ask (venue A) + NO ask (venue B) + fee buffer < 1.00
```

...you've locked in `1.00 - total_cost` for less than a dollar. The engine
checks both directions (YES on A / NO on B, and YES on B / NO on A) for
every *cross-venue* pair and every outcome in a market group.

**Cross-venue only, deliberately.** `check_binary_cross_venue_arbs` skips
any pair on the same venue. On one well-functioning order book, YES and NO
for the same proposition are two sides of the same book (a NO ask is
mechanically about `1 - a YES bid`), so they structurally can't cross. A
same-venue "match" almost always means the matcher grouped two different
propositions together, and it's a real risk, not a theoretical one: see the
incident below, which is exactly what happened.

A second guard rejects any pair whose close dates disagree (comparing
`close_time`/`endDate` at the calendar-day level, when both sides report
one). Two rows that are genuinely the same bet should resolve on the same
day. A mismatch is a decisive sign they're actually different propositions,
for example one leg of a date ladder ("before 2027") paired against another
("before 2029").

### The bug that made both guards necessary

Kalshi's own `title` field for ticker `KXRECOGROC-29` reads *"Will Trump
recognize Somaliland?"* But that market's `rules_primary` shows it's
actually about the Republic of China's recognition, unrelated to the real
Somaliland market (`KXRECOGSOMALI-29`). Both tickers are on Kalshi, so
before the cross-venue guard existed, the matcher (which builds
`canonical_market_name` from each venue's own title text) merged them, and
the binary check happily "arbitraged" between two unrelated bets, showing a
large fake edge, because a venue's own metadata was wrong. Restricting the
binary check to cross-venue pairs makes this whole failure class impossible
regardless of *why* a title collided. Same-venue comparisons can't
demonstrate real arbitrage in the first place, so there's no reason to
allow them.

### A flat fee buffer is not the same as real fees

`fee_buffer` (default 0.003) is a flat, venue-agnostic simplification
subtracted from gross edge before `arb_engine.py` calls something `PASS`.
It has no idea what either venue actually charges. `slippage.py` computes
the real number: each venue's own taker-fee formula (`fee = rate * qty *
price * (1 - price)`), walked level-by-level against real order-book depth.
Confirmed directly on live data (2026-08-19): of 6 markets the flat 0.3%
buffer classified `PASS`, 4 had a real, fee-adjusted profit of exactly
**$0**. Kalshi's ~7% taker coefficient and Polymarket's own posted
per-market rate can each individually exceed the flat buffer, especially
near a coin-flip price where `p*(1-p)` peaks.

Both surfaces now reflect this. `PASS`/"ARB FOUND" requires the flat-buffer
math to clear **and** `slippage.py`'s real, per-venue-fee-adjusted profit to
be positive at some tradeable size. A flat-buffer pass that real fees erase
shows as a yellow `FLAT-BUFFER PASS -- REAL FEES ERASE IT` panel in the CLI
and an amber `NOT PROFITABLE AFTER FEES` badge on the website. (`opp.status`
itself still prints `PASS` in the body; that's arb_engine's own honest,
unchanged classification, and only the trust-signaling banner color and
title are adjusted.) `arb_engine.py`'s detection and classification math is
not modified for this. The check lives entirely in the display layer
(`opportunity_view._real_fee_status`, `terminal_reporter.print_opportunity`),
same as every other feature added on top of it. See `slippage.py`'s module
docstring for the fee formula and why Kalshi's rate is flagged as an
estimate while Polymarket's is venue-confirmed.

## Market matching: a verified crosswalk, plus exact-title fallback

Kalshi and Polymarket phrase the same real-world bet differently. Nudging
that with fuzzy text similarity is genuinely risky: two titles can look
nearly identical while differing in resolution date, threshold, or
resolution source, and a false match recommends a trade that isn't
actually the same bet. So **the trusted path never uses fuzzy matching.**
Two things happen instead:

1. **`data/market_pairs.csv` is a real, live lookup table**, not just
   documentation. It maps `(venue, market_id)`, a stable ID and never
   title text, to a shared `canonical_market_name`/`market_type`.
   `load_market_pairs()`/`apply_market_pairs()` in `market_matcher.py` apply
   it before grouping. Every row in that file should represent a market a
   human has actually checked against both venues' real resolution rules
   and dates, not matched on similar-looking titles alone. That's exactly
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
   match. Kalshi and Polymarket essentially never phrase the same question
   identically, so this fallback alone finds close to zero cross-venue
   matches on live data. That's the expected, by-design result (see below).

Fuzzy matching *does* exist elsewhere in the codebase now, but only in
`find_title_candidates()`, the separate function that generates unverified
leads for manual review and the website's SCAN button. It never runs inside
`match_markets()`/`apply_market_pairs()` above. Nothing that function finds
is ever trusted, priced as a real arb, or written to
`data/market_pairs.csv` automatically, so loosening its matching doesn't
weaken the guarantee this section describes. See "Category-scoped
scanning, and finding new candidates to verify" below for exactly how it
works.

Adding a new verified pair means finding a market on both venues, reading
(not skimming) both platforms' actual resolution rules and dates,
confirming they really are the same proposition, then adding one row per
venue to `data/market_pairs.csv` with the venue's `market_id` (the Kalshi
ticker, or the Polymarket market `id`) and a shared
`canonical_market_name`. Two lessons from doing this across categories,
both worth knowing before verifying more:

- **Don't compare Kalshi's `close_time` directly.** It's an outer
  settlement deadline that can include a multi-year buffer for contested
  results (confirmed via a real market's `rules_secondary`: "remains open
  until the rescheduled election or two years from the original date"), so
  two venues covering the identical event can show close dates months or
  years apart. `KalshiImporter` now populates `close_time` from
  `expected_expiration_time` instead, which tracks much closer to
  Polymarket's `endDate` for genuinely-matching markets. Even that isn't
  absolute (Big Brother's dates still disagreed by about 3 months after
  the fix; reading the actual rules confirmed it was the same market
  anyway, with Polymarket's `endDate` just being a rolling estimate).
  Dates are a fast pre-filter, never the final word.
- **A title match is not a resolution-rules match, even when everything
  else lines up.** One Brazil-presidential-election candidate had a
  Polymarket `question` field ("Will Renan Santos win...") that flatly
  contradicted its own `description` field, which described an unrelated
  vote-margin-bracket market. This was a Polymarket-side data
  inconsistency in the same spirit as the Kalshi ROC/Somaliland
  mislabeling, and that candidate was excluded. A climate candidate ("Will
  2026 be the hottest year on record?") was excluded for a subtler reason:
  Kalshi's rule requires 2026 to beat both 2025's specific recorded value
  *and* a stated 1.28°C threshold, while Polymarket ranks 2026 against all
  years by the same underlying NASA GISS data series. The two could be
  equivalent if 1.28°C happens to be 2025's actual value, but that isn't
  confirmable without an external number neither venue's text supplies, so
  it was left out rather than assumed.

### Why a live binary scan may still show few candidates outside a verified series

The always-free default path (`match_markets()`, no `--category`) only
ever compares markets already in `data/market_pairs.csv` by exact key, so
outside the verified crosswalk it finds nothing. This is by design.
Growing what it finds means growing the crosswalk.

The separate `--category` discovery path (`find_title_candidates()`) used
to be exact-title-only too, and confirmed directly that exact matching
alone finds close to zero cross-venue candidates across most categories
(politics, financials, and tech all returned zero even scanning full
catalogs of 13,000+ listed markets). Elections, culture, and sports were
the exception, because "Will [name] win [event]?" and "Will [movie] be
delayed?" are template phrasings both venues happen to converge on. Most
other categories just don't share a phrasing convention at all.
`find_title_candidates()` is now fuzzy (see below), which meaningfully
closes that gap for politics and tech specifically. For example, it
catches "Will the US acquire any part of Greenland before Jan 1, 2027?"
against Polymarket's "Will the US acquire part of Greenland in 2026?",
which exact matching structurally cannot. It doesn't fully close the gap
for sports, financials, or climate: those categories ask many genuinely
*different* real questions about the same athlete, company, or threshold
band (for example "will Kirk Cousins retire" and "will Kirk Cousins start
Week 1" both name the same player but aren't the same bet), and
token-overlap similarity can't always tell that apart from a genuine
reworded match. That's a real limitation of bag-of-words matching without
true semantic understanding. See "Category-scoped scanning" below for how
the fuzzy matching works and where its false-positive tuning came from.

### Category-scoped scanning, and finding new candidates to verify

`--category <name>` scopes a kalshi/polymarket/live scan to one category
instead of the whole catalog. Each side is filtered differently: Kalshi's
own `category` field, filtered client-side (there's no server-side filter
for it, confirmed empirically the same way the missing sort parameter
was), and Polymarket's tag system, filtered server-side via `tag_slug` (a
real filter, unlike Kalshi's). The two venues don't share a taxonomy, so
every entry in `main.py`'s `_CATEGORY_ALIASES` table is a hand-verified
pair of filter values, not a guess. Kalshi's value is one of the literal
strings its `/events` endpoint actually returns (sampled 4,000 open events
and tabulated the distribution), and Polymarket's `tag_slug` was confirmed
to exist and return on-topic results via `GET /tags/slug/<slug>` before
being added. Currently verified:

| `--category` | Kalshi `category` | Polymarket `tag_slug` |
|---|---|---|
| `culture` | Entertainment | pop-culture |
| `politics` | Politics | politics |
| `elections` | Elections | elections |
| `sports` | Sports | sports |
| `financials` | Financials, Companies | finance |
| `economics` | Economics | economy |
| `tech` | Science and Technology | tech |
| `climate` | Climate and Weather | weather |

`financials` is the one alias with two Kalshi categories instead of one.
This turned out to be necessary (confirmed 2026-08-20) after a real missed
match: "When will Anthropic officially announce an IPO?" is filed under
Kalshi's event-level "Companies", not "Financials", even though its own
*series* record says "Financials". Kalshi isn't fully consistent between
the two. "Companies" also genuinely has matchable content that
Polymarket's broader `finance` tag covers on its own side, things like
IPOs and major executive or board changes. This was checked before adding
it: of the roughly 79 events actually tagged "Companies" at the event
level (a much smaller and more accurate number than the roughly 500 you'd
see at the series level), only a couple are Kalshi-exclusive KPI or spend
trackers with no realistic counterpart.

Kalshi's "Mentions" category is still left out. Polymarket has no single
equivalent tag for it (it fragments into dozens of specific tags like
"Trump Speech Mentions" instead of one general one), so there's nothing
to verify a pairing against yet.

Narrowing scope like this also makes it practical to search for *new*
crosswalk candidates instead of only checking the verified crosswalk.
`market_matcher.find_title_candidates()` tokenizes same-category titles
(lowercase, strip common English stopwords, keep numbers) and surfaces
cross-venue pairs whose *significant* tokens overlap enough, blocked via
an inverted token index first (a full cross product is infeasible at real
scale; a single category can list tens of thousands of rows per venue).
This is fuzzy on purpose, unlike the trusted crosswalk path above: nothing
it finds is ever trusted, priced as a real arb, or written to
`data/market_pairs.csv` automatically, so a wider net here costs nothing
in safety, only in how many leads show up for review.

The exact scoring came from four rounds of real correction against live
data, not a one-shot design:

- A first cut (plain overlap-coefficient ratio) produced 129,044 candidates
  on `--category sports` alone. Nearly all were false positives from titles
  sharing one long boilerplate event phrase ("...the 2038 Men's FIFA World
  Cup?" vs. "...host the final of the 2030 FIFA World Cup?", different
  countries, different years). Fixed by treating any token whose document
  frequency in that scan's batch exceeds 50 as a batch-local stopword,
  excluded from both blocking and the similarity score's numerator.
- That fix introduced a second bug: a short title built mostly from common
  words (for example "Will Kirk Cousins announce his retirement before the
  2026-27 NFL season?") reduces to almost nothing once common words are
  stripped, so it scored a trivial, perfect match against *any* other
  market naming the same athlete. "Be the Raiders' Week 1 starting QB?"
  scored 1.0 against it, a completely different real question. Fixed by
  scoring the shared, non-common tokens against each title's *original*
  length, not the post-filter remainder. This correctly drops that pair to
  0.33 while leaving a genuine reworded match (Fed-meeting wording, see the
  code's `_MIN_SIMILARITY` comment) unaffected.
- A third bug survived both of those fixes: a shared two-word proper noun
  (a city, a person's name) could still clear both gates on its own when
  the other title was short. "Will the San Francisco Pro Football team be
  announced as the host for the 2031 Pro Football Championship?" matched
  Polymarket's "Spread: San Francisco Giants (-2.5)" on nothing but "San
  Francisco," a football hosting question and an unrelated baseball
  point-spread bet. Fixed by raising `_MIN_SHARED_TOKENS` from 2 to 3.
  Checked against four live categories before committing to it: removed
  22/22 sports and 19/24 culture false positives this pattern produced,
  while every genuine match in politics (1/1) and elections (16/16)
  survived unchanged. See the code's `_MIN_SHARED_TOKENS` comment for the
  full data, including why 4 was tried and rejected: it also cuts real
  matches, since most genuine "name plus one context word" pairs only
  clear 3.
- A fourth pattern showed up specifically in the "company IPO" family:
  "When will Databricks officially announce an IPO?" and "Will Databricks
  IPO by December 31, 2026?" share only 2 tokens ("databricks", "ipo"),
  below the floor the third fix set. Tested six real pairs in this family
  and all six failed the same way. Added a narrow, curated exception
  (`_STRONG_MATCH_TOKENS`, starting with just "ipo") for when 2 shared
  tokens include a specific, unambiguous event word. This isn't risk-free
  on its own: "Which bank will lead Anthropic's IPO?" (a question about the
  underwriter) shares the identical shape with the genuine timing
  question above. An interrogative-lead guard
  (`_starts_with_interrogative`) closes that gap, confirmed to block the
  bank-underwriter case while leaving the six genuine matches untouched.

A title match here is still a **lead, not a verified pair**. Both the
website and the CLI (`main.py`'s `score_candidates()`) run every candidate
through the same real, unmodified arb_engine math and the same
`--min-edge`/`--fee-buffer` filters as a crosswalk market, then split the
results. Candidates that price into a real opportunity get shown with full
pricing (each panel's own "Match confidence: 0.00" is the one thing that
still marks it as unverified). Candidates that don't, whether that's no
live price on one side, a block from the close-date guard, or a
genuinely unprofitable price, fall back to the bare title table for
manual review. The website shows it with the same `PASS` /
`FEE_ADJUSTED_NO_EDGE` / `NO_EDGE` vocabulary as a crosswalk market rather
than a separate "unverified" badge. This is a deliberate product decision,
with a sitewide disclaimer instead of a per-card caveat, but it still
needs the same manual check (read both venues' actual resolution rules
and dates) before it's safe to promote into `data/market_pairs.csv`.

### Two speeds, on purpose

Fetching a venue's full catalog is expensive in two different ways.
Kalshi has no server-side relevance filter, so reaching any specific
market means paging through thousands of events (about 15 seconds for
6,000). Polymarket's event listing is cheap, but pricing a market means a
*separate* CLOB order-book call per market, and most of a catalog will
never match anything on the other venue anyway. Paying either cost on
every routine run doesn't scale, so there are two distinct modes instead
of one:

- **No `--category` (default).** The fast path. Only markets already in
  `data/market_pairs.csv` get fetched at all, by exact ticker/id
  (`KalshiImporter.get_normalized_prices_for_tickers()` /
  `PolymarketImporter.get_normalized_prices_for_ids()`), a handful of
  direct requests instead of paginating a whole catalog. A `--source live`
  run against the current crosswalk takes about a second. This is what
  routine "is there a real arb right now" runs should use, and it's also
  why growing `data/market_pairs.csv` is what actually grows what a plain
  `--source live` run can find.
- **`--category <name>`.** The discovery path, for finding new candidates.
  Kalshi is still fetched eagerly within the category (its pricing is free,
  bundled into the same response as the listing), but Polymarket is
  fetched as metadata only first (`get_market_metadata()`, no CLOB calls).
  Title-candidates are found from that metadata, and only the
  crosswalk-covered and newly-candidate-matched markets ever get priced via
  `get_normalized_prices_for_ids()`. The **Scan Summary** shows both the
  total listed and the number actually priced, so it's clear that "priced"
  doesn't mean everything else was ignored. It means everything else had
  nothing to compare it to. By default this is capped (500 Kalshi events,
  250 Polymarket events per category, higher for a couple of categories
  that need it; see `main.py`'s `_CATEGORY_SCAN_KALSHI_MAX_EVENTS*`
  constants) so a scan stays safely inside the live website's 512MB host.
  Add `--full-depth` to bypass that entirely and page each venue to its
  real end instead of a fixed limit. Confirmed directly (2026-08-20),
  Kalshi's whole catalog is about 11,000 events and Sports alone has about
  54,000 active markets, so this is real memory and real time (minutes,
  not seconds) on your own machine, never something the live deployment
  does.

## Why importers are separate from the arb engine

`arb_engine.py` never imports `requests`, never knows a venue's name, and
never makes a network call. It only takes `NormalizedPrice` /
`MarketGroup` objects and returns `ArbOpportunity` objects. That means:

- The math can be tested against mock data with confidence it will behave
  identically against live data.
- Adding a new venue (PredictIt, ForecastEx, anything else) means writing
  one new `VenueImporter` subclass. Nothing about matching, math, ranking,
  or display has to change.
- A bug in a venue's API integration can never corrupt the arbitrage math,
  and a bug in the math can never accidentally reach out to the network.

## Why we use executable ask prices only

Chart probabilities, last-traded prices, midpoints, and UI-rounded
percentages aren't prices you can actually transact at. An "arbitrage"
computed from any of those is fiction. The only prices that matter are the
best executable ask on each side, which is what you'd actually pay to buy
in right now. Every importer in this project is required to return real
ask (and bid) prices, not display values.

## Live data: Kalshi and Polymarket, no API key

This version pulls real markets from Kalshi and Polymarket over their
public, unauthenticated REST endpoints, verified directly against both
APIs before writing the importers:

- **Kalshi** (`external-api.kalshi.com/trade-api/v2/events`): market
  listings and top-of-book yes/no bid/ask are public. Kalshi's separate
  per-ticker orderbook endpoint (full depth) does require a signed API key,
  so this version deliberately doesn't use it. Top-of-book is all the arb
  math needs. There's no working sort/relevance parameter on this endpoint
  (confirmed empirically: `sort=volume` returns identical results to no
  sort), and the catalog is dominated by a long tail of thin sports/combo
  markets. For example, the Fed decision event used in the crosswalk sits
  about 5,200 events deep in the default ordering. `KalshiImporter`
  defaults to pulling 6,000 events (about 15 seconds) specifically so
  events like that are actually reachable. A lower default would silently
  miss exactly the markets most likely to matter.
- **Polymarket**: the Gamma API (`gamma-api.polymarket.com`) is used for
  market/event discovery and is fully public, and it does support sorting,
  so `PolymarketImporter` requests events ordered by 24h volume descending,
  to prioritize markets actually worth scanning. The CLOB API
  (`clob.polymarket.com`) is used for the actual executable order book
  (`/books`) and is public for reads. Only order placement requires
  signing.

No account, key, or secret is required to run `--source kalshi`,
`--source polymarket`, or `--source live`. That's intentional, and not
just a v1 shortcut: the public-facing website runs on the exact same
keyless read path, so there was never anything sensitive that needed to
be stripped out before making it public.

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
python src/main.py --source live --category sports --full-depth --min-edge -1
```

The first four are near-instant (well under a second for mock/csv/live,
live included; see "Two speeds, on purpose" above for why plain
`--source live` is fast). Only `--category` runs take real time, since
they have to list an entire category before pricing anything.
`--category elections` takes roughly 15-20 seconds, mostly Kalshi's
pagination cost, which no category filter avoids.

### Running the full, uncapped version

The public website (whatever's linked as the live demo) intentionally
scans at reduced depth, sized to stay inside a 512MB host rather than this
project's real limit. Locally, on your own machine, that ceiling doesn't
apply. Add `--full-depth` to any single-category live scan to page that
category to each venue's actual end instead of a fixed cap:

```bash
python src/main.py --source live --category sports --full-depth --min-edge -1
```

This is real time and real memory, and it's worth knowing that up front.
Confirmed directly (2026-08-20), Kalshi's whole catalog is about 11,000
events and Sports alone has about 54,000 active markets, so a full-depth
run on a big category takes minutes, not seconds, and holds
correspondingly more in memory while it runs. The website's own code
never triggers this: `load_prices_for_category()`'s `full_depth`
parameter defaults to `False`, and the site's call site never overrides
it, so this only ever runs when you choose to run it yourself.

`--min-edge -1` is worth using on any live run: it shows every candidate
trade actually compared, not only the ones clearing a threshold, which is
the fastest way to confirm the pipeline is reading and comparing real
data (see "Why a live binary scan may show zero candidates" above for why
the count may still be small on the fast path specifically). The **Scan
Summary** table prints regardless. It shows exactly how many prices were
read from each venue and how many market groups they formed, which is the
first thing to check: it proves the read pipeline worked even when the
arb filters find nothing to show.

## What the CLI arguments mean

| Flag | Choices / type | Default | Meaning |
|---|---|---|---|
| `--source` | mock, csv, kalshi, polymarket, live | mock | Where prices come from. `live` = Kalshi + Polymarket combined. |
| `--category` | culture, politics, elections, sports, financials, economics, tech, climate, all | none | Discovery mode: scope a scan to one category (or every verified category at once) and search for new candidates. Slower than the default fast path. See "Category-scoped scanning" below. |
| `--full-depth` | flag | off | Only valid with a single `--category` (not `all`). Bypasses the memory-safe scan caps entirely and pages each venue to its real end. Real time and memory on your own machine, never used by the live website. |
| `--min-edge` | float | 0.005 | Minimum net edge (after fee buffer) required to report a trade. |
| `--fee-buffer` | float | 0.003 | Flat cost/slippage buffer subtracted from gross edge before filtering. |
| `--top` | int | 20 | Max opportunities printed (the engine still scans everything internally). |
| `--bankroll` | float | 1000 | Bankroll used for the "estimated profit" display on each opportunity. |
