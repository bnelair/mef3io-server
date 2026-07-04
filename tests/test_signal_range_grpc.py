"""End-to-end gRPC tests for timestamp-based access (GetSignalRange)."""
import numpy as np
import pytest
from mef3io import MefReader

from mef3io_server.client import Mef3Client
from .conftest import mef3_file  # noqa: F401 - pytest fixture


@pytest.fixture()
def client_and_file(mef3_file, grpc_server_factory):  # noqa: F811
    port = grpc_server_factory(max_workers=4)
    client = Mef3Client(f"localhost:{port}")
    client.open_file(mef3_file)
    yield client, mef3_file
    client.close_file(mef3_file)
    client.shutdown()


def test_get_signal_range_matches_direct(client_and_file):
    client, fp = client_and_file
    rdr = MefReader(fp)
    channels = list(rdr.channels)[:4]
    cs = int(rdr.get_property('start_time', channels[0]))
    t1 = cs + 2_500_000
    t2 = cs + 33_000_000

    res = client.get_signal_range(fp, channels, t1, t2)
    assert res['error_message'] == ''
    assert res['dtype'] == 'float32'
    assert res['channel_names'] == channels

    ref = np.asarray(rdr.get_data(channels, t1, t2), dtype=np.float32)
    got = res['array']
    m = min(got.shape[1], ref.shape[1])
    assert abs(got.shape[1] - ref.shape[1]) <= 1
    np.testing.assert_allclose(got[:, :m], ref[:, :m], atol=1e-4, equal_nan=True)


def test_get_signal_range_all_channels_default(client_and_file):
    client, fp = client_and_file
    rdr = MefReader(fp)
    cs = int(rdr.get_property('start_time', list(rdr.channels)[0]))
    res = client.get_signal_range(fp, None, cs, cs + 4_000_000)
    assert res['array'].shape[0] == len(rdr.channels)


def test_get_signal_range_cache_reuse_second_read_matches(client_and_file):
    client, fp = client_and_file
    rdr = MefReader(fp)
    channels = list(rdr.channels)[:2]
    cs = int(rdr.get_property('start_time', channels[0]))
    t1, t2 = cs + 10_000_000, cs + 25_000_000
    first = client.get_signal_range(fp, channels, t1, t2)['array']
    second = client.get_signal_range(fp, channels, t1, t2)['array']
    np.testing.assert_array_equal(first, second)


def test_get_signal_range_error_on_unopened_file(grpc_server_factory):
    port = grpc_server_factory(max_workers=2)
    client = Mef3Client(f"localhost:{port}")
    res = client.get_signal_range("/nope/none.mefd", ["a"], 0, 1_000_000)
    assert res['array'] is None
    assert res['error_message']
    client.shutdown()
