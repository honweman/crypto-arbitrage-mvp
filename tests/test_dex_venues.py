from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from arbitrage_bot.dex_venues import probe_dex_venue


class DexVenueProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_hyperliquid_probe_is_read_only(self) -> None:
        with patch(
            "arbitrage_bot.dex_venues._probe_hyperliquid",
            new=AsyncMock(return_value={"market_count": 200, "position_count": 1}),
        ) as probe:
            result = await probe_dex_venue(
                venue="hyperliquid",
                wallet_address="0x0000000000000000000000000000000000000001",
            )

        self.assertEqual(result["status"], "healthy")
        self.assertFalse(result["live_trading_authorized"])
        self.assertEqual(result["detail"]["market_count"], 200)
        probe.assert_awaited_once()

    async def test_wallet_venue_requires_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a verified wallet"):
            await probe_dex_venue(venue="polymarket", wallet_address="")

    async def test_dydx_public_probe_does_not_require_evm_wallet(self) -> None:
        with patch(
            "arbitrage_bot.dex_venues._probe_dydx",
            new=AsyncMock(return_value={"market_count": 42}),
        ):
            result = await probe_dex_venue(venue="dydx", wallet_address="")

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["detail"]["market_count"], 42)


if __name__ == "__main__":
    unittest.main()
