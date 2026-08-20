"""Rich-based terminal output. This is the only file that prints anything."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from models import ArbOpportunity, NormalizedPrice

console = Console()

_TYPE_LABELS = {
    "binary": "Binary cross-venue",
}


def print_header(source: str) -> None:
    console.print(
        f"\n[bold]Arbitrage Engine for Prediction Markets[/bold]  "
        f"[dim](source={source})[/dim]\n"
    )


def print_scan_summary(
    prices_by_venue: dict[str, int],
    num_groups: int,
    num_opportunities_found: int,
    total_listed: int | None = None,
) -> None:
    """One-glance proof the read pipeline worked, independent of whether any
    opportunity ultimately cleared the filters -- markets were pulled per
    venue, grouped, and checked.

    total_listed, when given (category/discovery mode only), is how many
    markets were listed across both venues before the match-then-price step
    -- "Prices Loaded" only counts what was actually worth pricing, which is
    normally much smaller and would otherwise look like most of the category
    went unscanned rather than un-priced-because-unmatched.
    """
    table = Table(title="Scan Summary", show_header=True, header_style="bold cyan")
    table.add_column("Venue")
    table.add_column("Prices Loaded", justify="right")
    for venue, count in sorted(prices_by_venue.items()):
        table.add_row(venue, str(count))
    table.add_row("TOTAL", str(sum(prices_by_venue.values())), style="bold")
    console.print(table)
    if total_listed is not None:
        console.print(
            f"[dim]{total_listed} market(s) listed in category; "
            f"{sum(prices_by_venue.values())} priced (crosswalk + title-candidate matches only).[/dim]"
        )
    console.print(
        f"Grouped into [bold]{num_groups}[/bold] market group(s); "
        f"[bold]{num_opportunities_found}[/bold] opportunity(ies) passed filters.\n"
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _sizing_lines(opp: ArbOpportunity, sizing: dict | None) -> list[str]:
    """Max fill size before slippage erodes the edge, plus a real per-leg
    trading-fee breakdown at that size. See slippage.py for why Kalshi's
    side of the size walk is capped at its known top-of-book size rather
    than walked level-by-level like Polymarket's, and why Kalshi's fee rate
    is flagged as an estimate rather than a venue-confirmed figure.
    """
    if not sizing or not sizing.get("optimal_units"):
        reason = (sizing or {}).get("limiting_factor", "no liquidity/size data available")
        return [f"[bold]Max size before slippage/fees erode edge:[/bold] 0 units ({reason})"]

    units = sizing["optimal_units"]
    lines = [
        f"[bold]Max size before slippage/fees erode edge:[/bold] {units:,.0f} units "
        f"(limited by: {sizing['limiting_factor']})",
        f"  Avg cost per unit incl. fees: {sizing['avg_cost_per_unit']:.4f}",
    ]
    for leg in opp.legs:
        rate = sizing.get(f"{leg['side'].lower()}_fee_rate")
        fee = sizing.get(f"{leg['side'].lower()}_fees")
        if rate is None or fee is None:
            continue
        estimate_tag = " -- estimate, not venue-confirmed" if leg["venue"] == "Kalshi" else " -- from market data"
        lines.append(
            f"  {leg['venue']} {leg['side']} trading fee: {_money(fee)} (rate {rate:.4f}{estimate_tag})"
        )
    lines.append(f"  Total trading fees: {_money(sizing['total_fees'])}")
    lines.append(f"  Est. profit after real fees + fee buffer: {_money(sizing['estimated_profit'])}")
    return lines


def print_opportunity(
    opp: ArbOpportunity, bankroll: float, estimate: dict[str, float], sizing: dict | None = None
) -> None:
    lines = [
        f"[bold]Type:[/bold] {_TYPE_LABELS.get(opp.arb_type, opp.arb_type)}",
        f"[bold]Market:[/bold] {opp.market_name}",
        f"[bold]Outcome:[/bold] {opp.outcome_name or '(spans all outcomes)'}",
        "",
        "[bold]Legs:[/bold]",
    ]
    for leg in opp.legs:
        outcome_suffix = f" ({leg['outcome']})" if opp.outcome_name is None else ""
        lines.append(f"  - Buy {leg['side']} on {leg['venue']}{outcome_suffix} at {leg['price']:.4f}")

    lines += [
        "",
        f"[bold]Total cost:[/bold] {opp.total_cost:.4f}",
        f"[bold]Guaranteed payout:[/bold] {opp.guaranteed_payout:.4f}",
        f"[bold]Gross edge:[/bold] {opp.gross_edge:.4f}",
        f"[bold]Fee buffer:[/bold] {opp.fee_buffer:.4f}",
        f"[bold]Net edge:[/bold] {opp.net_edge:.4f}",
        f"[bold]Profit per $100 payout:[/bold] {_money(opp.net_edge * 100)}",
        f"[bold]Estimated profit on {_money(bankroll)} bankroll:[/bold] {_money(estimate['profit'])}",
        f"[bold]Match confidence:[/bold] {opp.match_confidence:.2f}",
        f"[bold]Estimated depth:[/bold] "
        f"{_money(opp.estimated_depth) if opp.estimated_depth is not None else 'Unknown'}",
    ]
    lines.extend(_sizing_lines(opp, sizing))
    lines += [
        f"[bold]Status:[/bold] {opp.status}",
        f"[bold]Notes:[/bold] {opp.notes or '-'}",
    ]

    # A flat fee_buffer (opp.fee_buffer) is a simplification -- it doesn't
    # know either venue's real per-market taker fee. sizing["estimated_profit"]
    # (slippage.py) does: it's net of both venues' real, nonlinear fee
    # schedules. A flat-buffer PASS with real_profit <= 0 means the real fees
    # are bigger than the flat buffer assumed, not a genuine arb -- confirmed
    # on live data 2026-08-19 (4 of 6 flat-buffer PASSes had real profit == 0).
    real_profit = sizing.get("estimated_profit") if sizing else None
    if opp.status == "NO EDGE":
        title, color = "[bold red]NOT PROFITABLE[/bold red]", "red"
    elif real_profit is None or real_profit <= 0:
        title, color = "[bold yellow]FLAT-BUFFER PASS -- REAL FEES ERASE IT[/bold yellow]", "yellow"
    else:
        title, color = "[bold green]ARB FOUND[/bold green]", "green"
    console.print(Panel("\n".join(lines), title=title, border_style=color))


def print_top_n_notice(shown: int, total: int) -> None:
    if total > shown:
        console.print(f"[dim]Showing top {shown} of {total} opportunities that passed filters.[/dim]\n")


def print_no_opportunities() -> None:
    console.print("[yellow]No opportunities passed the current filters.[/yellow]")


def print_candidate_matches(candidates: list[tuple[NormalizedPrice, NormalizedPrice]]) -> None:
    """Leads for the crosswalk, not opportunities -- these are never priced
    against each other or run through the arb engine, just titles that
    matched after light normalization. Deliberately plain (no green/red,
    no "FOUND"): the whole point is that these are unverified.
    """
    if not candidates:
        return
    table = Table(
        title=f"{len(candidates)} unverified title match(es) -- review before adding to data/market_pairs.csv",
        show_header=True, header_style="bold cyan",
    )
    table.add_column("Kalshi")
    table.add_column("Kalshi market_id")
    table.add_column("Polymarket")
    table.add_column("Polymarket market_id")
    for a, b in candidates:
        kalshi, poly = (a, b) if a.venue == "Kalshi" else (b, a)
        table.add_row(kalshi.raw_market_name, kalshi.market_id, poly.raw_market_name, poly.market_id)
    console.print(table)
    console.print(
        "[dim]These are not opportunities -- no prices were compared. A matching "
        "title is not proof of the same bet (see README). Read both venues' "
        "actual resolution rules before adding a pair to the crosswalk.[/dim]\n"
    )
