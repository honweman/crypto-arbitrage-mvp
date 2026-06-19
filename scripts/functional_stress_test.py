#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from aiohttp.test_utils import TestServer

from arbitrage_bot.config import (
    AlertConfig,
    BacktestConfig,
    BotConfig,
    DcaConfig,
    ExecutionAlgoConfig,
    MarketMakerConfig,
    OnchainMonitorConfig,
    PnlStoreConfig,
    PortfolioConfig,
    RiskConfig,
    SlowExecutionConfig,
    SpotGridConfig,
    StrategyCenterConfig,
    StrategyTimelineConfig,
    TradeLogConfig,
    WebSecurityConfig,
)
from arbitrage_bot.strategy_center import (
    SignalBotSettings,
    StrategyCenterStore,
    StrategyInstance,
)
from arbitrage_bot.web import create_app


SIGNAL_SECRET = "local-stress-secret"
STRATEGY_IDS = {
    "market_maker": True,
    "slow_execution": True,
    "spot_grid": True,
    "dca": True,
    "execution_algo": True,
    "backtest": True,
    "spot_spread": True,
    "cash_and_carry": True,
    "funding_arbitrage": True,
    "signal_bot": True,
}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def _summary(name: str, latencies_ms: list[float], errors: list[str], elapsed: float) -> dict[str, Any]:
    return {
        "name": name,
        "request_count": len(latencies_ms),
        "error_count": len(errors),
        "elapsed_seconds": elapsed,
        "requests_per_second": (len(latencies_ms) / elapsed) if elapsed > 0 else 0.0,
        "latency_ms": {
            "min": min(latencies_ms) if latencies_ms else 0.0,
            "mean": statistics.fmean(latencies_ms) if latencies_ms else 0.0,
            "p50": _percentile(latencies_ms, 50),
            "p95": _percentile(latencies_ms, 95),
            "p99": _percentile(latencies_ms, 99),
            "max": max(latencies_ms) if latencies_ms else 0.0,
        },
        "sample_errors": errors[:10],
    }


def make_test_config(data_dir: Path, *, max_recent_signals: int) -> BotConfig:
    return BotConfig(
        poll_seconds=30.0,
        order_book_depth=20,
        notional_quote=100.0,
        min_profit_quote=0.1,
        min_profit_bps=1.0,
        min_basis_bps=15.0,
        common_quote_currency="USD",
        quote_rates={"USD": 1.0, "USDC": 1.0, "USDT": 1.0},
        quote_rate_sources=[],
        onchain_monitor=OnchainMonitorConfig(enabled=False),
        market_maker=MarketMakerConfig(enabled=False, live_enabled=False),
        slow_execution=SlowExecutionConfig(enabled=False),
        spot_grid=SpotGridConfig(enabled=False, live_enabled=False),
        dca=DcaConfig(enabled=False, live_enabled=False),
        execution_algo=ExecutionAlgoConfig(enabled=False, live_enabled=False),
        backtest=BacktestConfig(enabled=False),
        strategy_center=StrategyCenterConfig(
            enabled=True,
            path=str(data_dir / "strategy_center.sqlite3"),
            max_recent_signals=max_recent_signals,
        ),
        portfolio=PortfolioConfig(enabled=False),
        spot_symbols=[],
        spot_markets=[],
        cash_and_carry_pairs=[],
        spot_exchanges=[],
        derivative_exchanges=[],
        risk=RiskConfig(
            enabled=True,
            trading_enabled=True,
            allow_live_trading=False,
            allow_market_maker=True,
            allow_slow_execution=True,
            strategy_enabled=dict(STRATEGY_IDS),
            max_order_quote=1.0,
            max_cycle_quote=5.0,
            max_open_orders=20,
            max_daily_loss_quote=0.0,
        ),
        trade_log=TradeLogConfig(
            enabled=True,
            path=str(data_dir / "trade_events.jsonl"),
            max_recent_events=50,
        ),
        strategy_timeline=StrategyTimelineConfig(
            enabled=True,
            path=str(data_dir / "strategy_timeline.jsonl"),
            max_recent_events=100,
        ),
        pnl_store=PnlStoreConfig(enabled=False, path=str(data_dir / "fill_pnl.sqlite3")),
        alerts=AlertConfig(enabled=False),
        web_security=WebSecurityConfig(
            password_env=None,
            cookie_secret_env=None,
            allowed_ips_env=None,
            cookie_secure=False,
            user_store_path=str(data_dir / "web_users.json"),
            registration_enabled=False,
        ),
    )


