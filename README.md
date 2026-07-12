# Crypto Arbitrage MVP

This is a dry-run scanner for two common crypto arbitrage families:

- Spot spread arbitrage across exchanges.
- Spot versus futures or perpetual basis arbitrage.
- Single-exchange triangular spot arbitrage.

The arbitrage scanner defaults to dry-run. Its optional live spot executor is
guarded by global, strategy, account, balance, slippage, stale-data, and order
budget checks. Market Maker and Auto Buy/Sell also default to non-live settings
and require explicit confirmation plus a fresh strategy preflight before they can
place orders.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.json config.json
python -m arbitrage_bot.main --config config.json --once
```

If you only want to run the pure unit tests without installing CCXT:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

To run a broader offline functional and pressure test of the web/API stack:

```bash
PYTHONPATH=src .venv/bin/python scripts/functional_stress_test.py \
  --state-requests 600 \
  --settings-requests 200 \
  --strategy-writes 20 \
  --signals 60 \
  --concurrency 50 \
  --write-concurrency 5 \
  --report /tmp/crypto-functional-stress.json
```

This starts an in-process aiohttp test server with temporary data files, no
configured exchanges, no real API credentials, and `allow_live_trading=false`.
It exercises state reads, strategy-center writes, funding settings, Signal Bot
webhooks, and concurrent API traffic without placing or canceling live orders.
State reads can safely be tested with higher concurrency than SQLite-backed writes;
increase `--write-concurrency` only when specifically testing strategy-center
store throughput.

GitHub Actions runs JSON validation, `compileall`, and `pytest` on pushes and
pull requests. A basic Dockerfile is included for later container deployment:

```bash
docker build -t crypto-arbitrage-mvp .
docker run --env-file .env -p 8080:8080 \
  -v "$PWD/config.acs.json:/app/config.acs.json:ro" \
  -v "$PWD/data:/app/data" \
  crypto-arbitrage-mvp
```

## Run modes

```bash
# Run both strategies once.
python -m arbitrage_bot.main --config config.json --once

# Run only cross-exchange spot spread scanning.
python -m arbitrage_bot.main --config config.json --strategy spot-spread --once

# Run only spot-futures basis scanning.
python -m arbitrage_bot.main --config config.json --strategy cash-and-carry --once

# Run only single-exchange triangular arbitrage scanning.
python -m arbitrage_bot.main --config config.json --strategy triangular-arbitrage --once

# Keep polling.
python -m arbitrage_bot.main --config config.json
```

Triangular arbitrage is configured under `triangular_arbitrage.routes`. Each
route specifies one exchange, a starting currency, and three spot symbols. The
scanner automatically tests valid three-leg cycles in both directions, simulates
fills from order book depth, deducts exchange fees, and reports profit in the
starting currency. It does not place live triangular orders.

## ACS spot arbitrage

Use `config.acs.example.json` when monitoring ACS across Bithumb, Bybit, Coinbase, and Upbit:

```bash
cp config.acs.example.json config.acs.json
python -m arbitrage_bot.main --config config.acs.json --strategy spot-spread --once
```

For 24-hour monitoring, run continuous mode. This reuses exchange clients across scans, polls on a fixed cadence, and keeps going until stopped:

```bash
python -m arbitrage_bot.main --config config.acs.json --strategy spot-spread --only-opportunities
```

For a faster local override:

```bash
python -m arbitrage_bot.main --config config.acs.json --strategy spot-spread --poll-seconds 0.5 --only-opportunities
```

The REST scanner is bounded by exchange rate limits and network latency. If a scan takes longer than the configured interval, the next scan starts immediately. For sub-second production latency, use exchange WebSocket order book streams.

To leave it running for a full day and save logs:

```bash
mkdir -p logs
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.main \
  --config config.acs.json \
  --strategy spot-spread \
  --poll-seconds 1 \
  --only-opportunities \
  --heartbeat-seconds 60 \
  > logs/opportunities.jsonl \
  2> logs/scanner.log
```

`opportunities.jsonl` contains only actionable opportunity JSON lines. `scanner.log` contains heartbeat and warning output.

To run the local monitor web UI:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.web \
  --config config.acs.json \
  --strategy spot-spread \
  --host 127.0.0.1 \
  --port 8080
```

Then open `http://127.0.0.1:8080`. The page shows scan health, latency, converted bid/ask prices, quote rates, and any live opportunities. The program switch next to the status pill pauses or resumes scanning without stopping the web server.

The dashboard receives live updates over a Server-Sent Events stream at
`/api/state/stream` (same payload and auth as `/api/state`, with `view`,
`sections`, and `interval` query parameters); browsers without EventSource
support, or any stream error, fall back to plain `/api/state` polling
automatically. JSON and static responses are gzip-compressed, and static
assets are served with immutable cache headers — bump the `?v=` version
string in `index.html` whenever `app.js`, `i18n.js`, or `styles.css`
change. The UI follows the operating system light/dark preference.

The web service also exposes Prometheus-compatible metrics at `/metrics` and
`/api/metrics`. A local scrape from the server itself is allowed without a
dashboard session; external requests still go through the same web authentication
and IP rules as the rest of the dashboard. Metrics include scan latency,
opportunity count, warnings, live/risk/program switches, order activity,
reconciliation issue counts, readiness status, per-account readiness,
per-strategy live/paused/configured state, and tracked MM/Grid order counts.

The top row shows configured positions, cash balances, and P/L attribution:

```json
"portfolio": {
  "enabled": true,
  "positions": [
    {
      "asset": "ACS",
      "position_base": 0.0,
      "average_entry_price": 0.0
    }
  ],
  "cash_balances": {
    "USDC": 0.0,
    "USDT": 0.0,
    "KRW": 0.0
  },
  "realized_pnl": {
    "market_maker": 0.0,
    "arbitrage": 0.0
  }
}
```

