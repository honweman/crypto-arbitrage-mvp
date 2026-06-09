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
from .market_making import build_symmetric_market_maker_plan
from .models import OrderBookSnapshot, Opportunity
from .pnl import build_portfolio_pnl
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

    .portfolio-bar {
      display: grid;
      grid-template-columns: repeat(7, minmax(120px, 1fr));
      gap: 1px;
      overflow: hidden;
      margin-bottom: 18px;
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
    .pill.paused { color: var(--muted); background: var(--surface-2); border-color: var(--line); }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .program-switch {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      user-select: none;
    }

    .program-switch input {
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }

    .switch-track {
      position: relative;
      width: 42px;
      height: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-2);
      transition: background 160ms ease, border-color 160ms ease;
    }

    .switch-track::after {
      content: "";
      position: absolute;
      top: 3px;
      left: 3px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: var(--surface);
      box-shadow: 0 1px 3px rgb(23 33 27 / 18%);
      transition: transform 160ms ease;
    }

    .program-switch input:checked + .switch-track {
      border-color: #b8dccb;
      background: #d9eee5;
    }

    .program-switch input:checked + .switch-track::after {
      transform: translateX(18px);
    }

    .program-switch input:focus-visible + .switch-track {
      outline: 2px solid var(--focus);
      outline-offset: 2px;
    }

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
      min-width: 1080px;
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
    .pnl-positive { color: var(--green); }
    .pnl-negative { color: var(--red); }
    .pnl-flat { color: var(--muted); }

    .holder-label {
      display: inline-flex;
      align-items: center;
      max-width: 260px;
      min-height: 24px;
      padding: 3px 8px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fbfcfb;
      color: var(--text);
      font-size: 12px;
      font-weight: 650;
      text-overflow: ellipsis;
      vertical-align: middle;
    }

    .holder-label.known {
      color: var(--blue);
      background: #e8f0fa;
      border-color: #bfd2ea;
    }

    .holder-label.unknown {
      color: var(--muted);
      background: var(--surface-2);
    }

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
      .header-actions { width: 100%; justify-content: space-between; }
      main { padding: 14px; }
      .portfolio-bar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
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
    <div class="header-actions">
      <label class="program-switch" title="Pause or resume scans">
        Program
        <input id="program-toggle" type="checkbox" checked>
        <span class="switch-track"></span>
      </label>
      <span id="status" class="pill starting">Starting</span>
    </div>
  </header>

  <main>
    <div class="portfolio-bar">
      <div class="metric">
        <div class="label">Position</div>
        <div id="portfolio-position" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Mark Price</div>
        <div id="portfolio-mark" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Position Value</div>
        <div id="portfolio-value" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Total P/L</div>
        <div id="portfolio-total-pnl" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">MM P/L</div>
        <div id="portfolio-mm-pnl" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Arb P/L</div>
        <div id="portfolio-arb-pnl" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Price Move</div>
        <div id="portfolio-price-pnl" class="value">--</div>
      </div>
    </div>

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
        <h2>Market Maker Plan</h2>
        <span id="mm-meta" class="subtle"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Side</th>
              <th class="num">Level</th>
              <th class="num">Price</th>
              <th class="num">Amount</th>
              <th class="num">Quote</th>
              <th class="num">Distance</th>
            </tr>
          </thead>
          <tbody id="mm-orders"></tbody>
        </table>
      </div>
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
              <th>Label</th>
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

    function renderMarketMaker(marketMaker) {
      const body = document.getElementById("mm-orders");
      body.innerHTML = "";
      if (!marketMaker || !marketMaker.plan || !marketMaker.plan.orders || marketMaker.plan.orders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No market maker plan.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const order of marketMaker.plan.orders) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="${order.side === "buy" ? "side-buy" : "side-sell"}">${order.side.toUpperCase()}</td>
          <td class="num">${order.level}</td>
          <td class="num">${fmt.format(order.price)}</td>
          <td class="num">${compact.format(order.amount)}</td>
          <td class="num">${money.format(order.quote_notional)}</td>
          <td class="num">${order.distance_bps.toFixed(2)} bps</td>
        `;
        body.appendChild(tr);
      }
    }

    function pnlClass(value) {
      if (value == null || Math.abs(value) < 1e-12) return "pnl-flat";
      return value > 0 ? "pnl-positive" : "pnl-negative";
    }

    function setPnl(id, value) {
      const el = document.getElementById(id);
      el.textContent = value == null ? "--" : `$${money.format(value)}`;
      el.className = `value ${pnlClass(value)}`;
    }

    function renderPortfolio(portfolio) {
      if (!portfolio || portfolio.status === "disabled") {
        text("portfolio-position", "--");
        text("portfolio-mark", "--");
        text("portfolio-value", "--");
        setPnl("portfolio-total-pnl", null);
        setPnl("portfolio-mm-pnl", null);
        setPnl("portfolio-arb-pnl", null);
        setPnl("portfolio-price-pnl", null);
        return;
      }

      text("portfolio-position", `${compact.format(portfolio.position_base || 0)} ${portfolio.asset || ""}`);
      text("portfolio-mark", portfolio.mark_price == null ? "--" : `$${fmt.format(portfolio.mark_price)}`);
      text("portfolio-value", portfolio.position_value == null ? "--" : `$${money.format(portfolio.position_value)}`);
      setPnl("portfolio-total-pnl", portfolio.total_pnl);
      setPnl("portfolio-mm-pnl", portfolio.sources?.market_maker);
      setPnl("portfolio-arb-pnl", portfolio.sources?.arbitrage);
      setPnl("portfolio-price-pnl", portfolio.sources?.price_move);
    }

    function shortAddress(address) {
      if (!address || address.length < 12) return address || "--";
      return `${address.slice(0, 6)}...${address.slice(-6)}`;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function renderHolders(onchain) {
      const body = document.getElementById("holders");
      body.innerHTML = "";
      if (!onchain || !onchain.holders || onchain.holders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">No holder data yet.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const holder of onchain.holders) {
        const delta = holder.delta_amount;
        const deltaText = delta == null ? "--" : `${delta >= 0 ? "+" : ""}${compact.format(delta)}`;
        const deltaClass = delta == null ? "" : delta >= 0 ? "ok" : "missing";
        const label = holder.label || "Unknown";
        const labelClass = holder.is_labeled ? "known" : "unknown";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${holder.rank}</td>
          <td><span class="holder-label ${labelClass}" title="${escapeHtml(label)}">${escapeHtml(label)}</span></td>
          <td title="${holder.owner}">${shortAddress(holder.owner)}</td>
          <td class="num">${compact.format(holder.amount)}</td>
          <td class="num">${holder.share_pct == null ? "--" : holder.share_pct.toFixed(4) + "%"}</td>
          <td class="num ${deltaClass}">${deltaText}</td>
          <td class="num">${holder.token_account_count}</td>
        `;
        body.appendChild(tr);
      }
    }

    let programToggleBusy = false;

    async function setProgramRunning(running) {
      if (programToggleBusy) return;
      programToggleBusy = true;
      const toggle = document.getElementById("program-toggle");
      toggle.disabled = true;
      try {
        const res = await fetch("/api/control", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ running }),
        });
        if (!res.ok) throw new Error("control failed");
        await refresh();
      } catch (error) {
        toggle.checked = !running;
      } finally {
        toggle.disabled = false;
        programToggleBusy = false;
      }
    }

    async function refresh() {
      try {
        const res = await fetch("/api/state", { cache: "no-store" });
        const data = await res.json();

        const status = document.getElementById("status");
        status.textContent = data.status || "unknown";
        status.className = `pill ${data.status || "error"}`;
        document.getElementById("program-toggle").checked = data.program?.running !== false;

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
        text("mm-meta", data.market_maker?.plan ? `${data.market_maker.mode || "dry_run"} · ${data.market_maker.plan.exchange} ${data.market_maker.plan.symbol} · mid ${fmt.format(data.market_maker.plan.mid_price)} · spread ${data.market_maker.plan.existing_spread_bps.toFixed(2)} bps` : (data.market_maker?.status || "disabled"));

        renderMarkets(data.markets);
        renderPortfolio(data.portfolio);
        renderMarketMaker(data.market_maker);
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
    document.getElementById("program-toggle").addEventListener("change", (event) => {
      setProgramRunning(event.target.checked);
    });
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
        "market_maker": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "error": None,
        },
        "portfolio": {
            "status": "disabled",
            "asset": cfg.portfolio.asset,
            "quote_currency": cfg.common_quote_currency,
            "position_base": cfg.portfolio.position_base,
            "average_entry_price": cfg.portfolio.average_entry_price,
            "mark_price": None,
            "mark_source_count": 0,
            "position_value": None,
            "total_pnl": 0.0,
            "sources": {
                "market_maker": 0.0,
                "arbitrage": 0.0,
                "price_move": 0.0,
            },
            "observed_at": None,
        },
        "program": {
            "running": True,
            "updated_at": time.time(),
        },
        "warnings": ["Waiting for first scan"],
    }


