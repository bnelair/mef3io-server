from mef_tools import MefWriter

import time

import pytest
import tempfile
import os
import numpy as np
import datetime
import grpc
import threading
from concurrent import futures

import mef3io_server.protobufs.gRPCMef3Server_pb2_grpc as pb2_grpc

from mef3io_server.server.mef3_server import gRPCMef3Server, FileManager

from .benchmark_data import load_benchmark_config, get_or_create_benchmark_file


@pytest.fixture(scope="session")
def mef3_file():
    """Legacy fixture for backward compatibility (5 minutes of data)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, "test_data.mefd")
        channel_names = [f"Ch{i}" for i in range(0, 64)]
        fs = 256
        duration_s = 5*60

        wrt = MefWriter(file_path, overwrite=True)
        wrt.mef_block_len = 1000
        wrt.max_nans_written = 0
        wrt.data_units = 'uV'

        start_uutc = int(np.round(datetime.datetime.now().timestamp() * 1e6))

        for ch in channel_names:
            x = np.random.randn(fs*duration_s)
            wrt.write_data(x, ch, start_uutc=start_uutc, sampling_freq=fs, precision=3, discont_handler=False)

        yield file_path


# MEF3 test data configuration constants
MEF3_TEST_CHANNELS = 64
MEF3_TEST_FS = 256
MEF3_TEST_PRECISION = 2
# Timestamp 100 days in the past (to simulate historical data)
MEF3_TEST_START_OFFSET_DAYS = 100

# For functional tests (1 hour)
MEF3_FUNCTIONAL_TEST_DURATION_S = 60 * 60  

# For benchmarks (2 hours)
MEF3_BENCHMARK_DURATION_S = 2 * 60 * 60


def record_benchmark_setup(benchmark, *, access, file_path, total_channels,
                           active_channels, fs, precision, duration_s,
                           num_chunks, segment_size_s, rounds, sleep_seconds=None,
                           server="none (direct MefReader)", **server_cfg):
    """Attach the file/dataset and server setup of a benchmark and print it.

    The info is stored in ``benchmark.extra_info`` (saved by ``--benchmark-save`` /
    ``--benchmark-json``) and printed so it is visible when running with ``-s``.
    Any extra keyword (``server_cfg``) describing the server tuning under test is
    recorded verbatim; ``None`` values are dropped.
    """
    info = {
        "access_pattern": access,
        # --- File / dataset under test ---
        "file": os.path.basename(file_path),
        "file_path": file_path,
        "total_channels_in_file": total_channels,
        "channels_under_test": active_channels,
        "sampling_rate_hz": fs,
        "precision": precision,
        "file_duration_s": duration_s,
        "file_duration_h": round(duration_s / 3600, 2),
        # --- Workload ---
        "num_chunks": num_chunks,
        "segment_size_s": segment_size_s,
        "rounds": rounds,
        "sleep_seconds": sleep_seconds,
        # --- Server config ---
        "server": server,
        # --- Host ---
        "host_cpu_count": os.cpu_count(),
    }
    info.update({k: v for k, v in server_cfg.items() if v is not None})
    benchmark.extra_info.update(info)
    print(f"\n[benchmark setup] {access}")
    for k, v in info.items():
        print(f"    {k}: {v}")


@pytest.fixture(scope="session")
def benchmark_config():
    """The dataset config driving benchmark file generation.

    Values come from ``tests/benchmark_config.json`` (or ``$BENCHMARK_CONFIG``);
    edit that file to switch between a small dev dataset and the full benchmark.
    """
    return load_benchmark_config()


@pytest.fixture(scope="session")
def benchmark_mef3_file(benchmark_config):
    """Path to a persistent, config-driven MEF3 benchmark file.

    The file is generated once from :func:`benchmark_config` and cached on disk
    (see ``tests/benchmark_data.py``); identical configs are reused across runs
    instead of being regenerated. Change the dataset by editing
    ``tests/benchmark_config.json``.
    """
    return get_or_create_benchmark_file(benchmark_config)


@pytest.fixture(scope="module")
def functional_test_mef3_file(tmp_path_factory):
    """
    Creates a realistic MEF3 file for functional tests.
    - 64 channels
    - 256 Hz sampling rate
    - 1 hour of data
    - precision=2 as specified
    - Timestamp set 100 days in past to simulate historical data
    
    Module-scoped so the file is created once for all tests in a module.
    """
    tmpdir = tmp_path_factory.mktemp("functional_test_data")
    pth = str(tmpdir)
    pth_mef = os.path.join(pth, "functional_test_data.mefd")
    
    wrt = MefWriter(pth_mef, overwrite=True)
    wrt.mef_block_len = 10000
    wrt.max_nans_written = 0
    
    # Use consistent timestamp in microseconds (MEF3 standard)
    # Set 100 days in the past to simulate historical data
    s = (datetime.datetime.now().timestamp() - 3600*24*MEF3_TEST_START_OFFSET_DAYS) * 1e6
    
    print("\n[Creating functional test MEF3 file - 1 hour of data]")
    for idx in range(MEF3_TEST_CHANNELS):
        chname = f"chan_{idx+1:03d}"
        x = np.random.randn(MEF3_FUNCTIONAL_TEST_DURATION_S * MEF3_TEST_FS)
        wrt.write_data(x, chname, s, MEF3_TEST_FS, precision=MEF3_TEST_PRECISION)
    print("[Functional test MEF3 file created successfully]")
    
    return pth_mef


@pytest.fixture(scope="function")
def launch_server_process():
    """
    Launches the gRPC server main() in a separate process.
    Used for functional tests that need server on port 50051.
    """
    import multiprocessing
    from mef3io_server.server.__main__ import main as server_entrypoint

    # The server is launched as a DAEMON process, which cannot itself spawn child
    # processes -- so the decode process pool must be off here (these functional
    # tests check correctness on the thread path, not parallel decode). Set the env
    # for the spawned child, then restore.
    prev = os.environ.get("USE_PROCESS_POOL")
    os.environ["USE_PROCESS_POOL"] = "0"
    try:
        proc = multiprocessing.Process(target=server_entrypoint, daemon=True)
        proc.start()
    finally:
        if prev is None:
            os.environ.pop("USE_PROCESS_POOL", None)
        else:
            os.environ["USE_PROCESS_POOL"] = prev

    # Wait for the server to bind the port and be ready
    time.sleep(3)

    yield

    # Terminate the process (triggers handle_sigterm in main)
    proc.terminate()
    proc.join(timeout=5)
    # Give a bit more time for the port to be released
    time.sleep(1.0)


# --- Server and Client Fixtures ---------------------------------------
def create_grpc_server(max_workers=4, **fm_kwargs):
    """Create a gRPC server, forwarding ``fm_kwargs`` to the FileManager.

    ``max_workers`` sizes both the gRPC servicer thread pool and the FileManager's
    thread-prefetch fallback; any other FileManager knob (``use_process_pool``,
    ``prefetch_ahead_windows``, ``tile_cache_bytes``, ...) is passed through.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    file_manager = FileManager(max_workers=max_workers, **fm_kwargs)
    servicer = gRPCMef3Server(file_manager)
    pb2_grpc.add_gRPCMef3ServerServicer_to_server(servicer, server)
    return server

