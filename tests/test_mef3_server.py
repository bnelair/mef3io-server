import pytest
import numpy as np
from concurrent import futures

from mef3io import MefReader

# Import your server implementation and generated protobuf files

import mef3io_server.protobufs.gRPCMef3Server_pb2 as pb2

from .conftest import mef3_file, grpc_stub_1, grpc_stub_2


def _get_signal_range(stub, file_path, channels, start_uutc, end_uutc):
    """Stream a GetSignalRange call and reassemble the full float32 array."""
    request = pb2.SignalRangeRequest(
        file_path=file_path, channel_names=list(channels or []),
        start_uutc=int(start_uutc), end_uutc=int(end_uutc),
    )
    tiles = []
    for chunk in stub.GetSignalRange(request):
        assert chunk.error_message == "", chunk.error_message
        arr = np.frombuffer(chunk.array_bytes, dtype=chunk.dtype)
        tiles.append(arr.reshape(chunk.shape))
    return np.concatenate(tiles, axis=1) if len(tiles) > 1 else tiles[0]


@pytest.mark.order(1)
def test_list_open_files(grpc_stub_1, mef3_file):
    # Should be empty before opening
    resp = grpc_stub_1.ListOpenFiles(pb2.ListOpenFilesRequest())
    assert mef3_file not in resp.file_paths

    # Open file
    grpc_stub_1.OpenFile(pb2.OpenFileRequest(file_path=mef3_file))
    resp = grpc_stub_1.ListOpenFiles(pb2.ListOpenFilesRequest())
    assert mef3_file in resp.file_paths

    # Close file
    grpc_stub_1.CloseFile(pb2.FileInfoRequest(file_path=mef3_file))
    resp = grpc_stub_1.ListOpenFiles(pb2.ListOpenFilesRequest())
    assert mef3_file not in resp.file_paths


def test_basic_functional(grpc_stub_1, mef3_file):
    """OpenFile + FileInfo metadata + a channels/time range read vs reference."""
    request = pb2.OpenFileRequest(file_path=mef3_file)
    response = grpc_stub_1.OpenFile(request)

    # Use actual file info
    assert response.file_opened is True
    assert response.file_path == mef3_file
    nch = response.number_of_channels
    assert nch > 0
    # Explicit per-channel metadata (parallel arrays).
    assert len(response.channel_names) == nch
    assert len(response.channel_sampling_rates) == nch
    assert len(response.channel_start_uutc) == nch
    assert len(response.channel_end_uutc) == nch
    assert response.start_uutc == min(response.channel_start_uutc)
    assert response.end_uutc == max(response.channel_end_uutc)

    response_info = grpc_stub_1.FileInfo(pb2.FileInfoRequest(file_path=mef3_file))
    assert response == response_info

    # Read the first 60 s of all channels and compare to the direct reader.
    start = response.start_uutc
    end = start + 60 * 1_000_000
    reconstructed = _get_signal_range(
        grpc_stub_1, mef3_file, response.channel_names, start, end)

    rdr = MefReader(mef3_file)
    data_reference = np.asarray(rdr.get_data(rdr.channels, start, end), dtype=np.float32)
    m = min(reconstructed.shape[1], data_reference.shape[1])
    np.testing.assert_allclose(
        reconstructed[:, :m], data_reference[:, :m], atol=1e-4, equal_nan=True)


import concurrent.futures


def test_concurrent_same_data(grpc_stub_1, grpc_stub_2, mef3_file):
    """Two concurrent stubs requesting the same range must get identical data."""
    info = grpc_stub_1.OpenFile(pb2.OpenFileRequest(file_path=mef3_file))
    start = info.start_uutc
    end = start + 60 * 1_000_000

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futs = [
            executor.submit(_get_signal_range, grpc_stub_1, mef3_file, info.channel_names, start, end),
            executor.submit(_get_signal_range, grpc_stub_2, mef3_file, info.channel_names, start, end),
        ]
        results = [f.result() for f in futs]

    # Both results should be identical and match the reference
    np.testing.assert_array_equal(results[0], results[1])

    rdr = MefReader(mef3_file)
    data_reference = np.asarray(rdr.get_data(rdr.channels, start, end), dtype=np.float32)
    m = min(results[0].shape[1], data_reference.shape[1])
    np.testing.assert_allclose(
        results[0][:, :m], data_reference[:, :m], atol=1e-4, equal_nan=True)


def test_concurrent_different_data(grpc_stub_1, grpc_stub_2, mef3_file):
    """Two concurrent stubs requesting different ranges, each checked vs reference."""
    info = grpc_stub_1.OpenFile(pb2.OpenFileRequest(file_path=mef3_file))
    start0 = info.start_uutc
    end0 = start0 + 60 * 1_000_000
    start1, end1 = end0, end0 + 60 * 1_000_000

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futs = [
            executor.submit(_get_signal_range, grpc_stub_1, mef3_file, info.channel_names, start0, end0),
            executor.submit(_get_signal_range, grpc_stub_2, mef3_file, info.channel_names, start1, end1),
        ]
        result0, result1 = [f.result() for f in futs]

    rdr = MefReader(mef3_file)
    ref0 = np.asarray(rdr.get_data(rdr.channels, start0, end0), dtype=np.float32)
    ref1 = np.asarray(rdr.get_data(rdr.channels, start1, end1), dtype=np.float32)

    m0 = min(result0.shape[1], ref0.shape[1])
    np.testing.assert_allclose(result0[:, :m0], ref0[:, :m0], atol=1e-4, equal_nan=True)
    m1 = min(result1.shape[1], ref1.shape[1])
    np.testing.assert_allclose(result1[:, :m1], ref1[:, :m1], atol=1e-4, equal_nan=True)
