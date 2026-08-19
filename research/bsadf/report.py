"""Produce the proposal's Table 4 (effective sample size) from a price panel."""

from __future__ import annotations

import json
import math
from datetime import date

import numpy as np

from .episodes import (Episode, default_min_duration, flagged_day_count,
                       market_cycles, stamp_episodes)
from .psy import (bsadf_sequence, controlled_critical_values, min_window,
                  monte_carlo_critical_values)


def run_panel(series: dict[str, list[tuple[str, float]]], *, lags: int = 0,
              level: float = 0.95, reps: int = 2000, min_duration: int | None = None,
              merge_gap: int = 30, cycle_window: int = 30, min_obs: int = 400,
              seed: int = 20260819, verbose: bool = True) -> dict:
    """Label every coin with the two-stage PSY procedure, then aggregate.

    Stage 1 is the GSADF test against the sup distribution: does this coin
    contain any explosive episode at all?  Stage 2 date-stamps only the coins
    that pass, and does so against a band calibrated to a path-wise false-alarm
    rate of ``1 - level`` rather than the pointwise quantile.  The naive
    procedure (pointwise band, no gate) is computed alongside it, because the
    gap between the two IS the expected false-label rate the design has to
    report.
    """
    cv_cache: dict[int, dict] = {}
    per_coin, episodes, naive_episodes = [], [], []
    total_rows = total_flagged = total_flagged_naive = 0
    alpha = 1.0 - level

    for coin, rows in sorted(series.items()):
        rows = [r for r in rows if r[1] and r[1] > 0]
        if len(rows) < min_obs:
            if verbose:
                print(f"  skip {coin}: {len(rows)} usable days < {min_obs}")
            continue
        dates = [r[0] for r in rows]
        price = np.array([r[1] for r in rows], dtype=float)
        logp = np.log(price)
        n = len(logp)

        seq = bsadf_sequence(logp, lags=lags)
        stat = float(np.nanmax(seq)) if np.any(~np.isnan(seq)) else float("nan")
        if n not in cv_cache:
            if verbose:
                print(f"  simulating null distribution for T={n} ({reps} reps)...")
            cv_cache[n] = controlled_critical_values(n, lags=lags, reps=reps,
                                                     alpha=alpha, base_quantile=level,
                                                     seed=seed)
        pack = cv_cache[n]
        band, pointwise = pack["band"], pack["pointwise"]
        gsadf_cv = pack["gsadf_cv"].get(level) or pack["gsadf_cv"][0.95]
        passes = bool(stat > gsadf_cv)

        md = min_duration if min_duration is not None else default_min_duration(n)
        eps = (stamp_episodes(coin, dates, seq, band, min_duration=md, merge_gap=merge_gap)
               if passes else [])
        eps_naive = stamp_episodes(coin, dates, seq, pointwise,
                                   min_duration=md, merge_gap=merge_gap)
        flagged = flagged_day_count(seq, band) if passes else 0

        episodes.extend(eps)
        naive_episodes.extend(eps_naive)
        total_rows += n
        total_flagged += flagged
        total_flagged_naive += flagged_day_count(seq, pointwise)
        per_coin.append({
            "coin": coin, "obs": n, "start": dates[0], "end": dates[-1],
            "gsadf": stat, "gsadf_cv": gsadf_cv, "passes_gsadf": passes,
            "flagged_days": flagged, "episodes": len(eps),
            "episodes_naive": len(eps_naive),
            "min_window": min_window(n), "min_duration": md,
            "band_shift": pack["shift"], "null_crossing_rate": pack["null_crossing_rate"],
        })
        if verbose:
            mark = "PASS" if passes else "  - "
            print(f"  {coin:<6} T={n:<5} GSADF={stat:6.2f} cv={gsadf_cv:5.2f} {mark} "
                  f"flagged={flagged:<4} episodes={len(eps)} (naive {len(eps_naive)})")

    cycles = market_cycles(episodes, cycle_window=cycle_window)
    return {
        "params": {"lags": lags, "level": level, "reps": reps, "merge_gap": merge_gap,
                   "cycle_window": cycle_window, "min_obs": min_obs, "seed": seed,
                   "procedure": "GSADF gate + path-wise calibrated BSADF band"},
        "per_coin": per_coin,
        "episodes": [e.as_dict() for e in episodes],
        "cycles": cycles,
        "totals": {
            "coins": len(per_coin),
            "coins_passing_gsadf": sum(1 for c in per_coin if c["passes_gsadf"]),
            "coin_days": total_rows,
            "flagged_coin_days": total_flagged,
            "flagged_coin_days_naive": total_flagged_naive,
            "coin_episodes": len(episodes),
            "coin_episodes_naive": len(naive_episodes),
            "market_cycles": len(cycles),
            "max_variables": max(1, len(cycles)),
        },
    }


