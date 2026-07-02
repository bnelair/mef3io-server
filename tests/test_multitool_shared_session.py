"""Multi-tool shared-session benchmark (USE CASE B).

The scenario: ONE iEEG session that several independent tools must process --
e.g. a spike detector, a seizure detector, and a spectral/QC pass -- with their
data access overlapping (they look at the same recording, often the same
regions).

* ``native_local``   -- every tool reads the file itself. MEF3 decode is
  decrypt + decompress, and nothing is shared: an overlapping region is decoded
  once PER TOOL. This is what "reading and decrypting/decompressing multiple
  times" costs. (We even give native the *best* case here: all tools share one
  in-process ``MefReader`` and the OS page cache, so only the decode work is
  duplicated, not the file open or the disk read.)
* ``grpc_shared_cache`` -- all tools are clients of ONE server with ONE shared
  tile cache. The first tool to touch a region decodes it (cold); every other
  tool that needs the same region is served warm from the cache -- decoded
  ONCE, not once per tool. This is the scenario the server is built to win.

The win scales with ``tool_overlap`` (how much the tools' data access overlaps)
and ``num_tools``. At ``tool_overlap = 0`` the tools share nothing, so the
server has nothing to reuse and only adds transport overhead -- reported
honestly by the same benchmark.

Workload (``num_tools``, ``tool_overlap``, window count/size, per-window
processing cost) is config-driven via ``benchmark_config.json`` -> ``workload``.
"""
import time

import numpy as np
import pytest
from mef_tools import MefReader

from brainmaze_mef3_server.client import Mef3Client

from .benchmark_data import get_workload, server_kwargs
from .conftest import record_benchmark_setup
from .test_automated_processing import run_detector

ROUNDS = 1


# --- Per-tool window plans ----------------------------------------------------

def plan_tool_windows(num_tools, num_chunks, overlap):
    """Return ``(per_tool_window_indices, n_distinct_windows)``.

    Every tool processes exactly ``num_chunks`` windows. The first
    ``round(overlap * num_chunks)`` are a **shared** set that all tools read (the
    overlapping region the server can reuse); the rest are **private** to each
    tool (distinct window indices no other tool touches).

    So the total distinct windows the machine must decode at least once is::

        shared + num_tools * (num_chunks - shared)

    Native decodes ``num_tools * num_chunks`` windows (no reuse); the shared
    server decodes ``n_distinct`` windows (each cold region once).
    """
    shared = int(round(max(0.0, min(1.0, overlap)) * num_chunks))
    shared_idx = list(range(shared))
    private_len = num_chunks - shared
    per_tool = []
    cursor = shared
    for _ in range(num_tools):
        private = list(range(cursor, cursor + private_len))
        cursor += private_len
        per_tool.append(shared_idx + private)
    return per_tool, cursor


def _window_bounds(start_uutc, seg_us, i):
    s = start_uutc + i * seg_us
    return s, s + seg_us


# --- Access patterns ----------------------------------------------------------

def native_multitool(rdr, channels, start_uutc, seg_us, per_tool, workload, timings):
    """Each tool re-reads (and re-decodes) its windows via the local MefReader."""
    timings.clear()
    for widx in per_tool:
        t0 = time.perf_counter()
        for i in widx:
            s, e = _window_bounds(start_uutc, seg_us, i)
            data = np.asarray(rdr.get_data(channels, s, e))
            run_detector(data, workload)
        timings.append(time.perf_counter() - t0)


def grpc_multitool(clients, file_path, channels, start_uutc, seg_us, per_tool,
                   workload, timings):
    """Each tool is its own client of the one shared-cache server."""
    timings.clear()
    for client, widx in zip(clients, per_tool):
        t0 = time.perf_counter()
        for i in widx:
            s, e = _window_bounds(start_uutc, seg_us, i)
            res = client.get_signal_range(file_path, channels, int(s), int(e))
            run_detector(res["array"], workload)
        timings.append(time.perf_counter() - t0)


# --- Shared setup / reporting -------------------------------------------------

def _plan(benchmark_config):
    """Resolve the multi-tool plan and sanity-check it fits the recording."""
    wl = get_workload(benchmark_config)
    num_tools = int(wl["num_tools"])
    num_chunks = int(wl["num_chunks"])
    seg_s = int(wl["segment_size_s"])
    overlap = float(wl["tool_overlap"])
    per_tool, n_distinct = plan_tool_windows(num_tools, num_chunks, overlap)
    return wl, num_tools, num_chunks, seg_s, overlap, per_tool, n_distinct


