from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from dataclasses import replace
from typing import Any

from .account_check import (
    _auth_env_status,
    _balance_currencies,
    _summarize_balance,
    _symbols_by_exchange,
)
from .asset_ledger import AssetLedgerStore
from .config import BotConfig, ExchangeConfig, load_config
from .exchanges import ExchangeManager
from .observability import configure_logging


LOGGER = logging.getLogger(__name__)


def _all_exchanges(cfg: BotConfig) -> list[ExchangeConfig]:
    return [*cfg.spot_exchanges, *cfg.derivative_exchanges]


def _isolated_config(cfg: BotConfig, account_key: str) -> BotConfig:
    matches = [exchange for exchange in _all_exchanges(cfg) if exchange.key == account_key]
    if not matches:
        available = ", ".join(sorted(exchange.key for exchange in _all_exchanges(cfg)))
        raise ValueError(f"unknown account {account_key!r}; available: {available}")
    target = matches[0]
    return replace(
        cfg,
        spot_exchanges=[target] if target.market_type == "spot" else [],
        derivative_exchanges=[target] if target.market_type != "spot" else [],
    )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_order(
    account_key: str, raw: dict[str, Any], symbol: str
) -> dict[str, Any]:
    return {
        "exchange": account_key,
        "id": str(raw.get("id") or raw.get("order") or ""),
        "client_order_id": str(
            raw.get("clientOrderId") or raw.get("client_order_id") or ""
        ),
        "symbol": str(raw.get("symbol") or symbol),
        "side": str(raw.get("side") or ""),
        "type": str(raw.get("type") or ""),
        "status": str(raw.get("status") or ""),
        "price": _number(raw.get("price")),
        "amount": _number(raw.get("amount")),
        "filled": _number(raw.get("filled")),
        "remaining": _number(raw.get("remaining")),
        "cost": _number(raw.get("cost")),
        "timestamp": _number(raw.get("timestamp")),
    }


def _normalize_trade(
    account_key: str, raw: dict[str, Any], symbol: str
) -> dict[str, Any]:
    return {
        "exchange": account_key,
        "id": str(raw.get("id") or ""),
        "order_id": str(raw.get("order") or raw.get("order_id") or ""),
        "symbol": str(raw.get("symbol") or symbol),
        "side": str(raw.get("side") or ""),
        "type": str(raw.get("type") or ""),
        "price": _number(raw.get("price")),
        "amount": _number(raw.get("amount")),
        "cost": _number(raw.get("cost")),
        "fee": raw.get("fee") if isinstance(raw.get("fee"), dict) else {},
        "timestamp": _number(raw.get("timestamp")),
        "source": "unattributed",
    }


async def fetch_account_balances_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    account_key: str,
) -> dict[str, Any]:
    exchange = next(row for row in _all_exchanges(cfg) if row.key == account_key)
    symbols = _symbols_by_exchange(cfg).get(account_key, [])
    auth = _auth_env_status(exchange)
    account: dict[str, Any] = {
        "exchange": account_key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "symbols": symbols,
        "auth": auth,
        "status": "ok",
        "warnings": [],
        "errors": [],
        "balance": {"checked": False, "currencies": []},
    }
    if not auth.get("private_checks_enabled"):
        account["status"] = "warning"
        account["warnings"].append("API env vars are missing")
        return account
    try:
        balance = await manager.fetch_balance(exchange)
        account["balance"] = {
            "checked": True,
            "currencies": _summarize_balance(
                balance,
                _balance_currencies(symbols),
                include_zero=False,
            ),
        }
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        account["status"] = "error"
        account["errors"].append(message)
        account["balance"] = {"checked": True, "currencies": [], "error": message}
    return account


