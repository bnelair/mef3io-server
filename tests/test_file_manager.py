import pytest
import numpy as np
import concurrent.futures
import os
import time

from mef3io_server.server.file_manager import FileManager
from mef3io import MefReader
import mef3io_server.protobufs.gRPCMef3Server_pb2 as pb2

from .conftest import mef3_file, record_benchmark_setup


def test_open_and_close_file(mef3_file):
    fm = FileManager()
    resp = fm.open_file(mef3_file)
    assert resp.file_opened
    assert resp.file_path == mef3_file
    assert mef3_file in fm.list_open_files()

    # Open again, should still be in open files and return error message
    resp_dup = fm.open_file(mef3_file)
    assert resp_dup.file_opened
    assert resp_dup.file_path == mef3_file
    assert mef3_file in fm.list_open_files()
    assert "already open" in resp_dup.error_message

    resp2 = fm.close_file(mef3_file)
    assert not resp2.file_opened
    assert mef3_file not in fm.list_open_files()


def test_file_info_exposes_per_channel_metadata(mef3_file):
    """File info must expose channels, per-channel fs and per-channel start/end."""
    fm = FileManager()
    fm.open_file(mef3_file)
    info = fm.get_file_info(mef3_file)
    assert info.file_opened
    assert info.number_of_channels > 0
    nch = info.number_of_channels
    # Parallel per-channel arrays.
    assert len(info.channel_names) == nch
    assert len(info.channel_sampling_rates) == nch
    assert len(info.channel_start_uutc) == nch
    assert len(info.channel_end_uutc) == nch
    # Global span is the min/max over channels.
    assert info.start_uutc == min(info.channel_start_uutc)
    assert info.end_uutc == max(info.channel_end_uutc)
    assert info.duration_s == pytest.approx((info.end_uutc - info.start_uutc) / 1e6)
    # Cross-check against the reader directly.
    rdr = MefReader(mef3_file)
    assert list(info.channel_names) == list(rdr.channels)
    fm.shutdown()


def test_channel_subset_and_order_preserved(mef3_file):
    """Requesting a subset of channels in arbitrary order returns exactly that."""
    fm = FileManager(tile_duration_s=10)
    fm.open_file(mef3_file)
    rdr = fm._files[mef3_file]['reader']
    all_channels = list(rdr.channels)
    selected = [all_channels[3], all_channels[1], all_channels[5]]
    cs = int(min(rdr.get_property('start_time')))

    res = fm.read_signal_range(mef3_file, selected, cs, cs + 5_000_000)
    assert res['channel_names'] == selected
    assert res['array'].shape[0] == len(selected)

    ref = np.asarray(rdr.get_data(selected, cs, cs + 5_000_000), dtype=np.float32)
    m = min(res['array'].shape[1], ref.shape[1])
    np.testing.assert_allclose(res['array'][:, :m], ref[:, :m], atol=1e-4, equal_nan=True)
    fm.shutdown()


def test_read_signal_range_errors(mef3_file):
    fm = FileManager()
    fm.open_file(mef3_file)
    rdr = fm._files[mef3_file]['reader']
    cs = int(min(rdr.get_property('start_time')))
    # Unknown channel.
    with pytest.raises(ValueError):
        fm.read_signal_range(mef3_file, ["no_such_channel"], cs, cs + 1_000_000)
    # Empty window.
    with pytest.raises(ValueError):
        fm.read_signal_range(mef3_file, None, cs, cs)
    # File not open.
    with pytest.raises(ValueError):
        fm.read_signal_range("not_open.mefd", None, cs, cs + 1_000_000)
    fm.shutdown()


def test_error_handling_on_open():
    fm = FileManager()
    resp = fm.open_file("badfile.mef")
    assert not resp.file_opened


def test_shutdown_thread_pool():
    for k in range(5):
        fm = FileManager()
        time.sleep(.1)
        fm.shutdown()
        time.sleep(.1)


SEGMENT_SIZE_S = 60
NUM_WINDOWS = 5


def access_pattern(file_manager, file_path, channels, start_uutc):
    """Read NUM_WINDOWS sequential windows via the tile-cache path.

    Exercises ``FileManager.read_signal_range`` (the timestamp-based tile cache +
    background tile prefetch). Prefetch's benefit shows on windows 1..N-1, whose
    tiles the previous window scheduled ahead.
    """
    seg_us = int(SEGMENT_SIZE_S * 1e6)
    for i in range(NUM_WINDOWS):
        s = int(start_uutc) + i * seg_us
        _ = file_manager.read_signal_range(file_path, channels, s, s + seg_us)


