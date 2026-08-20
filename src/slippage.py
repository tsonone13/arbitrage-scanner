"""Optimal trade size under slippage, plus a real trading-fee breakdown,
layered on top of already-detected opportunities. This never changes
whether something is detected as an arb -- arb_engine.py's detection,
filtering, and PASS/NO EDGE classification are untouched. All this
does is take an opportunity that already exists and estimate (a) how large
a real fill could be before per-unit slippage erodes the edge below
breakeven, and (b) what that fill would actually cost in venue trading
fees.

Deliberately asymmetric in what it can model, because the two venues
expose different amounts of detail through their public, keyless APIs:
- Polymarket's CLOB API returns the full multi-level ask book, so its leg
  of a trade is walked level-by-level for genuine slippage, and its market
  data carries the market's own exact taker fee rate (feeSchedule.rate --
  confirmed against Polymarket's own docs, which say to read fee
  parameters from market details rather than a static table, and
  cross-checked directly: a Fed-decision market's rate of 0.05 matches the
  documented "economics" category rate exactly).
- Kalshi's public API only exposes top-of-book size (yes_ask_size_fp, and
  yes_bid_size_fp doubling as NO-ask liquidity -- a NO ask is mechanically
  the same resting order as a YES bid). Its exact per-market fee rate lives
  behind an authenticated endpoint (GET /margin/fee_tiers) this project
  doesn't call, so KalshiImporter fills in the publicly documented default
  taker coefficient (0.07) instead of a verified per-market figure. Both
  of these are real data limitations, not approximations being hidden --
  see "limiting_factor" and "fee_rate_is_estimate" in the result.
"""

import math

from models import ArbOpportunity, NormalizedPrice


def _leg_levels(price: NormalizedPrice, side: str) -> list[tuple[float, float]]:
    """[(price, size), ...] ask levels for the side actually being bought,
    cheapest first. Falls back to a flat level at top-of-book size, then to
    the generic `depth` field (what mock/CSV data and Kalshi both carry),
    when no multi-level book is available.
    """
    if side == "YES":
        book, flat_price, flat_size = price.yes_ask_book, price.yes_ask, price.yes_ask_size
    else:
        book, flat_price, flat_size = price.no_ask_book, price.no_ask, price.no_ask_size

    if book:
        return list(book)
    size = flat_size if flat_size is not None else price.depth
    if flat_price is not None and size is not None and size > 0:
        return [(flat_price, size)]
    return []


def _taker_fee(venue: str, price: float, quantity: float, rate: float) -> float:
    """fee = rate * quantity * price * (1 - price), with each venue's own
    rounding convention:
    - Kalshi: rounds up to the nearest cent (documented: "a transaction fee
      on the expected earnings on the contract").
    - Polymarket: rounds to 5 decimal places, min chargeable fee $0.00001
      (docs.polymarket.com/trading/fees).
    Applied once per book level walked, not on the blended average price --
    the formula is nonlinear in price, so summing per-level fees is more
    accurate than computing one fee off an average.
    """
    raw = rate * quantity * price * (1.0 - price)
    if venue == "Kalshi":
        # A tiny epsilon before ceil() matters: 0.07*100*0.6*0.4 is exactly
        # $1.68 in real math but lands as 1.6800000000000004 in float64,
        # which ceil() would otherwise round up to an unearned extra cent.
        return math.ceil(raw * 100 - 1e-9) / 100
    if venue == "Polymarket":
        fee = round(raw, 5)
        return fee if fee >= 0.00001 else 0.0
    return raw