async def fetch_order_activity_snapshot(
    cfg: BotConfig,
    manager: ExchangeManager,
    account_key: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    exchange = next(row for row in _all_exchanges(cfg) if row.key == account_key)
    symbols = _symbols_by_exchange(cfg).get(account_key, [])
    auth = _auth_env_status(exchange)
    account: dict[str, Any] = {
        "exchange": account_key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "symbols": symbols,
        "status": "ok",
        "warnings": [],
        "errors": [],
        "open_orders": [],
        "closed_orders": [],
        "recent_trades": [],
    }
    if not auth.get("private_checks_enabled"):
        account["status"] = "warning"
        account["warnings"].append("API env vars are missing")
        return account
    for symbol in symbols:
        try:
            orders = await manager.fetch_open_orders(exchange, symbol=symbol)
            account["open_orders"].extend(
                _normalize_order(account_key, row, symbol)
                for row in orders
                if isinstance(row, dict)
            )
        except Exception as exc:
            account["errors"].append(
                f"{symbol} open orders: {exc.__class__.__name__}: {exc}"
            )
        try:
            closed = await manager.fetch_closed_orders(
                exchange, symbol=symbol, limit=limit
            )
            account["closed_orders"].extend(
                _normalize_order(account_key, row, symbol)
                for row in closed
                if isinstance(row, dict)
            )
        except Exception as exc:
            account["warnings"].append(
                f"{symbol} closed orders: {exc.__class__.__name__}: {exc}"
            )
        try:
            trades = await manager.fetch_my_trades(
                exchange, symbol=symbol, limit=limit
            )
            account["recent_trades"].extend(
                _normalize_trade(account_key, row, symbol)
                for row in trades
                if isinstance(row, dict)
            )
        except Exception as exc:
            account["warnings"].append(
                f"{symbol} fills: {exc.__class__.__name__}: {exc}"
            )
    if account["errors"]:
        account["status"] = "error"
    elif account["warnings"]:
        account["status"] = "warning"
    return account


class AccountWorker:
    def __init__(
        self,
        cfg: BotConfig,
        account_key: str,
        *,
        interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        once: bool = False,
    ) -> None:
        if not cfg.asset_ledger.enabled:
            raise ValueError("asset_ledger.enabled must be true for account workers")
        self.cfg = _isolated_config(cfg, account_key)
        self.account_key = account_key
        self.interval_seconds = max(
            5.0,
            float(interval_seconds or cfg.asset_ledger.worker_interval_seconds),
        )
        self.timeout_seconds = max(
            3.0,
            float(timeout_seconds or cfg.asset_ledger.worker_timeout_seconds),
        )
        self.once = once
        self.worker_id = f"account-reader:{account_key}"
        self.ledger = AssetLedgerStore(cfg.asset_ledger)
        self.manager = ExchangeManager()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _fetch_cycle(self) -> tuple[dict[str, Any], dict[str, Any]]:
        async def fetch_consistent_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
            activity = await fetch_order_activity_snapshot(
                self.cfg, self.manager, self.account_key
            )
            balances = await fetch_account_balances_snapshot(
                self.cfg, self.manager, self.account_key
            )
            return balances, activity

        return await asyncio.wait_for(
            fetch_consistent_snapshot(), timeout=self.timeout_seconds
        )

    async def run_cycle(self) -> dict[str, Any]:
        started_at = time.time()
        self.ledger.update_worker_heartbeat(
            worker_id=self.worker_id,
            account_key=self.account_key,
            pid=os.getpid(),
            status="running",
            last_started_at=started_at,
            metadata={
                "interval_seconds": self.interval_seconds,
                "timeout_seconds": self.timeout_seconds,
                "read_only": True,
            },
        )
        try:
            balances, activity = await self._fetch_cycle()
            result = self.ledger.record_account_snapshot(
                account_key=self.account_key,
                balance_account=balances,
                order_account=activity,
                source=self.worker_id,
            )
            finished_at = time.time()
            next_due_at = finished_at + self.interval_seconds
            status = "ok" if result.get("status") == "ok" else str(result.get("status"))
            self.ledger.update_worker_heartbeat(
                worker_id=self.worker_id,
                account_key=self.account_key,
                pid=os.getpid(),
                status=status,
                last_success_at=finished_at if status != "error" else None,
                last_error_at=finished_at if status == "error" else None,
                next_due_at=next_due_at,
                increment_cycle=True,
                increment_error=status == "error",
                last_error="; ".join(
                    str(item)
                    for item in [
                        *(balances.get("errors") or []),
                        *(activity.get("errors") or []),
                    ]
                ),
                metadata={
                    "interval_seconds": self.interval_seconds,
                    "timeout_seconds": self.timeout_seconds,
                    "read_only": True,
                    "duration_seconds": finished_at - started_at,
                },
            )
            return result
        except Exception as exc:
            failed_at = time.time()
            message = f"{exc.__class__.__name__}: {exc}"
            self.ledger.update_worker_heartbeat(
                worker_id=self.worker_id,
                account_key=self.account_key,
                pid=os.getpid(),
                status="error",
                last_error_at=failed_at,
                next_due_at=failed_at + self.interval_seconds,
                increment_cycle=True,
                increment_error=True,
                last_error=message,
                metadata={
                    "interval_seconds": self.interval_seconds,
                    "timeout_seconds": self.timeout_seconds,
                    "read_only": True,
                    "duration_seconds": failed_at - started_at,
                },
            )
            LOGGER.exception("account worker cycle failed account=%s", self.account_key)
            return {
                "enabled": True,
                "account_key": self.account_key,
                "status": "error",
                "error": message,
                "observed_at": failed_at,
            }

    async def run(self) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        try:
            while not self._stop.is_set():
                latest = await self.run_cycle()
                if self.once:
                    break
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.interval_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            await self.manager.close()
            self.ledger.update_worker_heartbeat(
                worker_id=self.worker_id,
                account_key=self.account_key,
                pid=os.getpid(),
                status="stopped",
                next_due_at=None,
                metadata={
                    "interval_seconds": self.interval_seconds,
                    "timeout_seconds": self.timeout_seconds,
                    "read_only": True,
                },
            )
        return latest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only isolated exchange account reconciliation worker"
    )
    parser.add_argument("--config", default="config.acs.json")
    parser.add_argument("--account", required=True, help="Exchange account key")
    parser.add_argument("--interval-seconds", type=float, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--once", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(args.config)
    worker = AccountWorker(
        cfg,
        args.account,
        interval_seconds=args.interval_seconds,
        timeout_seconds=args.timeout_seconds,
        once=args.once,
    )
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, worker.stop)
        except NotImplementedError:
            pass
    return await worker.run()


def main() -> None:
    configure_logging()
    args = build_parser().parse_args()
    result = asyncio.run(_run(args))
    if args.once:
        print(json_dumps(result))


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


if __name__ == "__main__":
    main()
