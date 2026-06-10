from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import Any

from aiohttp import web

from .account_check import _auth_env_status, _balance_currencies, _summarize_balance
from .config import (
    AssetPosition,
    BotConfig,
    ExchangeConfig,
    SlowExecutionConfig,
    SpotMarketConfig,
    load_config,
)
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
from .slow_execution import build_slow_execution_plan
from .solana import SolanaTokenClient, fetch_top_token_owners
from .strategies.spot_spread import find_converted_spot_spread_opportunities
from .trade_log import (
    read_recent_trade_entries,
    summarize_trade_entries,
    write_trade_event,
)


ACCOUNT_BALANCE_POLL_SECONDS = 10.0
ORDER_ACTIVITY_POLL_SECONDS = 5.0
ORDER_ACTIVITY_LIMIT = 20
PNL_SOURCE_LABELS = {
    "market_maker": "Market Maker",
    "arbitrage": "Arbitrage",
    "auto_buy_sell": "Auto Buy/Sell",
    "manual": "Manual",
    "unattributed": "Unattributed",
}


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
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 1px;
      overflow: hidden;
      margin-bottom: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--line);
    }

    .metric {
      min-width: 0;
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
      font-size: 18px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .metric .detail {
      margin-top: 5px;
      overflow: hidden;
      text-overflow: ellipsis;
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

    .stacked-table {
      margin-top: 10px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1080px;
    }

    .balance-table {
      min-width: 680px;
    }

    .orders-table {
      min-width: 1040px;
    }

    .fills-table {
      min-width: 1140px;
    }

    .balance-table th,
    .balance-table td {
      padding: 9px 10px;
      font-size: 12px;
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

    .risk-ok { color: var(--green); font-weight: 700; }
    .risk-blocked { color: var(--red); font-weight: 700; }
    .risk-off { color: var(--muted); font-weight: 700; }

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

    .control-panel {
      display: grid;
      grid-template-columns: repeat(8, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 10px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }

    .field {
      display: grid;
      gap: 5px;
      min-width: 0;
    }

    .field label,
    .check-field {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .field input,
    .field select {
      width: 100%;
      min-height: 34px;
      padding: 6px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfb;
      color: var(--text);
      font: inherit;
      font-size: 13px;
    }

    .check-field {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      align-self: end;
    }

    .account-field {
      grid-column: span 2;
    }

    .account-options {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      min-height: 34px;
      padding: 5px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfb;
    }

    .account-option {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 24px;
      max-width: 100%;
      padding: 3px 8px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--surface);
      font-size: 12px;
      font-weight: 650;
      cursor: pointer;
      user-select: none;
    }

    .account-option input {
      width: 13px;
      height: 13px;
      margin: 0;
      flex: 0 0 auto;
    }

    .account-option span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .account-option:has(input:checked) {
      color: var(--blue);
      border-color: #bfd2ea;
      background: #e8f0fa;
    }

    .control-button {
      min-height: 34px;
      align-self: end;
      border: 1px solid var(--focus);
      border-radius: 6px;
      background: var(--focus);
      color: white;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      cursor: pointer;
    }

    .control-button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }

    .danger-button {
      min-height: 28px;
      padding: 5px 9px;
      border: 1px solid #e6bbb4;
      border-radius: 6px;
      background: #fbe9e6;
      color: var(--red);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      cursor: pointer;
    }

    .danger-button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }

    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; }
      .header-actions { width: 100%; justify-content: space-between; }
      main { padding: 14px; }
      .portfolio-bar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .statusbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .balance-table { min-width: 620px; }
      .orders-table { min-width: 960px; }
      .fills-table { min-width: 1080px; }
      .control-panel { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .account-field { grid-column: 1 / -1; }
      .opportunity { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>ACS Arbitrage Monitor</h1>
      <div class="subtle">Bithumb ACS/KRW · Bybit ACS/USDT · Coinbase ACS/USDC · Upbit ACS/USDT</div>
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
        <div id="portfolio-position-detail" class="subtle detail">--</div>
      </div>
      <div class="metric">
        <div class="label">Cash Position</div>
        <div id="portfolio-cash" class="value">--</div>
        <div id="portfolio-cash-detail" class="subtle detail">--</div>
      </div>
      <div class="metric">
        <div class="label">Balances</div>
        <div id="account-balances-total" class="value">--</div>
        <div id="account-balances-detail" class="subtle detail">--</div>
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
        <div class="label">Auto P/L</div>
        <div id="portfolio-auto-pnl" class="value">--</div>
      </div>
      <div class="metric">
        <div class="label">Other P/L</div>
        <div id="portfolio-other-pnl" class="value">--</div>
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
        <h2>Account Balances</h2>
        <span id="account-balances-meta" class="subtle"></span>
      </div>
      <div class="table-wrap">
        <table class="balance-table">
          <thead>
            <tr>
              <th>Account</th>
              <th>Currency</th>
              <th class="num">Free</th>
              <th class="num">Used</th>
              <th class="num">Total</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="account-balances"></tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-title">
        <h2>Orders & Fills</h2>
        <span id="orders-meta" class="subtle"></span>
      </div>
      <div class="table-wrap">
        <table class="orders-table">
          <thead>
            <tr>
              <th>Account</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Status</th>
              <th class="num">Price</th>
              <th class="num">Amount</th>
              <th class="num">Filled</th>
              <th class="num">Remaining</th>
              <th class="num">Cost</th>
              <th>Updated</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="open-orders"></tbody>
        </table>
      </div>
      <div class="table-wrap stacked-table">
        <table class="fills-table">
          <thead>
            <tr>
              <th>Account</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Source</th>
              <th class="num">Price</th>
              <th class="num">Amount</th>
              <th class="num">Cost</th>
              <th class="num">P/L</th>
              <th>Fee</th>
              <th>Order</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody id="recent-fills"></tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-title">
        <h2>Risk & Events</h2>
        <span id="risk-meta" class="subtle"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Time</th>
              <th>Strategy</th>
              <th>Mode</th>
              <th>Status</th>
              <th>Exchange</th>
              <th>Symbol</th>
              <th>Side</th>
              <th class="num">Orders</th>
              <th class="num">Placed</th>
              <th class="num">Canceled</th>
              <th class="num">Notional</th>
              <th>Risk</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody id="events"></tbody>
        </table>
      </div>
    </section>

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
        <h2>Auto Buy/Sell</h2>
        <span id="slow-meta" class="subtle"></span>
      </div>
      <form id="slow-form" class="control-panel">
        <label class="check-field">
          <input id="slow-enabled" type="checkbox">
          Enabled
        </label>
        <div class="field account-field">
          <label>Account</label>
          <div id="slow-accounts" class="account-options"></div>
        </div>
        <div class="field">
          <label for="slow-side">Side</label>
          <select id="slow-side">
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </div>
        <div class="field">
          <label for="slow-total-base">Total Base</label>
          <input id="slow-total-base" type="number" min="0" step="any">
        </div>
        <div class="field">
          <label for="slow-slice-min">Min/Order</label>
          <input id="slow-slice-min" type="number" min="0" step="any">
        </div>
        <div class="field">
          <label for="slow-slice-max">Max/Order</label>
          <input id="slow-slice-max" type="number" min="0" step="any">
        </div>
        <label class="check-field">
          <input id="slow-randomize" type="checkbox">
          Random
        </label>
        <div class="field">
          <label for="slow-interval">Place Sec</label>
          <input id="slow-interval" type="number" min="1" step="any">
        </div>
        <div class="field">
          <label for="slow-ttl">Cancel Sec</label>
          <input id="slow-ttl" type="number" min="0" step="any">
        </div>
        <div class="field">
          <label for="slow-stop-price">Stop Price</label>
          <input id="slow-stop-price" type="number" min="0" step="any">
        </div>
        <button id="slow-apply" class="control-button" type="submit">Apply</button>
      </form>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Side</th>
              <th>Exchange</th>
              <th>Symbol</th>
              <th class="num">Mid Price</th>
              <th class="num">Slice Amount</th>
              <th class="num">Quote</th>
              <th class="num">Submitted</th>
              <th class="num">Remaining</th>
              <th class="num">Interval</th>
              <th class="num">Cancel</th>
              <th class="num">Stop</th>
            </tr>
          </thead>
          <tbody id="slow-orders"></tbody>
        </table>
      </div>
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
    const shortNumber = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });

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

    function formatBalanceAmount(value) {
      if (value == null) return "--";
      return Math.abs(value) >= 1_000_000 ? shortNumber.format(value) : fmt.format(value);
    }

    function balanceStatusClass(status) {
      return status === "ok" ? "ok" : "missing";
    }

    function sortBalanceCurrencies(rows) {
      const preferredOrder = { ACS: 0, USDC: 1, USDT: 2, USD: 3, KRW: 4 };
      return [...(rows || [])].sort((left, right) => {
        const leftRank = preferredOrder[left.currency] ?? 99;
        const rightRank = preferredOrder[right.currency] ?? 99;
        return leftRank === rightRank
          ? String(left.currency).localeCompare(String(right.currency))
          : leftRank - rightRank;
      });
    }

    function renderAccountBalanceSummary(accountBalances) {
      const totals = sortBalanceCurrencies(accountBalances?.totals || []);
      const valueEl = document.getElementById("account-balances-total");
      const detailEl = document.getElementById("account-balances-detail");
      if (totals.length === 0) {
        valueEl.textContent = "--";
        detailEl.textContent = accountBalances?.status || "--";
        detailEl.title = detailEl.textContent;
        return;
      }

      valueEl.textContent = totals.length === 1
        ? `${formatBalanceAmount(totals[0].total)} ${totals[0].currency}`
        : `${totals.length} currencies`;
      const detail = totals
        .slice(0, 5)
        .map((row) => `${row.currency} ${formatBalanceAmount(row.total)}`)
        .join(" · ");
      detailEl.textContent = detail;
      detailEl.title = totals
        .map((row) => `${row.currency} free ${formatBalanceAmount(row.free)} · used ${formatBalanceAmount(row.used)} · total ${formatBalanceAmount(row.total)}`)
        .join(" | ");
    }

    function renderAccountBalances(accountBalances) {
      renderAccountBalanceSummary(accountBalances);
      text(
        "account-balances-meta",
        accountBalances
          ? `${accountBalances.status || "unknown"} · checked ${accountBalances.checked_account_count || 0}/${accountBalances.total_account_count || 0} · ${formatAge(accountBalances.last_finished)}`
          : ""
      );

      const body = document.getElementById("account-balances");
      body.innerHTML = "";
      const accounts = accountBalances?.accounts || [];
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No account balances yet.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const account of accounts) {
        const rows = sortBalanceCurrencies(account.balance?.currencies || []);
        if (rows.length === 0) {
          const message = account.balance?.error || account.balance?.skipped_reason || "No non-zero target balances.";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.exchange)}</td>
            <td colspan="4">${escapeHtml(message)}</td>
            <td class="${balanceStatusClass(account.status)}">${escapeHtml(account.status || "--")}</td>
          `;
          body.appendChild(tr);
          continue;
        }

        for (const row of rows) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.exchange)}</td>
            <td>${escapeHtml(row.currency)}</td>
            <td class="num">${formatBalanceAmount(row.free)}</td>
            <td class="num">${formatBalanceAmount(row.used)}</td>
            <td class="num">${formatBalanceAmount(row.total)}</td>
            <td class="${balanceStatusClass(account.status)}">${escapeHtml(account.status || "--")}</td>
          `;
          body.appendChild(tr);
        }
      }
    }

    function formatTimestamp(value) {
      if (value == null) return "--";
      const ts = Number(value);
      if (!Number.isFinite(ts)) return "--";
      return new Date(ts).toLocaleString();
    }

    function formatFee(fee) {
      if (!fee) return "--";
      const cost = fee.cost == null ? "--" : formatBalanceAmount(fee.cost);
      return fee.currency ? `${cost} ${fee.currency}` : cost;
    }

    function shortId(value) {
      if (!value) return "--";
      const textValue = String(value);
      return textValue.length > 12 ? `${textValue.slice(0, 8)}...` : textValue;
    }

    function orderSideClass(side) {
      return side === "buy" ? "side-buy" : side === "sell" ? "side-sell" : "";
    }

    function displaySource(value) {
      if (value === "market_maker") return "Market Maker";
      if (value === "arbitrage") return "Arbitrage";
      if (value === "auto_buy_sell" || value === "slow_execution") return "Auto Buy/Sell";
      if (value === "manual") return "Manual";
      if (value === "unattributed") return "Unattributed";
      return value || "--";
    }

    function formatPnlValue(value) {
      return value == null ? "--" : `$${money.format(value)}`;
    }

    let cancelOrderBusy = new Set();

    async function cancelOrder(order, button) {
      const key = `${order.exchange}:${order.symbol}:${order.id}`;
      if (cancelOrderBusy.has(key)) return;
      cancelOrderBusy.add(key);
      button.disabled = true;
      button.textContent = "Canceling";
      try {
        const res = await fetch("/api/orders/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            exchange: order.exchange,
            symbol: order.symbol,
            order_id: order.id,
          }),
        });
        const payload = await res.json();
        if (!res.ok) throw new Error(payload.error || "cancel failed");
        if (payload.order_activity) {
          renderOrderActivity(payload.order_activity);
        }
        await refresh();
      } catch (error) {
        text("orders-meta", `cancel failed: ${error.message || error}`);
        button.disabled = false;
        button.textContent = "Cancel";
      } finally {
        cancelOrderBusy.delete(key);
      }
    }

    function renderOpenOrders(orderActivity) {
      const body = document.getElementById("open-orders");
      body.innerHTML = "";
      const orders = orderActivity?.open_orders || [];
      if (orders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="11">No open orders.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const order of orders) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(order.label || order.exchange)}</td>
          <td>${escapeHtml(order.symbol || "--")}</td>
          <td class="${orderSideClass(order.side)}">${escapeHtml(order.side ? order.side.toUpperCase() : "--")}</td>
          <td>${escapeHtml(order.status || "--")}</td>
          <td class="num">${order.price == null ? "--" : fmt.format(order.price)}</td>
          <td class="num">${formatBalanceAmount(order.amount)}</td>
          <td class="num">${formatBalanceAmount(order.filled)}</td>
          <td class="num">${formatBalanceAmount(order.remaining)}</td>
          <td class="num">${formatBalanceAmount(order.cost)}</td>
          <td>${formatTimestamp(order.timestamp)}</td>
          <td class="order-action"></td>
        `;
        const action = tr.querySelector(".order-action");
        const button = document.createElement("button");
        button.className = "danger-button";
        button.type = "button";
        button.textContent = "Cancel";
        button.disabled = !order.id;
        button.title = order.id || "";
        button.addEventListener("click", () => cancelOrder(order, button));
        action.appendChild(button);
        body.appendChild(tr);
      }
    }

    function renderRecentFills(orderActivity) {
      const body = document.getElementById("recent-fills");
      body.innerHTML = "";
      const fills = orderActivity?.recent_trades || [];
      if (fills.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="11">No recent fills.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const fill of fills) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(fill.label || fill.exchange)}</td>
          <td>${escapeHtml(fill.symbol || "--")}</td>
          <td class="${orderSideClass(fill.side)}">${escapeHtml(fill.side ? fill.side.toUpperCase() : "--")}</td>
          <td>${escapeHtml(fill.source_label || displaySource(fill.source))}</td>
          <td class="num">${fill.price == null ? "--" : fmt.format(fill.price)}</td>
          <td class="num">${formatBalanceAmount(fill.amount)}</td>
          <td class="num">${formatBalanceAmount(fill.cost)}</td>
          <td class="num ${pnlClass(fill.realized_pnl_common)}">${formatPnlValue(fill.realized_pnl_common)}</td>
          <td>${escapeHtml(formatFee(fill.fee))}</td>
          <td title="${escapeHtml(fill.order_id || "")}">${escapeHtml(shortId(fill.order_id))}</td>
          <td>${formatTimestamp(fill.timestamp)}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderOrderActivity(orderActivity) {
      text(
        "orders-meta",
        orderActivity
          ? `${orderActivity.status || "unknown"} · open ${orderActivity.open_order_count || 0} · fills ${orderActivity.recent_trade_count || 0} · P/L ${formatPnlValue(orderActivity.pnl_summary?.total_realized_pnl)} · checked ${orderActivity.checked_account_count || 0}/${orderActivity.total_account_count || 0} · ${formatAge(orderActivity.last_finished)}`
          : ""
      );
      renderOpenOrders(orderActivity);
      renderRecentFills(orderActivity);
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

    function renderOperations(ops) {
      const risk = ops?.risk || {};
      const alerts = ops?.alerts || {};
      const tradeLog = ops?.trade_log || {};
      const summary = tradeLog.summary || {};
      const riskState = risk.enabled === false ? "off" : risk.trading_enabled === false ? "trading off" : risk.allow_live_trading ? "live allowed" : "dry-run guarded";
      text(
        "risk-meta",
        `${riskState} · max/order $${money.format(risk.max_order_quote || 0)} · max/cycle $${money.format(risk.max_cycle_quote || 0)} · open ${risk.max_open_orders || 0} · depth $${money.format(risk.min_order_book_depth_quote || 0)} · slip ${risk.max_slippage_bps || 0} bps · events ${summary.event_count || 0} · blocked ${summary.blocked_event_count || 0} · alerts ${alerts.enabled ? "on" : "off"}`
      );

      const body = document.getElementById("events");
      body.innerHTML = "";
      const events = tradeLog.recent_entries || [];
      if (events.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="14">No trade events yet.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const event of events.slice(0, 20)) {
        const riskClass = event.risk_level === "blocked" ? "risk-blocked" : event.risk_level === "off" ? "risk-off" : "risk-ok";
        const reason = event.reason || "--";
        const eventId = event.event_id || "";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(eventId)}">${escapeHtml(eventId.slice(0, 8) || "--")}</td>
          <td>${formatAge(event.logged_at)}</td>
          <td>${escapeHtml(displayStrategy(event.strategy))}</td>
          <td>${escapeHtml(event.mode || "--")}</td>
          <td>${escapeHtml(event.status || "--")}</td>
          <td>${escapeHtml(event.exchange || "--")}</td>
          <td>${escapeHtml(event.symbol || "--")}</td>
          <td class="${event.side === "buy" ? "side-buy" : event.side === "sell" ? "side-sell" : ""}">${escapeHtml(event.side ? event.side.toUpperCase() : "--")}</td>
          <td class="num">${event.order_count ?? "--"}</td>
          <td class="num">${event.placed_count ?? "--"}</td>
          <td class="num">${event.canceled_count ?? "--"}</td>
          <td class="num">${event.total_quote_notional == null ? "--" : "$" + money.format(event.total_quote_notional)}</td>
          <td class="${riskClass}">${escapeHtml(event.risk_level || "--")}</td>
          <td title="${escapeHtml(reason)}">${escapeHtml(reason)}</td>
        `;
        body.appendChild(tr);
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

    function renderSlowExecution(slowExecution) {
      const body = document.getElementById("slow-orders");
      body.innerHTML = "";
      if (!slowExecution || !slowExecution.plan || !slowExecution.plan.order) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="11">${slowExecution?.status || "disabled"}</td>`;
        body.appendChild(tr);
        return;
      }

      const plan = slowExecution.plan;
      const order = plan.order;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="${order.side === "buy" ? "side-buy" : "side-sell"}">${order.side.toUpperCase()}</td>
        <td>${plan.exchange}</td>
        <td>${plan.symbol}</td>
        <td class="num">${fmt.format(order.price)}</td>
        <td class="num">${compact.format(order.amount)}</td>
        <td class="num">${money.format(order.quote_notional)}</td>
        <td class="num">${compact.format(order.submitted_base_before)} / ${compact.format(plan.total_base)}</td>
        <td class="num">${compact.format(plan.remaining_base)}</td>
        <td class="num">${plan.interval_seconds}s</td>
        <td class="num">${plan.order_ttl_seconds || 0}s</td>
        <td class="num">${plan.stop_price ? fmt.format(plan.stop_price) : "--"}</td>
      `;
      body.appendChild(tr);
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

    function formatPnlSourceDetail(portfolio) {
      const labels = {
        market_maker: "MM",
        arbitrage: "Arb",
        auto_buy_sell: "Auto",
        manual: "Manual",
        unattributed: "Unattributed",
        price_move: "Price",
      };
      return Object.entries(portfolio?.sources || {})
        .filter(([, value]) => value != null && Math.abs(value) >= 1e-12)
        .map(([key, value]) => `${labels[key] || key}: ${formatPnlValue(value)}`)
        .join(" | ");
    }

    function formatCashDetail(portfolio) {
      const balances = portfolio?.cash_balances || {};
      const preferredOrder = { USDC: 0, USDT: 1, USD: 2, KRW: 3 };
      const pieces = Object.entries(balances)
        .sort(([left], [right]) => {
          const leftRank = preferredOrder[left] ?? 99;
          const rightRank = preferredOrder[right] ?? 99;
          return leftRank === rightRank ? left.localeCompare(right) : leftRank - rightRank;
        })
        .map(([currency, amount]) => `${currency} ${compact.format(amount || 0)}`);
      const missing = portfolio?.cash_missing_rates || [];
      if (missing.length > 0) {
        pieces.push(`missing ${missing.join("/")}`);
      }
      return pieces.length === 0 ? "--" : pieces.join(" · ");
    }

    function formatPositionDetail(portfolio) {
      const positions = portfolio?.positions || [];
      if (positions.length === 0) {
        return portfolio?.asset ? `${compact.format(portfolio.position_base || 0)} ${portfolio.asset}` : "--";
      }
      return positions
        .map((position) => `${position.asset} ${compact.format(position.position_base || 0)}`)
        .join(" · ");
    }

    function formatMarkDetail(portfolio) {
      return (portfolio?.positions || [])
        .map((position) => {
          const mark = position.mark_price == null ? "--" : `$${fmt.format(position.mark_price)}`;
          return `${position.asset} ${mark}`;
        })
        .join(" · ");
    }

    function renderPortfolio(portfolio) {
      if (!portfolio || portfolio.status === "disabled") {
        text("portfolio-position", "--");
        text("portfolio-position-detail", "--");
        text("portfolio-cash", "--");
        text("portfolio-cash-detail", "--");
        text("portfolio-mark", "--");
        text("portfolio-value", "--");
        setPnl("portfolio-total-pnl", null);
        setPnl("portfolio-mm-pnl", null);
        setPnl("portfolio-arb-pnl", null);
        setPnl("portfolio-auto-pnl", null);
        setPnl("portfolio-other-pnl", null);
        setPnl("portfolio-price-pnl", null);
        document.getElementById("portfolio-total-pnl").title = "";
        return;
      }

      const positions = portfolio.positions || [];
      const positionDetail = formatPositionDetail(portfolio);
      if (positions.length > 1) {
        text("portfolio-position", `${positions.length} assets`);
        text("portfolio-position-detail", positionDetail);
      } else {
        text("portfolio-position", `${compact.format(portfolio.position_base || 0)} ${portfolio.asset || ""}`);
        text("portfolio-position-detail", "--");
      }
      document.getElementById("portfolio-position-detail").title = positionDetail;
      const cashValue = portfolio.cash_value == null ? null : portfolio.cash_value;
      text("portfolio-cash", cashValue == null ? "--" : `$${money.format(cashValue)}`);
      const cashDetail = formatCashDetail(portfolio);
      text("portfolio-cash-detail", cashDetail);
      document.getElementById("portfolio-cash-detail").title = cashDetail;
      const markDetail = formatMarkDetail(portfolio);
      text(
        "portfolio-mark",
        positions.length > 1
          ? "Mixed"
          : portfolio.mark_price == null ? "--" : `$${fmt.format(portfolio.mark_price)}`
      );
      document.getElementById("portfolio-mark").title = markDetail || "";
      text("portfolio-value", portfolio.position_value == null ? "--" : `$${money.format(portfolio.position_value)}`);
      setPnl("portfolio-total-pnl", portfolio.total_pnl);
      setPnl("portfolio-mm-pnl", portfolio.sources?.market_maker);
      setPnl("portfolio-arb-pnl", portfolio.sources?.arbitrage);
      setPnl("portfolio-auto-pnl", portfolio.sources?.auto_buy_sell);
      setPnl(
        "portfolio-other-pnl",
        (portfolio.sources?.manual || 0) + (portfolio.sources?.unattributed || 0)
      );
      setPnl("portfolio-price-pnl", portfolio.sources?.price_move);
      document.getElementById("portfolio-total-pnl").title = formatPnlSourceDetail(portfolio);
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

    function displayStrategy(value) {
      if (value === "slow_execution") return "Auto Buy/Sell";
      if (value === "market_maker") return "Market Maker";
      return value || "--";
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
    let slowFormDirty = false;
    let slowFormBusy = false;

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

    function numericValue(id) {
      const value = document.getElementById(id).value;
      if (value === "") return 0;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function setNumericField(id, value) {
      document.getElementById(id).value = value == null ? "" : String(value);
    }

    function selectedSlowAccount() {
      return document.querySelector('input[name="slow-account"]:checked')?.value || "";
    }

    function renderSlowExecutionAccounts(accounts, selectedExchange) {
      const body = document.getElementById("slow-accounts");
      const list = Array.isArray(accounts) ? accounts : [];
      const signature = JSON.stringify({
        accounts: list.map((account) => [account.key, account.label, account.id, account.market_type]),
        selectedExchange,
      });
      if (body.dataset.signature === signature) return;
      body.dataset.signature = signature;
      body.innerHTML = "";
      if (list.length === 0) {
        const empty = document.createElement("span");
        empty.className = "subtle";
        empty.textContent = "No accounts";
        body.appendChild(empty);
        return;
      }

      for (const account of list) {
        const label = document.createElement("label");
        label.className = "account-option";
        label.title = `${account.id || account.key} · ${account.market_type || "spot"}`;
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = "slow-account";
        checkbox.value = account.key;
        checkbox.checked = account.key === selectedExchange;
        checkbox.addEventListener("change", (event) => {
          if (event.target.checked) {
            document.querySelectorAll('input[name="slow-account"]').forEach((item) => {
              if (item !== event.target) item.checked = false;
            });
          } else if (!selectedSlowAccount()) {
            event.target.checked = true;
          }
          slowFormDirty = true;
        });
        const textNode = document.createElement("span");
        textNode.textContent = account.label || account.key;
        label.appendChild(checkbox);
        label.appendChild(textNode);
        body.appendChild(label);
      }
    }

    function renderSlowExecutionConfig(config, accounts) {
      if (!config || slowFormDirty || slowFormBusy) return;
      document.getElementById("slow-enabled").checked = Boolean(config.enabled);
      renderSlowExecutionAccounts(config.accounts || accounts, config.exchange || "");
      document.getElementById("slow-side").value = config.side || "sell";
      setNumericField("slow-total-base", config.total_base || 0);
      setNumericField("slow-slice-min", config.slice_base_min || config.slice_base || 0);
      setNumericField("slow-slice-max", config.slice_base_max || config.slice_base || 0);
      document.getElementById("slow-randomize").checked = Boolean(config.randomize_slice);
      setNumericField("slow-interval", config.interval_seconds || 60);
      setNumericField("slow-ttl", config.order_ttl_seconds || 0);
      setNumericField("slow-stop-price", config.stop_price || 0);
    }

    async function applySlowExecutionConfig(event) {
      event.preventDefault();
      if (slowFormBusy) return;
      slowFormBusy = true;
      const button = document.getElementById("slow-apply");
      button.disabled = true;
      const payload = {
        enabled: document.getElementById("slow-enabled").checked,
        exchange: selectedSlowAccount(),
        side: document.getElementById("slow-side").value,
        total_base: numericValue("slow-total-base"),
        slice_base_min: numericValue("slow-slice-min"),
        slice_base_max: numericValue("slow-slice-max"),
        randomize_slice: document.getElementById("slow-randomize").checked,
        interval_seconds: numericValue("slow-interval"),
        order_ttl_seconds: numericValue("slow-ttl"),
        stop_price: numericValue("slow-stop-price"),
      };
      try {
        const res = await fetch("/api/auto-buy-sell", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error("auto buy/sell update failed");
        slowFormDirty = false;
        await refresh();
      } finally {
        button.disabled = false;
        slowFormBusy = false;
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
        text("slow-meta", data.slow_execution?.plan ? `${data.slow_execution.mode || "dry_run"} · ${data.slow_execution.plan.exchange} ${data.slow_execution.plan.symbol} · ${data.slow_execution.plan.side.toUpperCase()} · mid ${fmt.format(data.slow_execution.plan.mid_price)}` : (data.slow_execution?.status || "disabled"));

        renderOperations(data.operations);
        renderSlowExecutionConfig(data.slow_execution?.config, data.slow_execution?.accounts);
        renderMarkets(data.markets);
        renderAccountBalances(data.account_balances);
        renderOrderActivity(data.order_activity);
        renderPortfolio(data.portfolio);
        renderMarketMaker(data.market_maker);
        renderSlowExecution(data.slow_execution);
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
    document.getElementById("slow-form").addEventListener("input", () => {
      slowFormDirty = true;
    });
    document.getElementById("slow-form").addEventListener("submit", applySlowExecutionConfig);
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


def slow_execution_config_to_dict(cfg: SlowExecutionConfig) -> dict[str, Any]:
    return asdict(cfg)


def build_operations_payload(cfg: BotConfig) -> dict[str, Any]:
    try:
        recent_entries = read_recent_trade_entries(cfg.trade_log)
        trade_log_error = None
    except OSError as exc:
        recent_entries = []
        trade_log_error = str(exc)
    trade_log_payload = asdict(cfg.trade_log)
    trade_log_payload["recent_entries"] = [
        entry.to_dict() for entry in recent_entries
    ]
    trade_log_payload["recent_events"] = [
        entry.raw for entry in recent_entries
    ]
    trade_log_payload["summary"] = summarize_trade_entries(recent_entries)
    trade_log_payload["error"] = trade_log_error
    return {
        "risk": asdict(cfg.risk),
        "alerts": asdict(cfg.alerts),
        "trade_log": trade_log_payload,
    }


def slow_execution_accounts(exchanges: Iterable[ExchangeConfig]) -> list[dict[str, str]]:
    return [
        {
            "key": exchange.key,
            "label": exchange.key,
            "id": exchange.id,
            "market_type": exchange.market_type,
        }
        for exchange in exchanges
    ]


def _exchange_balance_symbols(
    cfg: BotConfig,
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, list[str]]:
    symbols: dict[str, set[str]] = {}
    for market in cfg.spot_markets:
        symbols.setdefault(market.exchange, set()).add(market.symbol)

    if cfg.market_maker.exchange and cfg.market_maker.symbol:
        symbols.setdefault(cfg.market_maker.exchange, set()).add(cfg.market_maker.symbol)

    runtime_exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    if runtime_exec_cfg.exchange and runtime_exec_cfg.symbol:
        symbols.setdefault(runtime_exec_cfg.exchange, set()).add(runtime_exec_cfg.symbol)

    return {exchange: sorted(items) for exchange, items in symbols.items()}


def _all_account_exchanges(cfg: BotConfig) -> list[ExchangeConfig]:
    return [*cfg.spot_exchanges, *cfg.derivative_exchanges]


def _account_balance_status(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
        return "warning"
    if any(account["status"] == "error" for account in accounts):
        return "error"
    if any(account["status"] == "warning" for account in accounts):
        return "warning"
    return "ok"


async def _fetch_exchange_balance_payload(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
) -> dict[str, Any]:
    auth = _auth_env_status(exchange)
    account: dict[str, Any] = {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": exchange.id,
        "market_type": exchange.market_type,
        "symbols": symbols,
        "auth": {
            "configured": auth["configured"],
            "private_checks_enabled": auth["private_checks_enabled"],
            "missing_env": auth["missing_env"],
        },
        "status": "ok",
        "warnings": [],
        "errors": [],
        "balance": {
            "checked": False,
            "skipped_reason": None,
            "currencies": [],
        },
    }

    if not auth["configured"]:
        account["status"] = "warning"
        account["warnings"].append("API env vars are not configured")
        account["balance"]["skipped_reason"] = "api env vars not configured"
        return account
    if auth["missing_env"]:
        account["status"] = "warning"
        account["warnings"].append("one or more configured API env vars are not set")
        account["balance"]["skipped_reason"] = "api env vars missing"
        return account

    try:
        balance = await manager.fetch_balance(exchange)
    except Exception as exc:  # noqa: BLE001
        message = f"{exc.__class__.__name__}: {exc}"
        account["status"] = "error"
        account["errors"].append(message)
        account["balance"] = {
            "checked": True,
            "error": message,
            "currencies": [],
        }
        return account

    currencies = _summarize_balance(
        balance,
        _balance_currencies(symbols),
        include_zero=False,
    )
    account["balance"] = {
        "checked": True,
        "currencies": currencies,
    }
    return account


def _aggregate_account_balance_totals(
    accounts: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if not account.get("balance", {}).get("checked"):
            continue
        for row in account.get("balance", {}).get("currencies", []):
            currency = str(row["currency"]).upper()
            total_row = totals.setdefault(
                currency,
                {
                    "currency": currency,
                    "free": 0.0,
                    "used": 0.0,
                    "total": 0.0,
                },
            )
            for field in ("free", "used", "total"):
                value = row.get(field)
                if value is not None:
                    total_row[field] += float(value)

    preferred = {"ACS": 0, "USDC": 1, "USDT": 2, "USD": 3, "KRW": 4}
    return sorted(
        totals.values(),
        key=lambda row: (preferred.get(row["currency"], 99), row["currency"]),
    )


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_fee_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    fee = raw.get("fee")
    if not isinstance(fee, dict):
        return None
    cost = _number_or_none(fee.get("cost"))
    currency = fee.get("currency")
    if cost is None and currency is None:
        return None
    return {
        "cost": cost,
        "currency": str(currency) if currency is not None else "",
    }


def _normalize_order(
    exchange: ExchangeConfig,
    raw: dict[str, Any],
    fallback_symbol: str,
) -> dict[str, Any]:
    price = _number_or_none(raw.get("price"))
    amount = _number_or_none(raw.get("amount"))
    filled = _number_or_none(raw.get("filled"))
    remaining = _number_or_none(raw.get("remaining"))
    cost = _number_or_none(raw.get("cost"))
    if cost is None and price is not None and amount is not None:
        cost = price * amount
    return {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": str(raw.get("id", "")),
        "client_order_id": str(
            raw.get("clientOrderId") or raw.get("clientOrderID") or ""
        ),
        "symbol": str(raw.get("symbol") or fallback_symbol),
        "side": str(raw.get("side") or ""),
        "type": str(raw.get("type") or ""),
        "status": str(raw.get("status") or ""),
        "price": price,
        "average": _number_or_none(raw.get("average")),
        "amount": amount,
        "filled": filled,
        "remaining": remaining,
        "cost": cost,
        "fee": _order_fee_payload(raw),
        "timestamp": _number_or_none(raw.get("timestamp")),
        "datetime": raw.get("datetime"),
    }


def _normalize_trade(
    exchange: ExchangeConfig,
    raw: dict[str, Any],
    fallback_symbol: str,
) -> dict[str, Any]:
    return {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
        "id": str(raw.get("id", "")),
        "order_id": str(raw.get("order") or ""),
        "symbol": str(raw.get("symbol") or fallback_symbol),
        "side": str(raw.get("side") or ""),
        "type": str(raw.get("type") or ""),
        "price": _number_or_none(raw.get("price")),
        "amount": _number_or_none(raw.get("amount")),
        "cost": _number_or_none(raw.get("cost")),
        "fee": _order_fee_payload(raw),
        "timestamp": _number_or_none(raw.get("timestamp")),
        "datetime": raw.get("datetime"),
    }


def _activity_status(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
        return "warning"
    if any(account["status"] == "error" for account in accounts):
        return "error"
    if any(account["status"] == "warning" for account in accounts):
        return "warning"
    return "ok"


async def _fetch_exchange_order_activity(
    manager: ExchangeManager,
    exchange: ExchangeConfig,
    symbols: list[str],
    *,
    limit: int,
) -> dict[str, Any]:
    auth = _auth_env_status(exchange)
    account: dict[str, Any] = {
        "exchange": exchange.key,
        "label": exchange.label or exchange.key,
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
    if not symbols:
        account["status"] = "warning"
        account["warnings"].append("no configured symbols")
        return account
    if not auth["configured"]:
        account["status"] = "warning"
        account["warnings"].append("API env vars are not configured")
        return account
    if auth["missing_env"]:
        account["status"] = "warning"
        account["warnings"].append("one or more configured API env vars are not set")
        return account

    for symbol in symbols:
        try:
            open_orders = await manager.fetch_open_orders(exchange, symbol=symbol)
            account["open_orders"].extend(
                _normalize_order(exchange, order, symbol) for order in open_orders
            )
        except Exception as exc:  # noqa: BLE001
            account["errors"].append(
                f"{symbol} open orders failed: {exc.__class__.__name__}: {exc}"
            )

        try:
            closed_orders = await manager.fetch_closed_orders(
                exchange,
                symbol=symbol,
                limit=limit,
            )
            account["closed_orders"].extend(
                _normalize_order(exchange, order, symbol) for order in closed_orders
            )
        except Exception as exc:  # noqa: BLE001
            account["warnings"].append(
                f"{symbol} closed orders unavailable: {exc.__class__.__name__}: {exc}"
            )

        try:
            trades = await manager.fetch_my_trades(
                exchange,
                symbol=symbol,
                limit=limit,
            )
            account["recent_trades"].extend(
                _normalize_trade(exchange, trade, symbol) for trade in trades
            )
        except Exception as exc:  # noqa: BLE001
            account["warnings"].append(
                f"{symbol} fills unavailable: {exc.__class__.__name__}: {exc}"
            )

    if account["errors"]:
        account["status"] = "error"
    elif account["warnings"]:
        account["status"] = "warning"
    account["open_order_count"] = len(account["open_orders"])
    account["closed_order_count"] = len(account["closed_orders"])
    account["recent_trade_count"] = len(account["recent_trades"])
    return account


def _sort_activity_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: float(row.get("timestamp") or 0),
        reverse=True,
    )


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    quote = quote.partition(":")[0]
    return base.upper(), quote.upper()


def _source_for_strategy(strategy: str, event_type: str = "") -> str:
    key = (strategy or event_type or "").lower()
    if key == "market_maker":
        return "market_maker"
    if key in {"slow_execution", "auto_buy_sell", "slow_execution_cancel"}:
        return "auto_buy_sell"
    if key in {"arbitrage", "spot_spread", "spot-spread", "cash_and_carry"}:
        return "arbitrage"
    if key.startswith("manual"):
        return "manual"
    return "unattributed"


def _pnl_source_row(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "label": PNL_SOURCE_LABELS.get(source, source),
        "trade_count": 0,
        "notional_common": 0.0,
        "fees_common": 0.0,
        "realized_pnl": 0.0,
    }


def _attribution_keys(
    exchange: str,
    symbol: str,
    order_id: str,
) -> list[str]:
    if not order_id:
        return []
    keys = []
    if exchange and symbol:
        keys.append(f"{exchange}|{symbol}|{order_id}")
    keys.append(order_id)
    return keys


def build_order_attribution_map(entries: Iterable[Any]) -> dict[str, dict[str, Any]]:
    attribution: dict[str, dict[str, Any]] = {}
    for entry in entries:
        source = _source_for_strategy(
            getattr(entry, "strategy", ""),
            getattr(entry, "event_type", ""),
        )
        row = {
            "source": source,
            "source_label": PNL_SOURCE_LABELS.get(source, source),
            "strategy": getattr(entry, "strategy", ""),
            "event_type": getattr(entry, "event_type", ""),
            "event_id": getattr(entry, "event_id", ""),
            "mode": getattr(entry, "mode", ""),
            "logged_at": getattr(entry, "logged_at", None),
        }
        exchange = getattr(entry, "exchange", "")
        symbol = getattr(entry, "symbol", "")
        for order_id in getattr(entry, "placed_order_ids", []) or []:
            for key in _attribution_keys(exchange, symbol, str(order_id)):
                attribution.setdefault(key, row)
    return attribution


def _trade_attribution(
    trade: dict[str, Any],
    attribution: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for key in _attribution_keys(
        str(trade.get("exchange") or ""),
        str(trade.get("symbol") or ""),
        str(trade.get("order_id") or ""),
    ):
        if key in attribution:
            return attribution[key]
    return None


def _mark_prices_by_asset(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
) -> dict[str, float]:
    marks: dict[str, list[float]] = {}
    for market in cfg.spot_markets:
        book = books.get((market.exchange, market.symbol))
        rate = quote_rates.get(market.quote_currency)
        if book is None or rate is None or not book.bids or not book.asks:
            continue
        bid = book.bids[0].price
        ask = book.asks[0].price
        if bid <= 0 or ask <= 0 or bid >= ask:
            continue
        marks.setdefault(market.asset.upper(), []).append((bid + ask) / 2 * rate)
    return {
        asset: sum(values) / len(values)
        for asset, values in marks.items()
        if values
    }


def _fee_common_value(
    fee: dict[str, Any] | None,
    *,
    quote_rates: dict[str, float],
    mark_prices: dict[str, float],
) -> tuple[float | None, str | None]:
    if not fee:
        return 0.0, None
    cost = _number_or_none(fee.get("cost"))
    if cost is None:
        return 0.0, None
    currency = str(fee.get("currency") or "").upper()
    if not currency:
        return cost, None
    rate = quote_rates.get(currency)
    if rate is not None:
        return cost * rate, None
    mark = mark_prices.get(currency)
    if mark is not None:
        return cost * mark, None
    return None, currency


def enrich_recent_trades_with_pnl(
    cfg: BotConfig,
    trades: Iterable[dict[str, Any]],
    *,
    quote_rates: dict[str, float],
    books: dict[tuple[str, str], OrderBookSnapshot],
    attribution: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    attribution = attribution or {}
    average_prices = _configured_average_entry_prices(cfg)
    mark_prices = _mark_prices_by_asset(cfg, books, quote_rates)
    source_rows = {
        source: _pnl_source_row(source)
        for source in PNL_SOURCE_LABELS
    }
    missing_cost_basis: set[str] = set()
    missing_quote_rates: set[str] = set()
    missing_fee_rates: set[str] = set()
    enriched: list[dict[str, Any]] = []

    for trade in trades:
        row = dict(trade)
        match = _trade_attribution(row, attribution)
        source = match["source"] if match is not None else "unattributed"
        base, quote = _symbol_base_quote(str(row.get("symbol") or ""))
        side = str(row.get("side") or "").lower()
        price = _number_or_none(row.get("price"))
        amount = _number_or_none(row.get("amount"))
        cost = _number_or_none(row.get("cost"))
        if cost is None and price is not None and amount is not None:
            cost = price * amount
            row["cost"] = cost

        quote_rate = quote_rates.get(quote) if quote else None
        if quote and quote_rate is None:
            missing_quote_rates.add(quote)
        notional_common = (
            cost * quote_rate
            if cost is not None and quote_rate is not None
            else None
        )
        fee_common, missing_fee_currency = _fee_common_value(
            row.get("fee"),
            quote_rates=quote_rates,
            mark_prices=mark_prices,
        )
        if missing_fee_currency:
            missing_fee_rates.add(missing_fee_currency)

        realized_pnl: float | None = None
        fee_for_pnl = fee_common or 0.0
        if (
            side == "sell"
            and price is not None
            and amount is not None
            and quote_rate is not None
        ):
            average_entry = average_prices.get(base, 0.0)
            if average_entry > 0:
                realized_pnl = (
                    price * quote_rate - average_entry
                ) * amount - fee_for_pnl
            else:
                missing_cost_basis.add(base or row.get("symbol") or "")
                realized_pnl = -fee_for_pnl
        elif fee_common is not None:
            realized_pnl = -fee_common

        source_row = source_rows.setdefault(source, _pnl_source_row(source))
        source_row["trade_count"] += 1
        if notional_common is not None:
            source_row["notional_common"] += notional_common
        if fee_common is not None:
            source_row["fees_common"] += fee_common
        if realized_pnl is not None:
            source_row["realized_pnl"] += realized_pnl

        row.update(
            {
                "source": source,
                "source_label": PNL_SOURCE_LABELS.get(source, source),
                "attribution": match,
                "base_currency": base,
                "quote_currency": quote,
                "notional_common": notional_common,
                "fee_common": fee_common,
                "realized_pnl_common": realized_pnl,
            }
        )
        enriched.append(row)

    active_sources = {
        source: row
        for source, row in source_rows.items()
        if row["trade_count"] > 0 or abs(row["realized_pnl"]) >= 1e-12
    }
    total_realized = sum(row["realized_pnl"] for row in active_sources.values())
    total_fees = sum(row["fees_common"] for row in active_sources.values())
    total_notional = sum(row["notional_common"] for row in active_sources.values())
    summary = {
        "currency": cfg.common_quote_currency,
        "window": "recent_fills",
        "trade_count": len(enriched),
        "attributed_trade_count": sum(
            1 for row in enriched if row["source"] != "unattributed"
        ),
        "unattributed_trade_count": sum(
            1 for row in enriched if row["source"] == "unattributed"
        ),
        "total_realized_pnl": total_realized,
        "total_fees": total_fees,
        "total_notional": total_notional,
        "sources": active_sources,
        "missing_cost_basis": sorted(item for item in missing_cost_basis if item),
        "missing_quote_rates": sorted(missing_quote_rates),
        "missing_fee_rates": sorted(missing_fee_rates),
        "observed_at": time.time(),
    }
    return enriched, summary


async def fetch_order_activity_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    exec_cfg: SlowExecutionConfig | None = None,
    *,
    limit: int = ORDER_ACTIVITY_LIMIT,
    quote_rates: dict[str, float] | None = None,
    books: dict[tuple[str, str], OrderBookSnapshot] | None = None,
) -> dict[str, Any]:
    quote_rates = cfg.quote_rates if quote_rates is None else quote_rates
    books = {} if books is None else books
    try:
        recent_log_entries = read_recent_trade_entries(cfg.trade_log)
        attribution_warnings: list[str] = []
    except OSError as exc:
        recent_log_entries = []
        attribution_warnings = [f"trade log attribution unavailable: {exc}"]
    order_attribution = build_order_attribution_map(recent_log_entries)
    symbols_by_exchange = _exchange_balance_symbols(cfg, exec_cfg)
    exchanges = _all_account_exchanges(cfg)
    accounts = await asyncio.gather(
        *[
            _fetch_exchange_order_activity(
                manager,
                exchange,
                symbols_by_exchange.get(exchange.key, []),
                limit=limit,
            )
            for exchange in exchanges
        ]
    )
    open_orders = _sort_activity_rows(
        order for account in accounts for order in account["open_orders"]
    )
    closed_orders = _sort_activity_rows(
        order for account in accounts for order in account["closed_orders"]
    )[:limit]
    recent_trades = _sort_activity_rows(
        trade for account in accounts for trade in account["recent_trades"]
    )[:limit]
    open_orders = [
        {
            **order,
            "attribution": _trade_attribution(
                {
                    "exchange": order["exchange"],
                    "symbol": order["symbol"],
                    "order_id": order["id"],
                },
                order_attribution,
            ),
        }
        for order in open_orders
    ]
    recent_trades, pnl_summary = enrich_recent_trades_with_pnl(
        cfg,
        recent_trades,
        quote_rates=quote_rates,
        books=books,
        attribution=order_attribution,
    )
    errors = [
        f"{account['exchange']}: {error}"
        for account in accounts
        for error in account.get("errors", [])
    ]
    warnings = [
        f"{account['exchange']}: {warning}"
        for account in accounts
        for warning in account.get("warnings", [])
    ]
    warnings.extend(attribution_warnings)
    checked_accounts = sum(
        1
        for account in accounts
        if account.get("open_order_count") is not None and not account.get("errors")
    )
    return {
        "status": _activity_status(accounts),
        "accounts": accounts,
        "open_orders": open_orders,
        "closed_orders": closed_orders,
        "recent_trades": recent_trades,
        "pnl_summary": pnl_summary,
        "open_order_count": len(open_orders),
        "closed_order_count": len(closed_orders),
        "recent_trade_count": len(recent_trades),
        "checked_account_count": checked_accounts,
        "total_account_count": len(accounts),
        "last_finished": time.time(),
        "errors": errors,
        "warnings": warnings,
    }


def _find_exchange_by_key(cfg: BotConfig, key: str) -> ExchangeConfig:
    for exchange in _all_account_exchanges(cfg):
        if exchange.key == key:
            return exchange
    raise ValueError(f"unknown exchange account: {key}")


async def cancel_order_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    payload: dict[str, Any],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    exchange_key = str(payload.get("exchange", "")).strip()
    symbol = str(payload.get("symbol", "")).strip()
    order_id = str(payload.get("order_id", "")).strip()
    if not exchange_key:
        raise ValueError("exchange is required")
    if not symbol:
        raise ValueError("symbol is required")
    if not order_id:
        raise ValueError("order_id is required")

    exchange = _find_exchange_by_key(cfg, exchange_key)
    allowed_symbols = set(_exchange_balance_symbols(cfg, exec_cfg).get(exchange.key, []))
    if symbol not in allowed_symbols:
        raise ValueError(f"symbol is not configured for account: {symbol}")
    auth = _auth_env_status(exchange)
    if not auth["configured"]:
        raise ValueError("API env vars are not configured for this exchange")
    if auth["missing_env"]:
        raise ValueError("one or more configured API env vars are not set")

    canceled = await manager.cancel_order(
        exchange,
        symbol=symbol,
        order_id=order_id,
    )
    cancel_summary = (
        _normalize_order(exchange, canceled, symbol)
        if isinstance(canceled, dict)
        else {"id": order_id, "status": str(canceled), "symbol": symbol}
    )
    event = write_trade_event(
        cfg.trade_log,
        {
            "type": "manual_order_cancel",
            "strategy": "manual",
            "mode": "live",
            "status": "canceled",
            "plan": {
                "exchange": exchange.key,
                "symbol": symbol,
                "side": "",
            },
            "execution": {
                "canceled_count": 1,
                "placed_count": 0,
                "placed_order_ids": [],
                "canceled_order_ids": [order_id],
            },
            "risk": {
                "approved": True,
                "level": "manual",
                "reasons": [],
                "warnings": [],
                "order_count": 0,
                "total_quote_notional": 0.0,
            },
            "cancel_result": cancel_summary,
        },
    )
    return {
        "ok": True,
        "exchange": exchange.key,
        "symbol": symbol,
        "order_id": order_id,
        "canceled": cancel_summary,
        "event": event,
    }


def _configured_average_entry_prices(cfg: BotConfig) -> dict[str, float]:
    prices: dict[str, float] = {}
    if cfg.portfolio.asset:
        prices[cfg.portfolio.asset.upper()] = cfg.portfolio.average_entry_price
    for position in cfg.portfolio.positions:
        prices[position.asset.upper()] = position.average_entry_price
    return prices


def _configured_position_assets(cfg: BotConfig) -> set[str]:
    assets = {market.asset.upper() for market in cfg.spot_markets}
    if cfg.portfolio.asset:
        assets.add(cfg.portfolio.asset.upper())
    assets.update(position.asset.upper() for position in cfg.portfolio.positions)
    return assets


def _account_balance_totals_by_currency(
    account_balances: dict[str, Any],
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in account_balances.get("totals", []):
        currency = str(row.get("currency", "")).upper()
        if not currency:
            continue
        value = row.get("total")
        if value is not None:
            totals[currency] = float(value)
    return totals


def _apply_order_activity_pnl(
    payload: dict[str, Any],
    order_activity: dict[str, Any] | None,
) -> dict[str, Any]:
    sources = {
        str(source): float(value or 0.0)
        for source, value in (payload.get("sources") or {}).items()
    }
    for source in (
        "market_maker",
        "arbitrage",
        "auto_buy_sell",
        "manual",
        "unattributed",
        "price_move",
    ):
        sources.setdefault(source, 0.0)
    payload["sources"] = sources

    summary = (order_activity or {}).get("pnl_summary")
    if not isinstance(summary, dict):
        return payload

    for source, row in (summary.get("sources") or {}).items():
        if not isinstance(row, dict):
            continue
        realized_pnl = _number_or_none(row.get("realized_pnl"))
        if realized_pnl is None:
            continue
        source_key = str(source)
        sources[source_key] = sources.get(source_key, 0.0) + realized_pnl

    payload["sources"] = sources
    payload["total_pnl"] = sum(sources.values())
    payload["fill_pnl_summary"] = summary
    payload["fill_pnl_window"] = summary.get("window")
    payload["fill_pnl_observed_at"] = summary.get("observed_at")
    return payload


def build_synced_portfolio_pnl(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    quote_rates: dict[str, float],
    account_balances: dict[str, Any],
    order_activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if int(account_balances.get("checked_account_count", 0) or 0) <= 0:
        payload = build_portfolio_pnl(cfg, books, quote_rates)
        payload["balance_source"] = "configured"
        return _apply_order_activity_pnl(payload, order_activity)

    totals_by_currency = _account_balance_totals_by_currency(account_balances)
    position_assets = _configured_position_assets(cfg)
    average_prices = _configured_average_entry_prices(cfg)
    positions = [
        AssetPosition(
            asset=asset,
            position_base=totals_by_currency.get(asset, 0.0),
            average_entry_price=average_prices.get(asset, 0.0),
        )
        for asset in sorted(position_assets)
    ]
    cash_balances = {
        currency: amount
        for currency, amount in sorted(totals_by_currency.items())
        if currency not in position_assets
    }
    live_portfolio = replace(
        cfg.portfolio,
        enabled=True,
        positions=positions,
        cash_balances=cash_balances,
    )
    payload = build_portfolio_pnl(
        replace(cfg, portfolio=live_portfolio),
        books,
        quote_rates,
    )
    payload["balance_source"] = "live_accounts"
    payload["balance_status"] = account_balances.get("status")
    payload["balance_observed_at"] = account_balances.get("last_finished")
    return _apply_order_activity_pnl(payload, order_activity)


async def fetch_account_balances_payload(
    cfg: BotConfig,
    manager: ExchangeManager,
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    symbols_by_exchange = _exchange_balance_symbols(cfg, exec_cfg)
    exchanges = _all_account_exchanges(cfg)
    accounts = await asyncio.gather(
        *[
            _fetch_exchange_balance_payload(
                manager,
                exchange,
                symbols_by_exchange.get(exchange.key, []),
            )
            for exchange in exchanges
        ]
    )
    errors = [
        f"{account['exchange']}: {error}"
        for account in accounts
        for error in account.get("errors", [])
    ]
    return {
        "status": _account_balance_status(accounts),
        "accounts": accounts,
        "totals": _aggregate_account_balance_totals(accounts),
        "checked_account_count": sum(
            1 for account in accounts if account.get("balance", {}).get("checked")
        ),
        "total_account_count": len(accounts),
        "last_finished": time.time(),
        "errors": errors,
    }


def _slow_execution_overrides_from_payload(
    payload: dict[str, Any],
    allowed_exchanges: set[str] | None = None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        overrides["enabled"] = payload["enabled"]

    if "exchange" in payload:
        exchange = str(payload["exchange"]).strip()
        if not exchange:
            raise ValueError("exchange is required")
        if allowed_exchanges is not None and exchange not in allowed_exchanges:
            raise ValueError(f"unknown exchange account: {exchange}")
        overrides["exchange"] = exchange

    if "side" in payload:
        side = str(payload["side"]).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        overrides["side"] = side

    numeric_fields = {
        "total_base",
        "slice_base_min",
        "slice_base_max",
        "interval_seconds",
        "order_ttl_seconds",
        "stop_price",
    }
    for field in numeric_fields:
        if field not in payload:
            continue
        value = float(payload[field])
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
        overrides[field] = value

    if "randomize_slice" in payload:
        if not isinstance(payload["randomize_slice"], bool):
            raise ValueError("randomize_slice must be a boolean")
        overrides["randomize_slice"] = payload["randomize_slice"]

    if "interval_seconds" in overrides and overrides["interval_seconds"] <= 0:
        raise ValueError("interval_seconds must be positive")

    overrides["slice_base"] = 0.0
    overrides["slice_quote"] = 0.0
    return overrides


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
        "account_balances": {
            "status": "starting",
            "accounts": [],
            "totals": [],
            "checked_account_count": 0,
            "total_account_count": len(_all_account_exchanges(cfg)),
            "last_finished": None,
            "errors": [],
        },
        "order_activity": {
            "status": "starting",
            "accounts": [],
            "open_orders": [],
            "closed_orders": [],
            "recent_trades": [],
            "pnl_summary": {
                "currency": cfg.common_quote_currency,
                "window": "recent_fills",
                "trade_count": 0,
                "attributed_trade_count": 0,
                "unattributed_trade_count": 0,
                "total_realized_pnl": 0.0,
                "total_fees": 0.0,
                "total_notional": 0.0,
                "sources": {},
                "missing_cost_basis": [],
                "missing_quote_rates": [],
                "missing_fee_rates": [],
                "observed_at": None,
            },
            "open_order_count": 0,
            "closed_order_count": 0,
            "recent_trade_count": 0,
            "checked_account_count": 0,
            "total_account_count": len(_all_account_exchanges(cfg)),
            "last_finished": None,
            "errors": [],
            "warnings": [],
        },
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
        "slow_execution": {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": slow_execution_config_to_dict(cfg.slow_execution),
            "accounts": slow_execution_accounts(cfg.spot_exchanges),
            "error": None,
        },
        "portfolio": {
            "status": "disabled",
            "asset": cfg.portfolio.asset,
            "quote_currency": cfg.common_quote_currency,
            "position_base": cfg.portfolio.position_base,
            "average_entry_price": cfg.portfolio.average_entry_price,
            "positions": [
                {
                    "asset": position.asset,
                    "position_base": position.position_base,
                    "average_entry_price": position.average_entry_price,
                    "mark_price": None,
                    "mark_source_count": 0,
                    "position_value": None,
                    "price_move_pnl": 0.0,
                    "status": "starting",
                }
                for position in cfg.portfolio.positions
            ],
            "position_missing_marks": [],
            "cash_balances": cfg.portfolio.cash_balances,
            "cash_balances_common": {},
            "cash_value": 0.0,
            "cash_missing_rates": [],
            "mark_price": None,
            "mark_source_count": 0,
            "position_value": None,
            "total_pnl": 0.0,
            "sources": {
                "market_maker": 0.0,
                "arbitrage": 0.0,
                "auto_buy_sell": 0.0,
                "manual": 0.0,
                "unattributed": 0.0,
                "price_move": 0.0,
            },
            "observed_at": None,
        },
        "program": {
            "running": True,
            "updated_at": time.time(),
        },
        "operations": build_operations_payload(cfg),
        "warnings": ["Waiting for first scan"],
    }


class MonitorState:
    def __init__(self, cfg: BotConfig, poll_seconds: float) -> None:
        self._lock = asyncio.Lock()
        self._program_running = True
        self._program_updated_at = time.time()
        self._slow_execution_overrides: dict[str, Any] = {}
        self._payload = _build_initial_payload(cfg, poll_seconds)
        self._recent_opportunities: deque[dict[str, Any]] = deque(maxlen=100)

    async def get(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._payload))

    async def is_running(self) -> bool:
        async with self._lock:
            return self._program_running

    async def slow_execution_config(
        self,
        base_config: SlowExecutionConfig,
    ) -> SlowExecutionConfig:
        async with self._lock:
            return replace(base_config, **self._slow_execution_overrides)

    async def set_slow_execution_overrides(
        self,
        overrides: dict[str, Any],
    ) -> None:
        async with self._lock:
            self._slow_execution_overrides.update(overrides)
            if "slow_execution" in self._payload:
                current_config = self._payload["slow_execution"].get("config", {})
                current_config.update(overrides)
                self._payload["slow_execution"]["config"] = current_config

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

    async def set_order_activity(self, order_activity: dict[str, Any]) -> None:
        async with self._lock:
            self._payload["order_activity"] = order_activity

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
        account_balances: dict[str, Any],
        order_activity: dict[str, Any],
        onchain: dict[str, Any],
        market_maker: dict[str, Any],
        slow_execution: dict[str, Any],
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
                "account_balances": account_balances,
                "order_activity": order_activity,
                "onchain": onchain,
                "market_maker": market_maker,
                "slow_execution": slow_execution,
                "portfolio": portfolio,
                "program": {
                    "running": self._program_running,
                    "updated_at": self._program_updated_at,
                },
                "operations": build_operations_payload(cfg),
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
                    "operations": build_operations_payload(cfg),
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


def build_slow_execution_payload(
    cfg: BotConfig,
    books: dict[tuple[str, str], OrderBookSnapshot],
    exec_cfg: SlowExecutionConfig | None = None,
) -> dict[str, Any]:
    exec_cfg = cfg.slow_execution if exec_cfg is None else exec_cfg
    config_payload = slow_execution_config_to_dict(exec_cfg)
    accounts = slow_execution_accounts(cfg.spot_exchanges)
    if not exec_cfg.enabled:
        return {
            "status": "disabled",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": None,
        }

    book = books.get((exec_cfg.exchange, exec_cfg.symbol))
    if book is None:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": f"Missing {exec_cfg.exchange} {exec_cfg.symbol}",
        }

    try:
        plan = build_slow_execution_plan(book, exec_cfg)
    except ValueError as exc:
        return {
            "status": "error",
            "mode": "dry_run",
            "plan": None,
            "config": config_payload,
            "accounts": accounts,
            "error": str(exc),
        }

    return {
        "status": plan.status,
        "mode": "dry_run",
        "plan": plan.to_dict(),
        "config": config_payload,
        "accounts": accounts,
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
    account_balances_payload = _build_initial_payload(cfg, poll_seconds)[
        "account_balances"
    ]
    order_activity_payload = _build_initial_payload(cfg, poll_seconds)[
        "order_activity"
    ]
    market_maker_payload = _build_initial_payload(cfg, poll_seconds)["market_maker"]
    slow_execution_payload = _build_initial_payload(cfg, poll_seconds)[
        "slow_execution"
    ]
    portfolio_payload = _build_initial_payload(cfg, poll_seconds)["portfolio"]
    previous_onchain_amounts: dict[str, float] = {}
    next_onchain_scan = 0.0
    next_balance_scan = 0.0
    next_order_activity_scan = 0.0
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
                runtime_slow_execution = await state.slow_execution_config(
                    cfg.slow_execution
                )
                portfolio_books: dict[tuple[str, str], OrderBookSnapshot] = {}
                if strategy in {"all", "spot-spread"} and cfg.spot_markets:
                    symbols_by_exchange = _symbols_for_configured_spot_markets(cfg)
                    if (
                        runtime_slow_execution.enabled
                        and runtime_slow_execution.exchange
                        and runtime_slow_execution.symbol
                    ):
                        symbols_by_exchange.setdefault(
                            runtime_slow_execution.exchange,
                            set(),
                        ).add(runtime_slow_execution.symbol)
                    books = await manager.fetch_order_books(
                        cfg.spot_exchanges,
                        symbols_by_exchange,
                        cfg.order_book_depth,
                    )
                    portfolio_books = books
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
                    slow_execution_payload = build_slow_execution_payload(
                        cfg,
                        books,
                        runtime_slow_execution,
                    )
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
                    slow_execution_payload = {
                        "status": "disabled",
                        "mode": "dry_run",
                        "plan": None,
                        "config": slow_execution_config_to_dict(
                            runtime_slow_execution
                        ),
                        "accounts": slow_execution_accounts(cfg.spot_exchanges),
                        "error": None,
                    }
                    portfolio_payload = _build_initial_payload(cfg, poll_seconds)[
                        "portfolio"
                    ]

                now = time.monotonic()
                if now >= next_balance_scan:
                    try:
                        account_balances_payload = await fetch_account_balances_payload(
                            cfg,
                            manager,
                            runtime_slow_execution,
                        )
                    except Exception as exc:  # noqa: BLE001
                        account_balances_payload = {
                            "status": "error",
                            "accounts": [],
                            "totals": [],
                            "checked_account_count": 0,
                            "total_account_count": len(_all_account_exchanges(cfg)),
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                        }
                    next_balance_scan = now + ACCOUNT_BALANCE_POLL_SECONDS

                if now >= next_order_activity_scan:
                    try:
                        order_activity_payload = await fetch_order_activity_payload(
                            cfg,
                            manager,
                            runtime_slow_execution,
                            quote_rates=quote_rates,
                            books=portfolio_books,
                        )
                    except Exception as exc:  # noqa: BLE001
                        order_activity_payload = {
                            "status": "error",
                            "accounts": [],
                            "open_orders": [],
                            "closed_orders": [],
                            "recent_trades": [],
                            "pnl_summary": {
                                "currency": cfg.common_quote_currency,
                                "window": "recent_fills",
                                "trade_count": 0,
                                "attributed_trade_count": 0,
                                "unattributed_trade_count": 0,
                                "total_realized_pnl": 0.0,
                                "total_fees": 0.0,
                                "total_notional": 0.0,
                                "sources": {},
                                "missing_cost_basis": [],
                                "missing_quote_rates": [],
                                "missing_fee_rates": [],
                                "observed_at": None,
                            },
                            "open_order_count": 0,
                            "closed_order_count": 0,
                            "recent_trade_count": 0,
                            "checked_account_count": 0,
                            "total_account_count": len(_all_account_exchanges(cfg)),
                            "last_finished": time.time(),
                            "errors": [str(exc)],
                            "warnings": [],
                        }
                    next_order_activity_scan = now + ORDER_ACTIVITY_POLL_SECONDS

                if portfolio_books:
                    portfolio_payload = build_synced_portfolio_pnl(
                        cfg,
                        portfolio_books,
                        quote_rates,
                        account_balances_payload,
                        order_activity_payload,
                    )

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
                if account_balances_payload.get("status") == "error":
                    errors = account_balances_payload.get("errors") or ["unavailable"]
                    warnings = [*warnings, f"Account balances: {errors[0]}"]
                if order_activity_payload.get("status") == "error":
                    errors = order_activity_payload.get("errors") or ["unavailable"]
                    warnings = [*warnings, f"Orders: {errors[0]}"]
                if market_maker_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Market maker: {market_maker_payload.get('error')}",
                    ]
                if slow_execution_payload.get("status") == "error":
                    warnings = [
                        *warnings,
                        f"Auto Buy/Sell: {slow_execution_payload.get('error')}",
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
                    account_balances=account_balances_payload,
                    order_activity=order_activity_payload,
                    onchain=onchain_payload,
                    market_maker=market_maker_payload,
                    slow_execution=slow_execution_payload,
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


async def api_slow_execution(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        accounts = slow_execution_accounts(cfg.spot_exchanges)
        allowed_exchanges = {account["key"] for account in accounts}
        overrides = _slow_execution_overrides_from_payload(
            payload,
            allowed_exchanges=allowed_exchanges,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    await state.set_slow_execution_overrides(overrides)
    current_config = await state.slow_execution_config(cfg.slow_execution)
    return web.json_response(
        {
            "ok": True,
            "config": slow_execution_config_to_dict(current_config),
            "accounts": slow_execution_accounts(cfg.spot_exchanges),
        }
    )


async def api_cancel_order(request: web.Request) -> web.Response:
    state: MonitorState = request.app["monitor_state"]
    cfg: BotConfig = request.app["config"]
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    manager = ExchangeManager()
    try:
        runtime_slow_execution = await state.slow_execution_config(cfg.slow_execution)
        result = await cancel_order_payload(
            cfg,
            manager,
            payload,
            runtime_slow_execution,
        )
        order_activity = await fetch_order_activity_payload(
            cfg,
            manager,
            runtime_slow_execution,
        )
        await state.set_order_activity(order_activity)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"{exc.__class__.__name__}: {exc}"},
            status=500,
        )
    finally:
        await manager.close()

    result["order_activity"] = order_activity
    return web.json_response(result)


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
    app["config"] = cfg

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
    app.router.add_post("/api/auto-buy-sell", api_slow_execution)
    app.router.add_post("/api/slow-execution", api_slow_execution)
    app.router.add_post("/api/orders/cancel", api_cancel_order)
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
