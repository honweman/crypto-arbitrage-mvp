import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from arbitrage_bot.account_check import _symbols_by_exchange, run_account_checks
from arbitrage_bot.config import (
    BotConfig,
    CrossExchangeRebalanceConfig,
    ExchangeConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
)
from arbitrage_bot.models import BookLevel, OrderBookSnapshot


class FakeAccountCheckManager:
    def __init__(self) -> None:
        self.private_calls = 0

    async def fetch_market_info(
        self,
        _: ExchangeConfig,
        *,
        symbol: str,
    ) -> dict[str, object]:
        return {
            "id": symbol.replace("/", ""),
            "symbol": symbol,
            "active": True,
            "type": "spot",
            "spot": True,
            "precision": {"amount": 1e-6, "price": 1e-8},
            "limits": {
                "amount": {"min": 1.0},
                "price": {"min": 0.000001},
                "cost": {"min": 0.1},
            },
        }

    async def fetch_order_book(
        self,
        cfg: ExchangeConfig,
        symbol: str,
        _: int,
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            exchange=cfg.key,
            symbol=symbol,
            bids=[BookLevel(price=0.00014, amount=100_000.0)],
            asks=[BookLevel(price=0.00016, amount=100_000.0)],
            timestamp_ms=123456,
        )

    async def fetch_balance(self, _: ExchangeConfig) -> dict[str, object]:
        self.private_calls += 1
        return {
            "free": {"ACS": 1000.0, "USDT": 20.0},
            "used": {"ACS": 0.0, "USDT": 1.0},
            "total": {"ACS": 1000.0, "USDT": 21.0},
        }

    async def fetch_open_orders(
        self,
        _: ExchangeConfig,
        *,
        symbol: str,
    ) -> list[dict[str, object]]:
        self.private_calls += 1
        return [
            {
                "id": "order-1",
                "clientOrderId": "client-1",
                "symbol": symbol,
                "side": "buy",
                "type": "limit",
                "price": 0.00014,
                "amount": 1000.0,
                "filled": 0.0,
                "remaining": 1000.0,
                "status": "open",
                "timestamp": 123456,
            }
        ]

    async def fetch_currency_status(
        self,
        _: ExchangeConfig,
        *,
        currencies: set[str],
    ) -> dict[str, object]:
        return {
            "checked": True,
            "unsupported": False,
            "currencies": {
                currency: {
                    "active": True,
                    "deposit": True,
                    "withdraw": True,
                    "fee": 0.1,
                }
                for currency in currencies
            },
        }


