import pytest
import numpy as np

from mef3io_server.client import Mef3Client

import time
import grpc
from concurrent import futures
import mef3io_server.protobufs.gRPCMef3Server_pb2_grpc as pb2_grpc
from mef3io_server.server.mef3_server import gRPCMef3Server, FileManager

@pytest.fixture(scope="session")
def grpc_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    file_manager = FileManager(max_workers=4)
    servicer = gRPCMef3Server(file_manager)
    pb2_grpc.add_gRPCMef3ServerServicer_to_server(servicer, server)

    # Bind an OS-assigned ephemeral port; a fixed port is not portable (Windows
    # refuses to rebind a recently-used port and it can collide with other
    # test servers).
    port = server.add_insecure_port("localhost:0")
    server.start()
    time.sleep(0.1)
    yield port
    server.stop(0)

@pytest.fixture(scope="module")
def client(grpc_server):
    c = Mef3Client(f"localhost:{grpc_server}")
    yield c
    c.shutdown()

def test_open_and_info(client, mef3_file):
    info = client.open_file(mef3_file)
    assert info["file_opened"]
    assert info["file_path"] == mef3_file
    assert info["number_of_channels"] > 0
    # Explicit per-channel metadata: names, fs, start/end (parallel lists).
    nch = info["number_of_channels"]
    assert len(info["channel_names"]) == nch
    assert len(info["channel_sampling_rates"]) == nch
    assert len(info["channel_start_uutc"]) == nch
    assert len(info["channel_end_uutc"]) == nch
    assert info["start_uutc"] == min(info["channel_start_uutc"])
    assert info["end_uutc"] == max(info["channel_end_uutc"])

    info2 = client.get_file_info(mef3_file)
    assert info2 == info

def test_get_signal_range(client, mef3_file):
    info = client.open_file(mef3_file)
    channels = info["channel_names"][:3]
    t1 = info["start_uutc"]
    t2 = t1 + 10_000_000  # 10 s

    meta = client.get_signal_range(mef3_file, channels, t1, t2)
    array = meta["array"]
    assert array is not None
    assert isinstance(array, np.ndarray)
    assert array.shape[0] == len(channels)
    assert meta["channel_names"] == channels
    assert meta["start_uutc"] is not None
    assert meta["end_uutc"] is not None
    assert meta["dtype"] is not None
    assert meta["shape"] == array.shape
    assert meta["error_message"] == ''

def test_get_signal_range_all_channels_default(client, mef3_file):
    """Empty channel list means all channels in the file."""
    info = client.open_file(mef3_file)
    t1 = info["start_uutc"]
    meta = client.get_signal_range(mef3_file, None, t1, t1 + 2_000_000)
    assert meta["array"].shape[0] == info["number_of_channels"]

def test_list_and_close(client, mef3_file):
    client.open_file(mef3_file)
    files = client.list_open_files()
    assert mef3_file in files

    resp = client.close_file(mef3_file)
    assert not resp["file_opened"]
    files = client.list_open_files()
    assert mef3_file not in files
