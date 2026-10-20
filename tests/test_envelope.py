"""GetEnvelope: per-block min/max from the MEF block index, per channel.

* the arrays equal mef3io's own Reader.block_envelope (nothing recomputed);
* a window returns the overlapping blocks; empty channels means all;
* unknown channels and files not open are errors, over gRPC too.
"""
import numpy as np
import pytest

import mef3io
from mef3io_server.client import Mef3Client
from mef3io_server.server.file_manager import FileManager

START = 1_700_000_000_000_000


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("env") / "env.mefd")
    rng = np.random.default_rng(2)
    with mef3io.Writer(path, overwrite=True) as w:
        for ch in ("a", "b"):
            w.write(ch, rng.normal(0, 50, 256 * 120).round(), START, fs=256.0, precision=0)
    return path


@pytest.fixture
def fm():
    m = FileManager(use_process_pool=False)
    yield m
    m.shutdown()


def test_the_envelope_is_mef3ios(fm, session):
    fm.open_file(session, client_id="v")
    got = dict(fm.read_envelope(session, None, client_id="v"))
    assert sorted(got) == ["a", "b"]
    with mef3io.Reader(session) as r:
        for ch in ("a", "b"):
            want = r.block_envelope(ch)
            for key in ("start_uutc", "end_uutc", "min", "max"):
                np.testing.assert_array_equal(got[ch][key], want[key])
    assert len(got["a"]["min"]) == 12  # 120 s in 10-s blocks


def test_a_window_and_errors(fm, session):
    fm.open_file(session, client_id="v")
    got = dict(fm.read_envelope(session, ["a"], START + 15_000_000, START + 35_000_000))
    assert list((got["a"]["start_uutc"] - START) // 1_000_000) == [10, 20, 30]
    with pytest.raises(ValueError, match="Unknown channels"):
        dict(fm.read_envelope(session, ["nope"]))
    with pytest.raises(ValueError, match="not open"):
        dict(fm.read_envelope("/no/such.mefd", None))


def test_over_grpc(session, grpc_server_factory):
    port = grpc_server_factory(use_process_pool=False)
    c = Mef3Client(f"localhost:{port}")
    try:
        c.open_file(session)
        env = c.get_envelope(session, ["b"], START, START + 20_000_000)
        assert list(env) == ["b"] and len(env["b"]["max"]) == 2
        assert env["b"]["start_uutc"].dtype == np.int64
        assert len(c.get_envelope(session)) == 2  # all channels
        with pytest.raises(RuntimeError, match="Unknown channels"):
            c.get_envelope(session, ["nope"])
    finally:
        c.shutdown()


def test_tiles_of_whole_mef_blocks(tmp_path):
    """respect_mef_blocks: a 60 s tile over 7-s blocks becomes 63 s (9 blocks),
    so tile edges fall on block boundaries; reads are unchanged."""
    path = str(tmp_path / "blocks.mefd")
    x = np.arange(256 * 200, dtype=float)
    with mef3io.Writer(path, overwrite=True, block_length=256 * 7) as w:
        w.write("a", x, START, fs=256.0, precision=0)
    plain = FileManager(use_process_pool=False, tile_duration_s=60)
    blocks = FileManager(use_process_pool=False, tile_duration_s=60, respect_mef_blocks=True)
    try:
        for m in (plain, blocks):
            m.open_file(path, client_id="v")
        with blocks._lock:
            fs, cs, S, _ = blocks._get_tile_meta_unsafe(blocks._files[path], "a")
        assert S == 256 * 63  # 9 blocks of 7 s
        t0, t1 = START + 50_000_000, START + 130_000_000
        a = plain.read_signal_range(path, ["a"], t0, t1, client_id="v")["array"]
        b = blocks.read_signal_range(path, ["a"], t0, t1, client_id="v")["array"]
        np.testing.assert_array_equal(a, b)
    finally:
        plain.shutdown()
        blocks.shutdown()


def test_a_long_channel_comes_in_several_messages(session, grpc_server_factory):
    """Each channel is chunked (here 5 rows a message) and joined again by the
    client, so a month of 1-s blocks stays under gRPC's 4 MB message limit."""
    port = grpc_server_factory(use_process_pool=False)
    c = Mef3Client(f"localhost:{port}")
    try:
        c.open_file(session)
        whole = c.get_envelope(session, ["a"])
        FileManager.ENVELOPE_CHUNK_ROWS = 5
        try:
            chunked = c.get_envelope(session, ["a", "b"])
        finally:
            FileManager.ENVELOPE_CHUNK_ROWS = 100_000
        assert len(chunked["a"]["min"]) == 12
        for key in ("start_uutc", "end_uutc", "min", "max"):
            np.testing.assert_array_equal(chunked["a"][key], whole["a"][key])
    finally:
        c.shutdown()


def test_tiles_fall_back_when_the_block_length_cannot_be_read(tmp_path, monkeypatch):
    path = str(tmp_path / "fallback.mefd")
    with mef3io.Writer(path, overwrite=True) as w:
        w.write("a", np.arange(256 * 30, dtype=float), START, fs=256.0, precision=0)
    m = FileManager(use_process_pool=False, tile_duration_s=7, respect_mef_blocks=True)
    try:
        m.open_file(path, client_id="v")
        rdr = m._files[path]["reader"]
        monkeypatch.setattr(rdr, "block_envelope",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no index")))
        with m._lock:
            _, _, S, _ = m._get_tile_meta_unsafe(m._files[path], "a")
        assert S == 256 * 7  # the plain tile length
    finally:
        m.shutdown()
