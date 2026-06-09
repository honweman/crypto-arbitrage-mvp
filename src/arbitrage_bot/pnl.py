from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any

from .config import BotConfig
from .models import OrderBookSnapshot


@dataclass(frozen=True)
class PortfolioPnl:
    status: str
    asset: str
    quote_currency: str
    position_base: float
    average_entry_price: float
    cash_balances: dict[str, float]
    cash_balances_common: dict[str, float]
    cash_value: float
    cash_missing_rates: list[str]
    mark_price: float | None
    mark_source_count: int
    position_value: float | None
    total_pnl: float
    market_maker_pnl: float
    arbitrage_pnl: float
    price_move_pnl: float
    observed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "asset": self.asset,
            "quote_currency": self.quote_currency,
            "position_base": self.position_base,
            "average_entry_price": self.average_entry_price,
            "cash_balances": self.cash_balances,
            "cash_balances_common": self.cash_balances_common,
            "cash_value": self.cash_value,
            "cash_missing_rates": self.cash_missing_rates,
            "mark_price": self.mark_price,
            "mark_source_count": self.mark_source_count,
            "position_value": self.position_value,
            "total_pnl": self.total_pnl,
            "sources": {
                "market_maker": self.market_maker_pnl,
                "arbitrage": self.arbitrage_pnl,
                "price_move": self.price_move_pnl,
            },
            "observed_at": self.observed_at,
        }


def _book_mid_common(
    book: OrderBookSnapshot | None,
    quote_rate: float | None,
) -> float | None:
    if book is None or quote_rate is None:
        return None
    if not book.bids or not book.asks:
        return None
    bid = book.bids[0].price
    ask = book.asks[0].price
    if bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return (bid + ask) / 2 * quote_rate


def _cash_positions_common(
    cash_balances: dict[str, float],
    quote_rates: dict[str, float],
) -> tuple[dict[str, float], float, list[str]]:
    cash_common = {}
    missing_rates = []
    for currency, amount in cash_balances.items():
        currency_key = currency.upper()
        rate = quote_rates.get(currency_key)
        if rate is None:
            missing_rates.append(currency_key)
            continue
        cash_common[currency_key] = amount * rate
    return cash_common, sum(cash_common.values()), sorted(missing_rates)


def build_portfolio_pnl(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
) -> dict[str, Any]:
    portfolio = cfg.portfolio
    asset = portfolio.asset or (
        cfg.spot_markets[0].asset if cfg.spot_markets else ""
    )
    if not portfolio.enabled:
        cash_common, cash_value, cash_missing = _cash_positions_common(
            portfolio.cash_balances,
            quote_rates,
        )
        return PortfolioPnl(
            status="disabled",
            asset=asset,
            quote_currency=cfg.common_quote_currency,
            position_base=portfolio.position_base,
            average_entry_price=portfolio.average_entry_price,
            cash_balances=portfolio.cash_balances,
            cash_balances_common=cash_common,
            cash_value=cash_value,
            cash_missing_rates=cash_missing,
            mark_price=None,
            mark_source_count=0,
            position_value=None,
            total_pnl=0.0,
            market_maker_pnl=0.0,
            arbitrage_pnl=0.0,
            price_move_pnl=0.0,
            observed_at=time(),
        ).to_dict()

    mark_prices = [
        mid
        for market in cfg.spot_markets
        if market.asset == asset
        for mid in [
            _book_mid_common(
                books.get((market.exchange, market.symbol)),
                quote_rates.get(market.quote_currency),
            )
        ]
        if mid is not None
    ]
    cash_common, cash_value, cash_missing = _cash_positions_common(
        portfolio.cash_balances,
        quote_rates,
    )
    mark_price = sum(mark_prices) / len(mark_prices) if mark_prices else None
    position_value = (
        portfolio.position_base * mark_price if mark_price is not None else None
    )
    price_move_pnl = (
        portfolio.position_base * (mark_price - portfolio.average_entry_price)
        if mark_price is not None
        else 0.0
    )
    market_maker_pnl = portfolio.realized_pnl.get("market_maker", 0.0)
    arbitrage_pnl = portfolio.realized_pnl.get("arbitrage", 0.0)
    total_pnl = market_maker_pnl + arbitrage_pnl + price_move_pnl

    return PortfolioPnl(
        status="ok" if mark_price is not None else "missing_mark",
        asset=asset,
        quote_currency=cfg.common_quote_currency,
        position_base=portfolio.position_base,
        average_entry_price=portfolio.average_entry_price,
        cash_balances=portfolio.cash_balances,
        cash_balances_common=cash_common,
        cash_value=cash_value,
        cash_missing_rates=cash_missing,
        mark_price=mark_price,
        mark_source_count=len(mark_prices),
        position_value=position_value,
        total_pnl=total_pnl,
        market_maker_pnl=market_maker_pnl,
        arbitrage_pnl=arbitrage_pnl,
        price_move_pnl=price_move_pnl,
        observed_at=time(),
    ).to_dict()
