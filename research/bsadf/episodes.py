"""Date-stamping BSADF exceedances and collapsing them into independent events.

Three levels matter for the proposal's Table 4 and they are very different sizes:

* flagged coin-days   -- raw exceedances, badly autocorrelated
* coin-level episodes -- runs of exceedances merged within a coin
* market-wide cycles  -- episodes that overlap in time across coins

The last one is the effective cluster count for two-way clustered inference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class Episode:
    coin: str
    start_index: int
    end_index: int
    start_date: str
    end_date: str
    duration: int
    peak_strength: float

    def as_dict(self):
        return asdict(self)


def default_min_duration(n_obs: int) -> int:
    """PSY's minimum-duration rule for daily data."""
    return max(3, int(round(math.log(max(n_obs, 3)))))


def stamp_episodes(coin: str, dates, bsadf: np.ndarray, cv: np.ndarray,
                   min_duration: int | None = None, merge_gap: int = 30) -> list[Episode]:
    """Turn a BSADF sequence and its recursive critical values into episodes."""
    bsadf = np.asarray(bsadf, dtype=float)
    cv = np.asarray(cv, dtype=float)
    n = len(bsadf)
    if min_duration is None:
        min_duration = default_min_duration(n)

    flagged = np.zeros(n, dtype=bool)
    valid = ~np.isnan(bsadf) & ~np.isnan(cv)
    flagged[valid] = bsadf[valid] > cv[valid]

    runs = []
    start = None
    for i in range(n):
        if flagged[i] and start is None:
            start = i
        elif not flagged[i] and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, n - 1))

    runs = [r for r in runs if (r[1] - r[0] + 1) >= min_duration]

    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] - 1 < merge_gap:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)

    out = []
    for s, e in merged:
        seg = bsadf[s : e + 1] - cv[s : e + 1]
        out.append(Episode(
            coin=coin,
            start_index=int(s),
            end_index=int(e),
            start_date=str(dates[s]),
            end_date=str(dates[e]),
            duration=int(e - s + 1),
            peak_strength=float(np.nanmax(seg)) if seg.size else float("nan"),
        ))
    return out


def flagged_day_count(bsadf: np.ndarray, cv: np.ndarray) -> int:
    bsadf = np.asarray(bsadf, dtype=float)
    cv = np.asarray(cv, dtype=float)
    valid = ~np.isnan(bsadf) & ~np.isnan(cv)
    return int(np.sum(bsadf[valid] > cv[valid]))


def market_cycles(episodes: list[Episode], cycle_window: int = 30) -> list[dict]:
    """Chain coin-level episodes into market-wide cycles by calendar overlap.

    Two episodes join the same cycle when their start dates are within
    ``cycle_window`` days of the running cluster, i.e. single-linkage on entries.
    """
    import datetime as _dt

    def parse(d):
        return _dt.date.fromisoformat(str(d)[:10])

    items = sorted(episodes, key=lambda e: parse(e.start_date))
    cycles = []
    for ep in items:
        d = parse(ep.start_date)
        if cycles and (d - cycles[-1]["_last"]).days <= cycle_window:
            cycles[-1]["coins"].append(ep.coin)
            cycles[-1]["episodes"] += 1
            cycles[-1]["end"] = max(cycles[-1]["end"], parse(ep.end_date))
            cycles[-1]["_last"] = d
        else:
            cycles.append({
                "start": d, "end": parse(ep.end_date), "_last": d,
                "coins": [ep.coin], "episodes": 1,
            })
    out = []
    for c in cycles:
        out.append({
            "start": c["start"].isoformat(),
            "end": c["end"].isoformat(),
            "n_episodes": c["episodes"],
            "n_coins": len(set(c["coins"])),
            "coins": sorted(set(c["coins"])),
        })
    return out
