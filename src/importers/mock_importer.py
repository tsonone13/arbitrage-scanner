"""Fake multi-venue data for exercising the pipeline without any network calls.

market_type is "binary" for a single yes/no proposition (only the per-outcome
binary cross-venue check is valid on it) or "multi_outcome" for a complete,
mutually-exclusive, exhaustive outcome set (full-book YES/NO checks are only
valid there -- see the guard in arb_engine.py). A single outcome carved out of
a bigger event, like "World Cup Winner / USA", is still "binary": the other
~30 countries aren't in this dataset, so treating it as a complete partition
would produce a nonsense full-book "arb".

Five required scenarios, in order: (1) binary arb, (2) binary no-arb,
(3) multi-outcome full-book YES arb, (4) multi-outcome no-arb,
(5) tiny edge that min-edge filtering should drop.
"""

from models import NormalizedPrice
from importers.base import VenueImporter

_TS = "2026-08-17T12:00:00Z"

_MOCK_ROWS: list[dict] = [
    # --- Scenario 1: binary market with a real cross-venue arb (spec's worked example) ---
    dict(
        venue="Polymarket", market_id="poly-wcup-usa",
        canonical_market_name="2026 World Cup Winner", raw_market_name="Will the USA win the 2026 FIFA World Cup?",
        outcome_name="USA", market_type="binary",
        yes_bid=0.021, yes_ask=0.023, no_bid=0.975, no_ask=0.977,
        depth=500.0, volume=125000.0, close_time="2026-07-19T23:59:00Z",
        resolution_notes="Resolves YES if USA wins the 2026 FIFA World Cup Final.", timestamp=_TS,
    ),
    dict(
        venue="Kalshi", market_id="KXWCUP-26-USA",
        canonical_market_name="2026 World Cup Winner", raw_market_name="Will the United States win the 2026 World Cup?",
        outcome_name="USA", market_type="binary",
        yes_bid=0.034, yes_ask=0.036, no_bid=0.962, no_ask=0.964,
        depth=750.0, volume=98000.0, close_time="2026-07-19T23:59:00Z",
        resolution_notes="Resolves YES if USA wins the 2026 World Cup.", timestamp=_TS,
    ),

    # --- Scenario 2: binary market with no arbitrage ---
    dict(
        venue="Kalshi", market_id="KXFED-26SEP-CUT",
        canonical_market_name="Fed Cuts Rates at September 2026 Meeting",
        raw_market_name="Will the Fed cut rates at the September 2026 FOMC meeting?",
        outcome_name="Yes", market_type="binary",
        yes_bid=0.60, yes_ask=0.62, no_bid=0.38, no_ask=0.40,
        depth=2000.0, volume=340000.0, close_time="2026-09-17T18:00:00Z",
        resolution_notes="Resolves per official FOMC statement.", timestamp=_TS,
    ),
    dict(
        venue="Polymarket", market_id="poly-fed-sep-cut",
        canonical_market_name="Fed Cuts Rates at September 2026 Meeting",
        raw_market_name="Fed rate cut in September?",
        outcome_name="Yes", market_type="binary",
        yes_bid=0.59, yes_ask=0.61, no_bid=0.39, no_ask=0.41,
        depth=1500.0, volume=410000.0, close_time="2026-09-17T18:00:00Z",
        resolution_notes="Resolves per official FOMC statement.", timestamp=_TS,
    ),

    # --- Scenario 3: multi-outcome market with a full-book YES arb ---
    dict(
        venue="Kalshi", market_id="KXOSCAR-26-A",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Will Movie A win Best Picture?",
        outcome_name="Movie A", market_type="multi_outcome",
        yes_bid=0.20, yes_ask=0.22, no_bid=0.78, no_ask=0.80,
        depth=300.0, volume=50000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="Polymarket", market_id="poly-oscar-a",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Best Picture: Movie A?",
        outcome_name="Movie A", market_type="multi_outcome",
        yes_bid=0.22, yes_ask=0.24, no_bid=0.77, no_ask=0.79,
        depth=400.0, volume=60000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="Polymarket", market_id="poly-oscar-b",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Best Picture: Movie B?",
        outcome_name="Movie B", market_type="multi_outcome",
        yes_bid=0.16, yes_ask=0.18, no_bid=0.82, no_ask=0.84,
        depth=350.0, volume=45000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="Kalshi", market_id="KXOSCAR-26-B",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Will Movie B win Best Picture?",
        outcome_name="Movie B", market_type="multi_outcome",
        yes_bid=0.18, yes_ask=0.20, no_bid=0.81, no_ask=0.83,
        depth=300.0, volume=38000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="PredictIt", market_id="PI-OSCAR-C",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Movie C to win Best Picture",
        outcome_name="Movie C", market_type="multi_outcome",
        yes_bid=0.13, yes_ask=0.15, no_bid=0.85, no_ask=0.87,
        depth=200.0, volume=21000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="ForecastEx", market_id="FX-OSCAR-C",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Movie C Best Picture contract",
        outcome_name="Movie C", market_type="multi_outcome",
        yes_bid=0.15, yes_ask=0.17, no_bid=0.83, no_ask=0.86,
        depth=150.0, volume=17000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="ForecastEx", market_id="FX-OSCAR-D",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Movie D Best Picture contract",
        outcome_name="Movie D", market_type="multi_outcome",
        yes_bid=0.38, yes_ask=0.40, no_bid=0.60, no_ask=0.62,
        depth=250.0, volume=26000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),
    dict(
        venue="PredictIt", market_id="PI-OSCAR-D",
        canonical_market_name="2026 Best Picture Winner", raw_market_name="Movie D to win Best Picture",
        outcome_name="Movie D", market_type="multi_outcome",
        yes_bid=0.40, yes_ask=0.42, no_bid=0.59, no_ask=0.61,
        depth=180.0, volume=19000.0, close_time="2027-03-14T02:00:00Z",
        resolution_notes="Resolves per Academy Awards ceremony result.", timestamp=_TS,
    ),

    # --- Scenario 4: multi-outcome market with no arbitrage (fairly priced) ---
    dict(
        venue="Kalshi", market_id="KXNBA-27-TEAMW",
        canonical_market_name="2027 NBA Champion", raw_market_name="Will Team W win the 2027 NBA Championship?",
        outcome_name="Team W", market_type="multi_outcome",
        yes_bid=0.33, yes_ask=0.35, no_bid=0.75, no_ask=0.77,
        depth=300.0, volume=40000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="Polymarket", market_id="poly-nba-teamw",
        canonical_market_name="2027 NBA Champion", raw_market_name="Team W - 2027 NBA Champion?",
        outcome_name="Team W", market_type="multi_outcome",
        yes_bid=0.35, yes_ask=0.37, no_bid=0.78, no_ask=0.80,
        depth=350.0, volume=55000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="Polymarket", market_id="poly-nba-teamx",
        canonical_market_name="2027 NBA Champion", raw_market_name="Team X - 2027 NBA Champion?",
        outcome_name="Team X", market_type="multi_outcome",
        yes_bid=0.28, yes_ask=0.30, no_bid=0.70, no_ask=0.72,
        depth=320.0, volume=47000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="Kalshi", market_id="KXNBA-27-TEAMX",
        canonical_market_name="2027 NBA Champion", raw_market_name="Will Team X win the 2027 NBA Championship?",
        outcome_name="Team X", market_type="multi_outcome",
        yes_bid=0.30, yes_ask=0.32, no_bid=0.73, no_ask=0.75,
        depth=280.0, volume=41000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="PredictIt", market_id="PI-NBA-TEAMY",
        canonical_market_name="2027 NBA Champion", raw_market_name="Team Y to win 2027 NBA title",
        outcome_name="Team Y", market_type="multi_outcome",
        yes_bid=0.23, yes_ask=0.25, no_bid=0.76, no_ask=0.78,
        depth=200.0, volume=23000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="ForecastEx", market_id="FX-NBA-TEAMY",
        canonical_market_name="2027 NBA Champion", raw_market_name="Team Y NBA champion contract",
        outcome_name="Team Y", market_type="multi_outcome",
        yes_bid=0.25, yes_ask=0.27, no_bid=0.79, no_ask=0.81,
        depth=180.0, volume=19000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="ForecastEx", market_id="FX-NBA-TEAMZ",
        canonical_market_name="2027 NBA Champion", raw_market_name="Team Z NBA champion contract",
        outcome_name="Team Z", market_type="multi_outcome",
        yes_bid=0.18, yes_ask=0.20, no_bid=0.81, no_ask=0.83,
        depth=210.0, volume=25000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),
    dict(
        venue="PredictIt", market_id="PI-NBA-TEAMZ",
        canonical_market_name="2027 NBA Champion", raw_market_name="Team Z to win 2027 NBA title",
        outcome_name="Team Z", market_type="multi_outcome",
        yes_bid=0.20, yes_ask=0.22, no_bid=0.84, no_ask=0.86,
        depth=190.0, volume=21000.0, close_time="2027-06-20T02:00:00Z",
        resolution_notes="Resolves per NBA Finals result.", timestamp=_TS,
    ),

    # --- Scenario 5: tiny edge that min-edge filtering should drop ---
    dict(
        venue="Kalshi", market_id="KXNYCRAIN-26AUG25",
        canonical_market_name="Will It Rain in NYC on Aug 25 2026",
        raw_market_name="Will measurable rain fall in NYC on Aug 25, 2026?",
        outcome_name="Yes", market_type="binary",
        yes_bid=0.498, yes_ask=0.500, no_bid=0.501, no_ask=0.505,
        depth=1000.0, volume=20000.0, close_time="2026-08-25T23:59:00Z",
        resolution_notes="Resolves per NWS official precipitation record.", timestamp=_TS,
    ),
    dict(
        venue="Polymarket", market_id="poly-nyc-rain-0825",
        canonical_market_name="Will It Rain in NYC on Aug 25 2026",
        raw_market_name="NYC rain on August 25?",
        outcome_name="Yes", market_type="binary",
        yes_bid=0.505, yes_ask=0.510, no_bid=0.494, no_ask=0.496,
        depth=900.0, volume=18000.0, close_time="2026-08-25T23:59:00Z",
        resolution_notes="Resolves per NWS official precipitation record.", timestamp=_TS,
    ),
]