def _micro_setup(fm, mef3_file):
    """Open the file and return (channels, start_uutc) for the micro-benchmark."""
    fm.open_file(mef3_file)
    rdr = fm._files[mef3_file]['reader']
    channels = list(rdr.channels)
    start_uutc = int(min(rdr.get_property('start_time')))
    return channels, start_uutc


@pytest.mark.benchmark
def test_with_prefetch_real_file(benchmark, mef3_file):
    """Benchmark the tile-cache access pattern WITH prefetching on a REAL file."""
    fm = FileManager(prefetch_ahead_windows=1, prefetch_behind_windows=1, max_workers=12)
    channels, start_uutc = _micro_setup(fm, mef3_file)
    record_benchmark_setup(
        benchmark,
        access="FileManager read_signal_range WITH prefetch (in-process)",
        file_path=mef3_file,
        total_channels=len(channels),
        active_channels=len(channels),
        fs=256, precision=3, duration_s=5 * 60,  # matches the mef3_file fixture
        num_chunks=NUM_WINDOWS, segment_size_s=SEGMENT_SIZE_S, rounds="auto",
        server="FileManager (in-process, no gRPC)",
        prefetch_ahead_windows=1, prefetch_behind_windows=1,
    )
    benchmark(access_pattern, fm, mef3_file, channels, start_uutc)
    fm.shutdown()


@pytest.mark.benchmark
def test_no_prefetch_real_file(benchmark, mef3_file):
    """Benchmark the tile-cache access pattern WITHOUT prefetching on a REAL file."""
    fm = FileManager(prefetch_ahead_windows=0, prefetch_behind_windows=0)
    channels, start_uutc = _micro_setup(fm, mef3_file)
    record_benchmark_setup(
        benchmark,
        access="FileManager read_signal_range WITHOUT prefetch (in-process)",
        file_path=mef3_file,
        total_channels=len(channels),
        active_channels=len(channels),
        fs=256, precision=3, duration_s=5 * 60,  # matches the mef3_file fixture
        num_chunks=NUM_WINDOWS, segment_size_s=SEGMENT_SIZE_S, rounds="auto",
        server="FileManager (in-process, no gRPC)",
        prefetch_ahead_windows=0, prefetch_behind_windows=0,
    )
    benchmark(access_pattern, fm, mef3_file, channels, start_uutc)
    fm.shutdown()


def test_integrity_multithreaded_read_real(mef3_file):
    """5 concurrent clients reading the whole file window-by-window must all get
    identical, reference-correct data (shared tile cache, channels+time API)."""
    fm = FileManager(tile_duration_s=10)
    fm.open_file(mef3_file)
    rdr = fm._files[mef3_file]['reader']
    channels = list(rdr.channels)
    start = int(min(rdr.get_property('start_time')))
    end = int(max(rdr.get_property('end_time')))
    win_us = int(60 * 1e6)

    def read_all_windows():
        parts = []
        s = start
        while s < end:
            e = min(s + win_us, end)
            res = fm.read_signal_range(mef3_file, channels, s, e)
            parts.append(res['array'])
            s = e
        return np.concatenate(parts, axis=1)

    # Run 5 clients in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: read_all_windows(), range(5)))

    # All results should be identical
    for arr in results[1:]:
        np.testing.assert_array_equal(arr, results[0])

    # The data should match the reference read (float32, <=1 sample rounding)
    ref = np.asarray(rdr.get_data(channels, start, end), dtype=np.float32)
    m = min(results[0].shape[1], ref.shape[1])
    np.testing.assert_allclose(results[0][:, :m], ref[:, :m], atol=1e-4, equal_nan=True)
    fm.shutdown()


def test_open_nonexistent_file_returns_not_opened(tmp_path):
    fm = FileManager()
    non_existent_path = os.path.join(tmp_path, "does_not_exist.mef")
    resp = fm.open_file(non_existent_path)
    assert isinstance(resp, pb2.FileInfoResponse)
    assert resp.file_path == non_existent_path
    assert resp.file_opened is False