`Account Balances` reads live private balances from exchanges with configured API env vars every 10 seconds, aggregates target currencies across accounts, and shows per-account free/used/total balances in the monitor table. When at least one account balance is available, the top `Position` and `Cash Position` cards sync from those live account totals: configured spot base assets such as ACS are treated as positions, while quote and cash currencies such as USD, USDC, USDT, and KRW are treated as cash. If no private account balance is available, the cards fall back to the configured `portfolio` values. `Price Move` is calculated only when an `average_entry_price` is configured; otherwise it stays at zero until cost basis is available. The mark price for each asset is the average converted mid price across available spot books for that asset. `MM P/L`, `Arb P/L`, and `Auto P/L` start with configured `realized_pnl` values and then add the daily fill P/L snapshot when `pnl_store.enabled` is true. Manual or unmatched fills roll into `Other P/L`, and fills without a known order source are shown as `Unattributed`.

`Orders & Fills` reads private open orders, recently closed orders, and recent fills for configured symbols every 5 seconds when the account API env vars are available. Recent fills include source attribution, converted notional, fees, and estimated realized P/L. Buy fills only count fees as realized P/L; sell fills use the configured `average_entry_price` for that asset when it is available. When `pnl_store.enabled` is true, fills are upserted into SQLite at `pnl_store.path`, deduplicated by exchange/symbol/trade identity, and aggregated into a daily P/L snapshot by source. Each open order row includes a `Cancel` action that sends a guarded cancel request for that exact exchange, symbol, and order ID, then writes a `manual_order_cancel` event to the trade log and refreshes the order state.

To add another spot asset later, add its markets to `spot_markets`, add one position entry under `portfolio.positions`, and add any new quote-currency conversion to `quote_rates` or `quote_rate_sources`:

```json
{
  "portfolio": {
    "enabled": true,
    "positions": [
      {
        "asset": "ACS",
        "position_base": 0.0,
        "average_entry_price": 0.0
      },
      {
        "asset": "XYZ",
        "position_base": 0.0,
        "average_entry_price": 0.0
      }
    ]
  },
  "spot_markets": [
    {
      "asset": "ACS",
      "exchange": "bybit-spot",
      "symbol": "ACS/USDT",
      "quote_currency": "USDT"
    },
    {
      "asset": "XYZ",
      "exchange": "bybit-spot",
      "symbol": "XYZ/USDT",
      "quote_currency": "USDT"
    }
  ]
}
```

The web monitor also shows a dry-run market maker ladder when `market_maker.enabled` is true. With the ACS config and `--poll-seconds 1`, the page fetches the latest REST order book every second and recalculates the 20 planned bid/ask orders from the fresh mid price. The ACS example config targets Bybit `ACS/USDT` with 10 bid levels and 10 ask levels, spread symmetrically within a 10% one-sided price band around the mid price. For example, a 10-level ladder with `price_band_pct: 10.0` places levels roughly 1%, 2%, ..., 10% away from the mid price on each side. If you want a 10% total width, use `price_band_pct: 5.0`.

`depth_shape: "linear"` makes the first level closest to the top of book the smallest, then increases quote size on each farther level. `quote_per_level` is the average quote amount per active level, so the per-side total remains approximately `levels * quote_per_level`; `depth_shape: "flat"` restores the old uniform sizing. `min_order_quote` is treated as a per-level floor when the total quote budget is large enough, which helps avoid creating inner orders below exchange minimum notional.

`reprice_threshold_bps` reduces unnecessary cancel/replace churn. When live MM already has tracked orders and the new target ladder moved less than this threshold, the engine keeps the current orders instead of canceling and placing a new ladder. Set it to `0.0` for the old always-replace behavior; values around `1-5` bps are usually a better starting point for thin spot markets.

When the exchange adapter supports it, MM uses batch order APIs for ladder placement and cancellation. Binance spot, Binance USDT perpetuals, Bybit spot, and Bybit perpetuals are enabled for batch create/cancel; unsupported exchanges automatically stay on the guarded single-order path.

The web background MM loop also has an optional order book cache. If the active exchange client advertises `watchOrderBook`, the loop maintains a fresh WebSocket book and builds the next ladder from that cached snapshot; otherwise it safely falls back to the existing REST fetch path. The monitor shows the current MM market data source, age, and whether WebSocket watching is unsupported. This keeps today's setup compatible while leaving a clean upgrade path for native exchange WebSocket clients or `ccxt.pro`.

`quote_per_level` and `min_order_quote` are always expressed in the selected exchange pair's quote currency: USDT for Bybit/Upbit `ACS/USDT`, USDC for Coinbase `ACS/USDC`, and KRW for Bithumb `ACS/KRW`. Risk checks convert those quote amounts into `common_quote_currency` using `quote_rates` before applying `max_order_quote`, `max_cycle_quote`, and exposure limits. If a quote rate is missing, live MM is blocked.

Exchange support is intentionally conservative. Binance, Bybit, Coinbase, and Upbit support post-only limit orders through ccxt. Bithumb does not expose post-only support through ccxt, so Bithumb MM with `post_only: true` is blocked before order placement; only set `post_only: false` and `risk.require_post_only: false` for Bithumb if you explicitly accept taker-fill risk. Bithumb also does not support client order ids through ccxt, so its MM orders can only be tracked in memory until the process restarts.

Always run the account preflight before live MM. Public ccxt metadata can expose exchange minimums, but they can change. Bybit `ACS/USDT` currently requires about 5 USDT minimum order cost. Bithumb public metadata may report 500 KRW for `ACS/KRW`, while its private v2 API enforces 5,000 KRW; the adapter therefore uses the stricter 5,000 KRW floor before submitting an order. If you raise `quote_per_level`, also raise `risk.max_cycle_quote` enough for `levels * 2 * quote_per_level` after common-currency conversion.

The account preflight also checks transfer metadata when the exchange adapter
supports it. For each configured symbol currency it reports currency active
state, deposit availability, withdrawal availability, fees, and network-level
status when available. Treat unsupported or unavailable transfer status as a
manual verification item before cross-exchange arbitrage.

```json
"market_maker": {
  "enabled": true,
  "exchange": "bybit-spot",
  "symbol": "ACS/USDT",
  "levels": 10,
  "price_band_pct": 10.0,
  "quote_per_level": 1.0,
  "depth_shape": "linear",
  "min_order_quote": 0.1,
  "min_distance_bps": 0.0,
  "reprice_threshold_bps": 2.0,
  "poll_seconds": 1,
  "post_only": true,
  "cancel_existing_orders": false,
  "client_order_prefix": "crypto-arb-mm"
}
```

