"""Daily price acquisition for the bubble-labelling pipeline.

Primary source is Binance spot klines; CoinGecko market_chart is the fallback.
Both are optional -- ``load_csv_directory`` lets the pipeline run entirely on
data the researcher has already licensed and stored.
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone, timedelta

BINANCE = "https://api.binance.com/api/v3/klines"
COINGECKO = "https://api.coingecko.com/api/v3/coins/{id}/market_chart/range"

DEFAULT_UNIVERSE = [
    "BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOGE", "DOT", "LTC", "TRX",
    "AVAX", "LINK", "ATOM", "XLM", "UNI", "ETC", "BCH", "FIL", "NEAR", "ALGO",
    "VET", "ICP", "HBAR", "APE", "SAND", "MANA", "AXS", "AAVE", "EOS", "THETA",
    "XTZ", "EGLD", "FTM", "CAKE", "ZEC", "DASH", "NEO", "IOTA", "CHZ", "SHIB",
    "PEPE", "TON", "SUI", "ARB", "OP", "INJ", "RUNE", "GRT", "LDO", "CRV",
]

# Excluded by design: stablecoins, leveraged/inverse tokens, wrapped duplicates.
EXCLUDED = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "PAXG",
            "WBTC", "WETH", "STETH", "WBETH"}


class DataUnavailable(RuntimeError):
    """Raised when no configured source can be reached."""


def _get_json(url: str, timeout: int = 30, retries: int = 4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bsadf-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise DataUnavailable(f"{url} unreachable: {last}")


def fetch_binance_daily(symbol: str, start: str, end: str) -> list[tuple[str, float]]:
    """Daily closes for ``{symbol}USDT``. Returns [(iso_date, close), ...]."""
    start_ms = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows, cursor = [], start_ms
    while cursor < end_ms:
        url = (f"{BINANCE}?symbol={symbol}USDT&interval=1d"
               f"&startTime={cursor}&endTime={end_ms}&limit=1000")
        batch = _get_json(url)
        if not batch:
            break
        for k in batch:
            d = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).date().isoformat()
            rows.append((d, float(k[4])))
        cursor = batch[-1][0] + 86_400_000
        time.sleep(0.25)
    return rows


def fetch_coingecko_daily(coin_id: str, start: str, end: str) -> list[tuple[str, float]]:
    frm = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    to = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    url = COINGECKO.format(id=coin_id) + f"?vs_currency=usd&from={frm}&to={to}"
    payload = _get_json(url)
    out = {}
    for ts, price in payload.get("prices", []):
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
        out[d] = float(price)          # last observation of the day wins
    return sorted(out.items())


def load_csv_directory(path: str) -> dict[str, list[tuple[str, float]]]:
    """Load ``<SYMBOL>.csv`` files with columns ``date,close``."""
    series = {}
    for name in sorted(os.listdir(path)):
        if not name.lower().endswith(".csv"):
            continue
        symbol = os.path.splitext(name)[0].upper()
        if symbol in EXCLUDED:
            continue
        rows = []
        with open(os.path.join(path, name), newline="", encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                keys = {k.lower(): k for k in rec}
                d = rec[keys.get("date") or keys.get("time")]
                c = rec[keys.get("close") or keys.get("price")]
                if c in (None, "", "NaN"):
                    continue
                rows.append((str(d)[:10], float(c)))
        if rows:
            series[symbol] = sorted(rows)
    if not series:
        raise DataUnavailable(f"no usable CSV files in {path}")
    return series


def synthetic_universe(n_coins: int = 40, start: str = "2021-01-01", end: str = "2025-12-31",
                       n_market_cycles: int = 4, seed: int = 20260819):
    """Synthetic panel with a known number of market-wide explosive cycles.

    Used to validate the pipeline end to end when no market data is reachable.
    The generator is the pipeline's own test oracle, never a research input.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    n_days = (d1 - d0).days + 1
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(n_days)]

    # market-wide cycle anchors, plus a per-coin jitter so entries are not identical
    anchors = sorted(rng.choice(np.arange(200, n_days - 200), size=n_market_cycles, replace=False))
    series = {}
    for c in range(n_coins):
        name = f"SYN{c:02d}"
        eps = rng.standard_normal(n_days) * 0.045
        log_p = np.cumsum(eps) + 5.0
        for a in anchors:
            if rng.random() > 0.55:          # not every coin joins every cycle
                continue
            s = int(a + rng.integers(-12, 13))
            length = int(rng.integers(28, 70))
            s = max(0, min(s, n_days - length - 1))
            delta = float(rng.uniform(0.012, 0.03))
            bump = np.cumsum(np.full(length, delta) * (1 + np.arange(length) / length))
            log_p[s : s + length] += bump
            log_p[s + length :] += bump[-1] - float(rng.uniform(0.5, 0.9)) * bump[-1]
        series[name] = list(zip(dates, np.exp(log_p)))
    return series, [dates[a] for a in anchors]