class AccountCheckTest(unittest.IsolatedAsyncioTestCase):
    def test_rebalance_routes_are_included_in_preflight_symbols(self) -> None:
        cfg = replace(
            self._cfg(RiskConfig()),
            cross_exchange_rebalance=CrossExchangeRebalanceConfig(
                buy_exchange="bybit-spot",
                buy_symbol="ACS/USDT",
                sell_exchange="coinbase-spot",
                sell_symbol="ACS/USDC",
            ),
        )

        symbols = _symbols_by_exchange(cfg)

        self.assertIn("ACS/USDT", symbols["bybit-spot"])
        self.assertEqual(symbols["coinbase-spot"], ["ACS/USDC"])

    async def test_missing_api_env_skips_private_checks(self) -> None:
        cfg = self._cfg(RiskConfig(allow_live_trading=True))
        manager = FakeAccountCheckManager()

        with patch.dict(os.environ, {}, clear=True):
            payload = await run_account_checks(cfg, manager)

        account = payload["accounts"][0]
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(account["status"], "warning")
        self.assertFalse(account["auth"]["private_checks_enabled"])
        self.assertIn("BYBIT_API_KEY", account["auth"]["missing_env"])
        self.assertEqual(account["balance"]["checked"], False)
        self.assertEqual(account["open_orders"][0]["checked"], False)
        self.assertEqual(manager.private_calls, 0)
        self.assertTrue(account["markets"][0]["market"]["found"])
        self.assertEqual(account["markets"][0]["order_book"]["best_bid"], 0.00014)

    async def test_configured_api_env_runs_private_checks(self) -> None:
        cfg = self._cfg(RiskConfig(allow_live_trading=True))
        manager = FakeAccountCheckManager()

        with patch.dict(
            os.environ,
            {"BYBIT_API_KEY": "key", "BYBIT_SECRET": "secret"},
            clear=True,
        ):
            payload = await run_account_checks(cfg, manager)

        account = payload["accounts"][0]
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(account["auth"]["private_checks_enabled"])
        self.assertEqual(account["balance"]["checked"], True)
        balances = {
            item["currency"]: item
            for item in account["balance"]["currencies"]
        }
        self.assertEqual(balances["ACS"]["total"], 1000.0)
        self.assertEqual(balances["USDT"]["free"], 20.0)
        self.assertEqual(account["open_orders"][0]["count"], 1)
        transfer = {
            item["currency"]: item
            for item in account["transfer_status"]["currencies"]
        }
        self.assertTrue(transfer["ACS"]["withdraw"])
        self.assertEqual(
            account["open_orders"][0]["preview"][0]["client_order_id"],
            "client-1",
        )
        self.assertEqual(manager.private_calls, 2)

    async def test_transfer_status_warning_when_withdrawal_is_disabled(self) -> None:
        cfg = self._cfg(RiskConfig(allow_live_trading=True))
        manager = FakeAccountCheckManager()

        async def disabled_status(
            _: ExchangeConfig,
            *,
            currencies: set[str],
        ) -> dict[str, object]:
            return {
                "checked": True,
                "unsupported": False,
                "currencies": {
                    currency: {
                        "active": True,
                        "deposit": True,
                        "withdraw": currency != "ACS",
                    }
                    for currency in currencies
                },
            }

        manager.fetch_currency_status = disabled_status  # type: ignore[method-assign]

        with patch.dict(
            os.environ,
            {"BYBIT_API_KEY": "key", "BYBIT_SECRET": "secret"},
            clear=True,
        ):
            payload = await run_account_checks(cfg, manager)

        account = payload["accounts"][0]
        self.assertEqual(payload["status"], "warning")
        self.assertIn("bybit-spot ACS withdrawal is disabled", account["warnings"])

    async def test_transfer_status_warns_when_network_withdrawal_is_disabled(self) -> None:
        cfg = self._cfg(RiskConfig(allow_live_trading=True))
        manager = FakeAccountCheckManager()

        async def network_disabled_status(
            _: ExchangeConfig,
            *,
            currencies: set[str],
        ) -> dict[str, object]:
            return {
                "checked": True,
                "unsupported": False,
                "currencies": {
                    currency: {
                        "active": True,
                        "deposit": True,
                        "withdraw": True,
                        "networks": {
                            "SOL": {
                                "active": True,
                                "deposit": True,
                                "withdraw": currency != "ACS",
                            }
                        },
                    }
                    for currency in currencies
                },
            }

        manager.fetch_currency_status = network_disabled_status  # type: ignore[method-assign]

        with patch.dict(
            os.environ,
            {"BYBIT_API_KEY": "key", "BYBIT_SECRET": "secret"},
            clear=True,
        ):
            payload = await run_account_checks(cfg, manager)

        account = payload["accounts"][0]
        self.assertEqual(payload["status"], "warning")
        self.assertIn(
            "bybit-spot ACS SOL withdrawal is disabled",
            account["warnings"],
        )

    async def test_exchange_filter_reports_missing_configured_exchange(self) -> None:
        cfg = self._cfg(RiskConfig(allow_live_trading=True))
        manager = FakeAccountCheckManager()

        payload = await run_account_checks(
            cfg,
            manager,
            exchange_keys=["coinbase-spot"],
        )

        self.assertEqual(payload["status"], "error")
        self.assertEqual(
            payload["errors"],
            ["exchange is not configured: coinbase-spot"],
        )
        self.assertEqual(payload["accounts"], [])

    def _cfg(self, risk: RiskConfig) -> BotConfig:
        return BotConfig(
            poll_seconds=1.0,
            order_book_depth=20,
            notional_quote=200.0,
            min_profit_quote=0.1,
            min_profit_bps=1.0,
            min_basis_bps=15.0,
            common_quote_currency="USD",
            quote_rates={"USD": 1.0, "USDT": 1.0},
            quote_rate_sources=[],
            onchain_monitor=OnchainMonitorConfig(),
            market_maker=MarketMakerConfig(),
            slow_execution=SlowExecutionConfig(),
            portfolio=PortfolioConfig(),
            spot_symbols=[],
            spot_markets=[
                SpotMarketConfig(
                    asset="ACS",
                    exchange="bybit-spot",
                    symbol="ACS/USDT",
                    quote_currency="USDT",
                )
            ],
            cash_and_carry_pairs=[],
            spot_exchanges=[
                ExchangeConfig(
                    id="bybit",
                    label="bybit-spot",
                    market_type="spot",
                    api_key_env="BYBIT_API_KEY",
                    secret_env="BYBIT_SECRET",
                )
            ],
            derivative_exchanges=[],
            risk=risk,
        )


if __name__ == "__main__":
    unittest.main()