To preview the exact orders without placing anything:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.market_maker \
  --config config.acs.json
```

To recalculate the dry-run ladder every second from the current order book:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.market_maker \
  --config config.acs.json \
  --loop \
  --poll-seconds 1
```

To place real orders, configure API env vars on the target exchange entry, fund the account, and run with explicit live confirmation:

```bash
export BYBIT_API_KEY="..."
export BYBIT_SECRET="..."
# or:
export BITHUMB_API_KEY="..."
export BITHUMB_SECRET="..."
# Upbit Indonesia / id.upbit.com
export UPBIT_ID_API_KEY="..."
export UPBIT_ID_SECRET="..."

PYTHONPATH=src .venv/bin/python -m arbitrage_bot.market_maker \
  --config config.acs.json \
  --live \
  --confirm-live-orders
```

Continuous live replacement cancels open orders on the configured symbol before each new ladder, so only use it in an isolated market-making account:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.market_maker \
  --config config.acs.json \
  --loop \
  --live \
  --confirm-live-orders \
  --replace-existing
```

The market maker CLI clamps its effective loop interval to at least 1 second. Every-second live replacement can still hit exchange order-rate limits quickly because it may cancel and place up to 20 orders per cycle. The web background MM loop can use a WebSocket order book cache when the installed exchange client supports it; if not, it uses REST and reports `WS unsupported` in the MM status text.

The monitor can also show and configure a dry-run Auto Buy/Sell plan when `slow_execution.enabled` is true. By default this tool submits one buy or sell marketable limit order at the current execution-side top of book: buys use the best ask and sells use the best bid. Set `price_mode` to `maker` to quote on the passive side instead: buys use the best bid and sells use the best ask. Use `price_offset_bps` to move the order farther away from the book top, such as selling slightly above the best ask. The speed is configured with `interval_seconds`; live orders can also be canceled after `order_ttl_seconds`. The config key stays `slow_execution` for backward compatibility, but the user-facing feature name is Auto Buy/Sell.

```json
"slow_execution": {
  "enabled": true,
  "exchange": "bybit-spot",
  "symbol": "ACS/USDT",
  "side": "sell",
  "total_base": 100000.0,
  "total_quote": 0.0,
  "unlimited_total": false,
  "slice_mode": "configured",
  "slice_base": 0.0,
  "slice_base_min": 3000.0,
  "slice_base_max": 5000.0,
  "slice_quote": 0.0,
  "randomize_slice": true,
  "interval_seconds": 60.0,
  "order_ttl_seconds": 20.0,
  "start_price": 0.00015,
  "stop_price": 0.00012,
  "price_mode": "taker",
  "price_offset_bps": 0.0,
  "min_order_quote": 0.1,
  "post_only": false,
  "cancel_existing_orders": false,
  "client_order_prefix": "crypto-arb-slow"
}
```

Use `total_base` to cap the total base asset amount, such as ACS, or `total_quote` to cap the total quote currency amount, such as USDC, USDT, or KRW. If both are set, Auto Buy/Sell stops when either cap is reached. Set `unlimited_total: true` only for intentionally open-ended tasks; the task will keep cycling until stopped by price, paused, blocked by risk, or manually stopped. Progress is shown against `total_quote` when it is configured; otherwise it uses `total_base`. Unlimited tasks show filled amount against `Unlimited`.

Use `slice_mode: "configured"` with exactly one per-order sizing mode: `slice_base`, `slice_quote`, or the `slice_base_min`/`slice_base_max` range. For example, a 3,000 to 5,000 ACS range with `randomize_slice: true` chooses a random amount in that range for each planned order. With `randomize_slice: false`, the range uses the minimum amount as the fixed slice. `slice_quote: 10` means each slice is about 10 USDT worth of ACS at the current execution-side price. Use `slice_mode: "top_level"` when each order should match the current top-of-book amount: sells use the best ask amount and buys use the best bid amount. Auto Buy/Sell `price_mode: "taker"` uses a marketable limit price: buys at the current best ask and sells at the current best bid. Keep `slow_execution.post_only` false and `risk.require_post_only` false for this strategy if you want immediate taker-style execution.

The web page exposes runtime controls for the selected account, `enabled`, `side`, `price_mode`, `price_offset_bps`, `unlimited_total`, `slice_mode`, `total_base`, `total_quote`, the min/max base order size range, randomization, `interval_seconds`, `order_ttl_seconds`, `start_price`, and `stop_price`. The account checkbox list comes from `spot_exchanges`, so multiple accounts should be added as separate exchange entries with distinct `label` values. These page edits affect the running monitor immediately but do not write back to `config.acs.json`.

The Settings page also includes Spot Grid and DCA Bot panels. These modules currently produce dry-run order plans and run through the same risk gate used by the live trading console. Spot Grid supports pair/account, lower and upper price, grid count, arithmetic or geometric spacing, quote per grid, take profit, stop loss, auto rebuild intent, maximum position, maximum order count, minimum grid step, cancel retry count, and post-only mode. The grid module also builds adjacent replacement orders after a grid fill: a filled buy proposes a sell one grid above, and a filled sell proposes a buy one grid below. DCA Bot supports pair/account, side, trigger price, interval, quote per order, size multiplier, max orders, average entry, take profit, maximum position, maximum loss, maker/taker price mode, and offset bps. Live order placement for these two modules is intentionally not enabled until the execution loop and explicit live confirmation flow are added.

The Settings page includes a per-user Historical Backtest workspace for Spot Grid and DCA strategies. Select an active project, one of its strategy instances, an assigned spot account, a candle timeframe, and 20-500 history bars. The runner fetches public exchange OHLCV data, applies configurable fees, slippage, and bar latency, and reports strategy return, buy-and-hold return, excess return, drawdown, annualized volatility, Sharpe/Sortino, turnover, fees, trades, and an equity curve. It does not decrypt API credentials and cannot create or cancel live orders. TWAP/VWAP/POV and the legacy synthetic-path backtest remain hidden while their execution and modeling assumptions are being hardened.

The control page also exposes runtime market setup. `Markets` configures spot arbitrage symbols per account, while `Cash & Carry Pairs` configures spot-vs-contract symbols for basis scanning. Cash & Carry now scans both positive basis (`buy spot + sell contract`) and negative basis (`sell/borrow spot + buy contract`) opportunities. For Binance USDT perpetuals and Bybit USDT perpetuals, ccxt symbols usually look like `BTC/USDT:USDT` or `ETH/USDT:USDT`; the spot side remains `BTC/USDT` or `ETH/USDT`. The ACS example config includes `binance-spot`, `binance-swap` (`binanceusdm`), and `bybit-swap`, but does not assume ACS is listed on those venues. Add only the symbols that actually exist on the target exchange.

Options arbitrage is available as a dry-run scanner with `option_combos` and `options_arbitrage`. Each combo links a spot market to a same-strike call/put pair, then checks put-call parity for conversion and reverse-conversion opportunities. The status page also shows an option chain table with expiry, strike, call/put, bid/ask, mark, spread, depth, volume/open interest, and Greeks when the exchange feed provides them. `min_option_depth_quote`, `max_option_spread_bps`, `min_days_to_expiry_open`, and `expiry_reminder_days` act as paper execution controls: thin depth, wide spreads, or near-expiry contracts block new paper order tickets before live order generation is allowed. With multiple configured strikes/expiries, the scanner also reports paper candidates for box spreads, vertical spreads, calendar spreads, and IV anomalies when enough market data is available. It does not place option orders; generated order tickets are paper-only and require explicit final confirmation before any future live workflow.

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.main \
  --config config.json \
  --strategy options-arbitrage \
  --once
```

