from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Literal


Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class BookLevel:
    price: float
    amount: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    exchange: str
    symbol: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    timestamp_ms: int | None = None
    source: str = "rest"
    received_at: float = field(default_factory=time)


@dataclass(frozen=True)
class FillEstimate:
    side: Side
    quantity_base: float
    average_price: float
    gross_quote: float
    fee_quote: float

    @property
    def net_quote(self) -> float:
        if self.side == "buy":
            return self.gross_quote + self.fee_quote
        return self.gross_quote - self.fee_quote


@dataclass(frozen=True)
class OpportunityLeg:
    exchange: str
    symbol: str
    side: Side
    quantity_base: float
    average_price: float
    fee_quote: float
    quote_currency: str | None = None
    gross_quote: float | None = None
    net_quote: float | None = None
    common_quote_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "quantity_base": self.quantity_base,
            "average_price": self.average_price,
            "fee_quote": self.fee_quote,
        }
        if self.quote_currency is not None:
            data["quote_currency"] = self.quote_currency
        if self.gross_quote is not None:
            data["gross_quote"] = self.gross_quote
        if self.net_quote is not None:
            data["net_quote"] = self.net_quote
        if self.common_quote_rate is not None:
            data["common_quote_rate"] = self.common_quote_rate
        return data


@dataclass(frozen=True)
class Opportunity:
    strategy: str
    profit_quote: float
    profit_bps: float
    legs: list[OpportunityLeg]
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "profit_quote": self.profit_quote,
            "profit_bps": self.profit_bps,
            "legs": [leg.to_dict() for leg in self.legs],
            "metadata": self.metadata,
            "observed_at": self.observed_at,
        }
