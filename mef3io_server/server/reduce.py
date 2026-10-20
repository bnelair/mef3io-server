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
    bucket and the mean of the next bucket's candidates, which is what keeps
    the drawn line's shape (LTTB) while never dropping a bucket's extremes.
    Candidates sit at their bucket's centre for the triangle (half a bucket at
    most from their true position, i.e. under a pixel). Rows are processed
    together, bucket by bucket. A bucket without data gives NaN and does not
    move the line's anchor.
    """
    data = np.asarray(data, dtype=np.float64)
    rows, n = data.shape
    n_points = max(0, min(int(n_points), n))  # never more points than samples
    values = np.full((rows, n_points), np.nan)
    if n_points == 0:
        return values
    edges = (np.arange(n_points + 1, dtype=np.int64) * n) // n_points
    # One pass each, NaN-aware (fmin/fmax ignore NaN; an all-NaN bucket stays NaN).
    lo = np.fmin.reduceat(data, edges[:-1], axis=1)
    hi = np.fmax.reduceat(data, edges[:-1], axis=1)
    has = ~np.isnan(lo)
    centre = (edges[:-1] + edges[1:] - 1) / 2.0
    mid = (lo + hi) / 2.0

    first = np.argmax(~np.isnan(data), axis=1)
    ax = first.astype(np.float64)
    ay = data[np.arange(rows), first]
    for k in range(n_points):
        if k + 1 < n_points:
            cx, cy = centre[k + 1], mid[:, k + 1]
        else:
            cx, cy = float(n - 1), data[:, n - 1]
        cy = np.where(np.isnan(cy), ay, cy)  # no next data: only the anchor counts
        xk = centre[k]
        # Same x for both candidates: the larger triangle is the one farther
        # from the line anchor -> next-bucket mean, evaluated at this bucket.
        line = ay + (cy - ay) * (xk - ax) / np.where(cx == ax, 1.0, cx - ax)
        pick_hi = np.abs(hi[:, k] - line) >= np.abs(lo[:, k] - line)
        val = np.where(pick_hi, hi[:, k], lo[:, k])
        ok = has[:, k]
        values[ok, k] = val[ok]
        ax = np.where(ok, xk, ax)
        ay = np.where(ok, val, ay)
    return values