For `start_price`, a sell schedule waits until the best bid is at or above the start price before placing the first marketable sell order. A buy schedule waits until the best ask is at or below the start price before placing the first marketable buy order. For `stop_price`, a sell schedule stops when the best bid is at or below the stop price, and a buy schedule stops when the best ask is at or below the stop price. Buy stop checks run before the start gate, so a buy task with `stop_price` above `start_price` stops before placing an order once the ask reaches or passes the stop level.

To preview the next slice without placing anything:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.auto_buy_sell \
  --config config.acs.json
```

To simulate the full schedule in dry-run mode:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.auto_buy_sell \
  --config config.acs.json \
  --loop
```

To place real marketable limit orders, configure API env vars on the target exchange entry, fund the account, and run with explicit live confirmation:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.auto_buy_sell \
  --config config.acs.json \
  --loop \
  --live \
  --confirm-live-orders
```

The preferred command name is now `arbitrage_bot.auto_buy_sell`; `arbitrage_bot.slow_executor` remains as a backward-compatible alias. Auto Buy/Sell tracks submitted base amount, not confirmed fills. If an order rests unfilled, it still counts toward the submitted schedule. For fill-aware execution, add order and trade polling before using it for larger live sizes.

## Cross-exchange inventory rebalance

The Trading page includes a synthetic cross-exchange rebalance task. It buys a
project token with cash on the source account and simultaneously sells the same
base quantity on the destination account. The result is equivalent to moving
cash from the source venue to the destination venue while moving project-token
inventory in the opposite direction. It does not make an on-chain deposit,
withdrawal, or exchange-to-exchange transfer, so both accounts must be funded in
advance.

The two pairs may use different quote currencies. For example, buying `ACS/KRW`
on Bithumb and selling `ACS/USDC` on Coinbase uses `quote_rates` to convert both
legs into `common_quote_currency` before checking the target amount, cycle size,
fees, rebalance cost, slippage, and global risk limits.

`total_quote_common` and `quote_per_cycle_common` are source-account spend
targets. The destination account normally receives less after spreads and fees;
both source spend and destination proceeds are tracked separately. Before a live
cycle, both legs are reduced to the same exchange-valid base quantity so
different amount precisions cannot create a false residual position.

```json
"cross_exchange_rebalance": {
  "enabled": false,
  "live_enabled": false,
  "buy_exchange": "bithumb-spot",
  "buy_symbol": "ACS/KRW",
  "sell_exchange": "coinbase-spot",
  "sell_symbol": "ACS/USDC",
  "total_quote_common": 100.0,
  "quote_per_cycle_common": 10.0,
  "interval_seconds": 30.0,
  "order_ttl_seconds": 2.0,
  "max_cost_bps": 50.0,
  "max_slippage_bps": 50.0,
  "buy_quote_reserve": 0.0,
  "sell_base_reserve": 0.0,
  "coordinate_market_maker": true,
  "coordination_timeout_seconds": 30.0,
  "block_conflicting_open_orders": true,
  "halt_on_error": true
}
```

The task is disabled and dry-run by default. Live execution requires all of the
global live risk switches, both account switches,
`risk.strategy_enabled.cross_exchange_rebalance=true`, `live_enabled=true`, and
the exact web confirmation phrase `ENABLE LIVE REBALANCE`. Opposite-side open
orders on either route block the cycle to avoid self-trading with MM or another
strategy. With `coordinate_market_maker=true`, a cost-qualified live cycle first
places an account-and-symbol hold on matching MM instances. Those instances stop
replenishing, cancel their orders, and must acknowledge a clean exchange order
sync before the rebalance refreshes both books and proceeds. MM resumes after a
normal cycle. A cancellation/sync failure, conflicting order, or unbalanced leg
keeps the hold active until the issue is resolved or the rebalance is paused or
disabled. The hold has a renewable expiry so an abandoned task cannot pause MM
forever. Insufficient source cash, insufficient destination token inventory,
stale books, excessive slippage, and missing FX rates also block before orders
are submitted. If the two legs fill by different amounts or one leg fails, the
task records the residual hedge, advances no progress, and remains halted for
manual review. Confirmed balanced progress is persisted in
`data/cross_exchange_rebalance_runtime.json` across service restarts.

## Read-only account preflight

Before using `--live`, run the account preflight check. It does not create or cancel orders. It checks configured API environment variables, public market metadata, current order book, private balances, open orders, and the active risk switches for each configured account.

```bash
export BYBIT_API_KEY="..."
export BYBIT_SECRET="..."

PYTHONPATH=src .venv/bin/python -m arbitrage_bot.account_check \
  --config config.acs.json \
  --exchange bybit-spot \
  --symbol ACS/USDT
