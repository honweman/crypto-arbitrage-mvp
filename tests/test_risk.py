from pathlib import Path
import tempfile
import time
import unittest

from arbitrage_bot.config import RiskConfig, TradeLogConfig
from arbitrage_bot.risk import RiskMarketContext, RiskOrder, evaluate_order_batch
from arbitrage_bot.trade_log import (
    normalize_trade_event,
    read_recent_trade_entries,
    read_recent_trade_events,
    summarize_trade_entries,
    write_trade_event,
)


class RiskTest(unittest.TestCase):
    def _order(
        self,
        quote_notional: float = 1.0,
        *,
        side: str = "sell",
        amount: float = 1000.0,
        price: float = 0.001,
        exchange: str = "bybit-spot",
        symbol: str = "ACS/USDT",
        distance_bps: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> RiskOrder:
        return RiskOrder(
            strategy="slow_execution",
            exchange=exchange,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            amount=amount,
            price=price,
            quote_notional=quote_notional,
            distance_bps=distance_bps,
            slippage_bps=slippage_bps,
        )

    def _market(self, **overrides: object) -> RiskMarketContext:
        data = {
            "exchange": "bybit-spot",
            "symbol": "ACS/USDT",
            "best_bid": 0.00014,
            "best_ask": 0.00016,
            "mid_price": 0.00015,
            "bid_depth_quote": 100.0,
            "ask_depth_quote": 100.0,
            "max_level_gap_bps": 10.0,
            "order_book_timestamp_ms": int(time.time() * 1000),
        }
        data.update(overrides)
        return RiskMarketContext(**data)  # type: ignore[arg-type]

    def test_blocks_live_trading_until_explicitly_allowed(self) -> None:
        decision = evaluate_order_batch(
            RiskConfig(allow_live_trading=False),
            [self._order()],
            strategy="slow_execution",
            live=True,
        )

        self.assertFalse(decision.approved)
        self.assertIn("risk.allow_live_trading is false", decision.reasons)

    def test_blocks_order_above_max_quote(self) -> None:
        decision = evaluate_order_batch(
            RiskConfig(allow_live_trading=True, max_order_quote=5.0),
            [self._order(quote_notional=6.0)],
            strategy="slow_execution",
            live=True,
        )

        self.assertFalse(decision.approved)
        self.assertIn("exceeds max_order_quote", decision.reasons[0])

    def test_disabled_risk_approves_batch(self) -> None:
        decision = evaluate_order_batch(
            RiskConfig(enabled=False),
            [self._order(quote_notional=1000.0)],
            strategy="slow_execution",
            live=True,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.level, "off")

    def test_blocks_global_strategy_and_account_switches(self) -> None:
        global_decision = evaluate_order_batch(
            RiskConfig(allow_live_trading=True, trading_enabled=False),
            [self._order()],
            strategy="slow_execution",
            live=True,
        )
        strategy_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                strategy_enabled={"slow_execution": False},
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
        )
        account_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                account_enabled={"bybit-spot": False},
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
        )

        self.assertIn("risk.trading_enabled is false", global_decision.reasons)
        self.assertIn(
            "risk.strategy_enabled.slow_execution is false",
            strategy_decision.reasons,
        )
        self.assertIn(
            "risk.account_enabled.bybit-spot is false",
            account_decision.reasons,
        )

    def test_blocks_position_exposure_and_daily_loss_limits(self) -> None:
        position_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_position_base_by_asset={"ACS": 1000.0},
            ),
            [
                self._order(
                    quote_notional=0.09,
                    side="buy",
                    amount=600.0,
                    price=0.00015,
                )
            ],
            strategy="slow_execution",
            live=True,
            market=self._market(),
            current_positions_base={"ACS": 500.0},
        )
        exposure_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_exposure_quote_by_asset={"ACS": 0.05},
            ),
            [
                self._order(
                    quote_notional=0.09,
                    side="buy",
                    amount=600.0,
                    price=0.00015,
                )
            ],
            strategy="slow_execution",
            live=True,
            market=self._market(),
        )
        loss_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_daily_loss_quote=10.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            daily_pnl_quote=-12.0,
        )

        self.assertTrue(
            any("projected position" in reason for reason in position_decision.reasons)
        )
        self.assertTrue(
            any("projected exposure" in reason for reason in exposure_decision.reasons)
        )
        self.assertTrue(any("daily loss" in reason for reason in loss_decision.reasons))

    def test_blocks_open_order_and_cancel_limits(self) -> None:
        open_order_decision = evaluate_order_batch(
            RiskConfig(allow_live_trading=True, max_open_orders=5),
            [self._order()],
            strategy="slow_execution",
            live=True,
            existing_open_order_count=5,
        )
        cancel_count_decision = evaluate_order_batch(
            RiskConfig(allow_live_trading=True, max_cancels_per_cycle=2),
            [self._order()],
            strategy="slow_execution",
            live=True,
            expected_cancel_count=3,
        )
        cancel_frequency_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                min_seconds_between_cancels=60.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            expected_cancel_count=1,
            last_cancel_at=time.time(),
        )

        self.assertIn(
            "projected open orders 6 exceeds max_open_orders 5",
            open_order_decision.reasons,
        )
        self.assertIn(
            "expected cancels 3 exceeds max_cancels_per_cycle 2",
            cancel_count_decision.reasons,
        )
        self.assertTrue(
            any(
                "min_seconds_between_cancels" in reason
                for reason in cancel_frequency_decision.reasons
            )
        )

    def test_blocks_market_quality_and_price_anomalies(self) -> None:
        depth_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                min_order_book_depth_quote=10.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            market=self._market(bid_depth_quote=5.0),
        )
        gap_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_order_book_gap_bps=100.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            market=self._market(max_level_gap_bps=200.0),
        )
        jump_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_price_jump_bps=50.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            market=self._market(mid_price=0.00016),
            previous_mid_price=0.00015,
        )
        stale_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_order_book_age_seconds=1.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            market=self._market(
                order_book_timestamp_ms=int((time.time() - 5) * 1000),
            ),
        )
        fresh_received_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_order_book_age_seconds=1.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            market=self._market(
                order_book_timestamp_ms=int((time.time() - 5) * 1000),
                order_book_received_at=time.time(),
            ),
        )
        stale_received_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_order_book_age_seconds=1.0,
            ),
            [self._order()],
            strategy="slow_execution",
            live=True,
            market=self._market(
                order_book_timestamp_ms=int(time.time() * 1000),
                order_book_received_at=time.time() - 5,
            ),
        )
        slippage_decision = evaluate_order_batch(
            RiskConfig(
                allow_live_trading=True,
                max_slippage_bps=10.0,
            ),
            [
                self._order(
                    side="buy",
                    amount=1000.0,
                    price=0.00017,
                    quote_notional=0.17,
                )
            ],
            strategy="slow_execution",
            live=True,
            market=self._market(),
        )

        self.assertTrue(any("bid depth" in reason for reason in depth_decision.reasons))
        self.assertTrue(
            any("order book gap" in reason for reason in gap_decision.reasons)
        )
        self.assertTrue(any("price jump" in reason for reason in jump_decision.reasons))
        self.assertTrue(
            any("order book age" in reason for reason in stale_decision.reasons)
        )
        self.assertTrue(fresh_received_decision.approved)
        self.assertTrue(
            any("order book age" in reason for reason in stale_received_decision.reasons)
        )
        self.assertTrue(
            any("slippage" in reason for reason in slippage_decision.reasons)
        )


