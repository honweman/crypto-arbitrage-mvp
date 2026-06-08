from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable

from .config import BotConfig, CashAndCarryPair, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .models import Opportunity
from .strategies.cash_and_carry import find_cash_and_carry_opportunities
from .strategies.spot_spread import find_spot_spread_opportunities


StrategyName = str


def _symbols_for_all(
    exchanges: Iterable[ExchangeConfig],
    symbols: Iterable[str],
) -> dict[str, set[str]]:
    symbol_set = set(symbols)
    return {exchange.key: set(symbol_set) for exchange in exchanges}


def _spot_symbols_for_cash_and_carry(pairs: Iterable[CashAndCarryPair]) -> set[str]:
    return {pair.spot_symbol for pair in pairs}


def _derivative_symbols_for_cash_and_carry(pairs: Iterable[CashAndCarryPair]) -> set[str]:
    return {pair.derivative_symbol for pair in pairs}


async def scan_once(cfg: BotConfig, strategy: StrategyName) -> list[Opportunity]:
    manager = ExchangeManager()
    opportunities: list[Opportunity] = []
    try:
        if strategy in {"all", "spot-spread"}:
            spot_books = await manager.fetch_order_books(
                cfg.spot_exchanges,
                _symbols_for_all(cfg.spot_exchanges, cfg.spot_symbols),
                cfg.order_book_depth,
            )
            opportunities.extend(
                find_spot_spread_opportunities(
                    books=spot_books,
                    exchanges=cfg.spot_exchanges,
                    symbols=cfg.spot_symbols,
                    notional_quote=cfg.notional_quote,
                    min_profit_quote=cfg.min_profit_quote,
                    min_profit_bps=cfg.min_profit_bps,
                )
            )

        if strategy in {"all", "cash-and-carry"}:
            spot_symbols = _spot_symbols_for_cash_and_carry(cfg.cash_and_carry_pairs)
            derivative_symbols = _derivative_symbols_for_cash_and_carry(
                cfg.cash_and_carry_pairs
            )
            spot_books = await manager.fetch_order_books(
                cfg.spot_exchanges,
                _symbols_for_all(cfg.spot_exchanges, spot_symbols),
                cfg.order_book_depth,
            )
            derivative_books = await manager.fetch_order_books(
                cfg.derivative_exchanges,
                _symbols_for_all(cfg.derivative_exchanges, derivative_symbols),
                cfg.order_book_depth,
            )
            funding_rates = await manager.fetch_funding_rates(
                cfg.derivative_exchanges,
                _symbols_for_all(cfg.derivative_exchanges, derivative_symbols),
            )
            opportunities.extend(
                find_cash_and_carry_opportunities(
                    spot_books=spot_books,
                    derivative_books=derivative_books,
                    spot_exchanges=cfg.spot_exchanges,
                    derivative_exchanges=cfg.derivative_exchanges,
                    pairs=cfg.cash_and_carry_pairs,
                    notional_quote=cfg.notional_quote,
                    min_profit_quote=cfg.min_profit_quote,
                    min_basis_bps=cfg.min_basis_bps,
                    funding_rates=funding_rates,
                )
            )
    finally:
        await manager.close()

    opportunities.sort(key=lambda item: item.profit_bps, reverse=True)
    return opportunities


def print_opportunities(opportunities: list[Opportunity]) -> None:
    if not opportunities:
        print(json.dumps({"opportunities": []}, ensure_ascii=True))
        return
    for opportunity in opportunities:
        print(json.dumps(opportunity.to_dict(), ensure_ascii=True, sort_keys=True))


async def run_loop(cfg: BotConfig, strategy: StrategyName, once: bool) -> None:
    while True:
        opportunities = await scan_once(cfg, strategy)
        print_opportunities(opportunities)
        sys.stdout.flush()

        if once:
            return
        await asyncio.sleep(cfg.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run crypto arbitrage scanner")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument(
        "--strategy",
        choices=["all", "spot-spread", "cash-and-carry"],
        default="all",
        help="Strategy to run",
    )
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    asyncio.run(run_loop(cfg, args.strategy, args.once))


if __name__ == "__main__":
    main()
