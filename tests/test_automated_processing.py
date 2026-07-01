"""Automated-processing benchmarks: a detector reading through a recording.

This models the analytics use case (seizure/spike detector, etc.) that walks a
recording window-by-window and does real signal processing on each window. The
point of the server is that its background prefetching decodes the *next* windows
(in a separate process/threads) while the client is busy computing the current
one, hiding decode latency behind processing time.

Three scenarios are compared on the same dataset and workload:

* ``native_local``     -- MefReader in the same process (the historical baseline
  that used to beat every server). Read and compute are serialized.
* ``grpc_with_prefetch`` -- server prefetching enabled; decode overlaps compute.
* ``grpc_no_prefetch``   -- server with prefetch off; pays transport per window
  with no overlap (expected to be the slowest).

Workload (window count/size, prefetch, and the per-window processing cost) is
config-driven via ``benchmark_config.json`` -> ``workload``.
"""
import time

import numpy as np
import pytest
from mef_tools import MefReader

from brainmaze_mef3_server.client import Mef3Client

from .benchmark_data import get_workload
from .conftest import record_benchmark_setup

ROUNDS = 1


# --- Detector stand-in -------------------------------------------------------

def detector_workload(data, repeats=1):
    """A representative, GIL-competing signal-processing load per window.

    Computes spike/seizure-style features (line length, short-time energy, and
    threshold crossings of a high-passed signal) so the cost scales with the
    number of channels and samples, like a real detector.

    Args:
        data (np.ndarray): Window of shape ``(n_channels, n_samples)``.
        repeats (int): How many times to repeat the feature pass (intensity knob).

    Returns:
        float: An aggregate score (prevents the work from being optimized away).
    """
    x = np.asarray(data, dtype=np.float64)
    if x.ndim == 1:
        x = x[np.newaxis, :]
    k = 8
    kernel = np.ones(k) / k
    score = 0.0
    for _ in range(max(1, int(repeats))):
        line_length = np.abs(np.diff(x, axis=1)).sum(axis=1)
        energy = np.mean(x * x, axis=1)
        moving_avg = np.apply_along_axis(
            lambda r: np.convolve(r, kernel, mode="same"), 1, x
        )
        high_passed = x - moving_avg
        thresh = 3.0 * high_passed.std(axis=1, keepdims=True)
        crossings = (np.abs(high_passed) > thresh).sum()
        score += float(line_length.sum() + energy.sum() + crossings)
    return score


def run_detector(data, workload):
    """Apply the configured per-window processing cost to a window of data."""
    if workload["processing_mode"] == "sleep":
        time.sleep(workload["processing_cost_s"])
    else:
        detector_workload(data, workload["compute_repeats"])


# --- Access patterns ---------------------------------------------------------

def native_processing(rdr, channels, start_uutc, workload):
    """Detector reading directly from MefReader in-process (local baseline)."""
    seg_us = workload["segment_size_s"] * 1e6
    for i in range(workload["num_chunks"]):
        s = start_uutc + i * seg_us
        e = s + seg_us
        data = np.array(rdr.get_data(channels, s, e))
        run_detector(data, workload)


def grpc_processing(client, file_path, workload):
    """Detector reading each window from the gRPC server."""
    for i in range(workload["num_chunks"]):
        res = client.get_signal_segment(file_path, i)
        run_detector(res["array"], workload)


def _record(benchmark, benchmark_config, workload, *, access, server, **server_cfg):
    record_benchmark_setup(
        benchmark,
        access=access,
        file_path=benchmark_config.get("data_dir", ""),
        total_channels=benchmark_config["channels"],
        active_channels=benchmark_config["channels"],
        fs=benchmark_config["sampling_rate_hz"],
        precision=benchmark_config["precision"],
        duration_s=benchmark_config["duration_s"],
        num_chunks=workload["num_chunks"],
        segment_size_s=workload["segment_size_s"],
        rounds=ROUNDS,
        server=server,
        **server_cfg,
    )
    benchmark.extra_info.update({
        "processing_mode": workload["processing_mode"],
        "processing_cost_s": workload["processing_cost_s"],
        "compute_repeats": workload["compute_repeats"],
    })


# --- Benchmarks --------------------------------------------------------------

@pytest.mark.benchmark
def test_processing_native_local(benchmark, benchmark_mef3_file, benchmark_config):
    """BASELINE: detector reading locally via MefReader (no server)."""
    workload = get_workload(benchmark_config)
    rdr = MefReader(benchmark_mef3_file)
    channels = list(rdr.channels)
    start_uutc = min(rdr.get_property("start_time"))

    _record(
        benchmark, benchmark_config, workload,
        access="detector NATIVE local MefReader (baseline)",
        server="none (direct MefReader)",
    )
    benchmark.pedantic(
        native_processing, args=(rdr, channels, start_uutc, workload), rounds=ROUNDS
    )


@pytest.mark.benchmark
def test_processing_grpc_with_prefetch(
    benchmark, benchmark_mef3_file, benchmark_config, grpc_server_factory
):
    """Detector reading via gRPC WITH prefetch (decode overlaps compute)."""
    workload = get_workload(benchmark_config)
    port = grpc_server_factory(
        n_prefetch=workload["n_prefetch"],
        cache_capacity_multiplier=workload["cache_capacity_multiplier"],
        max_workers=workload["max_workers"],
    )
    client = Mef3Client(f"localhost:{port}")
    client.open_file(benchmark_mef3_file)
    fi = client.get_file_info(benchmark_mef3_file)
    client.set_active_channels(benchmark_mef3_file, fi["channel_names"])
    client.set_signal_segment_size(benchmark_mef3_file, workload["segment_size_s"])

    _record(
        benchmark, benchmark_config, workload,
        access="detector gRPC WITH prefetch",
        server="gRPC",
        n_prefetch=workload["n_prefetch"],
        cache_capacity_multiplier=workload["cache_capacity_multiplier"],
        prefetch_workers=workload["max_workers"],
        grpc_threads=workload["max_workers"],
    )
    benchmark.pedantic(
        grpc_processing, args=(client, benchmark_mef3_file, workload), rounds=ROUNDS
    )

    client.close_file(benchmark_mef3_file)
    client.shutdown()


@pytest.mark.benchmark
def test_processing_grpc_no_prefetch(
    benchmark, benchmark_mef3_file, benchmark_config, grpc_server_factory
):
    """Detector reading via gRPC WITHOUT prefetch (transport, no overlap)."""
    workload = get_workload(benchmark_config)
    port = grpc_server_factory(n_prefetch=0, cache_capacity_multiplier=0, max_workers=1)
    client = Mef3Client(f"localhost:{port}")
    client.open_file(benchmark_mef3_file)
    fi = client.get_file_info(benchmark_mef3_file)
    client.set_active_channels(benchmark_mef3_file, fi["channel_names"])
    client.set_signal_segment_size(benchmark_mef3_file, workload["segment_size_s"])

    _record(
        benchmark, benchmark_config, workload,
        access="detector gRPC WITHOUT prefetch",
        server="gRPC",
        n_prefetch=0,
        cache_capacity_multiplier=0,
        prefetch_workers=1,
        grpc_threads=1,
    )
    benchmark.pedantic(
        grpc_processing, args=(client, benchmark_mef3_file, workload), rounds=ROUNDS
    )

    client.close_file(benchmark_mef3_file)
    client.shutdown()