class MonitorState:
    def __init__(self, cfg: BotConfig, poll_seconds: float) -> None:
        self._lock = asyncio.Lock()
        self._program_running = True
        self._program_updated_at = time.time()
        self._payload = _build_initial_payload(cfg, poll_seconds)
        self._recent_opportunities: deque[dict[str, Any]] = deque(maxlen=100)

    async def get(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._payload))

    async def is_running(self) -> bool:
        async with self._lock:
            return self._program_running

    async def set_running(self, running: bool) -> dict[str, Any]:
        async with self._lock:
            self._program_running = running
            self._program_updated_at = time.time()
            self._payload["program"] = {
                "running": self._program_running,
                "updated_at": self._program_updated_at,
            }
            if running:
                self._payload["status"] = "starting"
                self._payload["warnings"] = ["Resuming scans"]
            else:
                self._payload["status"] = "paused"
                self._payload["warnings"] = ["Program paused"]
            return json.loads(json.dumps(self._payload))

    async def set_paused(self) -> None:
        async with self._lock:
            self._payload["status"] = "paused"
            self._payload["program"] = {
                "running": self._program_running,
                "updated_at": self._program_updated_at,
            }
            self._payload["warnings"] = ["Program paused"]

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
        market_maker: dict[str, Any],
        portfolio: dict[str, Any],
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
                "market_maker": market_maker,
                "portfolio": portfolio,
                "program": {
                    "running": self._program_running,
                    "updated_at": self._program_updated_at,
                },
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
                    "program": {
                        "running": self._program_running,
                        "updated_at": self._program_updated_at,
                    },
                }
            )


