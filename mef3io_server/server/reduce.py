"""Reductions of a signal to fewer samples that stay faithful to it.

* :func:`minmax_ordered` -- the cache-rate reduction: each pair of output
  samples is one stretch's minimum and maximum, in the order they occurred.
* :func:`minmax_lttb` -- the per-request ``n_points`` reduction: exactly one
  real sample per output point, picked from that point's bucket's minimum and
  maximum by Largest-Triangle-Three-Buckets (MinMaxLTTB, Van Der Donckt et al.
  2023), so peaks survive and the line keeps its shape.

Both cut buckets evenly by sample count (samples are evenly spaced in time,
with NaN in gaps) and give NaN for a bucket that holds no data.
"""
import numpy as np


def _buckets(n, n_buckets):
    """Row/column gather indices for ``n_buckets`` near-equal buckets of ``n``."""
    edges = (np.arange(n_buckets + 1, dtype=np.int64) * n) // n_buckets
    lens = np.diff(edges)
    width = max(1, int(lens.max()))
    cols = np.arange(width, dtype=np.int64)
    inside = cols[None, :] < lens[:, None]
    idx = np.where(inside, edges[:-1, None] + cols[None, :], 0)
    return idx, inside


def _extremes(v, inside):
    """Positions (in the bucket) of each bucket's min and max, and which have data."""
    finite = inside & ~np.isnan(v)
    has = finite.any(axis=-1)
    i_min = np.where(finite, v, np.inf).argmin(axis=-1)
    i_max = np.where(finite, v, -np.inf).argmax(axis=-1)
    return i_min, i_max, has


def minmax_ordered(x, n_pairs):
    """Order-preserving min-max of ``x`` into ``n_pairs`` buckets (2 * n_pairs values).

    In each bucket the min and the max are emitted in the order they occur; an
    all-NaN bucket gives two NaN.
    """
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    out = np.full(2 * n_pairs, np.nan)
    if x.shape[0] == 0 or n_pairs <= 0:
        return out
    idx, inside = _buckets(x.shape[0], n_pairs)
    v = np.where(inside, x[idx], np.nan)
    i_min, i_max, has = _extremes(v, inside)
    rows = np.arange(n_pairs)
    out[0::2] = v[rows, np.minimum(i_min, i_max)]
    out[1::2] = v[rows, np.maximum(i_min, i_max)]
    out[np.repeat(~has, 2)] = np.nan
    return out


def minmax_lttb(data, n_points):
    """Reduce each row of ``data`` (channels x samples) to ``n_points`` real samples.

    The window is cut into ``n_points`` buckets (one per output point, e.g. one
    per pixel). Each bucket's candidates are its minimum and its maximum; the
    one kept forms the largest triangle with the point kept in the previous
    bucket and the mean of the next bucket's candidates (LTTB), which keeps the
    drawn line's shape. One point per bucket means one of a bucket's two
    extremes is dropped; so that the trace's amplitude is never understated,
    each row's overall maximum and minimum are then put back into their
    buckets (when both fall in the same bucket, the one farther from the
    row's typical level, the mean of its bucket midpoints, is kept). Candidates sit at their bucket's centre for the
    triangle (under one bucket from their true position). Rows are processed
    together, bucket by bucket; a bucket without data gives NaN.
    Reductions run in the input's own dtype (no float64 copy of the window).
    """
    data = np.asarray(data)
    if data.dtype.kind != "f":
        data = data.astype(np.float64)
    rows, n = data.shape
    n_points = max(0, min(int(n_points), n))  # never more points than samples
    values = np.full((rows, n_points), np.nan)
    if n_points == 0:
        return values
    edges = (np.arange(n_points + 1, dtype=np.int64) * n) // n_points
    # One pass each, NaN-aware (fmin/fmax ignore NaN; an all-NaN bucket stays NaN).
    lo = np.fmin.reduceat(data, edges[:-1], axis=1).astype(np.float64)
    hi = np.fmax.reduceat(data, edges[:-1], axis=1).astype(np.float64)
    has = ~np.isnan(lo)
    centre = (edges[:-1] + edges[1:] - 1) / 2.0
    mid = (lo + hi) / 2.0

    # The line's anchor starts at the first bucket with data (bucket-level, so
    # no pass over every sample, and always left of the next bucket).
    rr = np.arange(rows)
    first = np.argmax(has, axis=1)
    ax = centre[first].astype(np.float64)
    ay = mid[rr, first]
    last = data[:, n - 1].astype(np.float64)
    for k in range(n_points):
        if k + 1 < n_points:
            cx, cy = centre[k + 1], mid[:, k + 1]
        else:
            cx, cy = float(n - 1) + 0.5, last
        cy = np.where(np.isnan(cy), ay, cy)  # no next data: only the anchor counts
        xk = centre[k]
        span = np.where(cx > ax, cx - ax, 1.0)
        line = ay + (cy - ay) * (xk - ax) / span
        pick_hi = np.abs(hi[:, k] - line) >= np.abs(lo[:, k] - line)
        val = np.where(pick_hi, hi[:, k], lo[:, k])
        ok = has[:, k]
        values[ok, k] = val[ok]
        ax = np.where(ok, xk, ax)
        ay = np.where(ok, val, ay)

    # Never understate the amplitude: each row's overall max and min are shown.
    any_data = has.any(axis=1)
    if any_data.any():
        hi_m = np.where(has, hi, -np.inf)
        lo_m = np.where(has, lo, np.inf)
        k_hi, k_lo = hi_m.argmax(axis=1), lo_m.argmin(axis=1)
        g_hi, g_lo = hi_m[rr, k_hi], lo_m[rr, k_lo]
        # The channel's typical level, from the buckets (cheap: no pass over
        # the samples); the larger excursion from it wins a shared bucket.
        level = np.where(has, mid, 0.0).sum(axis=1) / np.maximum(has.sum(axis=1), 1)
        centre_v = np.where(any_data, level, 0.0)
        same = k_hi == k_lo
        prefer_hi = (g_hi - centre_v) >= (centre_v - g_lo)
        put_hi = any_data & (~same | prefer_hi)
        put_lo = any_data & (~same | ~prefer_hi)
        values[rr[put_hi], k_hi[put_hi]] = g_hi[put_hi]
        values[rr[put_lo], k_lo[put_lo]] = g_lo[put_lo]
    return values