```

Without `--exchange`, it checks all configured exchange entries. Without `--symbol`, it uses symbols from `spot_markets`, `market_maker`, Auto Buy/Sell, Spot Grid, DCA Bot, TWAP/VWAP/POV, and Backtest/Paper.

Useful variants:

```bash
# Check all configured ACS spot accounts and show zero balances too.
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.account_check \
  --config config.acs.json \
  --include-zero-balances

# Check one account with two symbols.
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.account_check \
  --config config.acs.json \
  --exchange bybit-spot \
  --symbol ACS/USDT \
  --symbol BTC/USDT
```

Treat a `status: "error"` result as a blocker for live testing. A `status: "warning"` often means the command could read public market data but live trading is still intentionally disabled, an API env var is missing, or the account is disabled by risk config.

For Coinbase Advanced, create a CDP Secret API Key with View and Trade permissions only, select the ECDSA signature algorithm, and IP-allowlist the server outbound IP. The Coinbase secret is usually an EC private key; it can be stored in the env file on one line with escaped newlines:

```bash
COINBASE_API_KEY=organizations/{org_id}/apiKeys/{key_id}
COINBASE_SECRET=-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n
```

The exchange client converts those escaped `\n` sequences back to real newlines before authenticating.

## Risk controls, events, and alerts

Live market maker and Auto Buy/Sell orders now pass through a risk gate before any exchange order is submitted. Dry-run mode still prints the plan, but the payload includes a `risk` decision so you can see whether the same action would be allowed in live mode.

By default, live trading is blocked until you explicitly set `risk.allow_live_trading` to `true`:

```json
"risk": {
  "enabled": true,
  "trading_enabled": true,
  "allow_live_trading": false,
  "allow_market_maker": true,
  "allow_slow_execution": true,
  "strategy_enabled": {
    "market_maker": true,
    "slow_execution": true
  },
  "account_enabled": {
    "bybit-spot": true
  },
  "require_post_only": false,
  "max_order_quote": 5.0,
  "max_cycle_quote": 25.0,
  "max_position_base": 0.0,
  "max_position_base_by_asset": {
    "ACS": 0.0
  },
  "max_exposure_quote": 0.0,
  "max_exposure_quote_by_asset": {
    "ACS": 0.0
  },
  "max_daily_loss_quote": 0.0,
  "max_orders_per_cycle": 30,
  "max_open_orders": 50,
  "max_cancels_per_cycle": 50,
  "min_seconds_between_cancels": 0.0,
  "max_existing_spread_bps": 2500.0,
  "max_price_distance_bps": 1500.0,
  "max_slippage_bps": 50.0,
  "min_order_book_depth_quote": 0.0,
  "max_order_book_gap_bps": 2000.0,
  "max_price_jump_bps": 1000.0,
  "max_plan_age_seconds": 5.0,
  "max_order_book_age_seconds": 10.0,
  "require_order_book_timestamp": false,
  "allowed_exchanges": [],
  "blocked_exchanges": [],
  "allowed_symbols": [],
  "blocked_symbols": []
}
```

The kill switches are `trading_enabled`, `allow_market_maker`, `allow_slow_execution`, `strategy_enabled`, and `account_enabled`. Position and exposure limits use `portfolio.positions` as the current base inventory and the current order book midpoint as the mark price. A `0.0` limit disables that specific check. Daily loss checks use configured `portfolio.realized_pnl` plus the current daily P/L snapshot when `pnl_store.enabled` is true.

Order safety checks include max single-order notional, max cycle notional, max planned orders, projected max open orders, max cancels per cycle, optional minimum seconds between cancel cycles, and exchange market-rule validation. Before live MM or Auto Buy/Sell orders are placed, the executor loads the exchange market metadata, rounds amount/price through CCXT precision helpers, and blocks the whole batch if any order violates exchange minimum/maximum amount, price, or cost limits. Market quality checks include minimum bid/ask depth, max bid/ask spread, max level-to-level order book gap, max adverse slippage, max price jump versus the previous cycle, max plan age, and max order book timestamp age.

Every market maker and Auto Buy/Sell cycle is written to JSONL when `trade_log.enabled` is true:

```json
"trade_log": {
  "enabled": true,
  "path": "data/trade_events.jsonl",
  "max_recent_events": 50,
  "rotate_max_bytes": 268435456,
  "rotate_keep_files": 12,
  "rotate_compress": true
}
```

The strategy timeline is a separate structured JSONL stream focused on decisions and explanations: why a strategy did not place an order, why it canceled, which risk condition blocked execution, which account/symbol was affected, and key timing/slippage metrics for spot arbitrage execution protection.

```json
"strategy_timeline": {
  "enabled": true,
  "path": "data/strategy_timeline.jsonl",
  "max_recent_events": 100,
  "rotate_max_bytes": 268435456,
  "rotate_keep_files": 12,
  "rotate_compress": true
}
```

When either JSONL file reaches `rotate_max_bytes`, the active file is moved to a
timestamped archive, a fresh active file is opened, and the archive is gzipped in
the background. `rotate_keep_files` limits the number of archived files kept per
log stream.

The monitor shows the current risk settings, strategy timeline, and normalized trade log rows in the `Risk & Events` table. You can also inspect the trade log from the command line:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.trade_log \
  --config config.acs.json \
  --limit 20
```

Inspect the strategy timeline:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.strategy_timeline \
  --config config.acs.json \
  --limit 30
```

For machine-readable output:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.trade_log \
  --config config.acs.json \
  --limit 20 \
  --json
```

Keep `data/` and local config files out of Git. The `alerts` block is reserved for notification routing:

```json
"alerts": {
  "enabled": false,
  "min_level": "warning",
  "webhook_url_env": null,
  "telegram_bot_token_env": null,
  "telegram_chat_id_env": null
}
```

## Runtime trading safeguards

- Every durable web configuration change is versioned in
  `data/config_versions.sqlite3`. The Settings page shows the actor, diff, and
  verification state and supports optimistic-concurrency rollback. A newly
  started live configuration is marked known-good only after healthy strategy
  cycles; a fresh blocked/error state restores the previous known-good version.
- Starting or changing a live MM, Auto Buy/Sell task, or cross-exchange
  rebalance requires a short-lived preflight token bound to the exact candidate
  parameters and current user. The check covers risk switches, order/cycle
  budgets, private-data freshness, API and market access, minimum order size,
  balances, quote conversion, order-book spread/depth, conflicting orders, and
  projected open-order count.