def _record(benchmark, benchmark_config, wl, *, access, server, num_tools,
            overlap, n_distinct, **server_cfg):
    record_benchmark_setup(
        benchmark, access=access,
        file_path=benchmark_config.get("data_dir", ""),
        total_channels=benchmark_config["channels"],
        active_channels=benchmark_config["channels"],
        fs=benchmark_config["sampling_rate_hz"],
        precision=benchmark_config["precision"],
        duration_s=benchmark_config["duration_s"],
        num_chunks=wl["num_chunks"], segment_size_s=wl["segment_size_s"],
        rounds=ROUNDS, server=server, **server_cfg,
    )
    benchmark.extra_info.update({
        "num_tools": num_tools,
        "tool_overlap": overlap,
        "distinct_windows_decoded": n_distinct,
        "native_windows_decoded": num_tools * wl["num_chunks"],
        "processing_mode": wl["processing_mode"],
        "compute_repeats": wl["compute_repeats"],
    })


def _report_tools(label, timings, n_distinct, native_windows):
    if not timings:
        return
    cold, warm = timings[0], timings[1:]
    print(f"\n[multitool:{label}] per-tool wall time (s): "
          + ", ".join(f"{t:.3f}" for t in timings))
    print(f"[multitool:{label}] tool 0 (cold): {cold:.3f}s | "
          f"tools 1..n (warm) mean: "
          f"{(sum(warm) / len(warm)) if warm else float('nan'):.3f}s")
    print(f"[multitool:{label}] distinct windows decoded once = {n_distinct} "
          f"(native decodes {native_windows})")


# --- Benchmarks ---------------------------------------------------------------

@pytest.mark.benchmark
def test_multitool_native_local(benchmark, benchmark_mef3_file, benchmark_config):
    """BASELINE: N tools each re-decode the shared session locally (no server)."""
    wl, num_tools, _, seg_s, overlap, per_tool, n_distinct = _plan(benchmark_config)

    rdr = MefReader(benchmark_mef3_file)
    channels = list(rdr.channels)
    start_uutc = min(rdr.get_property("start_time"))
    end_uutc = max(rdr.get_property("end_time"))
    seg_us = seg_s * 1e6
    assert n_distinct * seg_us <= (end_uutc - start_uutc), (
        f"recording too short: need {n_distinct} x {seg_s}s distinct windows")

    _record(benchmark, benchmark_config, wl,
            access="multi-tool NATIVE local MefReader (baseline)",
            server="none (direct MefReader)",
            num_tools=num_tools, overlap=overlap, n_distinct=n_distinct)

    timings = []
    benchmark.pedantic(
        native_multitool,
        args=(rdr, channels, start_uutc, seg_us, per_tool, wl, timings),
        rounds=ROUNDS,
    )
    _report_tools("native", timings, n_distinct, num_tools * wl["num_chunks"])


@pytest.mark.benchmark
def test_multitool_grpc_shared_cache(
    benchmark, benchmark_mef3_file, benchmark_config, grpc_server_factory
):
    """N tools share ONE server: an overlapping region is decoded once, not N times."""
    wl, num_tools, _, seg_s, overlap, per_tool, n_distinct = _plan(benchmark_config)

    port = grpc_server_factory(**server_kwargs(wl))
    # One client per tool -- each tool is an independent consumer of the server.
    clients = [Mef3Client(f"localhost:{port}") for _ in range(num_tools)]
    clients[0].open_file(benchmark_mef3_file)
    fi = clients[0].get_file_info(benchmark_mef3_file)
    channels = fi["channel_names"]
    start_uutc = fi["start_uutc"]
    seg_us = seg_s * 1e6

    _record(benchmark, benchmark_config, wl,
            access="multi-tool gRPC SHARED tile cache",
            server="gRPC (shared cache)",
            num_tools=num_tools, overlap=overlap, n_distinct=n_distinct,
            use_process_pool=wl["use_process_pool"],
            prefetch_ahead_windows=wl["prefetch_ahead_windows"],
            grpc_threads=wl["grpc_threads"])

    timings = []
    benchmark.pedantic(
        grpc_multitool,
        args=(clients, benchmark_mef3_file, channels, start_uutc, seg_us,
              per_tool, wl, timings),
        rounds=ROUNDS,
    )
    _report_tools("grpc", timings, n_distinct, num_tools * wl["num_chunks"])

    clients[0].close_file(benchmark_mef3_file)
    for c in clients:
        c.shutdown()
