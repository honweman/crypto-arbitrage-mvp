from __future__ import annotations

import time
import unittest

from arbitrage_bot.models import BookLevel, OrderBookSnapshot
from arbitrage_bot.polymarket_arbitrage import (
    PolymarketBook,
    parse_polymarket_books,
    scan_polymarket_arbitrage,
)
from arbitrage_bot.user_strategies import UserStrategy
from arbitrage_bot.user_workspace import UserExchangeAccount, UserProject


YES = "11111111"
NO = "22222222"


def project() -> UserProject:
    return UserProject.from_dict(
        {
            "id": "project-btc",
            "owner_email": "trader@example.com",
            "name": "BTC event",
            "asset": "BTC",
            "quote_currency": "USDC",
            "status": "active",
        }
    )


def strategy(parameters: dict) -> UserStrategy:
    return UserStrategy.from_dict(
        {
            "owner_email": "trader@example.com",
            "project_id": "project-btc",
            "strategy_type": "prediction_arbitrage",
            "parameters": parameters,
            "risk": {
                "max_order_quote": 20,
                "max_total_quote": 20,
                "max_daily_loss_quote": 10,
                "max_open_orders": 20,
                "max_slippage_bps": 100,
                "paper_fee_bps": 0,
            },
        }
    )


def poly_book(
    token_id: str,
    *,
    condition_id: str,
    bid: float,
    ask: float,
    neg_risk: bool = False,
) -> PolymarketBook:
    return PolymarketBook(
        token_id=token_id,
        condition_id=condition_id,
        snapshot=OrderBookSnapshot(
            exchange="polymarket",
            symbol=token_id,
            bids=[BookLevel(bid, 100)],
            asks=[BookLevel(ask, 100)],
            timestamp_ms=int(time.time() * 1000),
            received_at=time.time(),
        ),
        neg_risk=neg_risk,
        min_order_size=1,
        tick_size=0.01,
    )


