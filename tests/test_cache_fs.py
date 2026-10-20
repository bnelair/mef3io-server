"""Per-session cache rate (cache_fs): order-preserving min-max downsampling.

* the reduction keeps each bucket's min and max in time order, keeps spikes,
  and gives NaN only for buckets with no data;
* a session opened with cache_fs is served (and cached) at exactly that rate;
  without it, nothing changes;
* a channel recorded below cache_fs refuses the open, unless allow_mixed_rates
  serves it at its own rate;
* two clients with different rates on one file each get their own, and tiles
  from the worker processes are identical to in-process ones;
* over gRPC, the client passes cache_fs and sees the served rates.
"""
import numpy as np
import pytest

import mef3io
from mef3io_server.client import Mef3Client
from mef3io_server.server.file_manager import FileManager
from mef3io_server.server.tiles import minmax_ordered

START = 1_700_000_000_000_000
FAST, SLOW = 4096.0, 64.0
SPIKE_AT = 50.25  # seconds


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """'fast' at 4096 Hz: a ramp up then down, with one spike; 'slow' at 64 Hz."""
    path = str(tmp_path_factory.mktemp("cachefs") / "rates.mefd")
    n = int(120 * FAST)
    x = np.concatenate([np.arange(n // 2), np.arange(n // 2)[::-1]]).astype(float)
    x[int(SPIKE_AT * FAST)] = 10_000_000.0
    with mef3io.Writer(path, overwrite=True) as w:
        w.write("fast", x, START, fs=FAST, precision=0)
        w.write("slow", np.arange(int(120 * SLOW), dtype=float), START, fs=SLOW, precision=0)
    return path, x


@pytest.fixture
def fm():
    m = FileManager(use_process_pool=False, tile_duration_s=10)
    yield m
    m.shutdown()


def test_minmax_keeps_order_spikes_and_gaps():
    up = minmax_ordered(np.arange(100.0), 10)
    assert list(up[:4]) == [0, 9, 10, 19]  # min before max on a rising ramp
    down = minmax_ordered(np.arange(100.0)[::-1], 10)
    assert list(down[:2]) == [99, 90]  # max before min on a falling one
    spike = np.zeros(1000)
    spike[537] = 5.0
    assert minmax_ordered(spike, 7).max() == 5.0
    gap = np.arange(40.0)
    gap[10:20] = np.nan
    out = minmax_ordered(gap, 4)
    assert np.isnan(out[2:4]).all() and not np.isnan(out[[0, 1, 4, 5, 6, 7]]).any()
    uneven = minmax_ordered(np.arange(103.0), 10)  # 103 samples, 10 buckets
    assert uneven[0] == 0 and uneven[-1] == 102


def test_a_session_is_served_at_cache_fs(fm, session):
    path, x = session
    info = fm.open_file(path, client_id="viewer", cache_fs=128, allow_mixed_rates=True)
    assert info.file_opened and info.cache_fs == 128
    served = dict(zip(info.channel_names, info.channel_served_sampling_rates))
    assert served["fast"] == 128
    t0 = START + 40_000_000
    out = fm.read_signal_range(path, ["fast"], t0, t0 + 20_000_000, client_id="viewer")
    assert out["fs"] == 128 and out["array"].shape == (1, 20 * 128)
    y = out["array"][0]
    assert y.max() == 10_000_000  # the single-sample spike survives
    ramp = x[int(40 * FAST):int(60 * FAST)]
    assert y.min() == ramp.min()
    assert set(np.unique(y)) <= set(np.unique(ramp))  # every value is a real sample
    assert np.all(np.diff(y[:200]) >= 0)  # rising part: each pair (min, max) in order
    # Cached at that rate: an identical read decodes nothing new.
    tiles = len(fm._tile_cache)
    again = fm.read_signal_range(path, ["fast"], t0, t0 + 20_000_000, client_id="viewer",
                                 prefetch=False)
    assert len(fm._tile_cache) == tiles and (again["array"] == out["array"]).all()


def test_without_cache_fs_nothing_changes(fm, session):
    path, x = session
    fm.open_file(path, client_id="plain")
    t0 = START + 1_000_000
    out = fm.read_signal_range(path, ["fast"], t0, t0 + 1_000_000, client_id="plain")
    assert out["fs"] == FAST
    np.testing.assert_array_equal(out["array"][0], x[int(FAST):int(2 * FAST)])


def test_a_slower_channel_refuses_the_open_unless_allowed(fm, session):
    path, _ = session
    refused = fm.open_file(path, client_id="w", cache_fs=128)
    assert not refused.file_opened
    assert "slow (64 Hz)" in refused.error_message and "allow_mixed_rates" in refused.error_message
    assert fm.session_count() == 0 and path not in fm.list_open_files()  # nothing left open

    info = fm.open_file(path, client_id="w", cache_fs=128, allow_mixed_rates=True)
    assert info.file_opened
    assert dict(zip(info.channel_names, info.channel_served_sampling_rates)) == {
        "fast": 128, "slow": 64}
    out = fm.read_signal_range(path, ["slow"], START, START + 10_000_000, client_id="w")
    assert out["fs"] == SLOW
    np.testing.assert_array_equal(out["array"][0], np.arange(640))
    with pytest.raises(ValueError, match="share a sampling rate"):
        fm.read_signal_range(path, ["fast", "slow"], START, START + 10_000_000, client_id="w")


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf")])
def test_cache_fs_must_be_a_positive_rate(fm, session, bad):
    path, _ = session
    info = fm.open_file(path, client_id="v", cache_fs=bad, allow_mixed_rates=True)
    assert not info.file_opened and "positive" in info.error_message


def test_two_clients_two_rates_and_a_reopen_resets(fm, session):
    path, _ = session
    fm.open_file(path, client_id="a", cache_fs=128, allow_mixed_rates=True)
    fm.open_file(path, client_id="b", cache_fs=32, allow_mixed_rates=True)
    t0 = START + 10_000_000
    a = fm.read_signal_range(path, ["fast"], t0, t0 + 10_000_000, client_id="a")
    b = fm.read_signal_range(path, ["fast"], t0, t0 + 10_000_000, client_id="b")
    assert (a["fs"], b["fs"]) == (128, 32)
    assert a["array"].shape[1] == 1280 and b["array"].shape[1] == 320
    other = fm.read_signal_range(path, ["fast"], t0, t0 + 10_000_000, client_id="nobody")
    assert other["fs"] == FAST  # no session of its own: the original rate
    fm.open_file(path, client_id="a")  # re-open without cache_fs: back to native
    assert fm.read_signal_range(path, ["fast"], t0, t0 + 1_000_000, client_id="a")["fs"] == FAST


def test_worker_tiles_equal_in_process_tiles(session):
    path, _ = session
    t0 = START + 30_000_000
    results = []
    for pool in (False, True):
        m = FileManager(use_process_pool=pool, min_parallel_tiles=1, tile_duration_s=10,
                        reader_processes=2)
        try:
            m.open_file(path, client_id="v", cache_fs=128, allow_mixed_rates=True)
            results.append(m.read_signal_range(path, ["fast"], t0, t0 + 30_000_000,
                                               client_id="v", prefetch=False)["array"])
        finally:
            m.shutdown()
    np.testing.assert_array_equal(results[0], results[1])


def test_over_grpc(session, grpc_server_factory):
    path, _ = session
    port = grpc_server_factory(use_process_pool=False)
    c = Mef3Client(f"localhost:{port}")
    try:
        bad = c.open_file(path, cache_fs=128)
        assert not bad["file_opened"] and "allow_mixed_rates" in bad["error_message"]
        info = c.open_file(path, cache_fs=128, allow_mixed_rates=True)
        assert info["cache_fs"] == 128
        assert info["channel_served_sampling_rates"] == [128, 64] or \
            sorted(info["channel_served_sampling_rates"]) == [64, 128]
        out = c.get_signal_range(path, ["fast"], START, START + 5_000_000)
        assert out["array"].shape == (1, 5 * 128)
    finally:
        c.shutdown()