- Live limit submissions with client order IDs are journaled in
  `data/order_intents.sqlite3` before the exchange call. Repeating an intent
  replays or recovers the original order instead of submitting a duplicate.
  Unknown outcomes survive restart, block trading, and are never removed by
  journal compaction. Cancel operations retry and confirm that the order is no
  longer open.
- Spot-arbitrage execution records final fill evidence for both legs. Unknown
  submissions or incomplete fill reconciliation block automatic repair. An
  optional emergency hedge can neutralize a confirmed partial/one-leg fill on
  the filled venue, but it defaults off and has independent live, quote-cap,
  slippage, attempt, and TTL controls.
- The Records page groups daily fills, fees, realized P/L, fill rate, average
  fill, slippage, submission latency, MM spread capture, inventory residual,
  Auto Buy/Sell progress, and paper-versus-live difference by strategy instance.

## Cloud deployment and per-account IPs

For production, the cleaner setup is one exchange account per runner, container, or VM, with that runtime bound to its own static outbound IP at the cloud network layer. For example, run `bybit-mm-a`, `coinbase-arb-a`, and `upbit-arb-a` as separate processes or containers, then assign each one a dedicated NAT gateway, elastic IP, or cloud egress address. If the exchange account has IP whitelisting enabled, whitelist only the IP assigned to that account.

Use the deployment helper to perform a blue/green release on a systemd-based VM
without overwriting runtime secrets, config, data, or logs:

```bash
CRYPTO_ARB_DEPLOY_HOST=root@example.com scripts/deploy_cloud.sh
```

The helper alternates between `/opt/crypto-arbitrage-releases/blue` on port
`8081` and `green` on port `8082`. It installs dependencies while the current
release stays online, starts the candidate as a read-only standby, verifies
`/api/health`, atomically switches nginx, and then stops the old runtime. A
shared `flock` leader lease ensures only one release can run trading loops. If
the candidate cannot become a healthy leader, nginx and the previous service
are restored automatically. The first migration from the legacy port `8080`
uses a temporary guard lease so the candidate cannot trade before the legacy
service stops.

Live files remain under `/opt/crypto-arbitrage-mvp`: `config.acs.json`, `data/`,
and `/etc/crypto-arbitrage-mvp.env`. The helper excludes those paths from the
release archive and saves a mode-`0600` config snapshot under `data/` before
activation. Nginx must contain exactly one local `proxy_pass` for this service;
the helper changes only its port and runs `nginx -t` before reload. The active
slot is recorded in `data/active_release_slot`.

The health response reports the process role (`standby`, `leader_starting`,
`leader`, or `error`), loop status, order-intent recovery counts, and
`safe_to_replace`. Standby releases reject mutating `/api/*` calls with HTTP
`503` until they hold the leader lease.

The same helper can run from GitHub Actions: the manual `deploy` workflow
(Actions tab → deploy → Run workflow, confirmation input `deploy`) checks out
`main`, runs the test suite, and then executes `scripts/deploy_cloud.sh` over
SSH. Configure repository secrets `DEPLOY_SSH_KEY` and `DEPLOY_HOST`
(optionally `DEPLOY_DIR`, `DEPLOY_SERVICE`, and `DEPLOY_KNOWN_HOSTS` with
`ssh-keyscan` output to pin the host key). Use a dedicated deploy key pair
authorized on the server rather than a personal key, and pin
`DEPLOY_KNOWN_HOSTS` in production so the first connection cannot be
intercepted. The workflow is deliberately manual-only — merging to `main`
never deploys by itself.

The config `label` is the account identity used by the rest of the bot. Multiple accounts on the same exchange should be configured as separate exchange entries with the same `id` and different labels:

```json
{
  "id": "bybit",
  "label": "bybit-mm-a",
  "market_type": "spot",
  "api_key_env": "BYBIT_MM_A_API_KEY",
  "secret_env": "BYBIT_MM_A_SECRET",
  "options": {
    "defaultType": "spot"
  }
}
```

If you need to route an account through a proxy instead of cloud-level static egress, keep the proxy URL in an environment variable and reference only the variable name in config:

```json
{
  "id": "bybit",
  "label": "bybit-mm-a",
  "market_type": "spot",
  "api_key_env": "BYBIT_MM_A_API_KEY",
  "secret_env": "BYBIT_MM_A_SECRET",
  "https_proxy_env": "BYBIT_MM_A_HTTPS_PROXY",
  "options": {
    "defaultType": "spot"
  }
}
```

```bash
export BYBIT_MM_A_API_KEY="..."
export BYBIT_MM_A_SECRET="..."
export BYBIT_MM_A_HTTPS_PROXY="http://user:password@proxy-a.example.com:8080"
```

Supported proxy env fields are `http_proxy_env`, `https_proxy_env`, and `socks_proxy_env` for REST calls, plus `ws_proxy_env`, `wss_proxy_env`, and `ws_socks_proxy_env` for future WebSocket clients. Configure only one REST proxy env and one WebSocket proxy env per exchange entry. SOCKS proxies require the optional `aiohttp_socks` package used by CCXT.

## Web security and operations

The web monitor can be protected with a password and an IP allowlist without storing secrets in Git:

```json
"web_security": {
  "password_env": "CRYPTO_ARB_WEB_PASSWORD",
  "cookie_secret_env": "CRYPTO_ARB_WEB_COOKIE_SECRET",
  "allowed_ips_env": "CRYPTO_ARB_WEB_ALLOWED_IPS",
  "trust_proxy_headers": true,
  "cookie_secure": true,
  "user_store_path": "data/web_users.json",
  "user_workspace_path": "data/user_workspace.sqlite3",
  "credential_master_key_env": "CRYPTO_ARB_CREDENTIAL_MASTER_KEY",
  "registration_enabled": true,
  "bootstrap_admin_email_env": "CRYPTO_ARB_WEB_ADMIN_EMAIL",
  "totp_issuer": "Crypto Trading Dashboard",
  "verification_code_ttl_seconds": 600,
  "verification_resend_seconds": 60,
  "verification_max_attempts": 5
}
```

