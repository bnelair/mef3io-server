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
