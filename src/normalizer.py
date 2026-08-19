"""Helpers for converting raw venue data into the normalized 0-1 price convention."""


def cents_to_decimal(cents: float) -> float:
    """Convert a price in cents (e.g. 96.4) to a decimal probability (0.964)."""
    return cents / 100.0


def decimal_to_cents(price: float) -> float:
    """Convert a decimal probability (0.964) to cents (96.4)."""
    return price * 100.0


def validate_price(price: float | None) -> bool:
    """True if price is None or a valid probability in [0, 1]."""
    if price is None:
        return True
    return 0.0 <= price <= 1.0


def safe_float(value: object) -> float | None:
    """Best-effort conversion to float; returns None instead of raising."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