class TradeLogTest(unittest.TestCase):
    def test_normalizes_trade_event_for_display(self) -> None:
        entry = normalize_trade_event(
            {
                "type": "slow_execution",
                "mode": "live",
                "status": "blocked_by_risk",
                "logged_at": 123.0,
                "plan": {
                    "exchange": "bybit-spot",
                    "symbol": "ACS/USDT",
                    "order": {
                        "side": "sell",
                    },
                },
                "risk": {
                    "approved": False,
                    "level": "blocked",
                    "reasons": ["risk.allow_live_trading is false"],
                    "order_count": 1,
                    "total_quote_notional": 0.15,
                },
            }
        )

        self.assertEqual(entry.strategy, "slow_execution")
        self.assertEqual(entry.exchange, "bybit-spot")
        self.assertEqual(entry.symbol, "ACS/USDT")
        self.assertEqual(entry.side, "sell")
        self.assertEqual(entry.risk_level, "blocked")
        self.assertFalse(entry.risk_approved)
        self.assertEqual(entry.reason, "risk.allow_live_trading is false")
        self.assertEqual(entry.total_quote_notional, 0.15)
        self.assertEqual(len(entry.event_id), 16)

    def test_write_and_read_recent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = TradeLogConfig(
                enabled=True,
                path=str(Path(tmp) / "events.jsonl"),
                max_recent_events=2,
            )

            write_trade_event(cfg, {"type": "one"})
            write_trade_event(cfg, {"type": "two"})
            write_trade_event(cfg, {"type": "three"})
            events = read_recent_trade_events(cfg)

        self.assertEqual([event["type"] for event in events], ["three", "two"])
        self.assertTrue(all("logged_at" in event for event in events))

    def test_reads_normalized_entries_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = TradeLogConfig(
                enabled=True,
                path=str(Path(tmp) / "events.jsonl"),
                max_recent_events=10,
            )

            write_trade_event(
                cfg,
                {
                    "type": "market_maker",
                    "status": "placed",
                    "execution": {"placed_count": 2},
                    "risk": {
                        "level": "ok",
                        "approved": True,
                        "order_count": 2,
                        "total_quote_notional": 2.0,
                    },
                },
            )
            write_trade_event(
                cfg,
                {
                    "type": "slow_execution",
                    "status": "blocked_by_risk",
                    "risk": {"level": "blocked", "approved": False},
                },
            )
            entries = read_recent_trade_entries(cfg)
            summary = summarize_trade_entries(entries)

        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["placed_event_count"], 1)
        self.assertEqual(summary["blocked_event_count"], 1)
        self.assertEqual(summary["placed_order_count"], 2)
        self.assertEqual(summary["total_quote_notional"], 2.0)


if __name__ == "__main__":
    unittest.main()
