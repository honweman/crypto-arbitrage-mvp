"""Phillips-Shi-Yu (2015) recursive right-tailed unit-root tests.

Implements the SADF/GSADF statistics and the BSADF date-stamping sequence,
plus Monte-Carlo and wild-bootstrap recursive critical values.

The ADF specification is the one used throughout the bubble literature:

    dy_t = alpha + beta * y_{t-1} + sum_{j=1..k} phi_j * dy_{t-j} + e_t

and the test statistic is the t-ratio on ``beta`` (right-tailed).
"""

from __future__ import annotations

import numpy as np

try:  # optional acceleration
    from numba import njit, prange

    HAVE_NUMBA = True
except ImportError:  # pragma: no cover - exercised only without numba
    HAVE_NUMBA = False

    def njit(*args, **kwargs):
        def wrap(fn):
            return fn

        return wrap(args[0]) if args and callable(args[0]) else wrap

    prange = range


def min_window(n_obs: int) -> int:
    """PSY's rule of thumb for the smallest estimation window."""
    r0 = 0.01 + 1.8 / np.sqrt(n_obs)
    return max(int(np.floor(r0 * n_obs)), 12)


def build_design(y: np.ndarray, lags: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (X, dy) for the ADF regression, dropping the first ``lags + 1`` points.

    Columns of X are [const, y_{t-1}, dy_{t-1}, ..., dy_{t-lags}].
    """
    y = np.asarray(y, dtype=np.float64)
    y = y - y[0]  # level shift: leaves the t-ratio unchanged, helps conditioning
    dy_full = np.diff(y)
    n = len(dy_full) - lags
    if n <= lags + 3:
        raise ValueError("series too short for the requested lag order")
    cols = [np.ones(n), y[lags : lags + n]]
    for j in range(1, lags + 1):
        cols.append(dy_full[lags - j : lags - j + n])
    return np.column_stack(cols), dy_full[lags:]


@njit(cache=True)
def _cumulative_moments(x, yv):
    n, p = x.shape
    cxx = np.zeros((n + 1, p, p))
    cxy = np.zeros((n + 1, p))
    cyy = np.zeros(n + 1)
    for t in range(n):
        for a in range(p):
            xa = x[t, a]
            cxy[t + 1, a] = cxy[t, a] + xa * yv[t]
            for b in range(p):
                cxx[t + 1, a, b] = cxx[t, a, b] + xa * x[t, b]
        cyy[t + 1] = cyy[t] + yv[t] * yv[t]
    return cxx, cxy, cyy


@njit(cache=True)
def _inv_small(a):
    """Gauss-Jordan inverse of a small square matrix (avoids a LAPACK dependency).

    Returns (inverse, ok). ``ok`` is False when the matrix is numerically singular.
    """
    p = a.shape[0]
    m = a.copy()
    inv = np.zeros((p, p))
    for i in range(p):
        inv[i, i] = 1.0
    for col in range(p):
        piv = col
        big = abs(m[col, col])
        for r in range(col + 1, p):
            v = abs(m[r, col])
            if v > big:
                big = v
                piv = r
        if big < 1e-14:
            return inv, False
        if piv != col:
            for c in range(p):
                m[col, c], m[piv, c] = m[piv, c], m[col, c]
                inv[col, c], inv[piv, c] = inv[piv, c], inv[col, c]
        d = m[col, col]
        for c in range(p):
            m[col, c] /= d
            inv[col, c] /= d
        for r in range(p):
            if r == col:
                continue
            f = m[r, col]
            if f != 0.0:
                for c in range(p):
                    m[r, c] -= f * m[col, c]
                    inv[r, c] -= f * inv[col, c]
    return inv, True


@njit(cache=True)
def _tstat(cxx, cxy, cyy, s, e, ridge):
    """t-ratio on column 1 for the window of rows [s, e)."""
    p = cxx.shape[1]
    m = e - s
    if m <= p + 1:
        return np.nan
    a = cxx[e] - cxx[s]
    b = cxy[e] - cxy[s]
    yy = cyy[e] - cyy[s]
    for i in range(p):
        a[i, i] += ridge
    ainv, ok = _inv_small(a)
    if not ok:
        return np.nan
    beta = np.zeros(p)
    for i in range(p):
        acc = 0.0
        for j in range(p):
            acc += ainv[i, j] * b[j]
        beta[i] = acc
    bb = 0.0
    for i in range(p):
        bb += beta[i] * b[i]
    rss = yy - bb
    if rss <= 0.0:
        return np.nan
    sigma2 = rss / (m - p)
    var = sigma2 * ainv[1, 1]
    if var <= 0.0:
        return np.nan
    return beta[1] / np.sqrt(var)


@njit(cache=True)
def _bsadf_core(x, yv, minwin, ridge):
    n = x.shape[0]
    cxx, cxy, cyy = _cumulative_moments(x, yv)
    out = np.full(n, np.nan)
    for e in range(minwin, n + 1):
        best = -1e18
        for s in range(0, e - minwin + 1):
            t = _tstat(cxx, cxy, cyy, s, e, ridge)
            if t == t and t > best:  # t == t filters NaN
                best = t
        out[e - 1] = best
    return out


def bsadf_sequence(y: np.ndarray, lags: int = 1, minwin: int | None = None,
                   ridge: float = 1e-10) -> np.ndarray:
    """BSADF statistic for every endpoint, aligned to the ORIGINAL series index.

    Entries before the first estimable endpoint are NaN.
    """
    x, dy = build_design(y, lags)
    n = x.shape[0]
    if minwin is None:
        minwin = min_window(len(y))
    minwin = max(int(minwin), lags + 5)
    if minwin > n:
        return np.full(len(y), np.nan)
    seq = _bsadf_core(x, dy, minwin, ridge)
    padded = np.full(len(y), np.nan)
    padded[len(y) - n :] = seq
    return padded


def gsadf(y: np.ndarray, lags: int = 1, minwin: int | None = None) -> float:
    seq = bsadf_sequence(y, lags=lags, minwin=minwin)
    return float(np.nanmax(seq)) if np.any(~np.isnan(seq)) else float("nan")


@njit(parallel=True, cache=True)
def _simulate_bsadf(n_obs, lags, minwin, reps, seed, ridge):
    """BSADF paths for ``reps`` random walks of length ``n_obs``."""
    n_rows = n_obs - lags - 1
    out = np.full((reps, n_rows), np.nan)
    for b in prange(reps):
        np.random.seed(seed + b)
        e = np.random.standard_normal(n_obs)
        y = np.empty(n_obs)
        y[0] = 0.0
        for t in range(1, n_obs):
            y[t] = y[t - 1] + e[t]
        dy_full = np.empty(n_obs - 1)
        for t in range(n_obs - 1):
            dy_full[t] = y[t + 1] - y[t]
        p = 2 + lags
        x = np.empty((n_rows, p))
        yv = np.empty(n_rows)
        for t in range(n_rows):
            x[t, 0] = 1.0
            x[t, 1] = y[lags + t]
            for j in range(1, lags + 1):
                x[t, 1 + j] = dy_full[lags - j + t]
            yv[t] = dy_full[lags + t]
        out[b] = _bsadf_core(x, yv, minwin, ridge)
    return out


def monte_carlo_critical_values(n_obs: int, lags: int = 1, minwin: int | None = None,
                                reps: int = 2000, quantiles=(0.90, 0.95, 0.99),
                                seed: int = 20260819, ridge: float = 1e-10) -> dict:
    """Recursive critical value sequences under a driftless random-walk null.

    Returns ``{quantile: array aligned to the original series index}`` plus the
    scalar GSADF critical values under key ``"gsadf"``.
    """
    if minwin is None:
        minwin = min_window(n_obs)
    minwin = max(int(minwin), lags + 5)
    paths = _simulate_bsadf(n_obs, lags, minwin, int(reps), int(seed), ridge)
    out = {}
    for q in quantiles:
        cv = np.nanquantile(paths, q, axis=0)
        padded = np.full(n_obs, np.nan)
        padded[n_obs - paths.shape[1] :] = cv
        out[q] = padded
    sup = np.nanmax(paths, axis=1)
    out["gsadf"] = {q: float(np.nanquantile(sup, q)) for q in quantiles}
    out["reps"] = int(reps)
    return out


def wild_bootstrap_critical_values(y: np.ndarray, lags: int = 1, minwin: int | None = None,
                                   reps: int = 999, quantiles=(0.90, 0.95, 0.99),
                                   seed: int = 20260819, ridge: float = 1e-10) -> dict:
    """Phillips-Shi style wild bootstrap, robust to heteroskedasticity.

    Residuals from the null model (no autoregressive root) are reweighted with
    Rademacher multipliers and the series is regenerated under the null.
    """
    rng = np.random.default_rng(seed)
    y = np.asarray(y, dtype=np.float64)
    n_obs = len(y)
    if minwin is None:
        minwin = min_window(n_obs)
    minwin = max(int(minwin), lags + 5)

    dy = np.diff(y)
    # null model: dy_t = mu + sum phi_j dy_{t-j} + e_t   (beta constrained to 0)
    n = len(dy) - lags
    z = np.column_stack([np.ones(n)] + [dy[lags - j : lags - j + n] for j in range(1, lags + 1)])
    target = dy[lags:]
    coef, *_ = np.linalg.lstsq(z, target, rcond=None)
    resid = target - z @ coef

    paths = np.full((reps, n_obs - lags - 1), np.nan)
    for b in range(reps):
        w = rng.choice(np.array([-1.0, 1.0]), size=n)
        star = np.empty(n_obs)
        star[: lags + 1] = y[: lags + 1]
        d = np.empty(len(dy))
        d[:lags] = dy[:lags]
        for t in range(n):
            lagged = sum(coef[j + 1] * d[lags + t - 1 - j] for j in range(lags))
            d[lags + t] = coef[0] + lagged + resid[t] * w[t]
        for t in range(1, n_obs):
            star[t] = star[t - 1] + d[t - 1]
        paths[b] = bsadf_sequence(star, lags=lags, minwin=minwin)[n_obs - paths.shape[1] :]

    out = {}
    for q in quantiles:
        cv = np.nanquantile(paths, q, axis=0)
        padded = np.full(n_obs, np.nan)
        padded[n_obs - paths.shape[1] :] = cv
        out[q] = padded
    sup = np.nanmax(paths, axis=1)
    out["gsadf"] = {q: float(np.nanquantile(sup, q)) for q in quantiles}
    out["reps"] = int(reps)
    return out


def controlled_critical_values(n_obs: int, lags: int = 0, minwin: int | None = None,
                               reps: int = 2000, alpha: float = 0.05,
                               base_quantile: float = 0.95, seed: int = 20260819,
                               ridge: float = 1e-10) -> dict:
    """Recursive band calibrated so the PATH-WISE false-alarm rate equals ``alpha``.

    The pointwise quantile of the BSADF distribution is not a size-controlled
    band: a driftless random walk crosses it at some point with probability far
    above ``alpha`` simply because the path is inspected T times.  Following the
    real-time monitoring logic of Phillips and Shi (2020), a constant shift ``c``
    is added to the pointwise sequence and chosen so that, under the null, the
    share of paths breaching the shifted band anywhere is ``alpha``.

    Returns the shifted band, the shift, and the realised null crossing rate.
    """
    if minwin is None:
        minwin = min_window(n_obs)
    minwin = max(int(minwin), lags + 5)
    paths = _simulate_bsadf(n_obs, lags, minwin, int(reps), int(seed), ridge)
    base = np.nanquantile(paths, base_quantile, axis=0)

    def crossing_rate(shift):
        band = base + shift
        with np.errstate(invalid="ignore"):
            hit = np.nanmax(np.where(np.isnan(paths), -np.inf, paths - band), axis=1)
        return float(np.mean(hit > 0.0))

    lo, hi = 0.0, 3.0
    while crossing_rate(hi) > alpha and hi < 12.0:
        hi *= 1.5
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if crossing_rate(mid) > alpha:
            lo = mid
        else:
            hi = mid
    shift = 0.5 * (lo + hi)

    padded = np.full(n_obs, np.nan)
    padded[n_obs - paths.shape[1] :] = base + shift
    pointwise = np.full(n_obs, np.nan)
    pointwise[n_obs - paths.shape[1] :] = base
    sup = np.nanmax(paths, axis=1)
    return {
        "band": padded,
        "pointwise": pointwise,
        "shift": float(shift),
        "null_crossing_rate": crossing_rate(shift),
        "gsadf_cv": {q: float(np.nanquantile(sup, q)) for q in (0.90, 0.95, 0.99)},
        "reps": int(reps),
    }
