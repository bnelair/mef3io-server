"""
Benchmark tests for MEF3 server performance.
All benchmarks use the same dataset (2 hours, 64 channels, 256 Hz, precision=2)
and the same number of operations (20 chunks) for fair comparison.
"""
import pytest
import numpy as np
import threading
from mef_tools import MefReader
from brainmaze_mef3_server.client import Mef3Client

from .benchmark_data import get_workload, server_kwargs
from .conftest import record_benchmark_setup

import time


# Standard benchmark parameters
BENCHMARK_NUM_CHUNKS = 20  # All benchmarks use 20 chunks for fair comparison
BENCHMARK_SEGMENT_SIZE_S = 60  # 60 second segments
ROUNDS = 1
SLEEP_SECONDS = 0.3 # simulating think-time between windows (use case A)


# --- Helper functions for access patterns ---

def grpc_sequential_forward(client, file_path, channels, start_uutc, num_chunks):
    """Scroll forward through the recording via gRPC, one window at a time.

    Uses the timestamp-based :meth:`Mef3Client.get_signal_range` (tile cache +
    background prefetch), so prefetch loads the next tiles during the simulated
    think-time between windows.
    """
    seg_us = int(BENCHMARK_SEGMENT_SIZE_S * 1e6)
    for i in range(num_chunks):
        s = int(start_uutc) + i * seg_us
        _ = client.get_signal_range(file_path, channels, s, s + seg_us)
        time.sleep(SLEEP_SECONDS)  # Simulate slight processing delay



def direct_mef_reader_access(rdr, num_chunks):
    """
    Read data directly using MefReader (no server, no cache).
    Baseline for comparison.
    """
    channels = rdr.channels
    start = min(rdr.get_property('start_time')) / 1e6
    end = max(rdr.get_property('end_time')) / 1e6
    chunk_duration = BENCHMARK_SEGMENT_SIZE_S

    for i in range(num_chunks):
        chunk_start = start + i * chunk_duration
        chunk_end = chunk_start + chunk_duration
        ts = time.time()
        data = rdr.get_data(channels, chunk_start*1e6, chunk_end*1e6)
        te = time.time()
        # print(f"MefReader - Chunk {i} read in {te - ts} seconds")
        time.sleep(SLEEP_SECONDS)  # Simulate slight processing delay


# --- Benchmark Tests ---

@pytest.mark.benchmark
def test_baseline_direct_mef_reader(benchmark, benchmark_mef3_file, benchmark_config):
    """
    BASELINE: Direct MefReader access (no server, no cache).
    20 chunks, 60s each.
    """
    # Pre-read header outside of benchmark
    rdr = MefReader(benchmark_mef3_file)

    record_benchmark_setup(
        benchmark,
        access="direct MefReader (baseline, no server)",
        file_path=benchmark_mef3_file,
        total_channels=len(rdr.channels),
        active_channels=len(rdr.channels),
        fs=benchmark_config["sampling_rate_hz"],
        precision=benchmark_config["precision"],
        duration_s=benchmark_config["duration_s"],
        num_chunks=BENCHMARK_NUM_CHUNKS,
        segment_size_s=BENCHMARK_SEGMENT_SIZE_S,
        rounds=ROUNDS,
        sleep_seconds=SLEEP_SECONDS,
        server="none (direct MefReader)",
    )

    benchmark.pedantic(direct_mef_reader_access, args=(rdr, BENCHMARK_NUM_CHUNKS), rounds=ROUNDS)


@pytest.mark.benchmark
def test_grpc_sequential_forward_with_prefetch(benchmark, benchmark_mef3_file, benchmark_config, grpc_server_factory):
    """
    Sequential forward access via gRPC WITH prefetching.
    20 chunks, 60s each.
    """
    workload = get_workload(benchmark_config)
    port = grpc_server_factory(**server_kwargs(workload))
    client = Mef3Client(f"localhost:{port}")

    # Setup
    client.open_file(benchmark_mef3_file)
    fi = client.get_file_info(benchmark_mef3_file)
    channels = fi['channel_names']
    start_uutc = fi['start_uutc']
    client.set_active_channels(benchmark_mef3_file, channels)

    record_benchmark_setup(
        benchmark,
        access="gRPC sequential forward WITH prefetch (get_signal_range)",
        file_path=benchmark_mef3_file,
        total_channels=len(channels),
        active_channels=len(channels),
        fs=benchmark_config["sampling_rate_hz"],
        precision=benchmark_config["precision"],
        duration_s=benchmark_config["duration_s"],
        num_chunks=BENCHMARK_NUM_CHUNKS,
        segment_size_s=BENCHMARK_SEGMENT_SIZE_S,
        rounds=ROUNDS,
        sleep_seconds=SLEEP_SECONDS,
        server="gRPC",
        use_process_pool=workload["use_process_pool"],
        prefetch_ahead_windows=workload["prefetch_ahead_windows"],
        prefetch_behind_windows=workload["prefetch_behind_windows"],
        grpc_threads=workload["grpc_threads"],
    )

    # Benchmark
    benchmark.pedantic(grpc_sequential_forward, args=(client, benchmark_mef3_file, channels, start_uutc, BENCHMARK_NUM_CHUNKS), rounds=ROUNDS)

    # Cleanup
    client.close_file(benchmark_mef3_file)
    client.shutdown()


@pytest.mark.benchmark
def test_grpc_sequential_forward_no_prefetch(benchmark, benchmark_mef3_file, benchmark_config, grpc_server_factory):
    """
    Sequential forward access via gRPC WITHOUT prefetching.
    Uses BENCHMARK_NUM_CHUNKS chunks of BENCHMARK_SEGMENT_SIZE_S seconds each.
    """
    workload = get_workload(benchmark_config)
    # Genuinely disable look-ahead/behind on the range path; parallel decode of
    # the foreground read stays on (that is a server property, not "prefetch").
    port = grpc_server_factory(**server_kwargs(
        workload, prefetch_ahead_windows=0, prefetch_behind_windows=0, max_workers=1))
    client = Mef3Client(f"localhost:{port}")

    # Setup - server with window prefetch disabled
    client.open_file(benchmark_mef3_file)
    fi = client.get_file_info(benchmark_mef3_file)
    channels = fi['channel_names']
    start_uutc = fi['start_uutc']
    client.set_active_channels(benchmark_mef3_file, channels)

    record_benchmark_setup(
        benchmark,
        access="gRPC sequential forward WITHOUT prefetch (get_signal_range)",
        file_path=benchmark_mef3_file,
        total_channels=len(channels),
        active_channels=len(channels),
        fs=benchmark_config["sampling_rate_hz"],
        precision=benchmark_config["precision"],
        duration_s=benchmark_config["duration_s"],
        num_chunks=BENCHMARK_NUM_CHUNKS,
        segment_size_s=BENCHMARK_SEGMENT_SIZE_S,
        rounds=ROUNDS,
        sleep_seconds=SLEEP_SECONDS,
        server="gRPC",
        use_process_pool=workload["use_process_pool"],
        prefetch_ahead_windows=0,
        prefetch_behind_windows=0,
        grpc_threads=1,  # benchmark server uses ThreadPoolExecutor(max_workers)
    )

    # Benchmark
    benchmark.pedantic(grpc_sequential_forward, args=(client, benchmark_mef3_file, channels, start_uutc, BENCHMARK_NUM_CHUNKS), rounds=ROUNDS)

    # Cleanup
    client.close_file(benchmark_mef3_file)
    client.shutdown()


