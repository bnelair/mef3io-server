"""Correctness + parallel-decode measurement for the experimental ReaderProcessPool."""
import time

import numpy as np
import pytest
from mef_tools import MefReader

from brainmaze_mef3_server.server.reader_pool import ReaderProcessPool, _worker_read
from .benchmark_data import load_benchmark_config, get_workload
from .conftest import mef3_file  # noqa: F401 - pytest fixture
from .test_automated_processing import run_detector


def _pool_workers():
    """Number of reader-pool worker processes, from benchmark_config.json."""
    return int(get_workload(load_benchmark_config())["reader_pool_workers"])


def test_reader_pool_matches_direct(mef3_file):  # noqa: F811
    rdr = MefReader(mef3_file)
    channels = list(rdr.channels)  # 64 channels in the fixture
    cs = int(rdr.get_property('start_time', channels[0]))
    t1, t2 = cs + 1_000_000, cs + 11_000_000  # 10 s window

    pool = ReaderProcessPool(max_workers=_pool_workers())
    try:
        got = pool.read_window(mef3_file, channels, t1, t2, n_splits=4)
    finally:
        pool.shutdown()

    ref = np.asarray(rdr.get_data(channels, t1, t2), dtype=np.float32)
    m = min(got.shape[1], ref.shape[1])
    assert got.shape[0] == len(channels)
    np.testing.assert_allclose(got[:, :m], ref[:, :m], atol=1e-4, equal_nan=True)


@pytest.mark.benchmark
def test_reader_pool_parallel_speedup(mef3_file):  # noqa: F811
    """Measure parallel decode (processes) vs GIL-bound in-process decode.

    Prints the speedup; run with ``-s`` to see it. Not asserting a threshold
    (machine dependent), but the pool should beat sequential for a wide window.
    """
    rdr = MefReader(mef3_file)
    channels = list(rdr.channels)
    cs = int(rdr.get_property('start_time', channels[0]))
    t1, t2 = cs, cs + 240_000_000  # 240 s of all channels (wide, decode-heavy)

    workers = _pool_workers()
    pool = ReaderProcessPool(max_workers=workers)
    try:
        pool.warmup(mef3_file, channels[0])  # amortize process spawn / reader open

        t0 = time.perf_counter()
        seq = np.asarray(rdr.get_data(channels, t1, t2), dtype=np.float32)
        seq_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        par = pool.read_window(mef3_file, channels, t1, t2, n_splits=workers)
        par_s = time.perf_counter() - t0
    finally:
        pool.shutdown()

    m = min(seq.shape[1], par.shape[1])
    np.testing.assert_allclose(seq[:, :m], par[:, :m], atol=1e-4, equal_nan=True)
    print(f"\n[reader_pool] {len(channels)} ch x 240 s, {workers} workers")
    print(f"[reader_pool] sequential in-process: {seq_s:.3f}s")
    print(f"[reader_pool] {workers}-process pool : {par_s:.3f}s")
    print(f"[reader_pool] speedup: {seq_s / par_s:.2f}x")


@pytest.mark.benchmark
def test_reader_pool_streaming_prefetch(benchmark_mef3_file, benchmark_config):
    """Read in ``read_window_s`` windows while workers prefetch ``prefetch_chunk_s``
    chunks ahead. Compares total time vs synchronous in-process reads.

    Exercises the two independent windows: the FOREGROUND read size and the
    (finer) PREFETCH chunk size, plus how far ahead and how many worker processes.
    """
    wl = get_workload(benchmark_config)
    workers = int(wl["reader_pool_workers"])
    read_window_s = int(wl["read_window_s"])
    chunk_s = int(wl["prefetch_chunk_s"])
    ahead = int(wl["prefetch_ahead_chunks"])
    chunks_per_window = max(1, round(read_window_s / chunk_s))

    rdr = MefReader(benchmark_mef3_file)
    channels = list(rdr.channels)
    cs = int(rdr.get_property("start_time", channels[0]))
    end = max(rdr.get_property("end_time"))
    chunk_us = int(chunk_s * 1e6)
    available_chunks = int((end - cs) // chunk_us)
    max_windows = min(6, available_chunks // chunks_per_window)
    assert max_windows >= 1, "benchmark file too short for one read window"
    total_chunks = max_windows * chunks_per_window

    # --- WITH pool prefetch: foreground reads served from prefetched chunks ---
    pool = ReaderProcessPool(max_workers=workers)
    scheduled = 0

    def schedule_up_to(target):
        nonlocal scheduled
        target = min(target, total_chunks)
        while scheduled < target:
            t0 = cs + scheduled * chunk_us
            pool.prefetch_chunk(scheduled, benchmark_mef3_file, channels, t0, t0 + chunk_us)
            scheduled += 1

    try:
        pool.warmup(benchmark_mef3_file, channels[0])
        t0 = time.perf_counter()
        schedule_up_to(chunks_per_window + ahead)  # prime the pipeline
        for w in range(max_windows):
            parts = []
            for c in range(w * chunks_per_window, (w + 1) * chunks_per_window):
                arr = pool.take_chunk(c)
                if arr is None:  # safety fallback: read in-process
                    t = cs + c * chunk_us
                    arr = _worker_read(benchmark_mef3_file, channels, t, t + chunk_us)
                parts.append(arr)
            m = min(p.shape[1] for p in parts)
            window = np.concatenate([p[:, :m] for p in parts], axis=1)
            schedule_up_to((w + 1) * chunks_per_window + chunks_per_window + ahead)
            run_detector(window, wl)
        prefetch_s = time.perf_counter() - t0
    finally:
        pool.shutdown()

    # --- baseline: synchronous in-process 5-min reads (no prefetch) ---
    win_us = int(read_window_s * 1e6)
    t0 = time.perf_counter()
    for w in range(max_windows):
        t = cs + w * win_us
        data = np.asarray(rdr.get_data(channels, t, t + win_us), dtype=np.float32)
        run_detector(data, wl)
    baseline_s = time.perf_counter() - t0

    print(f"\n[streaming] {len(channels)} ch, read_window={read_window_s}s, "
          f"prefetch_chunk={chunk_s}s x {chunks_per_window}/window, "
          f"ahead={ahead}, workers={workers}, windows={max_windows}, "
          f"processing={wl['processing_mode']}")
    print(f"[streaming] baseline (in-process, no prefetch): {baseline_s:.3f}s")
    print(f"[streaming] pool prefetch (read + prefetch)   : {prefetch_s:.3f}s")
    print(f"[streaming] speedup: {baseline_s / prefetch_s:.2f}x")