Set `CRYPTO_ARB_WEB_PASSWORD` and `CRYPTO_ARB_WEB_ALLOWED_IPS` in the server environment file. `CRYPTO_ARB_WEB_ALLOWED_IPS` accepts comma-separated IPs or CIDR ranges. When nginx terminates HTTPS, bind the Python app to `127.0.0.1` and pass `X-Real-IP` / `X-Forwarded-Proto` headers:

When `registration_enabled` is true, users register with an email verification code and choose a unique username. Passwords must be at least eight characters and contain a letter, a number, and a special character. Subsequent logins use username and password. Password reset codes are sent to the registered email. Set `CRYPTO_ARB_WEB_ADMIN_EMAIL` before opening registration: only that address may create the first administrator account. Later accounts start without asset permissions until an administrator assigns them. Existing email users are given a compatible username based on the part before `@`.

Registered users can open `Security` from the dashboard header to bind Google Authenticator or another standard TOTP application. Enabling or disabling TOTP requires both the current password and a valid six-digit code, rotates the session version, and signs out every existing session. Once enabled, every username/password login also requires the current TOTP code. A verified email password reset disables TOTP and rotates its secret so a lost authenticator device cannot permanently lock the account; the user must bind it again after recovery. Setup pages containing the TOTP secret are returned with `Cache-Control: no-store`. When `credential_master_key_env` is configured, TOTP secrets are encrypted at rest with AES-GCM using the same master key as per-user exchange credentials; existing plaintext TOTP fields are migrated atomically at service startup.

Each registered user gets an isolated project workspace. A user creates a project for an asset and quote currency, then adds exchange accounts with trade-only API credentials. The account form selects the exchange, market type, API region, and actual trading pair; `Load Pairs` uses public exchange metadata and shows the reported minimum order value when available. Upbit supports both Global and Indonesia (`id.Upbit`) API regions, and Bithumb user accounts use API v2.0.

Platform exchange balances, orders, P/L, strategy configuration, cancellation,
and live controls are administrator-only. Ordinary users see only their own
project workspace, encrypted account metadata, paper strategies, backtests, and
P/L. Even an administrator cannot test, edit, decrypt, or run another user's
exchange account through the user workspace API; administrators can only
approve or disable that user's project scope. Each owner also has an independent
paper-trading switch and caps for total exposure, daily loss, open orders, and
active strategies.

New user projects remain pending until an administrator approves the asset. Save an exchange account, run its read-only `Test` action, and only then enable it. The test reads market metadata, order book, the selected pair's base/quote balances, and open-order count; it never creates or cancels an order. A successful test is valid for 24 hours. Changing credentials, exchange, API region, market type, or pair resets the account to unverified, and stale or failed checks disable it. API secrets are write-only in the browser and encrypted at rest with AES-GCM; neither state APIs nor audit records return the secret values.

The settings page summarizes each project's seven setup steps and points to the next required action: approval, exchange account, encrypted credentials, read-only connection test, account enablement, paper strategy creation, or paper strategy enablement. Account rows show their own readiness progress and the remaining lifetime of a successful connection test. Workspace readiness is calculated from one batched project/account/strategy snapshot so adding more users and strategies does not create per-strategy database reads.

Users can create isolated paper strategy instances for Market Maker, Auto Buy/Sell, Spot Grid, DCA, and cross-exchange Spot Arbitrage. Each instance is bound to one project and only that owner's verified exchange accounts, with its own order budget, total budget, daily-loss, open-order, slippage, and order-book-age limits. A strategy can be resumed only when its project is active, its account count and symbols match, credentials remain available in the encrypted vault, withdrawal permission is confirmed disabled, and every account has a fresh successful connection test. Credential or market changes, failed/stale connection checks, and project scope changes automatically pause affected instances.

Per-user strategy instances are intentionally paper-only: they do not submit or cancel exchange orders, never decrypt the account API credentials, and their payloads reject `live_enabled`. A background scheduler reads public order books, shares one fetch across strategies and accounts watching the same public market, and runs each enabled instance at its configured refresh/scan interval. The global program switch pauses this scheduler too. Existing global MM, Auto Buy/Sell, and arbitrage runners remain separately configured.

Paper state, virtual fills, and the strategy timeline are stored separately in `data/user_paper_trading.sqlite3`. Writes use one SQLite transaction with optimistic version checks, so a stale cycle cannot overwrite a reset, strategy edit, or newer cycle. State survives service restarts; each user sees only their own strategy records, while administrators retain operational visibility. The UI shows status/reason, progress, virtual open orders, cumulative and daily P/L, recent activity, and the exact retained state/fill/event counts before reset. History is compacted per strategy to the newest 5,000 fills and 500 events.

Historical backtest runs are stored separately in `data/user_backtests.sqlite3`, scoped by owner and retained for the newest 50 runs per user, with at most three active runs per user. A restart marks any in-flight run as interrupted instead of silently presenting a partial result. Public OHLCV responses are cached briefly and shared between identical requests, while the still-open current candle is excluded and persisted strategy results remain user-isolated. The model currently evaluates the completed-candle close path only; it does not reconstruct intrabar price order, maker queue priority, spread changes inside a candle, or real network/exchange latency. Backtest output is research evidence, not a live-return forecast.

The simulator currently models execution as follows:

- Auto Buy/Sell and DCA use visible public order-book depth, configurable paper fees, and a max-slippage check. Auto Buy/Sell evaluates its start and stop price before every fill.
- Market Maker and Spot Grid only fill virtual orders created in an earlier cycle when the current top of book crosses their limit. The remaining virtual book is rebuilt at each configured refresh.
- Spot Arbitrage converts quote currencies with the latest monitor quote rates and commits both simulated legs together; if either leg lacks depth, balance, or acceptable slippage, neither leg is recorded.
- Daily-loss stops, pauses, configuration blocks, and market-data failures clear virtual open orders while retaining balances, fills, and P/L history.

Paper balances are synthetic and are derived from each strategy's `max_total_quote`; they are not the user's real exchange balances. P/L is marked from current mid prices and the latest configured quote-currency conversion. The model does not reproduce maker queue priority, fills that occur between REST snapshots, exchange minimum-notional/precision rejection, network/order latency, transfer costs, or one-leg failure after a real submission. Treat the results as a safety and behavior check, not a live-return forecast. A future per-user live executor must add private balance reservation, fill ownership, exchange reconciliation, idempotent client order IDs, partial-fill hedging, and explicit staged approval before these instances may control real accounts.

