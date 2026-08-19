"""CLI: build the bubble-labelling panel and emit the Table 4 numbers.

Examples
--------
    python -m bsadf.cli --source binance --start 2021-01-01 --end 2025-12-31 --out out/
    python -m bsadf.cli --source csv --csv-dir data/prices --out out/
    python -m bsadf.cli --source synthetic --reps 500 --out out/     # pipeline check only
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import data as dataset
from .report import run_panel, to_markdown


def build_parser():
    p = argparse.ArgumentParser(prog="bsadf", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=("binance", "coingecko", "csv", "synthetic"),
                   default="binance")
    p.add_argument("--csv-dir", help="directory of <SYMBOL>.csv with date,close")
    p.add_argument("--symbols", help="comma-separated override of the coin universe")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--lags", type=int, default=0,
                   help="ADF lag order; 0 reproduces the PSY critical-value tables")
    p.add_argument("--level", type=float, default=0.95)
    p.add_argument("--reps", type=int, default=2000, help="Monte-Carlo replications")
    p.add_argument("--min-duration", type=int, default=None,
                   help="default: round(log T)")
    p.add_argument("--merge-gap", type=int, default=30)
    p.add_argument("--cycle-window", type=int, default=30)
    p.add_argument("--min-obs", type=int, default=400)
    p.add_argument("--seed", type=int, default=20260819)
    p.add_argument("--out", default="out")
    return p


def collect(args) -> dict:
    if args.source == "csv":
        if not args.csv_dir:
            raise SystemExit("--csv-dir is required with --source csv")
        return dataset.load_csv_directory(args.csv_dir)
    if args.source == "synthetic":
        series, anchors = dataset.synthetic_universe(start=args.start, end=args.end,
                                                     seed=args.seed)
        print(f"synthetic panel: {len(series)} coins, planted cycles at {anchors}")
        return series
    symbols = ([s.strip().upper() for s in args.symbols.split(",")]
               if args.symbols else dataset.DEFAULT_UNIVERSE)
    symbols = [s for s in symbols if s not in dataset.EXCLUDED]
    series = {}
    for sym in symbols:
        try:
            rows = (dataset.fetch_binance_daily(sym, args.start, args.end)
                    if args.source == "binance"
                    else dataset.fetch_coingecko_daily(sym.lower(), args.start, args.end))
        except dataset.DataUnavailable as exc:
            print(f"  {sym}: {exc}", file=sys.stderr)
            continue
        if rows:
            series[sym] = rows
            print(f"  {sym}: {len(rows)} daily closes")
    if not series:
        raise SystemExit(
            "no price data retrieved. If outbound access to the exchange API is "
            "blocked, download the closes separately and rerun with "
            "--source csv --csv-dir <dir>."
        )
    return series


def main(argv=None):
    args = build_parser().parse_args(argv)
    series = collect(args)
    result = run_panel(series, lags=args.lags, level=args.level, reps=args.reps,
                       min_duration=args.min_duration, merge_gap=args.merge_gap,
                       cycle_window=args.cycle_window, min_obs=args.min_obs,
                       seed=args.seed)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    md = to_markdown(result)
    with open(os.path.join(args.out, "table4.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print()
    print(md)
    return result


if __name__ == "__main__":
    main()
