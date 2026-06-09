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

Then open `http://127.0.0.1:8080`. The page shows scan health, latency, converted ACS bid/ask prices, quote rates, and any live opportunities. The program switch next to the status pill pauses or resumes scanning without stopping the web server.

The top row shows configured ACS position and P/L attribution:

```json
"portfolio": {
  "enabled": true,
  "asset": "ACS",
  "position_base": 0.0,
  "average_entry_price": 0.0,
  "realized_pnl": {
    "market_maker": 0.0,
    "arbitrage": 0.0
  }
}
```

`Price Move` is calculated from `position_base * (current_mark_price - average_entry_price)`, where the mark price is the average converted mid price across available ACS spot books. `MM P/L` and `Arb P/L` currently read from `realized_pnl`; once live fills are recorded, those fields can be populated automatically from market-maker and arbitrage executions.

The web monitor also shows a dry-run market maker ladder when `market_maker.enabled` is true. With the ACS config and `--poll-seconds 1`, the page fetches the latest REST order book every second and recalculates the 20 planned bid/ask orders from the fresh mid price. The ACS example config targets Bybit `ACS/USDT` with 10 bid levels and 10 ask levels, spread symmetrically within a 10% one-sided price band around the mid price. For example, a 10-level ladder with `price_band_pct: 10.0` places levels roughly 1%, 2%, ..., 10% away from the mid price on each side. If you want a 10% total width, use `price_band_pct: 5.0`.

```json
"market_maker": {
  "enabled": true,
  "exchange": "bybit-spot",
  "symbol": "ACS/USDT",
  "levels": 10,
  "price_band_pct": 10.0,
  "quote_per_level": 1.0,
  "min_order_quote": 0.1,
  "min_distance_bps": 0.0,
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

The market maker CLI clamps its effective loop interval to at least 1 second. Every-second live replacement can still hit exchange order-rate limits quickly because it may cancel and place up to 20 orders per cycle.

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

1. Add account balance checks and inventory targets per exchange.
2. Add paper trading with order lifecycle simulation.
3. Add stronger exchange precision and minimum order validation.
4. Add transfer and withdrawal availability checks.
5. Add an execution engine with kill switches, max loss, and per-exchange rate limits.
6. Store quotes, decisions, and fills in a database for post-trade analysis.
