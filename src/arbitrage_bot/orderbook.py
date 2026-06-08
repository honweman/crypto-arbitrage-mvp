from __future__ import annotations

from .models import BookLevel, FillEstimate, Side


def normalize_levels(raw_levels: list[list[float]] | list[tuple[float, float]]) -> list[BookLevel]:
    return [
        BookLevel(price=float(price), amount=float(amount))
        for price, amount, *_ in raw_levels
        if float(price) > 0 and float(amount) > 0
    ]


def available_base(levels: list[BookLevel]) -> float:
    return sum(level.amount for level in levels)


def max_base_for_quote(levels: list[BookLevel], quote_budget: float) -> float:
    remaining_quote = quote_budget
    total_base = 0.0
    for level in levels:
        max_quote_at_level = level.price * level.amount
        if remaining_quote >= max_quote_at_level:
            total_base += level.amount
            remaining_quote -= max_quote_at_level
            continue
        total_base += remaining_quote / level.price
        break
    return total_base


def estimate_fill(
    levels: list[BookLevel],
    side: Side,
    quantity_base: float,
    fee_bps: float,
) -> FillEstimate | None:
    if quantity_base <= 0:
        return None

    remaining = quantity_base
    gross_quote = 0.0
    for level in levels:
        take = min(remaining, level.amount)
        gross_quote += take * level.price
        remaining -= take
        if remaining <= 1e-12:
            break

    if remaining > 1e-9:
        return None

    average_price = gross_quote / quantity_base
    fee_quote = gross_quote * fee_bps / 10_000
    return FillEstimate(
        side=side,
        quantity_base=quantity_base,
        average_price=average_price,
        gross_quote=gross_quote,
        fee_quote=fee_quote,
    )
