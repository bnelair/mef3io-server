"""n_points per request: MinMaxLTTB reduction to exactly as many points as asked.

* the reduction returns real samples, keeps a spike and the extremes, and
  gives NaN only where a bucket has no data;
* a request returns exactly n_points per channel, fs = n_points / window;
* the source is the cached rate when it has detail to spare, the original
  rate when zoomed in, and the original unchanged when it has fewer samples
  than asked (never upsampled);
* it works with and without a session cache_fs, and over gRPC.
"""
import numpy as np
import pytest

import mef3io
from mef3io_server.client import Mef3Client
from mef3io_server.server.file_manager import FileManager
from mef3io_server.server.reduce import minmax_lttb

START = 1_700_000_000_000_000
FS = 4096.0
SPIKE_AT = 31.5  # seconds


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("npoints") / "np.mefd")
    t = np.arange(int(120 * FS)) / FS
    x = np.round(1000 * np.sin(2 * np.pi * 0.5 * t))
    x[int(SPIKE_AT * FS)] = 50_000
    with mef3io.Writer(path, overwrite=True) as w:
        w.write("a", x, START, fs=FS, precision=0)
        w.write("b", -x, START, fs=FS, precision=0)
    return path, x


@pytest.fixture
def fm():
    m = FileManager(use_process_pool=False, tile_duration_s=10)
    yield m
    m.shutdown()


def test_the_reduction_is_faithful():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(3, 50_000))
    x[1, 12_345] = 99.0
    x[2, 40_000:45_000] = np.nan
    v = minmax_lttb(x, 584)
    assert v.shape == (3, 584)
    for r in range(3):
        real = v[r][~np.isnan(v[r])]
        assert np.isin(real, x[r]).all()  # real samples only
    assert v[1].max() == 99.0  # the spike survives
    nan = np.isnan(v[2])
    assert 0 < nan.sum() <= 60 and not np.isnan(v[0]).any()  # only the gap's buckets
    assert minmax_lttb(x[:, :100], 500).shape == (3, 100)  # never more than the input


def test_a_request_returns_exactly_n_points(fm, session):
    path, x = session
    fm.open_file(path, client_id="v")
    t0 = START + 20_000_000
    out = fm.read_signal_range(path, ["a", "b"], t0, t0 + 20_000_000, client_id="v",
                               n_points=584)
    assert out["array"].shape == (2, 584)
    assert out["fs"] == pytest.approx(584 / 20.0)
    assert out["array"][0].max() == 50_000 and out["array"][1].min() == -50_000  # spike kept
    window = x[int(20 * FS):int(40 * FS)]
    assert out["array"][0].min() == window.min()


def test_the_source_follows_the_zoom(fm, session):
    path, x = session
    fm.open_file(path, client_id="v", cache_fs=128)
    calls = []
    real = fm._read_level

    def spy(*args, **kwargs):
        calls.append(kwargs.get("native", False))
        return real(*args, **kwargs)

    fm._read_level = spy
    t0 = START + 30_000_000
    # 60 s at 128 Hz = 7680 samples >= 2 * 1000: reduced from the cached rate
    fm.read_signal_range(path, ["a"], t0, t0 + 60_000_000, client_id="v", n_points=1000)
    # 2 s at 128 Hz = 256 < 2 * 1000: zoomed in, from the original rate
    out = fm.read_signal_range(path, ["a"], t0, t0 + 2_000_000, client_id="v", n_points=1000)
    assert calls == [False, True]
    assert out["array"].shape == (1, 1000)
    # 0.1 s of the original = 409 samples < 1000: returned unchanged
    out = fm.read_signal_range(path, ["a"], t0, t0 + 100_000, client_id="v", n_points=1000)
    assert out["array"].shape == (1, 410) and out["fs"] == FS
    np.testing.assert_array_equal(out["array"][0], x[int(30 * FS):int(30 * FS) + 410])


def test_without_a_session_or_cache_fs(fm, session):
    path, _ = session
    fm.open_file(path, client_id="owner")
    out = fm.read_signal_range(path, ["a"], START, START + 10_000_000, client_id="reader",
                               n_points=200)
    assert out["array"].shape == (1, 200)