def _missing_market_warnings(rows: Iterable[dict[str, Any]]) -> list[str]:
    return [
        f"Missing {row['exchange']} {row['symbol']}"
        for row in rows
        if row["status"] != "ok"
    ]


def build_market_maker_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
) -> dict[str, Any]:
    maker_cfg = cfg.market_maker
    if not maker_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "error": None,
        }

    book = books.get((maker_cfg.exchange, maker_cfg.symbol))
    if book is None:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "error": f"Missing {maker_cfg.exchange} {maker_cfg.symbol}",
        }

    try:
        plan = build_symmetric_market_maker_plan(book, maker_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "error": str(exc),
        }

    return {
        "status": "planned",
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "error": None,
    }


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
    labels = onchain_cfg.address_labels
    for holder in holders:
        previous = previous_amounts.get(holder["owner"])
        holder["delta_amount"] = (
            None if previous is None else holder["amount"] - previous
        )
        label = labels.get(holder["owner"])
        holder["label"] = label or "Unknown"
        holder["is_labeled"] = label is not None

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
    market_maker_payload = _build_initial_payload(cfg, poll_seconds)["market_maker"]
    portfolio_payload = _build_initial_payload(cfg, poll_seconds)["portfolio"]
    previous_onchain_amounts: dict[str, float] = {}
    next_onchain_scan = 0.0
    scan_count = 0
    try:
        while True:
            if not await state.is_running():
                await state.set_paused()
                await asyncio.sleep(0.5)
                continue

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
                    market_maker_payload = build_market_maker_payload(cfg, books)
                    portfolio_payload = build_portfolio_pnl(cfg, books, quote_rates)
                else:
                    opportunities = await scan_with_manager(cfg, strategy, manager)
                    rows = []
                    quote_rates = cfg.quote_rates
                    warnings = []
                    market_maker_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "error": None,
                    }
                    portfolio_payload = _build_initial_payload(cfg, poll_seconds)[
                        "portfolio"
                    ]

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
                if market_maker_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Market maker: {market_maker_payload.get('error')}",
                    ]

                elapsed = time.monotonic() - monotonic_started
                if not await state.is_running():
                    await state.set_paused()
                    continue
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
                    market_maker=market_maker_payload,
                    portfolio=portfolio_payload,
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


async def api_control(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    running = payload.get("running")
    if not isinstance(running, bool):
        return web.json_response({"error": "running must be a boolean"}, status=400)

    return web.json_response(await state.set_running(running))


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
    app.router.add_post("/api/control", api_control)
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
