# Crypto Arbitrage MVP

This is a dry-run scanner for two common crypto arbitrage families:

- Spot spread arbitrage across exchanges.
- Spot versus futures or perpetual basis arbitrage.

The first version intentionally does not place live orders. It estimates executable edge from order book depth, fees, and a target notional size, then prints opportunities. Add live execution only after paper trading, reconciliation, and account-level risk controls are proven.

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

Use `config.acs.example.json` when monitoring ACS across Bithumb, Bybit, and Coinbase:

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

Then open `http://127.0.0.1:8080`. The page shows scan health, latency, converted ACS bid/ask prices, quote rates, and any live opportunities.

The same monitor also tracks the ACS Solana token mint configured in `onchain_monitor`. It shows the top 10 owner wallets inferred from the largest ACS token accounts, their balances, supply share, and balance changes between Solana polling rounds.

```json
"onchain_monitor": {
  "enabled": true,
  "network": "solana",
  "rpc_url": "https://solana-rpc.publicnode.com",
  "token_mint": "5MAYDfq5yxtudAhtfyuMBuHZjgAbaS9tbEyEQYAhDS5y",
  "label": "ACS",
  "top_n": 10,
  "poll_seconds": 60
}
```

Public Solana RPC endpoints can rate-limit holder calls. For production 24-hour monitoring, replace `onchain_monitor.rpc_url` with a dedicated Helius, QuickNode, Alchemy, or similar RPC URL.

That config treats USD as the common reporting currency and compares:

- Bithumb `ACS/KRW`
- Bybit `ACS/USDT`
- Coinbase `ACS/USDC`

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
3. Add exchange precision and minimum order validation.
4. Add transfer and withdrawal availability checks.
5. Add an execution engine with kill switches, max loss, and per-exchange rate limits.
6. Store quotes, decisions, and fills in a database for post-trade analysis.
