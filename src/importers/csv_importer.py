"""Loads normalized prices from a CSV file.

Unlike the other importers, one CSV file can hold rows from several venues
at once (each row names its own venue) -- a deliberate, pragmatic exception
to "one importer instance per venue", useful for hand-built or exported
fixture data.
"""

import pandas as pd

from models import NormalizedPrice
from importers.base import VenueImporter
from normalizer import safe_float, validate_price

_REQUIRED_COLUMNS = [
    "venue", "market_id", "canonical_market_name", "raw_market_name",
    "outcome_name", "market_type",
]
_PRICE_COLUMNS = ["yes_bid", "yes_ask", "no_bid", "no_ask"]
_OPTIONAL_NUMERIC_COLUMNS = ["depth", "volume"]
_OPTIONAL_TEXT_COLUMNS = ["close_time", "resolution_notes", "timestamp"]


def _clean_text(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


class CsvImporter(VenueImporter):
    """Reads NormalizedPrice rows from a CSV file (multiple venues per file)."""

    def __init__(self, path: str):
        self._path = path

    @property
    def venue_name(self) -> str:
        return "csv"

    def list_markets(self) -> list[dict]:
        df = pd.read_csv(self._path)
        return df.to_dict("records")

    def get_orderbook(self, market_id: str) -> dict:
        df = pd.read_csv(self._path)
        matches = df[df["market_id"] == market_id]
        if matches.empty:
            raise KeyError(f"No market_id {market_id!r} found in {self._path}")
        return matches.iloc[0].to_dict()

    def get_normalized_prices(self) -> list[NormalizedPrice]:
        try:
            df = pd.read_csv(self._path)
        except (FileNotFoundError, pd.errors.EmptyDataError) as exc:
            raise RuntimeError(f"Could not read CSV prices from {self._path}: {exc}") from exc

        missing_columns = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing_columns:
            raise ValueError(f"{self._path} is missing required columns: {missing_columns}")

        prices: list[NormalizedPrice] = []
        for row_num, row in enumerate(df.to_dict("records"), start=2):  # +2: header + 1-indexing
            venue = _clean_text(row.get("venue"))
            market_id = _clean_text(row.get("market_id"))
            canonical_market_name = _clean_text(row.get("canonical_market_name"))
            outcome_name = _clean_text(row.get("outcome_name"))
            market_type = _clean_text(row.get("market_type"))
            if not all([venue, market_id, canonical_market_name, outcome_name, market_type]):
                print(f"[csv_importer] skipping row {row_num}: missing a required identity field")
                continue

            prices_by_field = {col: safe_float(row.get(col)) for col in _PRICE_COLUMNS}
            invalid_fields = [f for f, v in prices_by_field.items() if not validate_price(v)]
            if invalid_fields:
                print(f"[csv_importer] skipping row {row_num}: invalid price(s) in {invalid_fields}")
                continue

            prices.append(NormalizedPrice(
                venue=venue,
                market_id=market_id,
                canonical_market_name=canonical_market_name,
                raw_market_name=_clean_text(row.get("raw_market_name")) or canonical_market_name,
                outcome_name=outcome_name,
                market_type=market_type,
                yes_bid=prices_by_field["yes_bid"],
                yes_ask=prices_by_field["yes_ask"],
                no_bid=prices_by_field["no_bid"],
                no_ask=prices_by_field["no_ask"],
                depth=safe_float(row.get("depth")),
                volume=safe_float(row.get("volume")),
                close_time=_clean_text(row.get("close_time")),
                resolution_notes=_clean_text(row.get("resolution_notes")),
                timestamp=_clean_text(row.get("timestamp")),
            ))
        return prices