def seed_strategy_center(cfg: BotConfig) -> None:
    store = StrategyCenterStore(
        cfg.strategy_center.path,
        max_recent_signals=cfg.strategy_center.max_recent_signals,
    )
    store.upsert_strategy(
        StrategyInstance.from_dict(
            {
                "id": "stress-mm",
                "name": "Stress ACS MM",
                "strategy_type": "market_maker",
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "asset": "ACS",
                "enabled": True,
                "live_enabled": False,
                "parameters": {"levels": 2, "quote_per_level": 1},
                "risk_overrides": {"max_order_quote": 1},
            }
        )
    )
    store.update_signal_bot(
        SignalBotSettings.from_dict(
            {
                "enabled": True,
                "webhook_secret_env": "SIGNAL_BOT_WEBHOOK_SECRET",
                "default_strategy_id": "stress-mm",
                "max_signal_age_seconds": 60,
                "dedupe_seconds": 300,
            }
        )
    )


async def request_json(
    session: ClientSession,
    method: str,
    url: str,
    *,
    expected_status: int = 200,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with session.request(method, url, json=payload) as response:
        text = await response.text()
        if response.status != expected_status:
            raise AssertionError(
                f"{method} {url} returned {response.status}, expected {expected_status}: {text[:240]}"
            )
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{method} {url} returned non-JSON: {text[:240]}") from exc


async def run_functional_checks(base_url: str, session: ClientSession) -> dict[str, Any]:
    health = await request_json(session, "GET", f"{base_url}/api/health")
    state = await request_json(session, "GET", f"{base_url}/api/state?view=settings")
    if state.get("strategy_center", {}).get("status") != "ok":
        raise AssertionError("strategy center did not load in settings state")

    account_result = await request_json(
        session,
        "POST",
        f"{base_url}/api/strategy-center",
        payload={
            "action": "upsert_account",
            "account": {
                "id": "stress-coinbase",
                "label": "Stress Coinbase",
                "exchange": "coinbase-spot",
                "asset_scope": ["ACS"],
                "api_key_env": "COINBASE_API_KEY",
                "secret_env": "COINBASE_SECRET",
                "enabled": True,
            },
        },
    )
    strategy_result = await request_json(
        session,
        "POST",
        f"{base_url}/api/strategy-center",
        payload={
            "action": "upsert_strategy",
            "strategy": {
                "id": "stress-grid",
                "name": "Stress Grid",
                "strategy_type": "spot_grid",
                "account_id": "stress-coinbase",
                "exchange": "coinbase-spot",
                "symbol": "ACS/USDC",
                "asset": "ACS",
                "enabled": True,
                "parameters": {"lower_price": 0.0001, "upper_price": 0.0002, "grid_count": 10},
                "risk_overrides": {"max_order_quote": 1},
            },
        },
    )
    funding_result = await request_json(
        session,
        "POST",
        f"{base_url}/api/strategy-center",
        payload={
            "action": "update_funding",
            "funding_arbitrage": {
                "enabled": True,
                "pair_id": "BTC hedge",
                "spot_exchange": "binance-spot",
                "spot_symbol": "BTC/USDT",
                "derivative_exchange": "binance-swap",
                "derivative_symbol": "BTC/USDT:USDT",
                "predicted_funding_rate_bps": 1.0,
                "min_funding_bps": 0.5,
                "min_liquidation_buffer_pct": 20,
            },
        },
    )
    await request_json(
        session,
        "POST",
        f"{base_url}/api/signal/tradingview?secret=bad",
        expected_status=403,
        payload={"id": "bad-secret", "symbol": "ACS/USDC", "side": "buy"},
    )
    signal_result = await request_json(
        session,
        "POST",
        f"{base_url}/api/signal/tradingview?secret={SIGNAL_SECRET}",
        payload={
            "id": "functional-signal-1",
            "strategy_id": "stress-mm",
            "symbol": "ACS/USDC",
            "side": "buy",
            "price": 0.00014,
            "quote_notional": 1.0,
        },
    )
    final_state = await request_json(session, "GET", f"{base_url}/api/state?view=settings")
    summary = final_state.get("strategy_center", {}).get("summary", {})
    if summary.get("strategy_count", 0) < 2:
        raise AssertionError(f"expected at least two strategy records, got {summary}")
    if summary.get("api_account_count", 0) < 1:
        raise AssertionError(f"expected at least one api account record, got {summary}")
    return {
        "health": health,
        "strategy_count": summary.get("strategy_count"),
        "api_account_count": summary.get("api_account_count"),
        "account_action_ok": bool(account_result.get("ok")),
        "strategy_action_ok": bool(strategy_result.get("ok")),
        "funding_enabled": bool(
            funding_result.get("strategy_center", {})
            .get("funding_arbitrage", {})
            .get("enabled")
        ),
        "signal_status": signal_result.get("signal", {}).get("status"),
    }


async def run_phase(
    *,
    name: str,
    session: ClientSession,
    method: str,
    url_factory: Any,
    payload_factory: Any | None,
    count: int,
    concurrency: int,
    expected_status: int = 200,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    latencies: list[float] = []
    errors: list[str] = []
    started = time.perf_counter()

    async def one(index: int) -> None:
        async with semaphore:
            url = url_factory(index)
            payload = payload_factory(index) if payload_factory else None
            request_started = time.perf_counter()
            try:
                async with session.request(method, url, json=payload) as response:
                    text = await response.text()
                    if response.status != expected_status:
                        errors.append(
                            f"{method} {url} -> {response.status}: {text[:200]}"
                        )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {url} -> {exc.__class__.__name__}: {exc}")
            finally:
                latencies.append((time.perf_counter() - request_started) * 1000.0)

    await asyncio.gather(*(one(index) for index in range(count)))
    elapsed = time.perf_counter() - started
    return _summary(name, latencies, errors, elapsed)


async def run_stress_suite(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="crypto-arb-stress-") as tmp:
        data_dir = Path(tmp)
        cfg = make_test_config(data_dir, max_recent_signals=max(args.signals + 20, 200))
        seed_strategy_center(cfg)
        os.environ["SIGNAL_BOT_WEBHOOK_SECRET"] = SIGNAL_SECRET
        app = create_app(cfg, "spot-spread", cfg.poll_seconds)
        server = TestServer(app)
        await server.start_server()
        base_url = str(server.make_url("/")).rstrip("/")
        timeout = ClientTimeout(total=args.timeout_seconds)
        try:
            async with ClientSession(timeout=timeout) as session:
                functional = await run_functional_checks(base_url, session)
                phases = [
                    await run_phase(
                        name="state_status",
                        session=session,
                        method="GET",
                        url_factory=lambda _: f"{base_url}/api/state?view=status",
                        payload_factory=None,
                        count=args.state_requests,
                        concurrency=args.concurrency,
                    ),
                    await run_phase(
                        name="state_settings",
                        session=session,
                        method="GET",
                        url_factory=lambda _: f"{base_url}/api/state?view=settings",
                        payload_factory=None,
                        count=args.settings_requests,
                        concurrency=max(1, min(args.concurrency, 20)),
                    ),
                    await run_phase(
                        name="strategy_center_writes",
                        session=session,
                        method="POST",
                        url_factory=lambda _: f"{base_url}/api/strategy-center",
                        payload_factory=lambda index: {
                            "action": "upsert_strategy",
                            "strategy": {
                                "id": f"stress-grid-{index}",
                                "name": f"Stress Grid {index}",
                                "strategy_type": "spot_grid",
                                "exchange": "coinbase-spot",
                                "symbol": "ACS/USDC",
                                "asset": "ACS",
                                "enabled": index % 2 == 0,
                                "parameters": {"grid_count": 10 + (index % 5)},
                                "risk_overrides": {"max_order_quote": 1},
                            },
                        },
                        count=args.strategy_writes,
                        concurrency=args.write_concurrency,
                    ),
                    await run_phase(
                        name="signal_webhooks",
                        session=session,
                        method="POST",
                        url_factory=lambda _: f"{base_url}/api/signal/tradingview?secret={SIGNAL_SECRET}",
                        payload_factory=lambda index: {
                            "id": f"stress-signal-{index}",
                            "strategy_id": "stress-mm",
                            "symbol": "ACS/USDC",
                            "side": "buy" if index % 2 == 0 else "sell",
                            "price": 0.00014 + index * 0.000000001,
                            "quote_notional": 1.0,
                        },
                        count=args.signals,
                        concurrency=args.write_concurrency,
                    ),
                ]
                final_state = await request_json(session, "GET", f"{base_url}/api/state?view=settings")
        finally:
            await server.close()

    failures = []
    for phase in phases:
        p95 = phase["latency_ms"]["p95"]
        if phase["error_count"]:
            failures.append(f"{phase['name']} had {phase['error_count']} error(s)")
        if p95 > args.max_p95_ms:
            failures.append(
                f"{phase['name']} p95 {p95:.2f}ms exceeded {args.max_p95_ms:.2f}ms"
            )
    return {
        "status": "fail" if failures else "pass",
        "thresholds": {
            "max_p95_ms": args.max_p95_ms,
            "timeout_seconds": args.timeout_seconds,
        },
        "config": {
            "state_requests": args.state_requests,
            "settings_requests": args.settings_requests,
            "strategy_writes": args.strategy_writes,
            "signals": args.signals,
            "concurrency": args.concurrency,
            "write_concurrency": args.write_concurrency,
            "live_trading_enabled": cfg.risk.allow_live_trading,
            "exchange_count": len(cfg.spot_exchanges) + len(cfg.derivative_exchanges),
        },
        "functional": functional,
        "phases": phases,
        "final_strategy_center_summary": final_state.get("strategy_center", {}).get("summary", {}),
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run offline functional and pressure tests for the crypto trading web stack."
    )
    parser.add_argument("--state-requests", type=int, default=600)
    parser.add_argument("--settings-requests", type=int, default=200)
    parser.add_argument("--strategy-writes", type=int, default=20)
    parser.add_argument("--signals", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--write-concurrency", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-p95-ms", type=float, default=1000.0)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = asyncio.run(run_stress_suite(args))
    output = json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
