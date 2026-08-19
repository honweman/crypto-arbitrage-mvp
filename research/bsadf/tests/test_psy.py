"""Validation suite for the PSY implementation.

The three checks that matter for a thesis: the simulated critical values match
the published finite-sample tables, empirical size is at the nominal level, and
a planted explosive episode is detected and date-stamped near its true start.
"""

import math
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bsadf.episodes import market_cycles, stamp_episodes
from bsadf.psy import (bsadf_sequence, controlled_critical_values, gsadf, min_window,
                       monte_carlo_critical_values)


# Phillips, Shi and Yu (2015, IER) finite-sample GSADF critical values,
# intercept-only ADF with lag order 0 and r0 = 0.01 + 1.8/sqrt(T).
PSY_REFERENCE = {200: {0.90: 1.89, 0.95: 2.14, 0.99: 2.60},
                 400: {0.90: 1.94, 0.95: 2.16, 0.99: 2.61}}


@pytest.mark.parametrize("n_obs", [200, 400])
def test_critical_values_match_published_tables(n_obs):
    cv = monte_carlo_critical_values(n_obs, lags=0, reps=1500, seed=11)
    for q, reference in PSY_REFERENCE[n_obs].items():
        assert abs(cv["gsadf"][q] - reference) < 0.22, (q, cv["gsadf"][q], reference)


def test_min_window_rule():
    assert min_window(1826) == int(math.floor((0.01 + 1.8 / math.sqrt(1826)) * 1826))


def test_pointwise_band_is_not_size_controlled():
    """Regression guard: the pointwise quantile over-flags badly, by construction.

    Inspecting the path at every endpoint means a driftless random walk crosses
    the pointwise 95% sequence almost surely.  This is the reason the pipeline
    uses a GSADF gate plus a calibrated band, and the reason the design has to
    report an expected false-label rate.
    """
    n_obs, trials = 300, 120
    cv = monte_carlo_critical_values(n_obs, lags=0, reps=1200, seed=101)[0.95]
    rng = np.random.default_rng(4242)
    breaches = 0
    for _ in range(trials):
        seq = bsadf_sequence(np.cumsum(rng.standard_normal(n_obs)), lags=0)
        valid = ~np.isnan(seq) & ~np.isnan(cv)
        breaches += bool(np.any(seq[valid] > cv[valid]))
    assert breaches / trials > 0.5


def test_calibrated_band_has_nominal_path_wise_size():
    n_obs, trials = 300, 300
    pack = controlled_critical_values(n_obs, lags=0, reps=1500, alpha=0.05, seed=101)
    assert abs(pack["null_crossing_rate"] - 0.05) < 0.02
    band = pack["band"]
    rng = np.random.default_rng(4242)          # independent of the calibration draw
    breaches = 0
    for _ in range(trials):
        seq = bsadf_sequence(np.cumsum(rng.standard_normal(n_obs)), lags=0)
        valid = ~np.isnan(seq) & ~np.isnan(band)
        breaches += bool(np.any(seq[valid] > band[valid]))
    size = breaches / trials
    assert 0.01 < size < 0.12, size


def test_detects_and_dates_a_planted_bubble():
    rng = np.random.default_rng(7)
    n_obs, start, length = 600, 380, 60
    logp = np.cumsum(rng.standard_normal(n_obs) * 0.02) + 5.0
    bump = np.cumsum(np.full(length, 0.02) * (1 + np.arange(length) / length))
    logp[start : start + length] += bump
    logp[start + length :] += bump[-1] * 0.35

    cv = monte_carlo_critical_values(n_obs, lags=0, reps=800, seed=13)[0.95]
    seq = bsadf_sequence(logp, lags=0)
    assert gsadf(logp, lags=0) > cv[~np.isnan(cv)].max() * 0.8

    dates = [f"2021-01-01"] * n_obs        # index-based assertions below
    dates = np.array(range(n_obs)).astype(str).tolist()
    eps = stamp_episodes("TEST", dates, seq, cv, min_duration=5, merge_gap=20)
    assert eps, "planted bubble not detected"
    detected = min(eps, key=lambda e: abs(e.start_index - start))
    assert abs(detected.start_index - start) <= 45, detected.start_index


def test_two_stage_procedure_yields_almost_no_episodes_under_the_null():
    """GSADF gate + calibrated band: random walks should almost never be labelled."""
    n_obs, trials = 500, 30
    pack = controlled_critical_values(n_obs, lags=0, reps=1000, alpha=0.05, seed=17)
    band, gate = pack["band"], pack["gsadf_cv"][0.95]
    rng = np.random.default_rng(99)
    total = 0
    for _ in range(trials):
        y = np.cumsum(rng.standard_normal(n_obs))
        seq = bsadf_sequence(y, lags=0)
        if np.nanmax(seq) <= gate:                 # stage 1 rejects the coin
            continue
        total += len(stamp_episodes("RW", [str(i) for i in range(n_obs)],
                                    seq, band, min_duration=6, merge_gap=20))
    assert total <= 3, total


def test_market_cycles_collapse_synchronised_episodes():
    class E:
        def __init__(self, coin, s, e):
            self.coin, self.start_date, self.end_date = coin, s, e

    eps = [E("A", "2024-03-01", "2024-03-20"),
           E("B", "2024-03-05", "2024-03-25"),
           E("C", "2024-03-09", "2024-04-02"),
           E("A", "2025-10-01", "2025-11-10"),
           E("D", "2025-10-14", "2025-11-01")]
    cycles = market_cycles(eps, cycle_window=30)
    assert len(cycles) == 2
    assert cycles[0]["n_coins"] == 3
    assert cycles[1]["n_coins"] == 2
