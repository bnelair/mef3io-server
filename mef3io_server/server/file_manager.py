from concurrent import futures
import threading
import time
import numpy as np
import mef3io_server.protobufs.gRPCMef3Server_pb2 as gRPCMef3Server_pb2

from mef3io_server.server.tile_cache import TileCache, CACHE_DTYPE
from mef3io_server.server.reader_pool import ReaderProcessPool
from mef3io_server.server.reduce import minmax_lttb
from mef3io_server.server.tiles import read_tile, samples_per_tile
from mef3io import MefReader
from mef3io_server.server.log_manager import get_logger
import os

logger = get_logger("mef3io_server.file_manager")


def is_running_in_docker():
    """Detect if running inside a Docker container."""
    return os.path.exists('/.dockerenv')


def get_actual_file_path(file_path):
    """Map the user-supplied absolute file path to the correct on-disk path.
    If running in Docker, prepend /host_root to absolute paths.
    """
    if is_running_in_docker() and os.path.isabs(file_path):
        return '/host_root' + file_path
    return file_path


class FileManager:
    """
    Manages the state and operations for multiple MEF files in a thread-safe manner.

    Every data access is oriented purely in **channels and time**: clients read
    arbitrary channels over arbitrary ``[start_uutc, end_uutc)`` windows via
    :meth:`read_signal_range`, served from a shared per-channel tile cache with
    parallel decode in worker processes and configurable window prefetch.

    Thread safety is ensured via a lock for all state-changing operations. Each file is managed independently.

    **Sessions.** A session is one *client* using one *file*, keyed by
    ``(client_id, file_path)``. The client id is a name tag the client library
    sends with every call (not authentication). Opening the same file again from
    the same client refreshes its session instead of creating another, so a tool
    that re-opens on every page view still holds one. Everything heavy is shared
    across sessions: one reader per file, and the tile cache, which is keyed by
    content (file, channel, tile), never by client. A file's reader closes when
    its last session closes or expires.

    * ``client_id=""`` is the default session of clients that send no id (older
      clients): exactly the behaviour before sessions existed, never expired.
    * Reading needs the file open by anyone; a client that has its own session
      refreshes it on every read (and, from step 2 on, uses its own settings).
    * Client sessions idle longer than ``session_ttl_s`` expire; at most
      ``max_sessions`` exist (the least recently used is closed first).
    """

    def __init__(self, max_workers=4,
                 tile_duration_s=60, tile_cache_bytes=512 * 1024 * 1024,
                 use_process_pool=True, reader_processes=None, prefetch_processes=None,
                 min_parallel_tiles=2, prefetch_ahead_windows=1,
                 prefetch_behind_windows=1, cache_ttl_s=1800,
                 session_ttl_s=1800, max_sessions=1000, respect_mef_blocks=False):
        """
        Initialize the FileManager.

        Args:
            max_workers (int): Maximum number of background threads for the
                thread-based prefetch fallback (used when the process pool is off).
            tile_duration_s (float): Time-tile length in seconds for the timestamp-based
                (``read_signal_range``) access path.
            tile_cache_bytes (int): Global byte budget for the tile cache (float32 tiles).
            use_process_pool (bool): Decode cold reads / prefetch in worker
                processes for parallel MEF3 decode (one mef3io session per
                worker). When ``False``, falls back to the in-process thread path.
            reader_processes (int or None): Total decode worker processes. ``None``
                auto-selects ``cpu_count - 1``.
            prefetch_processes (int or None): Of the total, how many form the
                background prefetch lane. ``None`` uses half. The remainder
                (always >= 1) is the reserved foreground lane so background
                prefetch can never starve an interactive read.
            min_parallel_tiles (int): Only fan a cold read out to the process pool
                when at least this many tiles are missing; smaller reads stay
                in-process (IPC is not worth it).
            prefetch_ahead_windows (int): How many full windows *ahead* of the
                requested window to prefetch (paging forward).
            prefetch_behind_windows (int): How many full windows *behind* the
                requested window to prefetch (paging backward).
            respect_mef_blocks (bool): Round each channel's tile length to a whole
                number of its MEF blocks (to the nearest; at least one) and start
                tiles at its first block, so tile edges fall on block boundaries
                and no block is decoded for two tiles. The block length is read
                from the block index (mef3io's ``block_envelope``). After a gap,
                or with uneven blocks, edges can drift off block boundaries:
                reads stay exact, only those blocks are decoded twice.
            session_ttl_s (float or None): Close a client's session after this many
                idle seconds (sessions of clients that send no id never expire).
            max_sessions (int): Most client sessions at once; opening one more
                closes the least recently used.
            cache_ttl_s (float or None): Discard tiles not accessed within this many
                seconds. ``None``/``0`` disables idle expiry.
        """
        self._files = {}
        self._lock = threading.Lock()

        # --- Timestamp-based (tile) access configuration ---
        self.tile_duration_s = tile_duration_s
        self.tile_cache_bytes = tile_cache_bytes
        self.cache_ttl_s = float(cache_ttl_s) if cache_ttl_s and cache_ttl_s > 0 else None
        # A single tile cache shared across all open files, keyed by
        # (file_path, channel, served fs, block_index): by content, never by
        # client, so sessions with the same rate share tiles. One global ceiling.
        self._tile_cache = TileCache(max_bytes=tile_cache_bytes, ttl_seconds=self.cache_ttl_s)

        # --- Process-pool decode configuration (two disjoint lanes) ---
        self.use_process_pool = bool(use_process_pool)
        total = int(reader_processes) if reader_processes else max(1, (os.cpu_count() or 2) - 1)
        if prefetch_processes is not None:
            prefetch_w = max(1, int(prefetch_processes))
        else:
            prefetch_w = max(1, total // 2)
        self._foreground_workers = max(1, total - prefetch_w)
        self._prefetch_workers = prefetch_w
        self.min_parallel_tiles = max(1, int(min_parallel_tiles))
        self.prefetch_ahead_windows = max(0, int(prefetch_ahead_windows))
        self.prefetch_behind_windows = max(0, int(prefetch_behind_windows))
        # Lazily created so constructing a FileManager never spawns processes; a
        # small read below min_parallel_tiles also never needs the pool.
        self._fg_pool = None
        self._bg_pool = None
        self._pool_lock = threading.Lock()

        # --- Thread pool: prefetch fallback when the process pool is off ---
        self._prefetch_executor = futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='cache_prefetch'
        )
        # Track in-progress tile prefetches: {file_path: set((channel, block_index))}
        self._tile_in_progress = {}

        # --- Sessions: (client_id, file_path) -> {'last_used': monotonic s} ---
        self.respect_mef_blocks = bool(respect_mef_blocks)
        self.session_ttl_s = float(session_ttl_s) if session_ttl_s and session_ttl_s > 0 else None
        self.max_sessions = max(1, int(max_sessions))
        self._sessions = {}
        # Settings of client sessions closed by expiry or the cap (not by the
        # client): a later read by that client restores its session with them,
        # instead of silently serving the original rate. Bounded like sessions.
        self._closed_settings = {}

        # --- Background TTL sweeper: frees idle tiles even with no traffic ---
        self._stop_sweeper = threading.Event()
        self._sweeper = None
        ttls = [t for t in (self.cache_ttl_s, self.session_ttl_s) if t is not None]
        if ttls:
            interval = max(1.0, min(ttls) / 10.0)
            self._sweeper = threading.Thread(
                target=self._sweeper_loop, args=(interval,),
                name='tile_cache_sweeper', daemon=True,
            )
            self._sweeper.start()

    # ------------------------------------------------------------------
    # Process-pool lanes + background TTL sweeper
    # ------------------------------------------------------------------
    def _get_fg_pool(self):
        """Foreground decode lane (reserved for interactive/cold reads)."""
        if not self.use_process_pool:
            return None
        if self._fg_pool is None:
            with self._pool_lock:
                if self._fg_pool is None:
                    self._fg_pool = ReaderProcessPool(max_workers=self._foreground_workers)
        return self._fg_pool

    def _get_bg_pool(self):
        """Background decode lane (prefetch only; never blocks foreground)."""
        if not self.use_process_pool:
            return None
        if self._bg_pool is None:
            with self._pool_lock:
                if self._bg_pool is None:
                    self._bg_pool = ReaderProcessPool(max_workers=self._prefetch_workers)
        return self._bg_pool

    def _sweeper_loop(self, interval):
        """Periodically discard tiles that have gone idle past the TTL."""
        while not self._stop_sweeper.wait(interval):
            try:
                if self.cache_ttl_s is not None:
                    n = self._tile_cache.evict_expired()
                    if n:
                        logger.debug(f"TTL sweep evicted {n} idle tiles")
                n = self.expire_sessions()
                if n:
                    logger.info(f"Closed {n} idle client session(s)")
            except Exception as e:  # noqa: BLE001 - sweeper must never die
                logger.error(f"Tile cache sweeper error: {e}")

    def open_file(self, file_path, client_id="", cache_fs=None, allow_mixed_rates=False):
        """Opens a MEF file for one client and returns its info.

        Args:
            file_path (str): Path to the MEF file.
            client_id (str): The calling client's id; ``""`` for clients that send
                none (the behaviour before sessions existed).
            cache_fs (float or None): Serve this session's reads at this rate
                (order-preserving min-max of the original), cached and prefetched
                at it. ``None``: each channel's own rate. A re-open replaces the
                session's settings.
            allow_mixed_rates (bool): With ``cache_fs``: a channel recorded below
                it is served at its own rate. Without it, such a channel makes
                the open fail, saying which channels and why.

        Returns:
            FileInfoResponse: Protobuf response with file info and open status.
        """
        actual_path = get_actual_file_path(file_path)
        if not os.path.exists(actual_path):
            logger.warning(f"Attempted to open non-existent file: {file_path}")
            return gRPCMef3Server_pb2.FileInfoResponse(
                file_path=file_path,
                file_opened=False,
                error_message=f"File does not exist: {file_path}"
            )
        with self._lock:
            already_open = file_path in self._files
            if not already_open:
                try:
                    rdr = MefReader(actual_path)
                    self._files[file_path] = {
                        'reader': rdr,
                        # Docker-mapped on-disk path; handed to worker processes so
                        # they can open their own MefReader for parallel decode.
                        'actual_path': actual_path,
                        # Per-channel tile metadata (tiles themselves live in the
                        # shared self._tile_cache)
                        # (channel, served fs) -> (fs, start, samples_per_tile, native fs)
                        'tile_meta': {},
                        'native_fs': {},  # channel -> its own sampling rate
                    }
                    logger.info(f"Opened file: {file_path}")
                except Exception as e:
                    logger.error(f"Error opening file {file_path}: {e}")
                    return gRPCMef3Server_pb2.FileInfoResponse(
                        file_path=str(file_path),
                        file_opened=False,
                        error_message=str(e)
                    )
            if cache_fs is not None and not client_id:
                refusal = ("cache_fs needs a client id: clients that send none share one "
                           "default session per file, so one would change the rate of "
                           "all (Mef3Client sends an id)")
            else:
                try:
                    refusal = self._check_rates_unsafe(file_path, cache_fs, allow_mixed_rates)
                except Exception as e:  # noqa: BLE001 - never leave an orphan reader
                    refusal = f"Cannot read the channels' sampling rates: {e}"
            if refusal is not None:
                if not already_open:  # opened for this call only: close it again
                    self._discard_reader(file_path, self._files.pop(file_path, None))
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path, file_opened=False, error_message=refusal)
            evicted = self._open_session_unsafe(client_id, file_path)
            self._sessions[(client_id, file_path)]['settings'] = (
                {'cache_fs': float(cache_fs), 'allow_mixed_rates': bool(allow_mixed_rates)}
                if cache_fs else {})
            self._closed_settings.pop((client_id, file_path), None)
            info = self._get_file_info_unsafe(file_path, client_id)
            if already_open and not client_id:
                # Unchanged for clients that send no id: a second open says so.
                info.error_message = f"File already open: {file_path}"
        for key, seen in evicted:
            self._close_session(*key, only_if_used_at=seen)
        return info

    def _check_rates_unsafe(self, file_path, cache_fs, allow_mixed_rates):
        """Why ``cache_fs`` cannot be served for this file, or ``None``.

        The server never upsamples: a channel recorded below ``cache_fs`` is
        refused, unless ``allow_mixed_rates`` serves it at its own rate.
        Assumes ``self._lock`` is held.
        """
        if cache_fs is None:
            return None
        if not cache_fs > 0 or cache_fs != cache_fs or cache_fs == float('inf'):
            return f"cache_fs must be a positive sampling rate (Hz), not {cache_fs}"
        if allow_mixed_rates:
            return None
        rdr = self._files[file_path]['reader']
        below = [(ch, float(fs)) for ch, fs in zip(rdr.channels, rdr.get_property('fsamp'))
                 if float(fs) < cache_fs]
        if not below:
            return None
        listed = ", ".join(f"{ch} ({fs:g} Hz)" for ch, fs in below[:10])
        more = f" and {len(below) - 10} more" if len(below) > 10 else ""
        return (f"cache_fs {cache_fs:g} Hz is above the recorded rate of {len(below)} "
                f"channel(s): {listed}{more}. The server does not upsample: choose a "
                "lower cache_fs, or pass allow_mixed_rates to serve those channels at "
                "their own rate.")

    def _discard_reader(self, file_path, state):
        """Close a reader that is no longer tracked (best effort)."""
        if state is None:
            return
        try:
            state['reader'].close()
        except Exception as e:  # noqa: BLE001 - nothing else to do
            logger.error(f"Error closing reader for {file_path}: {e}")

    # ------------------------------------------------------------------
    # Sessions: (client_id, file_path)
    # ------------------------------------------------------------------
    def _open_session_unsafe(self, client_id, file_path):
        """Create or refresh a session; return the sessions to close for the cap.

        Assumes ``self._lock`` is held. The evicted sessions are closed by the
        caller after releasing the lock (closing a file may close its reader).
        """
        key = (client_id, file_path)
        session = self._sessions.get(key)
        if session is None:
            session = self._sessions[key] = {}
            if client_id:
                logger.info(f"New session for a client on {file_path}")
        session['last_used'] = time.monotonic()
        clients = [k for k in self._sessions if k[0]]
        excess = len(clients) - self.max_sessions
        if excess <= 0:
            return []
        clients.sort(key=lambda k: self._sessions[k]['last_used'])
        return [(k, self._sessions[k]['last_used']) for k in clients if k != key][:excess]

    def _touch_session_unsafe(self, client_id, file_path):
        session = self._sessions.get((client_id, file_path))
        if session is not None:
            session['last_used'] = time.monotonic()
        return session

    def _close_session(self, client_id, file_path, only_if_used_at=None):
        """Remove one session; close the file's reader when it was the last.

        ``only_if_used_at``: the session's ``last_used`` when it was chosen to
        close (expiry, the cap). Chosen under the lock but closed after it was
        released, a session can be re-opened or read in between; it is then
        left alone, rather than closing a session (and file) just used.
        """
        with self._lock:
            session = self._sessions.get((client_id, file_path))
            if session is None:
                return None
            if only_if_used_at is not None and session['last_used'] != only_if_used_at:
                return None
            self._sessions.pop((client_id, file_path), None)
            if only_if_used_at is not None and session.get('settings'):
                # Closed for it (idle, or the cap), not by it: remember why it read
                # at its rate, so its next read is served the same way.
                self._closed_settings[(client_id, file_path)] = session['settings']
                while len(self._closed_settings) > self.max_sessions:
                    self._closed_settings.pop(next(iter(self._closed_settings)))
            if any(k[1] == file_path for k in self._sessions):
                return None
        return self._release_file(file_path)

    def expire_sessions(self, now=None):
        """Close client sessions idle longer than ``session_ttl_s``; return how many."""
        if self.session_ttl_s is None:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            idle = [(k, v['last_used']) for k, v in self._sessions.items()
                    if k[0] and now - v['last_used'] > self.session_ttl_s]
        closed = 0
        for key, seen in idle:
            with self._lock:
                still_idle = (key in self._sessions
                              and self._sessions[key]['last_used'] == seen)
            if still_idle:
                self._close_session(*key, only_if_used_at=seen)
                closed += 1
        return closed

    def session_count(self):
        """How many sessions exist (clients' and the default ones)."""
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------------
    # Timestamp-based access (tile cache): read any channels over any [t1, t2]
    # ------------------------------------------------------------------
    def _get_tile_meta_unsafe(self, state, channel, served_fs=None):
        """Return ``(fs, channel_start_uutc, samples_per_tile, native_fs)``.

        ``fs`` is the rate the tiles are served at: ``served_fs`` when given and
        below the channel's own rate (downsampled tiles), else the channel's own.
        Cached on the file state per (channel, rate). Assumes ``self._lock`` is
        held.
        """
        native = state['native_fs'].get(channel)
        if native is None:
            native = state['native_fs'][channel] = float(
                state['reader'].get_property('fsamp', channel))
        fs = served_fs if served_fs and served_fs < native else native
        meta = state['tile_meta'].get((channel, fs))
        if meta is None:
            cs = int(state['reader'].get_property('start_time', channel))
            meta = (fs, cs, samples_per_tile(self._tile_duration_unsafe(state, channel, cs),
                                             fs, native), native)
            state['tile_meta'][(channel, fs)] = meta
        return meta

    def _tile_duration_unsafe(self, state, channel, channel_start):
        """The tile length for a channel: ``tile_duration_s``, or with
        ``respect_mef_blocks`` the nearest whole number of its MEF blocks."""
        if not self.respect_mef_blocks:
            return self.tile_duration_s
        block_s = state.setdefault('block_s', {}).get(channel)
        if block_s is None:
            block_s = 0.0
            try:
                # The first blocks are enough: one index read, not the channel's.
                env = state['reader'].block_envelope(channel, channel_start,
                                                     channel_start + 3_600_000_000)
                lengths = (env['end_uutc'] - env['start_uutc'])[:100]
                if len(lengths):
                    block_s = float(np.median(lengths)) / 1e6
            except Exception as e:  # noqa: BLE001 - fall back to the plain length
                logger.warning(f"Cannot read the block length of {channel}: {e}")
            state['block_s'][channel] = block_s
        if block_s <= 0:
            return self.tile_duration_s
        return max(1, int(round(self.tile_duration_s / block_s))) * block_s

    def _read_tile_from_disk(self, rdr, channel, block_index, meta):
        """Read one tile for a channel (see ``tiles.read_tile``)."""
        fs, cs, S, native = meta
        return read_tile(
            rdr, channel, block_index, fs, cs, S, native,
            on_miss=lambda e: logger.debug(
                f"Tile read miss ({channel}, {block_index}) for range: {e}"))

    def _prefetch_tile(self, file_path, channel, block_index, meta):
        """Background worker: load one tile into the cache if not present/in-flight."""
        if block_index < 0:
            return
        fs = meta[0]
        cache_key = (file_path, channel, fs, block_index)
        with self._lock:
            if file_path not in self._files:
                return
            state = self._files[file_path]
            if self._tile_cache.contains(cache_key):
                return
            in_progress = self._tile_in_progress.setdefault(file_path, set())
            key = (channel, fs, block_index)
            if key in in_progress:
                return
            in_progress.add(key)
            rdr = state['reader']
        try:
            tile = self._read_tile_from_disk(rdr, channel, block_index, meta)
            with self._lock:
                still_open = file_path in self._files
            if still_open:
                self._tile_cache.put(cache_key, tile)
                logger.debug(f"Tile PREFETCHED: ({channel}, {block_index}) for {file_path}")
        except Exception as e:
            logger.error(f"Error prefetching tile ({channel}, {block_index}) for {file_path}: {e}")
        finally:
            with self._lock:
                s = self._tile_in_progress.get(file_path)
                if s is not None:
                    s.discard(key)

    def read_signal_range(self, file_path, channels, start_uutc, end_uutc, prefetch=True,
                          client_id="", n_points=None):
        """Read channels over ``[start_uutc, end_uutc)``; see :meth:`_read_level`.

        ``n_points`` (optional): return exactly that many points per channel,
        for example one per pixel, chosen by MinMaxLTTB (``reduce.minmax_lttb``):
        each point is a real sample, its bucket's minimum or maximum, picked to
        keep the line's shape, and each channel's overall max and min are
        always included. The source is the cheapest that still has
        detail to spare: the session's cached rate when it holds at least
        ``2 * n_points`` samples in the window, else the original rate (zoomed
        in). When the original holds ``n_points`` samples or fewer it is
        returned unchanged: ``n_points`` never upsamples. The points are evenly
        spaced, so ``fs`` in the result is ``n_points / window``.
        """
        if n_points is None:
            return self._read_level(file_path, channels, start_uutc, end_uutc, prefetch,
                                    client_id)
        n_points = int(n_points)
        if n_points <= 0:
            raise ValueError("n_points must be positive")
        if end_uutc <= start_uutc:
            raise ValueError("end_uutc must be greater than start_uutc")
        window_s = (end_uutc - start_uutc) / 1e6
        with self._lock:
            if file_path not in self._files:
                raise ValueError(f"File not open: {file_path}")
            state = self._files[file_path]
            names = list(channels) if channels else list(state['reader'].channels)
            session = self._sessions.get((client_id, file_path)) or {}
            cache_fs = session.get('settings', {}).get('cache_fs')
            try:
                served = min(self._get_tile_meta_unsafe(state, ch, cache_fs)[0] for ch in names)
            except Exception:  # noqa: BLE001 - unknown channels: _read_level says which
                served = None
        native = False
        if served is not None and round(window_s * served) < 2 * n_points:
            native = True  # zoomed in: the cached rate has too little detail
        result = self._read_level(file_path, channels, start_uutc, end_uutc, prefetch,
                                  client_id, native=native)
        data = result['array']
        if data.shape[1] <= n_points:
            return result  # never upsample
        result['array'] = minmax_lttb(data, n_points).astype(CACHE_DTYPE)
        result['fs'] = n_points / window_s
        return result

    def _read_level(self, file_path, channels, start_uutc, end_uutc, prefetch=True,
                    client_id="", native=False):
        """Read arbitrary channels over an arbitrary ``[start_uutc, end_uutc)`` window.

        Data is served from the per-channel tile cache, reading only the tiles that
        are missing and slicing the result sample-exact to the requested window.
        Neighboring tiles are prefetched in the background for smooth navigation.

        Args:
            file_path (str): Path to an open MEF file.
            channels (list[str] or None): Channels to read; ``None``/empty means
                all channels in the file.
            start_uutc (int): Inclusive window start in microseconds (uUTC).
            end_uutc (int): Exclusive window end in microseconds (uUTC).
            prefetch (bool): Whether to prefetch neighboring tiles.
            client_id (str): The calling client; its session (if it opened the
                file) is kept alive by the read. The file only has to be open
                by someone: tools may share a file one of them opened.

        Returns:
            dict: A dict with keys ``array`` (``np.ndarray`` of shape
            ``(n_channels, n_samples)``, ``float32``), ``channel_names``, ``fs``,
            ``start_uutc`` and ``end_uutc``.

        Raises:
            ValueError: If the file is not open, channels are invalid, the requested
                channels do not share a sampling rate, or the window is empty.
        """
        with self._lock:
            if file_path not in self._files:
                raise ValueError(f"File not open: {file_path}")
            if self._touch_session_unsafe(client_id, file_path) is None and client_id:
                kept = self._closed_settings.pop((client_id, file_path), None)
                if kept is not None:  # expired or evicted, the file still open: restore
                    self._sessions[(client_id, file_path)] = {
                        'last_used': time.monotonic(), 'settings': kept}
            state = self._files[file_path]
            rdr = state['reader']
            actual_path = state['actual_path']
            cache = self._tile_cache
            all_channels = list(rdr.channels)
            if not channels:
                channels = all_channels
            channels = list(channels)
            invalid = [c for c in channels if c not in all_channels]
            if invalid:
                raise ValueError(f"Unknown channels: {invalid}")
            session = self._sessions.get((client_id, file_path)) or {}
            cache_fs = None if native else session.get('settings', {}).get('cache_fs')
            metas = {ch: self._get_tile_meta_unsafe(state, ch, cache_fs) for ch in channels}

        if end_uutc <= start_uutc:
            raise ValueError("end_uutc must be greater than start_uutc")
        fs_values = {round(metas[ch][0], 6) for ch in channels}
        if len(fs_values) > 1:
            raise ValueError(f"Requested channels do not share a sampling rate: {fs_values}")
        fs = metas[channels[0]][0]
        n = int(round((end_uutc - start_uutc) * fs / 1e6))
        if n <= 0:
            raise ValueError("Requested window is shorter than one sample")

        # Per-channel block span covering the requested window.
        spans = {}
        for ch in channels:
            _, cs, S, _native = metas[ch]
            i1 = int(round((start_uutc - cs) * fs / 1e6))
            b1 = i1 // S
            b2 = (i1 + n - 1) // S
            spans[ch] = (b1, b2, i1)

        # Decode+cache the missing tiles, fanning out across the foreground
        # process pool when there is enough work to amortize the IPC.
        self._ensure_tiles_cached(file_path, actual_path, metas, spans)

        # Assemble sample-exact rows from cached tiles. Any residual miss (below
        # the parallel threshold, or a negative pre-start block) is read in-process.
        rows = []
        for ch in channels:
            b1, b2, i1 = spans[ch]
            _, cs, S, _native = metas[ch]
            parts = []
            for b in range(b1, b2 + 1):
                tile = cache.get((file_path, ch, fs, b)) if b >= 0 else None
                if tile is None:
                    tile = self._read_tile_from_disk(rdr, ch, b, metas[ch])
                    if b >= 0:
                        cache.put((file_path, ch, fs, b), tile)
                parts.append(tile)
            concat = parts[0] if len(parts) == 1 else np.concatenate(parts)
            offset = i1 - b1 * S
            row = concat[offset:offset + n]
            if row.shape[0] < n:  # window runs past end-of-file
                row = np.concatenate([row, np.full(n - row.shape[0], np.nan, dtype=CACHE_DTYPE)])
            rows.append(row.astype(CACHE_DTYPE, copy=False))

        data = np.vstack(rows) if len(rows) > 1 else rows[0][np.newaxis, :]

        if prefetch:
            self._schedule_window_prefetch(
                file_path, actual_path, channels, metas, start_uutc, end_uutc, fs
            )

        return {
            'array': data,
            'channel_names': channels,
            'fs': fs,
            'start_uutc': int(start_uutc),
            'end_uutc': int(end_uutc),
        }

    def _ensure_tiles_cached(self, file_path, actual_path, metas, spans):
        """Decode and cache tiles missing for ``spans`` in parallel (one task/channel).

        No-op when the process pool is disabled or when too few tiles are missing
        to be worth the IPC -- in both cases the assembly loop reads them in-process.
        """
        if not self.use_process_pool:
            return
        cache = self._tile_cache
        jobs = {}  # channel -> list of missing (>=0) block indices
        total_missing = 0
        for ch, (b1, b2, _) in spans.items():
            fs = metas[ch][0]
            missing = [b for b in range(max(0, b1), b2 + 1)
                       if not cache.contains((file_path, ch, fs, b))]
            if missing:
                jobs[ch] = missing
                total_missing += len(missing)
        if not jobs or total_missing < self.min_parallel_tiles:
            return
        pool = self._get_fg_pool()
        if pool is None:
            return
        try:
            futs = []
            for ch, blocks in jobs.items():
                fs, cs, S, native = metas[ch]
                futs.append((ch, pool.submit_read_tiles(actual_path, ch, blocks, fs, cs, S,
                                                        native)))
            for ch, fut in futs:
                try:
                    tiles = fut.result()
                except Exception as e:  # noqa: BLE001 - fall back to in-process assembly
                    logger.error(f"Parallel tile read failed for {ch} in {file_path}: {e}")
                    continue
                for b, tile in tiles.items():
                    cache.put((file_path, ch, metas[ch][0], b), tile)
        except Exception as e:  # noqa: BLE001 - e.g. BrokenProcessPool on submit
            # The pool is unusable (e.g. a worker died). Don't fail the read: the
            # assembly loop reads any still-missing tiles in-process.
            logger.error(f"Foreground decode pool failed for {file_path}: {e}")

    def _schedule_window_prefetch(self, file_path, actual_path, channels, metas,
                                  start_uutc, end_uutc, fs):
        """Prefetch tiles covering N windows ahead and M windows behind.

        With window span ``W = end - start``, look-ahead covers
        ``[end, end + ahead*W)`` and look-behind ``[start - behind*W, start)`` for
        each requested channel -- warming the next/previous 'page' so paging
        forward and backward is a cache hit. Runs on the background lane.
        """
        W = end_uutc - start_uutc
        if W <= 0:
            return
        ranges = []
        if self.prefetch_ahead_windows > 0:
            ranges.append((end_uutc, end_uutc + self.prefetch_ahead_windows * W))
        if self.prefetch_behind_windows > 0:
            ranges.append((start_uutc - self.prefetch_behind_windows * W, start_uutc))
        if not ranges:
            return
        for ch in channels:
            _, cs, S, _native = metas[ch]
            blocks = set()
            for ta, tb in ranges:
                if tb <= ta:
                    continue
                ia = int(round((ta - cs) * fs / 1e6))
                ib = int(round((tb - cs) * fs / 1e6))
                ba = ia // S
                bb = (ib - 1) // S
                for b in range(max(0, ba), bb + 1):
                    blocks.add(b)
            for b in sorted(blocks):
                self._schedule_prefetch_tile(file_path, actual_path, ch, b, metas[ch])

    def _schedule_prefetch_tile(self, file_path, actual_path, channel, block_index, meta):
        """Dispatch a single-tile prefetch to the background lane (or thread fallback)."""
        if block_index < 0:
            return
        pool = self._get_bg_pool()
        if pool is None:
            # Thread fallback: in-process read overlapping client think-time.
            self._prefetch_executor.submit(self._prefetch_tile, file_path, channel, block_index,
                                           meta)
            return
        fs, cs, S, native = meta
        cache_key = (file_path, channel, fs, block_index)
        key = (channel, fs, block_index)
        with self._lock:
            if file_path not in self._files:
                return
            if self._tile_cache.contains(cache_key):
                return
            in_progress = self._tile_in_progress.setdefault(file_path, set())
            if key in in_progress:
                return
            in_progress.add(key)
        try:
            fut = pool.submit_read_tiles(actual_path, channel, [block_index], fs, cs, S, native)
        except Exception as e:  # noqa: BLE001 - e.g. BrokenProcessPool; prefetch is best-effort
            logger.error(f"Prefetch submit failed ({channel}, {block_index}) for {file_path}: {e}")
            with self._lock:
                s = self._tile_in_progress.get(file_path)
                if s is not None:
                    s.discard(key)
            return
        fut.add_done_callback(
            lambda f, fp=file_path, ch=channel, r=fs, b=block_index:
                self._on_prefetch_done(f, fp, ch, r, b)
        )

    def _on_prefetch_done(self, fut, file_path, channel, fs, block_index):
        """Insert a prefetched tile into the cache and clear its in-flight marker."""
        try:
            tiles = fut.result()
        except Exception as e:  # noqa: BLE001 - a failed prefetch is non-fatal
            logger.error(f"Error prefetching tile ({channel}, {block_index}) for {file_path}: {e}")
            tiles = None
        try:
            if tiles:
                with self._lock:
                    still_open = file_path in self._files
                if still_open:
                    for b, tile in tiles.items():
                        self._tile_cache.put((file_path, channel, fs, b), tile)
                    logger.debug(f"Tile PREFETCHED: ({channel}, {block_index}) for {file_path}")
        finally:
            with self._lock:
                s = self._tile_in_progress.get(file_path)
                if s is not None:
                    s.discard((channel, fs, block_index))

    def read_envelope(self, file_path, channels=None, start_uutc=None, end_uutc=None,
                      client_id=""):
        """Per-block min/max of channels, from the MEF block index alone.

        Nothing is decoded and nothing is cached (the index is small, and mef3io
        keeps it in memory). Yields ``(channel, dict)`` with NumPy arrays
        ``start_uutc``, ``end_uutc``, ``min``, ``max`` (see
        ``mef3io.Reader.block_envelope``). The file must be open by someone;
        the caller's session, if any, is kept alive.
        """
        with self._lock:
            if file_path not in self._files:
                raise ValueError(f"File not open: {file_path}")
            self._touch_session_unsafe(client_id, file_path)
            rdr = self._files[file_path]['reader']
            all_channels = list(rdr.channels)
        channels = list(channels) if channels else all_channels
        invalid = [c for c in channels if c not in all_channels]
        if invalid:
            raise ValueError(f"Unknown channels: {invalid}")
        for ch in channels:
            # Outside the lock: mef3io readers take overlapping calls.
            try:
                env = rdr.block_envelope(ch, start_uutc, end_uutc)
            except AttributeError:  # the compat reader was closed meanwhile
                raise ValueError(f"File closed during the request: {file_path}") from None
            yield ch, env

    #: Most envelope rows in one message (32 B a row: ~3.2 MB, under gRPC's
    #: default 4 MB receive limit). A long recording in 1-s blocks has far
    #: more rows per channel, so a channel is sent in several messages.
    ENVELOPE_CHUNK_ROWS = 100_000

    def stream_envelope(self, file_path, channel_names, start_uutc=None, end_uutc=None,
                        client_id=""):
        """Yield ``ChannelEnvelope`` messages, each channel in chunks of at most
        ``ENVELOPE_CHUNK_ROWS`` rows (in order); an error as a single message."""
        step = max(1, int(self.ENVELOPE_CHUNK_ROWS))
        try:
            for ch, env in self.read_envelope(file_path, list(channel_names), start_uutc,
                                              end_uutc, client_id):
                rows = len(env['start_uutc'])
                for a in range(0, max(rows, 1), step):
                    b = a + step
                    yield gRPCMef3Server_pb2.ChannelEnvelope(
                        file_path=file_path, channel_name=ch,
                        start_uutc=env['start_uutc'][a:b].tolist(),
                        end_uutc=env['end_uutc'][a:b].tolist(),
                        min=env['min'][a:b].tolist(), max=env['max'][a:b].tolist())
        except Exception as e:
            logger.error(f"Error in read_envelope for {file_path}: {e}")
            yield gRPCMef3Server_pb2.ChannelEnvelope(file_path=file_path, error_message=str(e))

    def stream_signal_range(self, file_path, channel_names, start_uutc, end_uutc, client_id="",
                            n_points=None):
        """Yield a ``[start_uutc, end_uutc)`` read as ~2.5MB streamed SignalChunks.

        Wraps :meth:`read_signal_range` for the gRPC ``GetSignalRange`` RPC. On any
        error a single SignalChunk carrying ``error_message`` is yielded.
        """
        try:
            result = self.read_signal_range(file_path, list(channel_names), start_uutc, end_uutc,
                                            client_id=client_id, n_points=n_points)
        except Exception as e:
            logger.error(f"Error in read_signal_range for {file_path}: {e}")
            yield gRPCMef3Server_pb2.SignalChunk(file_path=file_path, error_message=str(e))
            return

        data = result['array']
        channels = result['channel_names']
        fs = result['fs']
        num_channels = data.shape[0]
        total_samples = data.shape[1]
        dtype_size = np.dtype(CACHE_DTYPE).itemsize
        max_bytes = int(2.5 * 1024 * 1024)
        samples_per_chunk = max(int(max_bytes / (num_channels * dtype_size)), 1)
        span = end_uutc - start_uutc

        for s in range(0, total_samples, samples_per_chunk):
            e = min(s + samples_per_chunk, total_samples)
            tile = np.ascontiguousarray(data[:, s:e])
            tile_start = int(start_uutc + (s / total_samples) * span)
            tile_end = int(start_uutc + (e / total_samples) * span)
            yield gRPCMef3Server_pb2.SignalChunk(
                file_path=file_path,
                array_bytes=tile.tobytes(),
                dtype='float32',
                shape=list(tile.shape),
                start_uutc=tile_start,
                end_uutc=tile_end,
                fs=fs,
                channel_names=channels,
                error_message="",
            )

    # --- Graceful shutdown of all background resources ---
    def shutdown(self):
        """Shuts down the prefetch thread pool, process pools, TTL sweeper, and
        closes any still-open mef3io reader sessions."""
        logger.info("Shutting down FileManager background resources...")
        self._stop_sweeper.set()
        # Wait for in-flight prefetch threads: they hold reader references, and
        # the sessions are closed right below.
        self._prefetch_executor.shutdown(wait=True, cancel_futures=True)
        for pool in (self._fg_pool, self._bg_pool):
            if pool is not None:
                pool.shutdown()
        with self._lock:
            for state in self._files.values():
                try:
                    state['reader'].close()
                except Exception as e:  # noqa: BLE001 - shutdown must not fail
                    logger.error(f"Error closing reader during shutdown: {e}")
            self._files.clear()
            self._sessions.clear()

    def _get_file_info_unsafe(self, file_path, client_id=""):
        """Internal helper to get file info. Assumes lock is already held.

        Args:
            file_path (str): Path to the MEF file.

        Returns:
            FileInfoResponse: Protobuf response with file info and open status.
        """
        if file_path not in self._files:
            return gRPCMef3Server_pb2.FileInfoResponse(
                file_path=file_path,
                file_opened=False
            )

        state = self._files[file_path]
        rdr = state['reader']
        fs = rdr.get_property('fsamp')
        session = self._sessions.get((client_id, file_path)) or {}
        cache_fs = session.get('settings', {}).get('cache_fs')
        ch_names = rdr.channels
        nch = len(ch_names)
        # Per-channel start/end timestamps -- parallel to channel_names.
        ch_starts = [int(s) for s in rdr.get_property('start_time')]
        ch_ends = [int(e) for e in rdr.get_property('end_time')]
        start_uutc = min(ch_starts)
        end_uutc = max(ch_ends)
        duration_s = (end_uutc - start_uutc) / 1e6

        return gRPCMef3Server_pb2.FileInfoResponse(
            file_path=file_path,
            file_opened=True,
            number_of_channels=nch,
            channel_names=ch_names,
            channel_sampling_rates=fs,
            start_uutc=start_uutc,
            end_uutc=end_uutc,
            duration_s=duration_s,
            channel_start_uutc=ch_starts,
            channel_end_uutc=ch_ends,
            channel_served_sampling_rates=[
                cache_fs if cache_fs and cache_fs < float(f) else float(f) for f in fs],
            **({'cache_fs': cache_fs} if cache_fs else {}),
        )

    def close_file(self, file_path, client_id=""):
        """Close one client's session on a file.

        The file's reader (and its cached tiles) are released only when no
        other session uses the file. For clients that send no id this is the
        behaviour before sessions existed.

        Args:
            file_path (str): Path to the MEF file.
            client_id (str): The calling client's id (``""``: none sent).

        Returns:
            FileInfoResponse: ``file_opened`` says whether the file is still open
            (for other sessions).
        """
        with self._lock:
            self._sessions.pop((client_id, file_path), None)
            self._closed_settings.pop((client_id, file_path), None)  # ended by the client
            others = any(k[1] == file_path for k in self._sessions)
            if others:
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path, file_opened=True, error_message="")
        return self._release_file(file_path)

    def _release_file(self, file_path):
        """Close a file's reader and drop its tiles (its last session is gone).

        Args:
            file_path (str): Path to the MEF file.

        Returns:
            FileInfoResponse: Protobuf response indicating the file is closed.
        """
        with self._lock:
            # Re-checked under the lock: a session may have opened the file again
            # between its last session closing and this call.
            if any(k[1] == file_path for k in self._sessions):
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path, file_opened=True, error_message="")
            # Drop the file from tracked state first, then release its resources.
            # A reader.close() failure must not leave the file half-open in
            # self._files while we report it closed -- the file is gone either way.
            state = self._files.pop(file_path, None)
            if state is None:
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path,
                    file_opened=False,
                    error_message=""
                )
            # Clean up in-progress tile prefetches and purge this file's tiles
            # from the shared cache.
            self._tile_in_progress.pop(file_path, None)
            self._tile_cache.evict_matching(lambda k: k[0] == file_path)
            # mef3io sessions must be closed explicitly to release the underlying
            # file handles; a failure here is logged but does not un-close the file.
            error_message = ""
            try:
                state['reader'].close()
            except Exception as e:  # noqa: BLE001 - the file is already removed
                logger.error(f"Error closing reader for {file_path}: {e}")
                error_message = str(e)
            logger.info(f"Closed and removed file: {file_path}")
            return gRPCMef3Server_pb2.FileInfoResponse(
                file_path=file_path,
                file_opened=False,
                error_message=error_message
            )

    def get_file_info(self, file_path, client_id=""):
        """Gets information about an open MEF file.

        Args:
            file_path (str): Path to the MEF file.

        Returns:
            FileInfoResponse: Protobuf response with file info and open status.
        """
        with self._lock:
            try:
                return self._get_file_info_unsafe(file_path, client_id)
            except Exception as e:
                logger.error(f"Error getting file info for {file_path}: {e}")
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path,
                    file_opened=False,
                    error_message=str(e)
                )

    def list_open_files(self):
        """Lists all currently open MEF files.

        Returns:
            list: List of file paths for open files.
        """
        with self._lock:
            return list(self._files.keys())

