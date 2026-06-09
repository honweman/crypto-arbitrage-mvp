from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

from aiohttp import web

from .config import BotConfig, SpotMarketConfig, load_config
from .exchanges import ExchangeManager
from .main import (
    StrategyName,
    _quote_rates_from_sources,
    _symbols_for_configured_spot_markets,
    scan_with_manager,
)
from .models import OrderBookSnapshot, Opportunity
from .solana import SolanaTokenClient, fetch_top_token_owners
from .strategies.spot_spread import find_converted_spot_spread_opportunities


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ACS Arbitrage Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f6f2;
      --surface: #ffffff;
      --surface-2: #eef1ee;
      --text: #17211b;
      --muted: #66736b;
      --line: #d8ded8;
      --green: #0f7a4f;
      --red: #b33b2e;
      --amber: #a66500;
      --blue: #285f9f;
      --focus: #101828;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }

    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 700;
    }

    main {
      width: min(1480px, 100%);
      margin: 0 auto;
      padding: 18px 24px 28px;
    }

    .statusbar {
      display: grid;
      grid-template-columns: repeat(7, minmax(120px, 1fr));
      gap: 1px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--line);
    }

    .metric {
      min-height: 78px;
      padding: 14px;
      background: var(--surface);
    }

    .metric .label {
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
    }

    .metric .value {
      font-variant-numeric: tabular-nums;
      font-size: 21px;
      font-weight: 700;
      white-space: nowrap;
    }

    .subtle {
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--surface-2);
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .pill.running { color: var(--green); background: #e7f3ed; border-color: #b8dccb; }
    .pill.degraded { color: var(--amber); background: #fff4df; border-color: #edd2a7; }
    .pill.error { color: var(--red); background: #fbe9e6; border-color: #e6bbb4; }
    .pill.starting { color: var(--blue); background: #e8f0fa; border-color: #bfd2ea; }

    section {
      margin-top: 18px;
    }

    .section-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin: 0 0 8px;
    }

    h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.3;
    }

    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 940px;
    }

    th, td {
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      white-space: nowrap;
      font-size: 13px;
    }

    th {
      color: var(--muted);
      background: #fafbf9;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }

    tbody tr:last-child td {
      border-bottom: 0;
    }

    .num {
      font-variant-numeric: tabular-nums;
      text-align: right;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .side-buy { color: var(--green); font-weight: 700; }
    .side-sell { color: var(--red); font-weight: 700; }
    .missing { color: var(--amber); font-weight: 650; }
    .ok { color: var(--green); font-weight: 650; }

    .feed {
      display: grid;
      gap: 8px;
    }

    .opportunity {
      display: grid;
      grid-template-columns: 160px 120px 1fr;
      gap: 12px;
      align-items: center;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    .legs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .leg {
      padding: 6px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfb;
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }

    .empty {
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--muted);
      font-size: 14px;
    }

    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 14px; }
      .statusbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .opportunity { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>ACS Arbitrage Monitor</h1>
      <div class="subtle">Bithumb ACS/KRW · Bybit ACS/USDT · Coinbase ACS/USDC</div>
    </div>
    <span id="status" class="pill starting">Starting</span>
  </header>

  <main>
    <div class="statusbar">
      <div class="metric">
        <div class="label">Scans</div>
        <div id="scan-count" class="value">0</div>
      </div>
      <div class="metric">
        <div class="label">Latency</div>
        <div id="latency" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Opportunity</div>
        <div id="opp-count" class="value">0</div>
      </div>
      <div class="metric">
        <div class="label">Notional</div>
        <div id="notional" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Threshold</div>
        <div id="threshold" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Updated</div>
        <div id="updated" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">On-chain</div>
        <div id="onchain-status" class="value">--</div>
      </div>
    </div>

    <section>
      <div class="section-title">
        <h2>Markets</h2>
        <span id="warnings" class="subtle"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Exchange</th>
              <th>Symbol</th>
              <th>Status</th>
              <th class="num">Bid</th>
              <th class="num">Ask</th>
              <th class="num">Bid USD</th>
              <th class="num">Ask USD</th>
              <th class="num">Bid Size</th>
              <th class="num">Ask Size</th>
            </tr>
          </thead>
          <tbody id="markets"></tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-title">
        <h2>Live Opportunities</h2>
        <span id="common-quote" class="subtle">USD</span>
      </div>
      <div id="opportunities" class="feed"></div>
    </section>

    <section>
      <div class="section-title">
        <h2>Quote Rates</h2>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Currency</th>
              <th class="num">USD Rate</th>
            </tr>
          </thead>
          <tbody id="rates"></tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-title">
        <h2>Solana Top Holders</h2>
        <span id="onchain-meta" class="subtle"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Rank</th>
              <th>Owner Wallet</th>
              <th class="num">Balance</th>
              <th class="num">Supply Share</th>
              <th class="num">Change</th>
              <th class="num">Token Accts</th>
            </tr>
          </thead>
          <tbody id="holders"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 10 });
    const money = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
    const compact = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

    function text(id, value) {
      document.getElementById(id).textContent = value;
    }

    function formatAge(ts) {
      if (!ts) return "--";
      const age = Math.max(0, Date.now() / 1000 - ts);
      return age < 60 ? `${age.toFixed(0)}s ago` : `${(age / 60).toFixed(1)}m ago`;
    }

    function renderMarkets(markets) {
      const body = document.getElementById("markets");
      body.innerHTML = "";
      for (const row of markets || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.exchange}</td>
          <td>${row.symbol}</td>
          <td class="${row.status === "ok" ? "ok" : "missing"}">${row.status}</td>
          <td class="num">${row.bid == null ? "--" : fmt.format(row.bid)}</td>
          <td class="num">${row.ask == null ? "--" : fmt.format(row.ask)}</td>
          <td class="num">${row.bid_common == null ? "--" : fmt.format(row.bid_common)}</td>
          <td class="num">${row.ask_common == null ? "--" : fmt.format(row.ask_common)}</td>
          <td class="num">${row.bid_size == null ? "--" : compact.format(row.bid_size)}</td>
          <td class="num">${row.ask_size == null ? "--" : compact.format(row.ask_size)}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderRates(rates) {
      const body = document.getElementById("rates");
      body.innerHTML = "";
      for (const [currency, rate] of Object.entries(rates || {}).sort()) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${currency}</td><td class="num">${fmt.format(rate)}</td>`;
        body.appendChild(tr);
      }
    }

    function renderOpportunities(items) {
      const root = document.getElementById("opportunities");
      root.innerHTML = "";
      if (!items || items.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No active opportunities at the current threshold.";
        root.appendChild(empty);
        return;
      }

      for (const item of items) {
        const el = document.createElement("div");
        el.className = "opportunity";
        const legs = (item.legs || []).map((leg) => `
          <span class="leg">
            <span class="${leg.side === "buy" ? "side-buy" : "side-sell"}">${leg.side.toUpperCase()}</span>
            ${leg.exchange} ${leg.symbol}
            @ ${fmt.format(leg.average_price)}
          </span>
        `).join("");
        el.innerHTML = `
          <div><strong>$${money.format(item.profit_quote)}</strong><div class="subtle">profit</div></div>
          <div><strong>${item.profit_bps.toFixed(2)} bps</strong><div class="subtle">edge</div></div>
          <div class="legs">${legs}</div>
        `;
        root.appendChild(el);
      }
    }

    function shortAddress(address) {
      if (!address || address.length < 12) return address || "--";
      return `${address.slice(0, 6)}...${address.slice(-6)}`;
    }

    function renderHolders(onchain) {
      const body = document.getElementById("holders");
      body.innerHTML = "";
      if (!onchain || !onchain.holders || onchain.holders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No holder data yet.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const holder of onchain.holders) {
        const delta = holder.delta_amount;
        const deltaText = delta == null ? "--" : `${delta >= 0 ? "+" : ""}${compact.format(delta)}`;
        const deltaClass = delta == null ? "" : delta >= 0 ? "ok" : "missing";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${holder.rank}</td>
          <td title="${holder.owner}">${shortAddress(holder.owner)}</td>
          <td class="num">${compact.format(holder.amount)}</td>
          <td class="num">${holder.share_pct == null ? "--" : holder.share_pct.toFixed(4) + "%"}</td>
          <td class="num ${deltaClass}">${deltaText}</td>
          <td class="num">${holder.token_account_count}</td>
        `;
        body.appendChild(tr);
      }
    }

    async function refresh() {
      try {
        const res = await fetch("/api/state", { cache: "no-store" });
        const data = await res.json();

        const status = document.getElementById("status");
        status.textContent = data.status || "unknown";
        status.className = `pill ${data.status || "error"}`;

        text("scan-count", data.scan?.count ?? 0);
        text("latency", data.scan?.elapsed_ms == null ? "--" : `${data.scan.elapsed_ms} ms`);
        text("opp-count", data.opportunities?.length ?? 0);
        text("notional", data.config ? `$${money.format(data.config.notional_quote)}` : "--");
        text("threshold", data.config ? `$${data.config.min_profit_quote} / ${data.config.min_profit_bps} bps` : "--");
        text("updated", formatAge(data.scan?.last_finished));
        text("onchain-status", data.onchain?.status || "off");
        text("common-quote", data.config?.common_quote_currency || "USD");
        text("warnings", (data.warnings || []).join(" · "));
        text("onchain-meta", data.onchain?.mint ? `${data.onchain.label || "Token"} · ${shortAddress(data.onchain.mint)} · ${formatAge(data.onchain.last_finished)}` : "");

        renderMarkets(data.markets);
        renderRates(data.quote_rates);
        renderOpportunities(data.opportunities);
        renderHolders(data.onchain);
      } catch (error) {
        const status = document.getElementById("status");
        status.textContent = "error";
        status.className = "pill error";
      }
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


def _top_level(book: OrderBookSnapshot | None, side: str) -> tuple[float | None, float | None]:
    if book is None:
        return (None, None)
    levels = book.bids if side == "bid" else book.asks
    if not levels:
        return (None, None)
    return (levels[0].price, levels[0].amount)


def build_market_rows(
    markets: Iterable[SpotMarketConfig],
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market in markets:
        book = books.get((market.exchange, market.symbol))
        rate = quote_rates.get(market.quote_currency)
        bid, bid_size = _top_level(book, "bid")
        ask, ask_size = _top_level(book, "ask")
        rows.append(
            {
                "asset": market.asset,
                "exchange": market.exchange,
                "symbol": market.symbol,
                "quote_currency": market.quote_currency,
                "status": "ok" if book is not None and rate is not None else "missing",
                "bid": bid,
                "ask": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "bid_common": bid * rate if bid is not None and rate is not None else None,
                "ask_common": ask * rate if ask is not None and rate is not None else None,
                "timestamp_ms": book.timestamp_ms if book is not None else None,
            }
        )
    return rows


def _build_initial_payload(cfg: BotConfig, poll_seconds: float) -> dict[str, Any]:
    return {
        "status": "starting",
        "config": {
            "poll_seconds": poll_seconds,
            "notional_quote": cfg.notional_quote,
            "min_profit_quote": cfg.min_profit_quote,
            "min_profit_bps": cfg.min_profit_bps,
            "common_quote_currency": cfg.common_quote_currency,
        },
        "scan": {
            "count": 0,
            "elapsed_ms": None,
            "last_started": None,
            "last_finished": None,
        },
        "markets": [],
        "quote_rates": cfg.quote_rates,
        "opportunities": [],
        "recent_opportunities": [],
        "onchain": {
            "status": "disabled",
            "label": cfg.onchain_monitor.label,
            "mint": cfg.onchain_monitor.token_mint,
            "holders": [],
            "last_finished": None,
            "error": None,
        },
        "warnings": ["Waiting for first scan"],
    }


class MonitorState:
    def __init__(self, cfg: BotConfig, poll_seconds: float) -> None:
        self._lock = asyncio.Lock()
        self._payload = _build_initial_payload(cfg, poll_seconds)
        self._recent_opportunities: deque[dict[str, Any]] = deque(maxlen=100)

    async def get(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._payload))

    async def set_scan_result(
        self,
        *,
        cfg: BotConfig,
        poll_seconds: float,
        scan_count: int,
        started_at: float,
        elapsed_ms: int,
        markets: list[dict[str, Any]],
        quote_rates: dict[str, float],
        opportunities: list[Opportunity],
        warnings: list[str],
        onchain: dict[str, Any],
    ) -> None:
        opportunity_dicts = [item.to_dict() for item in opportunities]
        for item in opportunity_dicts:
            self._recent_opportunities.appendleft(item)

        status = "running" if not warnings else "degraded"
        async with self._lock:
            self._payload = {
                "status": status,
                "config": {
                    "poll_seconds": poll_seconds,
                    "notional_quote": cfg.notional_quote,
                    "min_profit_quote": cfg.min_profit_quote,
                    "min_profit_bps": cfg.min_profit_bps,
                    "common_quote_currency": cfg.common_quote_currency,
                },
                "scan": {
                    "count": scan_count,
                    "elapsed_ms": elapsed_ms,
                    "last_started": started_at,
                    "last_finished": time.time(),
                },
                "markets": markets,
                "quote_rates": quote_rates,
                "opportunities": opportunity_dicts,
                "recent_opportunities": list(self._recent_opportunities),
                "onchain": onchain,
                "warnings": warnings,
            }

    async def set_error(
        self,
        *,
        cfg: BotConfig,
        poll_seconds: float,
        scan_count: int,
        started_at: float,
        elapsed_ms: int,
        error: str,
    ) -> None:
        async with self._lock:
            self._payload.update(
                {
                    "status": "error",
                    "config": {
                        "poll_seconds": poll_seconds,
                        "notional_quote": cfg.notional_quote,
                        "min_profit_quote": cfg.min_profit_quote,
                        "min_profit_bps": cfg.min_profit_bps,
                        "common_quote_currency": cfg.common_quote_currency,
                    },
                    "scan": {
                        "count": scan_count,
                        "elapsed_ms": elapsed_ms,
                        "last_started": started_at,
                        "last_finished": time.time(),
                    },
                    "warnings": [error],
                }
            )


def _missing_market_warnings(rows: Iterable[dict[str, Any]]) -> list[str]:
    return [
        f"Missing {row['exchange']} {row['symbol']}"
        for row in rows
        if row["status"] != "ok"
    ]


async def fetch_onchain_payload(
    cfg: BotConfig,
    client: SolanaTokenClient | None,
    previous_amounts: dict[str, float],
) -> tuple[dict[str, Any], dict[str, float]]:
    onchain_cfg = cfg.onchain_monitor
    if not onchain_cfg.enabled:
        return (
            {
                "status": "disabled",
                "label": onchain_cfg.label,
                "mint": onchain_cfg.token_mint,
                "holders": [],
                "last_finished": None,
                "error": None,
            },
            previous_amounts,
        )
    if onchain_cfg.network.lower() != "solana":
        return (
            {
                "status": "error",
                "label": onchain_cfg.label,
                "mint": onchain_cfg.token_mint,
                "holders": [],
                "last_finished": time.time(),
                "error": f"Unsupported network: {onchain_cfg.network}",
            },
            previous_amounts,
        )
    if client is None:
        return (
            {
                "status": "error",
                "label": onchain_cfg.label,
                "mint": onchain_cfg.token_mint,
                "holders": [],
                "last_finished": time.time(),
                "error": "Solana client is not configured",
            },
            previous_amounts,
        )

    data = await fetch_top_token_owners(
        client,
        onchain_cfg.token_mint,
        top_n=onchain_cfg.top_n,
    )
    holders = data["holders"]
    next_amounts = {item["owner"]: item["amount"] for item in holders}
    for holder in holders:
        previous = previous_amounts.get(holder["owner"])
        holder["delta_amount"] = (
            None if previous is None else holder["amount"] - previous
        )

    return (
        {
            "status": "running",
            "label": onchain_cfg.label,
            "mint": onchain_cfg.token_mint,
            "supply": data["supply"],
            "decimals": data["decimals"],
            "holders": holders,
            "source_account_count": data["source_account_count"],
            "last_finished": time.time(),
            "error": None,
        },
        next_amounts,
    )


async def monitor_loop(
    cfg: BotConfig,
    strategy: StrategyName,
    state: MonitorState,
    poll_seconds: float,
) -> None:
    manager = ExchangeManager()
    solana_client = (
        SolanaTokenClient(cfg.onchain_monitor.rpc_url)
        if cfg.onchain_monitor.enabled
        else None
    )
    onchain_payload = _build_initial_payload(cfg, poll_seconds)["onchain"]
    previous_onchain_amounts: dict[str, float] = {}
    next_onchain_scan = 0.0
    scan_count = 0
    try:
        while True:
            monotonic_started = time.monotonic()
            started_at = time.time()
            scan_count += 1
            try:
                if strategy in {"all", "spot-spread"} and cfg.spot_markets:
                    books = await manager.fetch_order_books(
                        cfg.spot_exchanges,
                        _symbols_for_configured_spot_markets(cfg),
                        cfg.order_book_depth,
                    )
                    quote_rates = _quote_rates_from_sources(cfg, books)
                    rows = build_market_rows(cfg.spot_markets, books, quote_rates)
                    opportunities = find_converted_spot_spread_opportunities(
                        books=books,
                        exchanges=cfg.spot_exchanges,
                        markets=cfg.spot_markets,
                        notional_quote=cfg.notional_quote,
                        min_profit_quote=cfg.min_profit_quote,
                        min_profit_bps=cfg.min_profit_bps,
                        quote_rates=quote_rates,
                        common_quote_currency=cfg.common_quote_currency,
                    )
                    warnings = _missing_market_warnings(rows)
                else:
                    opportunities = await scan_with_manager(cfg, strategy, manager)
                    rows = []
                    quote_rates = cfg.quote_rates
                    warnings = []

                now = time.monotonic()
                if cfg.onchain_monitor.enabled and now >= next_onchain_scan:
                    try:
                        onchain_payload, previous_onchain_amounts = (
                            await fetch_onchain_payload(
                                cfg,
                                solana_client,
                                previous_onchain_amounts,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        onchain_payload = {
                            "status": "error",
                            "label": cfg.onchain_monitor.label,
                            "mint": cfg.onchain_monitor.token_mint,
                            "holders": [],
                            "last_finished": time.time(),
                            "error": str(exc),
                        }
                    next_onchain_scan = (
                        now + max(1.0, cfg.onchain_monitor.poll_seconds)
                    )

                if onchain_payload.get("status") == "error":
                    warnings = [*warnings, f"On-chain: {onchain_payload.get('error')}"]

                elapsed = time.monotonic() - monotonic_started
                await state.set_scan_result(
                    cfg=cfg,
                    poll_seconds=poll_seconds,
                    scan_count=scan_count,
                    started_at=started_at,
                    elapsed_ms=int(elapsed * 1000),
                    markets=rows,
                    quote_rates=quote_rates,
                    opportunities=opportunities,
                    warnings=warnings,
                    onchain=onchain_payload,
                )
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - monotonic_started
                await state.set_error(
                    cfg=cfg,
                    poll_seconds=poll_seconds,
                    scan_count=scan_count,
                    started_at=started_at,
                    elapsed_ms=int(elapsed * 1000),
                    error=str(exc),
                )

            sleep_for = max(0.0, poll_seconds - (time.monotonic() - monotonic_started))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await manager.close()
        if solana_client is not None:
            await solana_client.close()


async def index(_: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")


async def api_state(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    return web.json_response(await state.get())


async def api_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_app(
    cfg: BotConfig,
    strategy: StrategyName,
    poll_seconds: float | None,
) -> web.Application:
    interval = cfg.poll_seconds if poll_seconds is None else poll_seconds
    app = web.Application()
    state = MonitorState(cfg, interval)
    app["monitor_state"] = state

    async def monitor_context(app_: web.Application) -> Any:
        task = asyncio.create_task(monitor_loop(cfg, strategy, state, interval))
        app_["monitor_task"] = task
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(monitor_context)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/health", api_health)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crypto arbitrage monitor web UI")
    parser.add_argument("--config", default="config.acs.json", help="Path to JSON config")
    parser.add_argument(
        "--strategy",
        choices=["all", "spot-spread", "cash-and-carry"],
        default="spot-spread",
        help="Strategy to monitor",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override config poll interval",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    app = create_app(cfg, args.strategy, args.poll_seconds)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
