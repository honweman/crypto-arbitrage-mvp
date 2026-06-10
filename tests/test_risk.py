from pathlib import Path
import tempfile
import unittest

from arbitrage_bot.config import RiskConfig, TradeLogConfig
from arbitrage_bot.risk import RiskOrder, evaluate_order_batch
from arbitrage_bot.trade_log import (
    normalize_trade_event,
    read_recent_trade_entries,
    read_recent_trade_events,
    summarize_trade_entries,
    write_trade_event,
)


class RiskTest(unittest.TestCase):
    def _order(self, quote_notional: float = 1.0) -> RiskOrder:
        return RiskOrder(
            strategy="slow_execution",
            exchange="bybit-spot",
            symbol="ACS/USDT",
            side="sell",
            amount=1000.0,
            price=0.001,
            quote_notional=quote_notional,
        )

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
