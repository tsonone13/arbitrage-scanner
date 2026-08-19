"""Placeholder importer for PredictIt.

TODO: wire up PredictIt's public read-only markets API (no auth has
historically been required for market data). Not implemented yet -- this
scanner does not place trades, so there is nothing here that would ever
need a key regardless.
"""

from models import NormalizedPrice
from importers.base import VenueImporter


class PredictItImporter(VenueImporter):
    """Not yet implemented. See TODO above."""

    @property
    def venue_name(self) -> str:
        return "PredictIt"

    def list_markets(self) -> list[dict]:
        raise NotImplementedError("PredictIt live importer not implemented yet.")

    def get_orderbook(self, market_id: str) -> dict:
        raise NotImplementedError("PredictIt live importer not implemented yet.")

    def get_normalized_prices(self) -> list[NormalizedPrice]:
        raise NotImplementedError("PredictIt live importer not implemented yet.")
