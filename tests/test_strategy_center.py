from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage_bot.strategy_center import (
    FundingArbitrageSettings,
    SignalBotSettings,
    SignalEvent,
    StrategyCenterStore,
    StrategyInstance,
    UserApiAccount,
    build_strategy_center_public_payload,
)


class StrategyCenterTest(unittest.TestCase):
    def _sample_strategy(self) -> StrategyInstance:
        return StrategyInstance.from_dict(
            {
                "name": "ACS Coinbase MM",
                "strategy_type": "market_maker",
                "owner_email": "trader@example.com",
                "account_id": "coinbase-main",
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "enabled": True,
                "parameters": {"levels": 2, "band_pct": 1},
                "risk_overrides": {"max_order_quote": 1},
            }
        )

    def _sample_account(self) -> UserApiAccount:
        return UserApiAccount.from_dict(
            {
                "id": "coinbase-main",
                "owner_email": "trader@example.com",
                "label": "Coinbase Main",
                "exchange": "coinbase-spot",
                "asset_scope": ["ACS"],
                "api_key_env": "COINBASE_API_KEY",
                "secret_env": "COINBASE_SECRET",
                "enabled": True,
            }
        )

    def test_store_roundtrip_and_user_public_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyCenterStore(Path(tmp) / "strategy_center.json")
            strategy = self._sample_strategy()
            account = self._sample_account()

            store.upsert_strategy(strategy)
            payload = store.upsert_api_account(account)

        public_payload = build_strategy_center_public_payload(
            payload,
            current_user_email="trader@example.com",
            current_user_role="user",
            allowed_assets=["ACS"],
        )

        self.assertEqual(public_payload["summary"]["strategy_count"], 1)
        self.assertEqual(public_payload["summary"]["api_account_count"], 1)
        self.assertEqual(
            public_payload["strategy_instances"][0]["name"],
            "ACS Coinbase MM",
        )
        self.assertEqual(
            public_payload["user_api_accounts"][0]["auth"]["missing_env"],
            ["COINBASE_API_KEY", "COINBASE_SECRET"],
        )

    def test_sqlite_store_roundtrip_and_signal_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyCenterStore(
                Path(tmp) / "strategy_center.sqlite3",
                max_recent_signals=3,
            )
            store.upsert_strategy(self._sample_strategy())
            store.upsert_api_account(self._sample_account())
            for index in range(5):
                store.append_signal(
                    SignalEvent.from_payload(
                        {
                            "id": f"signal-{index}",
                            "strategy_id": "coinbase-main",
                            "symbol": "ACS/USDC",
                            "side": "buy",
                            "message": f"entry {index}",
                        },
                        source="tradingview",
                        status="accepted",
                    )
                )
            payload = store.read()

        self.assertEqual(payload["strategy_instances"][0]["name"], "ACS Coinbase MM")
        self.assertEqual(payload["user_api_accounts"][0]["id"], "coinbase-main")
        self.assertEqual([row["id"] for row in payload["signals"]], ["signal-2", "signal-3", "signal-4"])

    def test_sqlite_store_migrates_legacy_json_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "strategy_center"
            json_path = base.with_suffix(".json")
            sqlite_path = base.with_suffix(".sqlite3")
            json_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "strategy_instances": [self._sample_strategy().to_dict()],
                        "user_api_accounts": [self._sample_account().to_dict()],
                        "signals": [
                            SignalEvent.from_payload(
                                {
                                    "id": "legacy-signal",
                                    "symbol": "ACS/USDC",
                                    "side": "sell",
                                },
                                source="custom",
                            ).to_dict()
                        ],
                    },
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )

            store = StrategyCenterStore(sqlite_path)
            payload = store.read()
            json_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "strategy_instances": [],
                        "user_api_accounts": [],
                        "signals": [],
                    }
                ),
                encoding="utf-8",
            )
            second_payload = store.read()

        self.assertEqual(payload["strategy_instances"][0]["name"], "ACS Coinbase MM")
        self.assertEqual(payload["signals"][0]["id"], "legacy-signal")
        self.assertEqual(second_payload["strategy_instances"][0]["name"], "ACS Coinbase MM")

    def test_public_payload_filters_owner_and_asset(self) -> None:
        payload = {
            "strategy_instances": [
                {
                    "id": "one",
                    "name": "ACS",
                    "strategy_type": "market_maker",
                    "owner_email": "trader@example.com",
                    "symbol": "ACS/USDC",
                },
                {
                    "id": "two",
                    "name": "BTC",
                    "strategy_type": "spot_grid",
                    "owner_email": "trader@example.com",
                    "symbol": "BTC/USDT",
                },
                {
                    "id": "three",
                    "name": "Other ACS",
                    "strategy_type": "dca",
                    "owner_email": "other@example.com",
                    "symbol": "ACS/USDT",
                },
            ],
            "user_api_accounts": [],
            "signals": [],
        }

        public_payload = build_strategy_center_public_payload(
            payload,
            current_user_email="trader@example.com",
            current_user_role="user",
            allowed_assets=["ACS"],
        )

        self.assertEqual(
            [row["id"] for row in public_payload["strategy_instances"]],
            ["one"],
        )

    def test_rejects_secret_values_in_strategy_and_account_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain secret values"):
            StrategyInstance.from_dict(
                {
                    "name": "bad",
                    "strategy_type": "market_maker",
                    "symbol": "ACS/USDC",
                    "parameters": {"api_key": "do-not-store"},
                }
            )

        with self.assertRaisesRegex(ValueError, "must not contain secret values"):
            UserApiAccount.from_dict(
                {
                    "exchange": "coinbase-spot",
                    "api_key": "do-not-store",
                    "secret_env": "COINBASE_SECRET",
                }
            )

    def test_signal_event_and_settings_sanitize_webhook_payload(self) -> None:
        settings = SignalBotSettings.from_dict(
            {
                "enabled": True,
                "webhook_secret_env": "SIGNAL_BOT_WEBHOOK_SECRET",
                "default_strategy_id": "acs-mm",
            }
        )

        event = SignalEvent.from_payload(
            {
                "id": "tv-1",
                "symbol": "ACS-USDC",
                "side": "buy",
                "price": 0.1,
                "quote_notional": 5,
            },
            source="tradingview",
            default_strategy_id=settings.default_strategy_id,
            status="accepted",
        )

        self.assertEqual(event.symbol, "ACS/USDC")
        self.assertEqual(event.strategy_id, "acs-mm")
        self.assertEqual(event.status, "accepted")

        with self.assertRaisesRegex(ValueError, "must not contain secret values"):
            SignalEvent.from_payload(
                {"symbol": "ACS/USDC", "secret": "do-not-store"},
                source="custom",
            )

    def test_signal_store_normalization_does_not_nest_raw_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyCenterStore(Path(tmp) / "strategy_center.json")
            event = SignalEvent.from_payload(
                {
                    "id": "signal-1",
                    "symbol": "ACS/USDC",
                    "side": "buy",
                    "message": "entry",
                },
                source="tradingview",
                status="accepted",
            )

            first = store.append_signal(event)
            second = store.read()

        self.assertEqual(first["signals"][0]["raw"]["message"], "entry")
        self.assertNotIn("raw", first["signals"][0]["raw"])
        self.assertNotIn("raw", second["signals"][0]["raw"])

    def test_api_account_env_status_requires_key_and_secret(self) -> None:
        account = UserApiAccount.from_dict(
            {
                "exchange": "coinbase-spot",
                "api_key_env": "COINBASE_API_KEY",
                "secret_env": "COINBASE_SECRET",
            }
        )

        with patch.dict(
            "os.environ",
            {"COINBASE_API_KEY": "set", "COINBASE_SECRET": "set"},
        ):
            self.assertTrue(account.env_status()["configured"])

        missing_secret = UserApiAccount.from_dict(
            {
                "exchange": "coinbase-spot",
                "api_key_env": "COINBASE_API_KEY",
            }
        )
        with patch.dict("os.environ", {"COINBASE_API_KEY": "set"}):
            self.assertFalse(missing_secret.env_status()["configured"])
            self.assertIn("secret_env", missing_secret.env_status()["missing_env"])

    def test_funding_arbitrage_validates_symbols(self) -> None:
        settings = FundingArbitrageSettings.from_dict(
            {
                "enabled": True,
                "spot_symbol": "BTC/USDT",
                "derivative_symbol": "BTC/USDT:USDT",
                "min_funding_bps": 1.5,
                "min_liquidation_buffer_pct": 25,
            }
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.min_funding_bps, 1.5)
        self.assertEqual(settings.min_liquidation_buffer_pct, 25)
        with self.assertRaisesRegex(ValueError, "BASE/QUOTE"):
            FundingArbitrageSettings.from_dict({"spot_symbol": "BTCUSDT"})


if __name__ == "__main__":
    unittest.main()