Generate the credential encryption key once on the server and store it in the protected service environment file:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Assign that value to `CRYPTO_ARB_CREDENTIAL_MASTER_KEY`. Back it up in a secret manager and do not rotate or delete it without first migrating the encrypted credential database. The environment file and `data/user_workspace.sqlite3` must never be committed to Git.

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

`trust_proxy_headers: true` only makes sense behind a proxy configured this way. The app trusts `X-Real-IP` (which nginx overwrites with the real socket peer) over `X-Forwarded-For`, and when only `X-Forwarded-For` is present it reads the rightmost hop rather than the leftmost one — the leftmost entry is whatever a client put there, while `$proxy_add_x_forwarded_for` always appends the address nginx itself observed. Set `trust_proxy_headers: false` if the app is ever exposed directly to clients, otherwise the IP allowlist and login lockout can be bypassed with a forged header.

Alerts support generic webhook, Telegram, and SMTP email:

```json
"alerts": {
  "enabled": true,
  "min_level": "warning",
  "webhook_url_env": "CRYPTO_ARB_WEBHOOK_URL",
  "telegram_bot_token_env": "CRYPTO_ARB_TELEGRAM_BOT_TOKEN",
  "telegram_chat_id_env": "CRYPTO_ARB_TELEGRAM_CHAT_ID",
  "email_from_env": "CRYPTO_ARB_EMAIL_FROM",
  "email_to_env": "CRYPTO_ARB_EMAIL_TO",
  "smtp_host_env": "CRYPTO_ARB_SMTP_HOST",
  "smtp_port_env": "CRYPTO_ARB_SMTP_PORT",
  "smtp_username_env": "CRYPTO_ARB_SMTP_USERNAME",
  "smtp_password_env": "CRYPTO_ARB_SMTP_PASSWORD",
  "smtp_tls": true,
  "auto_stop_enabled": true,
  "auto_stop_consecutive_errors": 3,
  "daily_report_enabled": true,
  "daily_report_time": "23:59"
}
```

Email registration and password recovery reuse `email_from_env`, `smtp_host_env`, `smtp_port_env`, `smtp_username_env`, `smtp_password_env`, and `smtp_tls`. They do not require alert delivery to be enabled and do not use the fixed `email_to_env` recipient.

Auto-stop pauses the program after repeated degraded/error cycles, or immediately when the daily P/L breaches `risk.max_daily_loss_quote`. Daily reports are sent through the configured alert channels once per local day.

Do not commit API keys, credential encryption keys, proxy URLs, or IP allowlist secrets. Put those values in local shell env vars, Docker/Kubernetes secrets, or the cloud secret manager for each account runner.

The same monitor also tracks the ACS Solana token mint configured in `onchain_monitor`. It shows the top 20 owner wallets inferred from the largest ACS token accounts, their labels when known, balances, supply share, cumulative balance changes since the first stored baseline, and a persisted holder change log. The change history is saved under `onchain_monitor.history_path`, so browser refreshes and service restarts do not reset the displayed wallet changes.

```json
"onchain_monitor": {
  "enabled": true,
  "network": "solana",
  "rpc_url": "https://solana-rpc.publicnode.com",
  "rpc_url_env": "SOLANA_RPC_URLS",
  "rpc_urls": [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com"
  ],
  "token_mint": "5MAYDfq5yxtudAhtfyuMBuHZjgAbaS9tbEyEQYAhDS5y",
  "label": "ACS",
  "top_n": 20,
  "poll_seconds": 60,
  "history_path": "data/onchain_holder_changes.json",
  "address_labels": {
    "8Mm46CsqxiyAputDUp2cXHg41HE3BfynTeMBDwzrMZQH": "Bithumb Hot Wallet 1",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit Hot Wallet",
    "9obNtb5GyUegcs3a1CbBkLuc5hEWynWfJC6gjz5uWQkE": "Coinbase Hot Wallet",
    "22Wnk8PwyWZV7BfkZGJEKT9jGGdtvu7xY6EXeRh7zkBa": "Crypto.com Hot Wallet 3",
    "CbxqZdi1EQneomjjkCkZBmsQenHxEEfs5nDiZxveYoGB": "Access Protocol Upgrade Authority"
  }
}
```

Public Solana RPC endpoints can rate-limit holder calls. For production 24-hour monitoring, set `SOLANA_RPC_URLS` to a dedicated Helius, QuickNode, Alchemy, or similar RPC URL. Multiple endpoints can be comma-separated; the monitor tries them in order and automatically falls back when one endpoint fails.

That config treats USD as the common reporting currency and compares:

- Bithumb `ACS/KRW`
- Bybit `ACS/USDT`
- Coinbase `ACS/USDC`
- Upbit `ACS/USDT`

KRW is not comparable to USD, USDT, or USDC directly. The config includes a fallback `KRW` to `USD` rate and a `quote_rate_sources` entry that tries to derive the live KRW conversion from Bithumb `USDT/KRW` order book mid price. USDT and USDC are treated as 1.0 USD by default; adjust `quote_rates` if you want to model a stablecoin discount or premium.

Before trading, update `fee_bps` to match your account tier and confirm all three exchanges support ACS deposits, withdrawals, and the same ACS network.

## Important assumptions

- Fees are configured manually in basis points.
- Results are pre-trade estimates, not guaranteed fills.
- Cross-exchange spot arbitrage requires pre-funded balances on both sides.
- Futures/perpetual basis trades require margin, liquidation controls, and funding-rate monitoring.
- Options arbitrage requires option margin, assignment/expiry controls, contract-size validation, and liquidity checks.
- Exchange-specific symbols vary. For CCXT perpetuals, symbols often look like `BTC/USDT:USDT`.

## Next steps before live trading

1. Calibrate paper fills against captured live order-book and account fill data, including maker queue and latency assumptions.
2. Enforce exchange minimum-notional, amount precision, and price-tick rules inside every paper strategy.
3. Store full quote and decision history in a database for post-trade analysis.
4. Add exchange statement reconciliation for audited daily realized P/L.
5. Add order-fill lifecycle alerts for stale, partially filled, or repeatedly rejected orders.
