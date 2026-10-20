"""Reading one cache tile, at the channel's own rate or downsampled.

Shared by the in-process path (``FileManager``) and the decode worker
processes (``reader_pool``) so a tile is byte-identical whichever produced it.

A tile covers ``S`` samples of the *served* grid (``fs``) starting at sample
``block_index * S``. When ``fs`` is below the channel's native rate, the tile's
span is decoded at the native rate and reduced with order-preserving min-max:
each pair of output samples covers ``2 / fs`` seconds and holds that bucket's
minimum and maximum *in the order they occurred*. The output is a regular
signal at exactly ``fs`` in which a spike survives (it is its bucket's min or
max), unlike plain decimation.
"""
import numpy as np

from mef3io_server.server.tile_cache import CACHE_DTYPE


def samples_per_tile(tile_duration_s, fs, native_fs):
    """Samples of one tile on the served grid; even when downsampling (pairs)."""
    if fs < native_fs:
        return 2 * max(1, int(round(tile_duration_s * fs / 2)))
    return max(1, int(round(tile_duration_s * fs)))


def minmax_ordered(x, n_pairs):
    """Order-preserving min-max of ``x`` into ``n_pairs`` buckets (2 * n_pairs values).

    Buckets split ``x`` evenly by sample count (the samples are evenly spaced
    in time, with NaN where the recording has a gap). In each bucket the min
    and the max are emitted in the order they occur; an all-NaN bucket gives
    two NaN.
    """
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    out = np.full(2 * n_pairs, np.nan)
    n = x.shape[0]
    if n == 0 or n_pairs <= 0:
        return out
    edges = (np.arange(n_pairs + 1, dtype=np.int64) * n) // n_pairs
    lens = np.diff(edges)
    width = int(lens.max())
    if width == 0:
        return out
    cols = np.arange(width, dtype=np.int64)
    inside = cols[None, :] < lens[:, None]
    idx = np.where(inside, edges[:-1, None] + cols[None, :], 0)
    v = np.where(inside, x[idx], np.nan)
    finite = ~np.isnan(v)
    has = finite.any(axis=1)
    i_min = np.where(finite, v, np.inf).argmin(axis=1)
    i_max = np.where(finite, v, -np.inf).argmax(axis=1)
    rows = np.arange(n_pairs)
    first = np.minimum(i_min, i_max)
    second = np.maximum(i_min, i_max)
    out[0::2] = v[rows, first]
    out[1::2] = v[rows, second]
    out[np.repeat(~has, 2)] = np.nan
    return out


def read_tile(reader, channel, block_index, fs, channel_start_uutc, samples, native_fs,
              on_miss=None):
    """Decode one tile: ``samples`` values on the ``fs`` grid, NaN-padded.

    ``fs == native_fs`` returns the samples themselves (the behaviour before
    downsampling existed); ``fs < native_fs`` returns their ordered min-max.
    ``on_miss(exc)`` is called when the read fails (out of range, damaged).
    """
    S = int(samples)
    t_start = channel_start_uutc + int(round(block_index * S * 1e6 / fs))
    t_end = channel_start_uutc + int(round((block_index + 1) * S * 1e6 / fs))
    try:
        raw = reader.get_data([channel], int(t_start), int(t_end))
    except Exception as e:  # noqa: BLE001 - out-of-range reads must not crash
        if on_miss is not None:
            on_miss(e)
        raw = None
    if raw is None:
        return np.full(S, np.nan, dtype=CACHE_DTYPE)
    arr = np.asarray(raw, dtype=np.float64).reshape(-1)
    if fs < native_fs:
        return minmax_ordered(arr, S // 2).astype(CACHE_DTYPE)
    if arr.shape[0] >= S:
        arr = arr[:S]
    else:
        arr = np.concatenate([arr, np.full(S - arr.shape[0], np.nan)])
    return arr.astype(CACHE_DTYPE)
