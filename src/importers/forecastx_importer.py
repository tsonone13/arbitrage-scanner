"""Placeholder importer for ForecastEx.

TODO: wire up ForecastEx's public market-data API once we've confirmed its
read-endpoint auth requirements the same way we verified Kalshi and
Polymarket. Not implemented yet -- this scanner does not place trades, so
there is nothing here that would ever need a key regardless.
"""

from models import NormalizedPrice
from importers.base import VenueImporter


class ForecastExImporter(VenueImporter):
    """Not yet implemented. See TODO above."""

    @property
    def venue_name(self) -> str:
        return "ForecastEx"

    def list_markets(self) -> list[dict]:
        raise NotImplementedError("ForecastEx live importer not implemented yet.")

    def get_orderbook(self, market_id: str) -> dict:
        raise NotImplementedError("ForecastEx live importer not implemented yet.")

    def get_normalized_prices(self) -> list[NormalizedPrice]:
        raise NotImplementedError("ForecastEx live importer not implemented yet.")
