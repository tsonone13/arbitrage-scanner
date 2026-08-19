"""Abstract interface every venue adapter must implement.

The rest of the pipeline (normalizer, matcher, arb engine) never talks to a
venue directly -- it only ever consumes NormalizedPrice objects. Adding a new
prediction-market site means writing one new VenueImporter subclass; nothing
downstream has to change.
"""

from abc import ABC, abstractmethod

from models import NormalizedPrice


class VenueImporter(ABC):
    """Base class for a single prediction-market venue adapter."""

    @property
    @abstractmethod
    def venue_name(self) -> str:
        """Short human-readable venue name, e.g. 'Kalshi'."""
        raise NotImplementedError

    @abstractmethod
    def list_markets(self) -> list[dict]:
        """Return raw (venue-specific, not normalized) market metadata."""
        raise NotImplementedError

    @abstractmethod
    def get_orderbook(self, market_id: str) -> dict:
        """Return raw (venue-specific, not normalized) orderbook data for one market."""
        raise NotImplementedError

    @abstractmethod
    def get_normalized_prices(self) -> list[NormalizedPrice]:
        """Return this venue's markets as fully normalized NormalizedPrice objects.

        This is the only method the rest of the pipeline calls. Every
        concrete importer, mock or live, must return well-formed
        NormalizedPrice instances here -- decimal prices in [0, 1], never
        raw cents or chart probabilities.
        """
        raise NotImplementedError