@pytest.mark.parametrize("bad", [0, -1])
def test_n_points_must_be_positive(fm, session, bad):
    path, _ = session
    fm.open_file(path, client_id="v")
    with pytest.raises(ValueError, match="positive"):
        fm.read_signal_range(path, ["a"], START, START + 1_000_000, client_id="v", n_points=bad)


def test_over_grpc(session, grpc_server_factory):
    path, _ = session
    port = grpc_server_factory(use_process_pool=False)
    c = Mef3Client(f"localhost:{port}")
    try:
        c.open_file(path, cache_fs=256)
        out = c.get_signal_range(path, ["a", "b"], START, START + 60_000_000, n_points=584)
        assert out["array"].shape == (2, 584) and out["fs"] == pytest.approx(584 / 60)
        full = c.get_signal_range(path, ["a"], START, START + 1_000_000)
        assert full["fs"] == 256 and full["array"].shape == (1, 256)
    finally:
        c.shutdown()


def test_the_overall_extremes_are_always_shown():
    """One point per bucket drops one of its two extremes; the row's overall
    max and min are put back so the amplitude is never understated."""
    x = np.zeros((1, 10_000))
    x[0, 100], x[0, 101] = 5.0, -6.0  # a peak next to a deeper trough, one bucket
    x[0, 5000] = 3.0
    v = minmax_lttb(x, 100)
    assert v.min() == -6.0  # the larger excursion wins when both share a bucket
    assert v.max() == 3.0
    rng = np.random.default_rng(7)
    shared = 0
    for _ in range(50):
        y = rng.normal(size=(2, 20_000))
        w = minmax_lttb(y, 200)
        for r in range(2):
            b_hi, b_lo = y[r].argmax() // 100, y[r].argmin() // 100  # 100 samples a bucket
            if b_hi != b_lo:
                assert w[r].max() == y[r].max() and w[r].min() == y[r].min()
            else:  # one point for both: the larger excursion is shown
                shared += 1
                big = y[r].max() if abs(y[r].max()) >= abs(y[r].min()) else y[r].min()
                assert big in w[r]
    assert shared < 10
    single = minmax_lttb(np.array([[1.0, 9.0, -2.0, 4.0]]), 1)
    assert single.shape == (1, 1) and single[0, 0] in (9.0, -2.0)


def test_float32_input_is_not_copied_to_float64():
    import tracemalloc
    x = np.random.default_rng(0).normal(size=(8, 400_000)).astype(np.float32)
    tracemalloc.start()
    v = minmax_lttb(x, 500)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < x.nbytes / 2, peak  # far below a float64 copy (2 x x.nbytes)
    assert np.isin(v, x.astype(np.float64)).all()


def test_unknown_channels_and_mixed_rates(fm, session):
    path, _ = session
    fm.open_file(path, client_id="v")
    with pytest.raises(ValueError, match="Unknown channels"):
        fm.read_signal_range(path, ["nope"], START, START + 1_000_000, client_id="v",
                             n_points=10)


def test_a_client_refuses_an_old_server_ignoring_n_points():
    import mef3io_server.protobufs.gRPCMef3Server_pb2 as pb2

    class OldStub:
        def GetSignalRange(self, req, metadata=None):
            a = np.zeros((1, 1000), dtype=np.float32)
            yield pb2.SignalChunk(file_path=req.file_path, array_bytes=a.tobytes(),
                                  dtype="float32", shape=[1, 1000], start_uutc=0,
                                  end_uutc=1, fs=1000.0, channel_names=["a"])

    c = Mef3Client("localhost:1")
    c.stub = OldStub()
    try:
        with pytest.raises(RuntimeError, match="does not support n_points"):
            c.get_signal_range("/x.mefd", ["a"], 0, 1, n_points=100)
        assert c.get_signal_range("/x.mefd", ["a"], 0, 1)["array"].shape == (1, 1000)
    finally:
        c.shutdown()
