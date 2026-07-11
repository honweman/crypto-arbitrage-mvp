import unittest

from arbitrage_bot.backtesting import (
    estimate_depth_execution,
    run_paper_backtest,
    synthetic_depth_levels,
    synthetic_price_series,
)
from arbitrage_bot.config import BacktestConfig, DcaConfig, ExecutionAlgoConfig, SpotGridConfig


class BacktestingTest(unittest.TestCase):
    def test_synthetic_series_uses_current_mid_when_prices_are_blank(self) -> None:
        prices = synthetic_price_series(
            BacktestConfig(step_count=5, price_start=0.0, price_end=0.0),
            current_mid=0.25,
        )

        self.assertEqual(len(prices), 5)
        self.assertAlmostEqual(prices[0], 0.25)

    def test_grid_backtest_outputs_metrics_and_drawdown(self) -> None:
        result = run_paper_backtest(
            BacktestConfig(
                enabled=True,
                strategy="spot_grid",
                symbol="ACS/USDT",
                initial_cash=100.0,
                step_count=80,
                price_start=1.0,
                price_end=0.8,
                volatility_bps=1000.0,
                trend_bps=-2000.0,
                max_recent_points=20,
            ),
            spot_grid=SpotGridConfig(
                enabled=True,
                symbol="ACS/USDT",
                lower_price=0.75,
                upper_price=1.1,
                grid_count=8,
                quote_per_grid=2.0,
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.strategy, "spot_grid")
        self.assertGreaterEqual(result.max_drawdown_pct, 0.0)
        self.assertGreater(result.trade_count, 0)
        self.assertLessEqual(len(result.points), 20)
        self.assertGreaterEqual(result.fee_quote, 0.0)

    def test_dca_backtest_applies_fees_and_fill_rate(self) -> None:
        result = run_paper_backtest(
            BacktestConfig(
                enabled=True,
                strategy="dca",
                symbol="ACS/USDT",
                initial_cash=100.0,
                step_count=20,
                price_start=1.0,
                price_end=0.7,
                volatility_bps=0.0,
            ),
            dca=DcaConfig(
                enabled=True,
                symbol="ACS/USDT",
                side="buy",
                trigger_price=1.0,
                quote_per_order=5.0,
                max_orders=3,
            ),
        )

        self.assertEqual(result.trade_count, 3)
        self.assertGreater(result.fee_quote, 0.0)
        self.assertGreater(result.fill_rate, 0.0)

    def test_execution_algo_backtest_slices_target_quote(self) -> None:
        result = run_paper_backtest(
            BacktestConfig(
                enabled=True,
                strategy="execution_algo",
                symbol="ACS/USDT",
                initial_cash=100.0,
                step_count=10,
                price_start=1.0,
                price_end=1.0,
                volatility_bps=0.0,
            ),
            execution_algo=ExecutionAlgoConfig(
                enabled=True,
                symbol="ACS/USDT",
                side="buy",
                algo="twap",
                total_quote=20.0,
                slice_count=4,
            ),
        )

        self.assertEqual(result.trade_count, 4)
        self.assertAlmostEqual(result.filled_quote, 20.0)
        self.assertAlmostEqual(result.fill_rate, 1.0)

    def test_depth_execution_uses_order_book_levels(self) -> None:
        levels = synthetic_depth_levels(
            reference_price=100.0,
            side="buy",
            quote_per_level=100.0,
            step_bps=100.0,
            level_count=2,
        )

        fill = estimate_depth_execution(
            levels,
            side="buy",
            reference_price=100.0,
            quote_notional=150.0,
        )

        self.assertIsNotNone(fill)
        assert fill is not None
        self.assertGreater(fill["average_price"], 100.0)
        self.assertGreater(fill["slippage_quote"], 0.0)

    def test_backtest_depth_and_latency_warnings_are_reported(self) -> None:
        result = run_paper_backtest(
            BacktestConfig(
                enabled=True,
                strategy="dca",
                symbol="ACS/USDT",
                initial_cash=100.0,
                step_count=5,
                price_start=1.0,
                price_end=1.0,
                volatility_bps=0.0,
                depth_simulation_enabled=True,
                depth_quote_per_level=100.0,
                latency_steps=1,
            ),
            dca=DcaConfig(
                enabled=True,
                symbol="ACS/USDT",
                side="buy",
                quote_per_order=5.0,
                max_orders=1,
            ),
        )

        self.assertEqual(result.trade_count, 1)
        self.assertGreater(result.slippage_quote, 0.0)
        self.assertTrue(
            any("depth simulation" in warning for warning in result.warnings)
        )
        self.assertTrue(any("latency" in warning for warning in result.warnings))

    def test_historical_series_reports_market_and_risk_metrics(self) -> None:
        timestamps = [1_700_000_000_000 + index * 3_600_000 for index in range(8)]
        prices = [1.0, 0.98, 0.95, 0.99, 1.02, 1.04, 1.01, 1.06]

        result = run_paper_backtest(
            BacktestConfig(
                enabled=True,
                strategy="dca",
                symbol="ACS/USDT",
                initial_cash=100.0,
                fee_bps=10.0,
                max_recent_points=20,
            ),
            dca=DcaConfig(
                enabled=True,
                symbol="ACS/USDT",
                side="buy",
                trigger_price=1.0,
                interval_seconds=7_200.0,
                quote_per_order=10.0,
                max_orders=3,
            ),
            price_series=prices,
            timestamps_ms=timestamps,
            timeframe_seconds=3_600.0,
            data_source="exchange_ohlcv",
        )

        self.assertEqual(result.data_source, "exchange_ohlcv")
        self.assertEqual(result.bar_count, len(prices))
        self.assertEqual(result.start_timestamp_ms, timestamps[0])
        self.assertEqual(result.end_timestamp_ms, timestamps[-1])
        self.assertAlmostEqual(result.benchmark_return_pct, 6.0)
        self.assertIsNotNone(result.annualized_volatility_pct)
        self.assertIsNotNone(result.sharpe_ratio)
        self.assertGreater(result.turnover_pct, 0.0)
        self.assertTrue(all(point.timestamp_ms for point in result.points))
        self.assertTrue(any("OHLCV" in warning for warning in result.warnings))

    def test_historical_dca_respects_interval_seconds(self) -> None:
        timestamps = [1_700_000_000_000 + index * 60_000 for index in range(6)]
        result = run_paper_backtest(
            BacktestConfig(
                enabled=True,
                strategy="dca",
                symbol="ACS/USDT",
                initial_cash=100.0,
            ),
            dca=DcaConfig(
                enabled=True,
                symbol="ACS/USDT",
                side="buy",
                quote_per_order=5.0,
                max_orders=10,
                interval_seconds=120.0,
            ),
            price_series=[1.0] * 6,
            timestamps_ms=timestamps,
            timeframe_seconds=60.0,
        )

        self.assertEqual(result.trade_count, 3)

    def test_historical_series_rejects_misaligned_timestamps(self) -> None:
        with self.assertRaisesRegex(ValueError, "timestamps must match"):
            run_paper_backtest(
                BacktestConfig(enabled=True, strategy="dca"),
                dca=DcaConfig(enabled=True),
                price_series=[1.0, 1.1],
                timestamps_ms=[1_700_000_000_000],
            )


if __name__ == "__main__":
    unittest.main()
