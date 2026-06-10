# Crypto Arbitrage MVP

This is a dry-run scanner for two common crypto arbitrage families:

- Spot spread arbitrage across exchanges.
- Spot versus futures or perpetual basis arbitrage.

The arbitrage scanner does not place live orders. It estimates executable edge from order book depth, fees, and a target notional size, then prints opportunities. The market maker command defaults to dry-run planning and requires explicit live confirmation before placing orders.

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

## Run modes

```bash
# Run both strategies once.
python -m arbitrage_bot.main --config config.json --once

# Run only cross-exchange spot spread scanning.
python -m arbitrage_bot.main --config config.json --strategy spot-spread --once

# Run only spot-futures basis scanning.
python -m arbitrage_bot.main --config config.json --strategy cash-and-carry --once

# Keep polling.
python -m arbitrage_bot.main --config config.json
```

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

When the exchange adapter supports it, MM uses batch order APIs for ladder placement and cancellation. Bybit spot is enabled for batch create/cancel; unsupported exchanges automatically stay on the guarded single-order path.

The web background MM loop also has an optional order book cache. If the active exchange client advertises `watchOrderBook`, the loop maintains a fresh WebSocket book and builds the next ladder from that cached snapshot; otherwise it safely falls back to the existing REST fetch path. The monitor shows the current MM market data source, age, and whether WebSocket watching is unsupported. This keeps today's setup compatible while leaving a clean upgrade path for native exchange WebSocket clients or `ccxt.pro`.

`quote_per_level` and `min_order_quote` are always expressed in the selected exchange pair's quote currency: USDT for Bybit/Upbit `ACS/USDT`, USDC for Coinbase `ACS/USDC`, and KRW for Bithumb `ACS/KRW`. Risk checks convert those quote amounts into `common_quote_currency` using `quote_rates` before applying `max_order_quote`, `max_cycle_quote`, and exposure limits. If a quote rate is missing, live MM is blocked.

Exchange support is intentionally conservative. Bybit, Coinbase, and Upbit support post-only limit orders through ccxt. Bithumb does not expose post-only support through ccxt, so Bithumb MM with `post_only: true` is blocked before order placement; only set `post_only: false` and `risk.require_post_only: false` for Bithumb if you explicitly accept taker-fill risk. Bithumb also does not support client order ids through ccxt, so its MM orders can only be tracked in memory until the process restarts.

Always run the account preflight before live MM. Public ccxt metadata can expose exchange minimums, but they can change. In a recent ACS public check, Bybit `ACS/USDT` required about 5 USDT minimum order cost, while Bithumb `ACS/KRW` reported a 500 KRW minimum cost. If you raise `quote_per_level`, also raise `risk.max_cycle_quote` enough for `levels * 2 * quote_per_level` after common-currency conversion.

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
export UPBIT_API_KEY="..."
export UPBIT_SECRET="..."

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

The monitor can also show and configure a dry-run Auto Buy/Sell plan when `slow_execution.enabled` is true. This tool submits one buy or sell marketable limit order at the current execution-side top of book: buys use the best ask and sells use the best bid. The speed is configured with `interval_seconds`; live orders can also be canceled after `order_ttl_seconds`. The config key stays `slow_execution` for backward compatibility, but the user-facing feature name is Auto Buy/Sell.

```json
"slow_execution": {
  "enabled": true,
  "exchange": "bybit-spot",
  "symbol": "ACS/USDT",
  "side": "sell",
  "total_base": 100000.0,
  "total_quote": 0.0,
  "slice_base": 0.0,
  "slice_base_min": 3000.0,
  "slice_base_max": 5000.0,
  "slice_quote": 0.0,
  "randomize_slice": true,
  "interval_seconds": 60.0,
  "order_ttl_seconds": 20.0,
  "stop_price": 0.00012,
  "min_order_quote": 0.1,
  "post_only": false,
  "cancel_existing_orders": false,
  "client_order_prefix": "crypto-arb-slow"
}
```

Use `total_base` to cap the total base asset amount, such as ACS, or `total_quote` to cap the total quote currency amount, such as USDC, USDT, or KRW. If both are set, Auto Buy/Sell stops when either cap is reached. Progress is shown against `total_quote` when it is configured; otherwise it uses `total_base`.

