from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExchangeConfig:
    id: str
    label: str | None = None
    market_type: str = "spot"
    fee_bps: float = 0.0
    api_key_env: str | None = None
    secret_env: str | None = None
    password_env: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.label or f"{self.id}:{self.market_type}"


@dataclass(frozen=True)
class CashAndCarryPair:
    spot_symbol: str
    derivative_symbol: str


@dataclass(frozen=True)
class BotConfig:
    poll_seconds: float
    order_book_depth: int
    notional_quote: float
    min_profit_quote: float
    min_profit_bps: float
    min_basis_bps: float
    spot_symbols: list[str]
    cash_and_carry_pairs: list[CashAndCarryPair]
    spot_exchanges: list[ExchangeConfig]
    derivative_exchanges: list[ExchangeConfig]


def _exchange_from_dict(raw: dict[str, Any]) -> ExchangeConfig:
    return ExchangeConfig(
        id=raw["id"],
        label=raw.get("label"),
        market_type=raw.get("market_type", "spot"),
        fee_bps=float(raw.get("fee_bps", 0.0)),
        api_key_env=raw.get("api_key_env"),
        secret_env=raw.get("secret_env"),
        password_env=raw.get("password_env"),
        options=dict(raw.get("options", {})),
    )


def load_config(path: str | Path) -> BotConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return BotConfig(
        poll_seconds=float(raw.get("poll_seconds", 10)),
        order_book_depth=int(raw.get("order_book_depth", 20)),
        notional_quote=float(raw.get("notional_quote", 1000)),
        min_profit_quote=float(raw.get("min_profit_quote", 0)),
        min_profit_bps=float(raw.get("min_profit_bps", 0)),
        min_basis_bps=float(raw.get("min_basis_bps", 0)),
        spot_symbols=list(raw.get("spot_symbols", [])),
        cash_and_carry_pairs=[
            CashAndCarryPair(
                spot_symbol=item["spot_symbol"],
                derivative_symbol=item["derivative_symbol"],
            )
            for item in raw.get("cash_and_carry_pairs", [])
        ],
        spot_exchanges=[
            _exchange_from_dict(item) for item in raw.get("spot_exchanges", [])
        ],
        derivative_exchanges=[
            _exchange_from_dict(item) for item in raw.get("derivative_exchanges", [])
        ],
    )
