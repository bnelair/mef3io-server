"""Client sessions: one session per (client, file), shared readers and tiles.

* re-opening the same file from the same client reuses its session;
* closing affects only the caller; the reader closes with the last session;
* a client may read a file another client opened (tools sharing one file);
* clients that send no id keep the behaviour from before sessions existed;
* idle client sessions expire, and the number of sessions is capped;
* the tile cache is keyed by content, so two clients share decoded tiles;
* over gRPC the client id travels as metadata, set by Mef3Client.
"""
import pytest

from mef3io_server.client import Mef3Client
from mef3io_server.server.file_manager import FileManager

from .conftest import mef3_file  # noqa: F401  (fixture)


def _window(fm, path):
    info = fm.get_file_info(path)
    ch = list(info.channel_names)[:2]
    t0 = info.start_uutc
    return ch, t0, t0 + 2_000_000


@pytest.fixture
def fm():
    m = FileManager(use_process_pool=False)
    yield m
    m.shutdown()


def test_reopening_reuses_the_session(fm, mef3_file):
    for _ in range(50):
        info = fm.open_file(mef3_file, client_id="viewer-1")
        assert info.file_opened and info.error_message == ""
    assert fm.session_count() == 1


def test_closing_affects_only_the_caller(fm, mef3_file):
    fm.open_file(mef3_file, client_id="a")
    fm.open_file(mef3_file, client_id="b")
    assert fm.session_count() == 2
    assert fm.close_file(mef3_file, client_id="a").file_opened  # b still has it
    ch, t0, t1 = _window(fm, mef3_file)
    assert fm.read_signal_range(mef3_file, ch, t0, t1, client_id="b")["array"].shape[0] == 2
    assert not fm.close_file(mef3_file, client_id="b").file_opened
    assert mef3_file not in fm.list_open_files()


def test_a_client_may_read_a_file_another_opened(fm, mef3_file):
    fm.open_file(mef3_file, client_id="tool-0")
    ch, t0, t1 = _window(fm, mef3_file)
    out = fm.read_signal_range(mef3_file, ch, t0, t1, client_id="tool-1")
    assert out["array"].shape[0] == 2
    assert fm.session_count() == 1  # reading does not create a session


def test_clients_without_an_id_behave_as_before(fm, mef3_file):
    assert fm.open_file(mef3_file).error_message == ""
    assert "already open" in fm.open_file(mef3_file).error_message
    assert not fm.close_file(mef3_file).file_opened
    with pytest.raises(ValueError, match="not open"):
        fm.read_signal_range(mef3_file, None, 0, 1)


def test_expiry(mef3_file):
    fm = FileManager(use_process_pool=False, session_ttl_s=60)
    try:
        fm.open_file(mef3_file, client_id="tab")
        assert fm.expire_sessions() == 0  # just used
        import time
        assert fm.expire_sessions(now=time.monotonic() + 61) == 1
        assert mef3_file not in fm.list_open_files()  # its last session: reader closed

        fm.open_file(mef3_file)  # no id: the default session, never expired
        fm.open_file(mef3_file, client_id="tab")
        assert fm.expire_sessions(now=time.monotonic() + 61) == 1
        assert mef3_file in fm.list_open_files()
    finally:
        fm.shutdown()


def test_a_read_keeps_the_session_alive(mef3_file):
    import time
    fm = FileManager(use_process_pool=False, session_ttl_s=60)
    try:
        fm.open_file(mef3_file, client_id="tab")
        start = time.monotonic()
        ch, t0, t1 = _window(fm, mef3_file)
        fm._sessions[("tab", mef3_file)]["last_used"] = start - 59
        fm.read_signal_range(mef3_file, ch, t0, t1, client_id="tab")
        assert fm.expire_sessions(now=start + 30) == 0
    finally:
        fm.shutdown()