def table4_rows(result: dict) -> list[list[str]]:
    """The four data columns of Table 4, filled with realised numbers."""
    t = result["totals"]
    p = result["params"]
    coins, days = t["coins"], t["coin_days"]
    flagged, eps, cycles = t["flagged_coin_days"], t["coin_episodes"], t["market_cycles"]
    naive_eps = t["coin_episodes_naive"]
    per_ep = (flagged / eps) if eps else float("nan")
    pct = int(p["level"] * 100)
    return [
        ["币-日观测行数", f"{coins} 币 × 平均 {days // max(coins, 1)} 日",
         f"{days:,} 行", "不是有效样本量；不能用于辩护模型复杂度"],
        ["通过 GSADF 检验的币种", f"GSADF > {pct}% sup 分布临界值",
         f"{t['coins_passing_gsadf']} / {coins}",
         "未通过者不进入日期标定，避免按构造产生的横截面假标签"],
        ["泡沫标记日", f"BSADF > 路径级校准band（假警报率 {100 - pct}%，{p['reps']} 次模拟）",
         f"{flagged:,} 币-日",
         f"平均每个 episode 含 {per_ep:.1f} 个标记日，彼此高度相关" if eps else "无"],
        ["币内独立 episode", f"最短持续期规则 + 间隔 < {p['merge_gap']} 日者合并",
         f"{eps}",
         f"每个 episode 约提供 1 个有效观测；点态band不设门槛时为 {naive_eps} 个，"
         f"差额即预期假标签量"],
        ["全市场独立周期", f"进入日相隔 ≤ {p['cycle_window']} 日者归为同一周期",
         f"{cycles}", "双向聚类下的有效 cluster 数；决定推断精度"],
        ["可支持的自变量数", "每个全市场周期不超过 1 个自由参数",
         f"主模型 ≤ {max(1, cycles)} 个预设变量", "超出部分只能作为探索性结果报告"],
    ]


def to_markdown(result: dict) -> str:
    t = result["totals"]
    lines = ["# BSADF 有效样本量报告", ""]
    lines.append(f"- 币种数: {t['coins']}")
    lines.append(f"- 币-日行数: {t['coin_days']:,}")
    lines.append(f"- 泡沫标记日: {t['flagged_coin_days']:,}")
    lines.append(f"- 通过 GSADF 检验的币种: {t['coins_passing_gsadf']} / {t['coins']}")
    lines.append(f"- 币内独立 episode: {t['coin_episodes']}"
                 f"（点态band无门槛时 {t['coin_episodes_naive']}）")
    lines.append(f"- 全市场独立周期: {t['market_cycles']}")
    lines.append("")
    lines.append("## 表 4")
    lines.append("| 层级 | 推算依据 | 数量级 | 对设计的约束 |")
    lines.append("|---|---|---|---|")
    for row in table4_rows(result):
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## 全市场周期")
    lines.append("| 起 | 止 | episode 数 | 币种数 |")
    lines.append("|---|---|---|---|")
    for c in result["cycles"]:
        lines.append(f"| {c['start']} | {c['end']} | {c['n_episodes']} | {c['n_coins']} |")
    lines.append("")
    lines.append("## 分币种")
    lines.append("| 币种 | 观测数 | 起 | 止 | GSADF | 通过 | 标记日 | episode | 无门槛 episode |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for c in result["per_coin"]:
        lines.append(f"| {c['coin']} | {c['obs']} | {c['start']} | {c['end']} | "
                     f"{c['gsadf']:.2f} | {'是' if c['passes_gsadf'] else '否'} | "
                     f"{c['flagged_days']} | {c['episodes']} | {c['episodes_naive']} |")
    return "\n".join(lines)