Use exactly one per-order sizing mode: `slice_base`, `slice_quote`, or the `slice_base_min`/`slice_base_max` range. For example, a 3,000 to 5,000 ACS range with `randomize_slice: true` chooses a random amount in that range for each planned order. With `randomize_slice: false`, the range uses the minimum amount as the fixed slice. `slice_quote: 10` means each slice is about 10 USDT worth of ACS at the current execution-side price. Auto Buy/Sell uses a marketable limit price: buys at the current best ask and sells at the current best bid. Keep `slow_execution.post_only` false and `risk.require_post_only` false for this strategy if you want immediate taker-style execution.

The web page exposes runtime controls for the selected account, `enabled`, `side`, `total_base`, `total_quote`, the min/max base order size range, randomization, `interval_seconds`, `order_ttl_seconds`, and `stop_price`. The account checkbox list comes from `spot_exchanges`, so multiple accounts should be added as separate exchange entries with distinct `label` values. These page edits affect the running monitor immediately but do not write back to `config.acs.json`.

For `stop_price`, a sell schedule stops when the best bid is at or below the stop price. A buy schedule stops when the best ask is at or above the stop price.

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

Without `--exchange`, it checks all configured exchange entries. Without `--symbol`, it uses symbols from `spot_markets`, `market_maker`, and Auto Buy/Sell.

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
  "max_recent_events": 50
}
```

The monitor shows the current risk settings and normalized trade log rows in the `Risk & Events` table. You can also inspect the same log from the command line:

```bash
PYTHONPATH=src .venv/bin/python -m arbitrage_bot.trade_log \
  --config config.acs.json \
  --limit 20
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

## Cloud deployment and per-account IPs

For production, the cleaner setup is one exchange account per runner, container, or VM, with that runtime bound to its own static outbound IP at the cloud network layer. For example, run `bybit-mm-a`, `coinbase-arb-a`, and `upbit-arb-a` as separate processes or containers, then assign each one a dedicated NAT gateway, elastic IP, or cloud egress address. If the exchange account has IP whitelisting enabled, whitelist only the IP assigned to that account.

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
  "cookie_secure": true
}
```

Set `CRYPTO_ARB_WEB_PASSWORD` and `CRYPTO_ARB_WEB_ALLOWED_IPS` in the server environment file. `CRYPTO_ARB_WEB_ALLOWED_IPS` accepts comma-separated IPs or CIDR ranges. When nginx terminates HTTPS, bind the Python app to `127.0.0.1` and pass `X-Real-IP` / `X-Forwarded-Proto` headers.

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

Auto-stop pauses the program after repeated degraded/error cycles, or immediately when the daily P/L breaches `risk.max_daily_loss_quote`. Daily reports are sent through the configured alert channels once per local day.

Do not commit API keys, proxy URLs, or IP allowlist secrets. Put those values in local shell env vars, Docker/Kubernetes secrets, or the cloud secret manager for each account runner.

The same monitor also tracks the ACS Solana token mint configured in `onchain_monitor`. It shows the top 20 owner wallets inferred from the largest ACS token accounts, their labels when known, balances, supply share, and balance changes between Solana polling rounds.

```json
"onchain_monitor": {
  "enabled": true,
  "network": "solana",
  "rpc_url": "https://solana-rpc.publicnode.com",
  "token_mint": "5MAYDfq5yxtudAhtfyuMBuHZjgAbaS9tbEyEQYAhDS5y",
  "label": "ACS",
  "top_n": 20,
  "poll_seconds": 60,
  "address_labels": {
    "8Mm46CsqxiyAputDUp2cXHg41HE3BfynTeMBDwzrMZQH": "Bithumb Hot Wallet 1",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit Hot Wallet",
    "9obNtb5GyUegcs3a1CbBkLuc5hEWynWfJC6gjz5uWQkE": "Coinbase Hot Wallet",
    "22Wnk8PwyWZV7BfkZGJEKT9jGGdtvu7xY6EXeRh7zkBa": "Crypto.com Hot Wallet 3",
    "CbxqZdi1EQneomjjkCkZBmsQenHxEEfs5nDiZxveYoGB": "Access Protocol Upgrade Authority"
  }
}
```

Public Solana RPC endpoints can rate-limit holder calls. For production 24-hour monitoring, replace `onchain_monitor.rpc_url` with a dedicated Helius, QuickNode, Alchemy, or similar RPC URL.

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
- Exchange-specific symbols vary. For CCXT perpetuals, symbols often look like `BTC/USDT:USDT`.

## Next steps before live trading

1. Add paper trading with order lifecycle simulation.
2. Add transfer and withdrawal availability checks.
3. Store full quote and decision history in a database for post-trade analysis.
4. Add exchange statement reconciliation for audited daily realized P/L.
5. Add order-fill lifecycle alerts for stale, partially filled, or repeatedly rejected orders.