def test_the_number_of_sessions_is_capped(mef3_file):
    fm = FileManager(use_process_pool=False, max_sessions=3)
    try:
        for k in range(5):
            fm.open_file(mef3_file, client_id=f"c{k}")
        assert fm.session_count() == 3
        assert {k[0] for k in fm._sessions} == {"c2", "c3", "c4"}  # oldest closed first
        assert mef3_file in fm.list_open_files()
    finally:
        fm.shutdown()


def test_two_clients_share_decoded_tiles(fm, mef3_file):
    fm.open_file(mef3_file, client_id="a")
    fm.open_file(mef3_file, client_id="b")
    ch, t0, t1 = _window(fm, mef3_file)
    first = fm.read_signal_range(mef3_file, ch, t0, t1, client_id="a", prefetch=False)
    tiles = len(fm._tile_cache)
    second = fm.read_signal_range(mef3_file, ch, t0, t1, client_id="b", prefetch=False)
    assert len(fm._tile_cache) == tiles  # served from a's tiles, nothing new decoded
    assert (first["array"] == second["array"]).all()


def test_over_grpc_the_id_travels_as_metadata(mef3_file, grpc_server_factory):
    port = grpc_server_factory(use_process_pool=False)
    a = Mef3Client(f"localhost:{port}", client_id="viewer-a")
    b = Mef3Client(f"localhost:{port}")  # a random id
    try:
        assert b.client_id != a.client_id and len(b.client_id) == 32
        for _ in range(3):
            assert a.open_file(mef3_file)["file_opened"]
        b.open_file(mef3_file)
        a.close_file(mef3_file)
        assert mef3_file in b.list_open_files()  # b's session keeps it open
        info = b.get_file_info(mef3_file)
        out = b.get_signal_range(mef3_file, info["channel_names"][:1], info["start_uutc"],
                                 info["start_uutc"] + 1_000_000)
        assert out["array"].shape[0] == 1
        b.close_file(mef3_file)
        assert mef3_file not in b.list_open_files()
    finally:
        a.shutdown()
        b.shutdown()


@pytest.mark.parametrize("bad", ["", "x" * 129, "tab\tid"])
def test_a_malformed_client_id_is_refused_by_the_client(bad):
    with pytest.raises(ValueError):
        Mef3Client("localhost:1", client_id=bad)


def test_a_session_used_after_it_was_chosen_to_expire_is_kept(mef3_file):
    """Expiry chooses idle sessions under the lock and closes them after it.
    A re-open in between (here, forced into that gap) must keep the session
    and its file open."""
    import time
    fm = FileManager(use_process_pool=False, session_ttl_s=60)
    try:
        fm.open_file(mef3_file, client_id="tab")
        fm._sessions[("tab", mef3_file)]["last_used"] -= 120  # idle for 2 min
        stale_now = time.monotonic()
        with fm._lock:  # expiry chooses it ...
            seen = fm._sessions[("tab", mef3_file)]["last_used"]
        fm.open_file(mef3_file, client_id="tab")  # ... the client comes back ...
        fm._close_session("tab", mef3_file, only_if_used_at=seen)  # ... then the close
        assert fm.session_count() == 1 and mef3_file in fm.list_open_files()
        assert fm.expire_sessions(now=stale_now) == 0
    finally:
        fm.shutdown()


def test_the_cap_never_closes_a_shared_file(mef3_file):
    fm = FileManager(use_process_pool=False, max_sessions=1)
    try:
        fm.open_file(mef3_file, client_id="old")
        fm.open_file(mef3_file, client_id="new")  # evicts "old", same file
        assert fm.session_count() == 1 and mef3_file in fm.list_open_files()
    finally:
        fm.shutdown()


@pytest.mark.parametrize("value, expected", [
    ("viewer-1", "viewer-1"), ("", ""), ("x" * 129, ""), ("tab\tid", ""), (None, "")])
def test_the_server_reads_the_client_id_safely(value, expected):
    from mef3io_server.server.mef3_server import CLIENT_ID_KEY, client_id_of

    class Ctx:
        def invocation_metadata(self):
            return () if value is None else ((CLIENT_ID_KEY, value),)

    assert client_id_of(Ctx()) == expected