class MockImporter(VenueImporter):
    """Serves the shared fake dataset, filtered down to one venue.

    Mirrors how a live importer works (one instance per venue) so that
    swapping this out for KalshiImporter/PolymarketImporter later is a
    drop-in change -- nothing in main.py or downstream needs to know the
    difference.
    """

    def __init__(self, venue: str):
        self._venue = venue

    @property
    def venue_name(self) -> str:
        return self._venue

    def list_markets(self) -> list[dict]:
        return [row for row in _MOCK_ROWS if row["venue"] == self._venue]

    def get_orderbook(self, market_id: str) -> dict:
        for row in _MOCK_ROWS:
            if row["venue"] == self._venue and row["market_id"] == market_id:
                return row
        raise KeyError(f"No mock market_id {market_id!r} for venue {self._venue!r}")

    def get_normalized_prices(self) -> list[NormalizedPrice]:
        return [NormalizedPrice(**row) for row in self.list_markets()]


def load_all_mock_prices() -> list[NormalizedPrice]:
    """Convenience entry point: every mock venue's prices, concatenated."""
    venues = sorted({row["venue"] for row in _MOCK_ROWS})
    prices: list[NormalizedPrice] = []
    for venue in venues:
        prices.extend(MockImporter(venue).get_normalized_prices())
    return prices
