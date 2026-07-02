"""Correctness tests for FileManager.read_signal_range (timestamp-based access)."""
import numpy as np
import pytest
from mef_tools import MefReader

from brainmaze_mef3_server.server.file_manager import FileManager
from .conftest import mef3_file  # noqa: F401 - pytest fixture


def _direct(rdr, channels, t1, t2):
    """Reference read straight from MefReader as float32."""
    return np.asarray(rdr.get_data(channels, t1, t2), dtype=np.float32)


@pytest.fixture()
def fm_and_file(mef3_file):  # noqa: F811
    fm = FileManager(tile_duration_s=10, tile_cache_bytes=64 * 1024 * 1024)
    fm.open_file(mef3_file)
    yield fm, mef3_file
    fm.shutdown()


def test_range_matches_direct_reader_aligned(fm_and_file):
    fm, fp = fm_and_file
    rdr = MefReader(fp)
    channels = list(rdr.channels)[:4]
    cs = int(rdr.get_property('start_time', channels[0]))
    # A window aligned to nothing in particular, spanning multiple tiles.
    t1 = cs + 3_000_000  # +3 s
    t2 = cs + 47_000_000  # +47 s (>1 tile of 10 s)

    res = fm.read_signal_range(fp, channels, t1, t2)
    ref = _direct(rdr, channels, t1, t2)

    assert res['channel_names'] == channels
    assert res['array'].dtype == np.float32
    # Allow a <=1 sample boundary difference from pymef's own rounding.
    got = res['array']
    m = min(got.shape[1], ref.shape[1])
    np.testing.assert_allclose(got[:, :m], ref[:, :m], rtol=0, atol=1e-4, equal_nan=True)


@pytest.mark.parametrize("offset_us,dur_us", [
    (0, 5_000_000),        # sub-tile, from channel start
    (1_234_567, 8_000_000),# odd offset, sub-tile
    (9_500_000, 3_000_000),# straddles a 10 s tile boundary
    (25_000_000, 20_000_000),  # spans several tiles
])
def test_range_various_windows_match_direct(fm_and_file, offset_us, dur_us):
    fm, fp = fm_and_file
    rdr = MefReader(fp)
    channels = list(rdr.channels)[:3]
    cs = int(rdr.get_property('start_time', channels[0]))
    t1 = cs + offset_us
    t2 = t1 + dur_us
    got = fm.read_signal_range(fp, channels, t1, t2)['array']
    ref = _direct(rdr, channels, t1, t2)
    m = min(got.shape[1], ref.shape[1])
    assert abs(got.shape[1] - ref.shape[1]) <= 1
    np.testing.assert_allclose(got[:, :m], ref[:, :m], rtol=0, atol=1e-4, equal_nan=True)


def test_repeated_and_overlapping_requests_use_cache(fm_and_file):
    fm, fp = fm_and_file
    rdr = MefReader(fp)
    channels = list(rdr.channels)[:2]
    cs = int(rdr.get_property('start_time', channels[0]))
    t1 = cs + 12_000_000
    t2 = cs + 28_000_000

    first = fm.read_signal_range(fp, channels, t1, t2)['array']
    cache = fm._tile_cache  # shared global tile cache
    assert len(cache) > 0  # tiles were cached
    assert all(k[0] == fp for k in cache._store)  # keyed by this file path

    # Identical request -> must return identical data (served from cache).
    again = fm.read_signal_range(fp, channels, t1, t2)['array']
    np.testing.assert_array_equal(first, again, )

    # Overlapping sub-window must match the corresponding slice of the first read.
    sub = fm.read_signal_range(fp, channels, t1 + 2_000_000, t2 - 2_000_000)['array']
    fs = fm._files[fp]['tile_meta'][channels[0]][0]
    off = int(round(2_000_000 * fs / 1e6))
    np.testing.assert_allclose(sub, first[:, off:off + sub.shape[1]], atol=1e-4, equal_nan=True)


def test_active_channels_default_and_validation(fm_and_file):
    fm, fp = fm_and_file
    rdr = MefReader(fp)
    cs = int(rdr.get_property('start_time', list(rdr.channels)[0]))
    # None channels -> all channels.
    res = fm.read_signal_range(fp, None, cs, cs + 2_000_000)
    assert res['array'].shape[0] == len(rdr.channels)
    # Invalid channel -> ValueError.
    with pytest.raises(ValueError):
        fm.read_signal_range(fp, ["nope"], cs, cs + 1_000_000)
    # Empty window -> ValueError.
    with pytest.raises(ValueError):
        fm.read_signal_range(fp, None, cs, cs)


def test_close_file_purges_its_tiles_from_shared_cache(mef3_file):  # noqa: F811
    fm = FileManager(tile_duration_s=10)
    fm.open_file(mef3_file)
    rdr = MefReader(mef3_file)
    ch = list(rdr.channels)[:2]
    cs = int(rdr.get_property('start_time', ch[0]))
    fm.read_signal_range(mef3_file, ch, cs, cs + 5_000_000)
    assert len(fm._tile_cache) > 0
    fm.close_file(mef3_file)
    assert len(fm._tile_cache) == 0  # global cache purged of this file's tiles
    fm.shutdown()


def test_range_matches_direct_thread_fallback(mef3_file):  # noqa: F811
    """With the process pool disabled, the in-process path must still be exact."""
    fm = FileManager(use_process_pool=False, tile_duration_s=10,
                     tile_cache_bytes=64 * 1024 * 1024)
    try:
        fm.open_file(mef3_file)
        rdr = MefReader(mef3_file)
        channels = list(rdr.channels)[:4]
        cs = int(rdr.get_property('start_time', channels[0]))
        t1, t2 = cs + 3_000_000, cs + 47_000_000
        got = fm.read_signal_range(mef3_file, channels, t1, t2)['array']
        ref = _direct(rdr, channels, t1, t2)
        m = min(got.shape[1], ref.shape[1])
        np.testing.assert_allclose(got[:, :m], ref[:, :m], atol=1e-4, equal_nan=True)
    finally:
        fm.shutdown()


def test_window_prefetch_targets_ahead_and_behind(fm_and_file):
    """The window-level prefetch schedules exactly the next/previous 'page' tiles."""
    fm, fp = fm_and_file
    rdr = MefReader(fp)
    ch = list(rdr.channels)[0]
    with fm._lock:
        state = fm._files[fp]
        meta = fm._get_tile_meta_unsafe(state, ch)
        actual_path = state['actual_path']
    fs, cs, S = meta
    fm.prefetch_ahead_windows = 1
    fm.prefetch_behind_windows = 1

    # 10 s window aligned to tile 10 (fixture tile_duration_s=10).
    t1, t2 = cs + 100_000_000, cs + 110_000_000

    recorded = []
    fm._schedule_prefetch_tile = lambda fpath, apath, c, b, m: recorded.append(b)
    fm._schedule_window_prefetch(fp, actual_path, [ch], {ch: meta}, t1, t2, fs)

    blocks = set(recorded)
    assert 9 in blocks    # behind window [90 s, 100 s)
    assert 11 in blocks   # ahead window  [110 s, 120 s)
    assert 10 not in blocks  # never re-prefetch the window's own tile


def test_read_past_end_is_nan_padded(fm_and_file):
    fm, fp = fm_and_file
    rdr = MefReader(fp)
    channels = list(rdr.channels)[:1]
    end = max(rdr.get_property('end_time'))
    # Start 1 s before end, ask for 5 s -> last 4 s are beyond EOF.
    res = fm.read_signal_range(fp, channels, end - 1_000_000, end + 4_000_000)
    assert np.isnan(res['array']).any()
