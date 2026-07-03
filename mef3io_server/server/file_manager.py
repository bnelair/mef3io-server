from concurrent import futures
import threading
import numpy as np
import mef3io_server.protobufs.gRPCMef3Server_pb2 as gRPCMef3Server_pb2

from mef3io_server.server.tile_cache import TileCache, CACHE_DTYPE
from mef3io_server.server.reader_pool import ReaderProcessPool
from mef_tools import MefReader
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
    """

    def __init__(self, max_workers=4,
                 tile_duration_s=60, tile_cache_bytes=512 * 1024 * 1024,
                 use_process_pool=True, reader_processes=None, prefetch_processes=None,
                 min_parallel_tiles=2, prefetch_ahead_windows=1,
                 prefetch_behind_windows=1, cache_ttl_s=1800):
        """
        Initialize the FileManager.

        Args:
            max_workers (int): Maximum number of background threads for the
                thread-based prefetch fallback (used when the process pool is off).
            tile_duration_s (float): Time-tile length in seconds for the timestamp-based
                (``read_signal_range``) access path.
            tile_cache_bytes (int): Global byte budget for the tile cache (float32 tiles).
            use_process_pool (bool): Decode cold reads / prefetch in worker
                processes for real parallel MEF3 decode (pymef is GIL-bound). When
                ``False``, falls back to the in-process thread path.
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
        # (file_path, channel, block_index), enforces one global memory ceiling.
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

        # --- Background TTL sweeper: frees idle tiles even with no traffic ---
        self._stop_sweeper = threading.Event()
        self._sweeper = None
        if self.cache_ttl_s is not None:
            interval = max(1.0, self.cache_ttl_s / 10.0)
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
                n = self._tile_cache.evict_expired()
                if n:
                    logger.debug(f"TTL sweep evicted {n} idle tiles")
            except Exception as e:  # noqa: BLE001 - sweeper must never die
                logger.error(f"Tile cache sweeper error: {e}")

    def open_file(self, file_path):
        """Opens a MEF file and initializes its state.

        Args:
            file_path (str): Path to the MEF file.

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
            if file_path in self._files:
                # File is already open, return info with error message
                info = self._get_file_info_unsafe(file_path)
                info.error_message = f"File already open: {file_path}"
                return info
            try:
                rdr = MefReader(actual_path)
                self._files[file_path] = {
                    'reader': rdr,
                    # Docker-mapped on-disk path; handed to worker processes so
                    # they can open their own MefReader for parallel decode.
                    'actual_path': actual_path,
                    # Per-channel tile metadata (tiles themselves live in the
                    # shared self._tile_cache)
                    'tile_meta': {},  # channel -> (fs, channel_start_uutc, samples_per_tile)
                }
                logger.info(f"Opened file: {file_path}")
            except Exception as e:
                logger.error(f"Error opening file {file_path}: {e}")
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=str(file_path),
                    file_opened=False,
                    error_message=str(e)
                )

            return self._get_file_info_unsafe(file_path)

    # ------------------------------------------------------------------
    # Timestamp-based access (tile cache): read any channels over any [t1, t2]
    # ------------------------------------------------------------------
    def _get_tile_meta_unsafe(self, state, channel):
        """Return (fs, channel_start_uutc, samples_per_tile) for a channel.

        Cached on the file state. Assumes ``self._lock`` is held.
        """
        meta = state['tile_meta'].get(channel)
        if meta is None:
            rdr = state['reader']
            fs = float(rdr.get_property('fsamp', channel))
            cs = int(rdr.get_property('start_time', channel))
            samples_per_tile = max(1, int(round(self.tile_duration_s * fs)))
            meta = (fs, cs, samples_per_tile)
            state['tile_meta'][channel] = meta
        return meta

    def _read_tile_from_disk(self, rdr, channel, block_index, fs, channel_start_uutc,
                             samples_per_tile):
        """Read one fixed-length tile for a channel, NaN-padding short/absent reads.

        The tile spans samples ``[block_index * S, (block_index + 1) * S)`` for this
        channel, addressed by converting those sample offsets back to uutc.
        """
        S = samples_per_tile
        t_start = channel_start_uutc + int(round(block_index * S * 1e6 / fs))
        t_end = channel_start_uutc + int(round((block_index + 1) * S * 1e6 / fs))
        try:
            raw = rdr.get_data([channel], t_start, t_end)
        except Exception as e:  # noqa: BLE001 - out-of-range reads should not crash
            logger.debug(f"Tile read miss ({channel}, {block_index}) for range: {e}")
            raw = None
        if raw is None:
            return np.full(S, np.nan, dtype=CACHE_DTYPE)
        arr = np.asarray(raw, dtype=np.float64).reshape(-1)
        if arr.shape[0] >= S:
            arr = arr[:S]
        elif arr.shape[0] < S:
            arr = np.concatenate([arr, np.full(S - arr.shape[0], np.nan)])
        return arr.astype(CACHE_DTYPE)

    def _prefetch_tile(self, file_path, channel, block_index):
        """Background worker: load one tile into the cache if not present/in-flight."""
        if block_index < 0:
            return
        cache_key = (file_path, channel, block_index)
        with self._lock:
            if file_path not in self._files:
                return
            state = self._files[file_path]
            if self._tile_cache.contains(cache_key):
                return
            in_progress = self._tile_in_progress.setdefault(file_path, set())
            key = (channel, block_index)
            if key in in_progress:
                return
            in_progress.add(key)
            rdr = state['reader']
            fs, cs, S = self._get_tile_meta_unsafe(state, channel)
        try:
            tile = self._read_tile_from_disk(rdr, channel, block_index, fs, cs, S)
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
                    s.discard((channel, block_index))

    def read_signal_range(self, file_path, channels, start_uutc, end_uutc, prefetch=True):
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
            metas = {ch: self._get_tile_meta_unsafe(state, ch) for ch in channels}

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
            _, cs, S = metas[ch]
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
            _, cs, S = metas[ch]
            parts = []
            for b in range(b1, b2 + 1):
                tile = cache.get((file_path, ch, b)) if b >= 0 else None
                if tile is None:
                    tile = self._read_tile_from_disk(rdr, ch, b, fs, cs, S)
                    if b >= 0:
                        cache.put((file_path, ch, b), tile)
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
            missing = [b for b in range(max(0, b1), b2 + 1)
                       if not cache.contains((file_path, ch, b))]
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
                fs, cs, S = metas[ch]
                futs.append((ch, pool.submit_read_tiles(actual_path, ch, blocks, fs, cs, S)))
            for ch, fut in futs:
                try:
                    tiles = fut.result()
                except Exception as e:  # noqa: BLE001 - fall back to in-process assembly
                    logger.error(f"Parallel tile read failed for {ch} in {file_path}: {e}")
                    continue
                for b, tile in tiles.items():
                    cache.put((file_path, ch, b), tile)
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
            _, cs, S = metas[ch]
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
            self._prefetch_executor.submit(self._prefetch_tile, file_path, channel, block_index)
            return
        cache_key = (file_path, channel, block_index)
        with self._lock:
            if file_path not in self._files:
                return
            if self._tile_cache.contains(cache_key):
                return
            in_progress = self._tile_in_progress.setdefault(file_path, set())
            key = (channel, block_index)
            if key in in_progress:
                return
            in_progress.add(key)
        fs, cs, S = meta
        try:
            fut = pool.submit_read_tiles(actual_path, channel, [block_index], fs, cs, S)
        except Exception as e:  # noqa: BLE001 - e.g. BrokenProcessPool; prefetch is best-effort
            logger.error(f"Prefetch submit failed ({channel}, {block_index}) for {file_path}: {e}")
            with self._lock:
                s = self._tile_in_progress.get(file_path)
                if s is not None:
                    s.discard((channel, block_index))
            return
        fut.add_done_callback(
            lambda f, fp=file_path, ch=channel, b=block_index: self._on_prefetch_done(f, fp, ch, b)
        )

    def _on_prefetch_done(self, fut, file_path, channel, block_index):
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
                        self._tile_cache.put((file_path, channel, b), tile)
                    logger.debug(f"Tile PREFETCHED: ({channel}, {block_index}) for {file_path}")
        finally:
            with self._lock:
                s = self._tile_in_progress.get(file_path)
                if s is not None:
                    s.discard((channel, block_index))

    def stream_signal_range(self, file_path, channel_names, start_uutc, end_uutc):
        """Yield a ``[start_uutc, end_uutc)`` read as ~2.5MB streamed SignalChunks.

        Wraps :meth:`read_signal_range` for the gRPC ``GetSignalRange`` RPC. On any
        error a single SignalChunk carrying ``error_message`` is yielded.
        """
        try:
            result = self.read_signal_range(file_path, list(channel_names), start_uutc, end_uutc)
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
        """Shuts down the prefetch thread pool, process pools, and TTL sweeper."""
        logger.info("Shutting down FileManager background resources...")
        self._stop_sweeper.set()
        self._prefetch_executor.shutdown(wait=False)
        for pool in (self._fg_pool, self._bg_pool):
            if pool is not None:
                pool.shutdown()

    # ... (rest of the FileManager methods: _get_file_info_unsafe, close_file, etc. remain the same) ...
    # Make sure to also add a shutdown method.
    def _get_file_info_unsafe(self, file_path):
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
        )

    def close_file(self, file_path):
        """Closes an open MEF file and cleans up its resources.

        Args:
            file_path (str): Path to the MEF file.

        Returns:
            FileInfoResponse: Protobuf response indicating the file is closed.
        """
        with self._lock:
            try:
                if file_path in self._files:
                    # Clean up resources if necessary (e.g., rdr.close())
                    del self._files[file_path]
                    # Clean up in-progress tile prefetches for this file
                    self._tile_in_progress.pop(file_path, None)
                    # Purge this file's tiles from the shared cache.
                    self._tile_cache.evict_matching(lambda k: k[0] == file_path)
                    logger.info(f"Closed and removed file: {file_path}")
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path,
                    file_opened=False,
                    error_message=""
                )
            except Exception as e:
                logger.error(f"Error closing file {file_path}: {e}")
                return gRPCMef3Server_pb2.FileInfoResponse(
                    file_path=file_path,
                    file_opened=False,
                    error_message=str(e)
                )

    def get_file_info(self, file_path):
        """Gets information about an open MEF file.

        Args:
            file_path (str): Path to the MEF file.

        Returns:
            FileInfoResponse: Protobuf response with file info and open status.
        """
        with self._lock:
            try:
                return self._get_file_info_unsafe(file_path)
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