@pytest.fixture(scope="function")
def grpc_server_factory():
    """
    Factory fixture to create and manage a gRPC server for a single test.
    Yields a function that starts the server and returns the port.
    Handles teardown automatically.
    """
    servers = []

    def _server_starter(max_workers=4, **fm_kwargs):
        server = create_grpc_server(max_workers=max_workers, **fm_kwargs)
        # Bind an OS-assigned ephemeral port (":0") and use whatever we get. A
        # fixed port is not portable: Windows refuses to rebind a port that is
        # still held/recently used, which flakes across tests.
        port = server.add_insecure_port("localhost:0")

        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()
        time.sleep(0.1)

        servers.append(server)
        print(f"\nStarted test gRPC server on port {port} with "
              f"max_workers={max_workers}, {fm_kwargs}")
        return port

    yield _server_starter

    # Teardown logic: stop all servers created by the factory
    for server in servers:
        print(f"\nStopping test gRPC server...")
        server.stop(0)

    # Add a small delay to ensure ports are released
    if servers:
        time.sleep(0.2)


@pytest.fixture(scope="function")
def shared_test_server():
    """
    Creates a shared gRPC server for testing with multiple stubs.
    Uses threading instead of multiprocessing to avoid fork issues.
    """
    # Bind an OS-assigned ephemeral port (":0"); a fixed port is not portable
    # (Windows refuses to rebind a recently-used port, and it can collide with
    # the session-scoped server in test_client.py).
    server = create_grpc_server(max_workers=4)
    port = server.add_insecure_port("localhost:0")

    # Start server in a thread
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()
    time.sleep(0.5)  # Wait for server to start
    
    yield port
    
    # Stop server
    server.stop(0)
    time.sleep(0.2)


@pytest.fixture(scope="function")
def grpc_stub_1(shared_test_server):
    """
    First gRPC client stub for concurrent testing.
    Connects to the shared test server.
    """
    port = shared_test_server
    channel = grpc.insecure_channel(f'localhost:{port}')
    stub = pb2_grpc.gRPCMef3ServerStub(channel)
    yield stub
    channel.close()


@pytest.fixture(scope="function")
def grpc_stub_2(shared_test_server):
    """
    Second gRPC client stub for concurrent testing.
    Connects to the shared test server.
    """
    port = shared_test_server
    channel = grpc.insecure_channel(f'localhost:{port}')
    stub = pb2_grpc.gRPCMef3ServerStub(channel)
    yield stub
    channel.close()