def max_profitable_units(
    yes_price: NormalizedPrice,
    no_price: NormalizedPrice,
    guaranteed_payout: float,
    fee_buffer: float,
) -> dict:
    """Walk both legs' ask books together (equal quantity on each side, since
    a unit needs one YES and one NO share) and find the largest quantity
    where the *marginal* unit -- price plus its real venue fee -- is still
    profitable. fee_buffer remains a small additional safety margin on top
    of the real fees (execution risk, timing between legs, anything not
    captured by a known trading fee), not a stand-in for the fees themselves.

    Returns optimal_units, total_cost (price only), fee breakdown per leg,
    avg_cost_per_unit (price + fees), estimated_profit (net of real fees and
    fee_buffer), limiting_factor, and whether either leg's fee rate is an
    estimate rather than a venue-confirmed figure.
    """
    yes_levels = sorted(_leg_levels(yes_price, "YES"))
    no_levels = sorted(_leg_levels(no_price, "NO"))
    yes_rate = yes_price.taker_fee_rate
    no_rate = no_price.taker_fee_rate

    empty = {
        "optimal_units": 0.0,
        "total_cost": 0.0,
        "avg_cost_per_unit": None,
        "yes_fees": 0.0,
        "no_fees": 0.0,
        "total_fees": 0.0,
        "yes_fee_rate": yes_rate,
        "no_fee_rate": no_rate,
        "estimated_profit": 0.0,
        "limiting_factor": "no liquidity/size data available for one or both legs",
        "fee_rate_is_estimate": yes_price.venue == "Kalshi" or no_price.venue == "Kalshi",
    }
    if not yes_levels or not no_levels or yes_rate is None or no_rate is None:
        if yes_levels and no_levels:
            empty["limiting_factor"] = "no fee rate available for one or both legs"
        return empty

    yes_idx = no_idx = 0
    yes_remaining = yes_levels[0][1]
    no_remaining = no_levels[0][1]
    total_units = 0.0
    total_cost = 0.0
    yes_fees = 0.0
    no_fees = 0.0
    # None is a real sentinel here, not just an initial value: the `else`
    # clause below only sets this on natural loop completion (both books
    # walked to the end), and the `break` path is handled separately after
    # the loop -- see that block for why the break path itself still needs
    # to distinguish two different reasons before picking a message.
    limiting_factor = None

    while yes_idx < len(yes_levels) and no_idx < len(no_levels):
        yes_lvl_price = yes_levels[yes_idx][0]
        no_lvl_price = no_levels[no_idx][0]
        step = min(yes_remaining, no_remaining)

        # Accept/reject this level using the *unrounded* marginal fee rate
        # (rate * price * (1-price)), not the rounded fee for whatever size
        # `step` happens to be. Price -- and so the true per-unit fee -- is
        # constant within a level, so this is the real per-unit economics of
        # taking this level at all; using the rounded step fee instead would
        # let an odd-sized first step's rounding overhead (up to a cent,
        # amortized over however many shares happen to be available right
        # at the top of book) wrongly veto a level that's genuinely
        # profitable per unit. The reported fee total below still uses each
        # venue's real rounding -- that's a genuine, if tiny, cost -- just
        # not what decides whether a level counts.
        yes_marginal_fee_rate = yes_rate * yes_lvl_price * (1.0 - yes_lvl_price)
        no_marginal_fee_rate = no_rate * no_lvl_price * (1.0 - no_lvl_price)
        marginal_unit_cost = yes_lvl_price + no_lvl_price + yes_marginal_fee_rate + no_marginal_fee_rate
        if marginal_unit_cost + fee_buffer >= guaranteed_payout:
            break

        step_yes_fee = _taker_fee(yes_price.venue, yes_lvl_price, step, yes_rate)
        step_no_fee = _taker_fee(no_price.venue, no_lvl_price, step, no_rate)

        total_units += step
        total_cost += step * (yes_lvl_price + no_lvl_price)
        yes_fees += step_yes_fee
        no_fees += step_no_fee
        yes_remaining -= step
        no_remaining -= step

        if yes_remaining <= 0:
            yes_idx += 1
            if yes_idx < len(yes_levels):
                yes_remaining = yes_levels[yes_idx][1]
        if no_remaining <= 0:
            no_idx += 1
            if no_idx < len(no_levels):
                no_remaining = no_levels[no_idx][1]
    else:
        ran_out_yes = yes_idx >= len(yes_levels)
        ran_out_no = no_idx >= len(no_levels)
        if ran_out_yes and ran_out_no:
            limiting_factor = "both legs' visible liquidity exhausted"
        elif ran_out_yes:
            limiting_factor = f"{yes_price.venue} YES-side visible liquidity exhausted"
        else:
            limiting_factor = f"{no_price.venue} NO-side visible liquidity exhausted"

    if limiting_factor is None:
        # Loop ended via `break` (an unprofitable level), not natural
        # exhaustion of both books. Two genuinely different situations
        # were both getting reported as "slippage crossed breakeven" here
        # before, which is actively misleading for the first one: if
        # total_units is still 0, NO level was ever walked -- the very
        # first, cheapest available price already doesn't clear breakeven,
        # so there's no liquidity or slippage story at all, just an
        # unprofitable route. Only once at least one level has actually
        # been walked profitably does a later rejection mean slippage (a
        # deeper, worse price) is what closed the window.
        if total_units == 0.0:
            limiting_factor = "not profitable at any size -- best available price on both legs is already at or past breakeven"
        else:
            limiting_factor = "slippage crossed breakeven before either leg's visible liquidity ran out"

    total_fees = yes_fees + no_fees
    profit = total_units * guaranteed_payout - total_cost - total_fees - total_units * fee_buffer
    return {
        "optimal_units": total_units,
        "total_cost": total_cost,
        "avg_cost_per_unit": ((total_cost + total_fees) / total_units) if total_units else None,
        "yes_fees": yes_fees,
        "no_fees": no_fees,
        "total_fees": total_fees,
        "yes_fee_rate": yes_rate,
        "no_fee_rate": no_rate,
        "estimated_profit": profit,
        "limiting_factor": limiting_factor,
        "fee_rate_is_estimate": yes_price.venue == "Kalshi" or no_price.venue == "Kalshi",
    }


def opportunity_sizing(
    opportunity: ArbOpportunity, prices_by_key: dict[tuple[str, str], NormalizedPrice]
) -> dict | None:
    """Convenience wrapper: find the opportunity's YES/NO legs' source rows
    by (venue, market_id) and run max_profitable_units(). Returns None if a
    leg's source row can't be found (shouldn't happen in practice) or this
    isn't a 2-leg binary opportunity.
    """
    if len(opportunity.legs) != 2:
        return None
    yes_leg = next((leg for leg in opportunity.legs if leg["side"] == "YES"), None)
    no_leg = next((leg for leg in opportunity.legs if leg["side"] == "NO"), None)
    if yes_leg is None or no_leg is None:
        return None

    yes_price = prices_by_key.get((yes_leg["venue"], yes_leg["market_id"]))
    no_price = prices_by_key.get((no_leg["venue"], no_leg["market_id"]))
    if yes_price is None or no_price is None:
        return None

    return max_profitable_units(yes_price, no_price, opportunity.guaranteed_payout, opportunity.fee_buffer)