class PolymarketArbitrageTest(unittest.TestCase):
    def test_parses_batch_books_and_normalizes_depth(self) -> None:
        books = parse_polymarket_books(
            [
                {
                    "asset_id": YES,
                    "market": "condition-1",
                    "timestamp": "1784460000000",
                    "bids": [
                        {"price": "0.44", "size": "10"},
                        {"price": "0.45", "size": "5"},
                    ],
                    "asks": [
                        {"price": "0.47", "size": "10"},
                        {"price": "0.46", "size": "5"},
                    ],
                    "min_order_size": "1",
                    "tick_size": "0.01",
                    "neg_risk": False,
                }
            ],
            depth=1,
            received_at=100,
        )

        self.assertEqual(books[YES].snapshot.bids[0].price, 0.45)
        self.assertEqual(books[YES].snapshot.asks[0].price, 0.46)
        self.assertEqual(books[YES].snapshot.source, "polymarket_clob_rest")

    def test_binary_complete_set_finds_depth_adjusted_buy_candidate(self) -> None:
        row = strategy(
            {
                "mechanism": "complete_set",
                "outcome_asset_ids": [YES, NO],
                "min_profit_bps": 100,
                "max_cycle_quote": 10,
                "conversion_cost_bps": 0,
            }
        )
        books = {
            YES: poly_book(YES, condition_id="condition-1", bid=0.44, ask=0.45),
            NO: poly_book(NO, condition_id="condition-1", bid=0.47, ask=0.48),
        }

        status, _, _, metrics, scan = scan_polymarket_arbitrage(
            row,
            project(),
            [],
            {},
            books,
            {"USDC": 1.0},
            now=time.time(),
        )

        self.assertEqual(status, "candidate")
        self.assertEqual(metrics["mechanism"], "complete_set_buy")
        self.assertGreater(metrics["profit_quote"], 0)
        self.assertEqual(len(metrics["legs"]), 2)
        self.assertFalse(scan["live_submit_allowed"])

    def test_unrelated_binary_conditions_are_not_treated_as_complete_set(self) -> None:
        row = strategy(
            {
                "mechanism": "complete_set",
                "outcome_asset_ids": [YES, NO],
                "max_cycle_quote": 10,
            }
        )
        books = {
            YES: poly_book(YES, condition_id="condition-1", bid=0.3, ask=0.4),
            NO: poly_book(NO, condition_id="condition-2", bid=0.3, ask=0.4),
        }

        status, _, _, _, scan = scan_polymarket_arbitrage(
            row,
            project(),
            [],
            {},
            books,
            {"USDC": 1.0},
            now=time.time(),
        )

        self.assertEqual(status, "waiting")
        self.assertEqual(scan["observation_count"], 0)

    def test_neg_risk_conversion_compares_no_with_other_yes_books(self) -> None:
        yes_ids = ["11111111", "22222222", "33333333"]
        no_ids = ["44444444", "55555555", "66666666"]
        row = strategy(
            {
                "mechanism": "neg_risk",
                "event_group_id": "event-election",
                "outcome_asset_ids": yes_ids,
                "neg_risk_no_asset_ids": no_ids,
                "min_profit_bps": 100,
                "max_cycle_quote": 10,
                "conversion_cost_bps": 0,
            }
        )
        books: dict[str, PolymarketBook] = {}
        for index, (yes_id, no_id) in enumerate(zip(yes_ids, no_ids)):
            books[yes_id] = poly_book(
                yes_id,
                condition_id=f"condition-{index}",
                bid=0.4,
                ask=0.45,
                neg_risk=True,
            )
            books[no_id] = poly_book(
                no_id,
                condition_id=f"condition-{index}",
                bid=0.25,
                ask=0.3,
                neg_risk=True,
            )

        status, _, _, metrics, _ = scan_polymarket_arbitrage(
            row,
            project(),
            [],
            {},
            books,
            {"USDC": 1.0},
            now=time.time(),
        )

        self.assertEqual(status, "candidate")
        self.assertEqual(metrics["mechanism"], "neg_risk_no_to_other_yes")
        self.assertGreater(metrics["profit_quote"], 0)

    def test_cross_venue_candidate_is_labeled_model_relative_value(self) -> None:
        now = time.time()
        row = strategy(
            {
                "mechanism": "cross_venue",
                "outcome_asset_ids": [YES, NO],
                "min_profit_bps": 10,
                "max_cycle_quote": 10,
                "strike_price": 100,
                "resolution_timestamp": now + 30 * 86_400,
                "annualized_volatility_pct": 50,
                "event_direction": "above",
                "resolution_source_confirmed": True,
            }
        )
        account = UserExchangeAccount.from_dict(
            {
                "id": "hyperliquid-swap",
                "owner_email": "trader@example.com",
                "project_id": "project-btc",
                "label": "Hyperliquid",
                "exchange": "hyperliquid",
                "market_type": "swap",
                "api_variant": "mainnet",
                "symbol": "BTC/USDC:USDC",
            }
        )
        hedge_book = OrderBookSnapshot(
            exchange="hyperliquid-swap",
            symbol=account.symbol,
            bids=[BookLevel(109.9, 100)],
            asks=[BookLevel(110.1, 100)],
            timestamp_ms=int(now * 1000),
            received_at=now,
        )
        books = {
            YES: poly_book(YES, condition_id="condition-1", bid=0.35, ask=0.4),
            NO: poly_book(NO, condition_id="condition-1", bid=0.55, ask=0.6),
        }

        status, _, _, metrics, _ = scan_polymarket_arbitrage(
            row,
            project(),
            [account],
            {account.id: hedge_book},
            books,
            {"USDC": 1.0},
            now=now,
        )

        self.assertEqual(status, "candidate")
        self.assertEqual(metrics["risk_class"], "model_relative_value")
        self.assertEqual(metrics["legs"][1]["venue_type"], "dex")
        self.assertFalse(metrics["live_submit_allowed"])


if __name__ == "__main__":
    unittest.main()
