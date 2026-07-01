from concurrent import futures
import threading
import numpy as np
import brainmaze_mef3_server.protobufs.gRPCMef3Server_pb2 as gRPCMef3Server_pb2

from brainmaze_mef3_server.server.cache import LRUCache
from brainmaze_mef3_server.server.tile_cache import TileCache, CACHE_DTYPE
from mef_tools import MefReader
from brainmaze_mef3_server.server.log_manager import get_logger
import os

logger = get_logger("brainmaze_mef3_server.file_manager")


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

    This class provides efficient, concurrent access to MEF3 files, including:
      - File open/close and info management
      - LRU caching and asynchronous prefetching of signal segments
      - Chunking of signal data for streaming
      - Active channel selection and order preservation
      - Error handling for invalid requests and file states

    Thread safety is ensured via a lock for all state-changing operations. Each file is managed independently.
    The cache and prefetching system is designed for high-throughput, low-latency access to large files.
    """

    def __init__(self, n_prefetch=2, cache_capacity_multiplier=5, max_workers=4,
                 tile_duration_s=60, tile_cache_bytes=512 * 1024 * 1024):
        """
        Initialize the FileManager.

        Args:
            n_prefetch (int): Number of chunks/tiles to prefetch before and after each request.
            cache_capacity_multiplier (int): Additional (window) cache capacity beyond the prefetch window.
            max_workers (int): Maximum number of background threads for prefetching.
            tile_duration_s (float): Time-tile length in seconds for the timestamp-based
                (``read_signal_range``) access path.
            tile_cache_bytes (int): Per-file byte budget for the tile cache (float32 tiles).
        """
        self._files = {}
        self._lock = threading.Lock()

        # --- NEW: Configuration for caching ---
        self.n_prefetch = n_prefetch  # Number of chunks to prefetch before and after
        self.cache_capacity = (n_prefetch * 2) + cache_capacity_multiplier

        # --- Timestamp-based (tile) access configuration ---
        self.tile_duration_s = tile_duration_s
        self.tile_cache_bytes = tile_cache_bytes
        # A single tile cache shared across all open files, keyed by
        # (file_path, channel, block_index), enforces one global memory ceiling.
        self._tile_cache = TileCache(max_bytes=tile_cache_bytes)

        # --- NEW: Dedicated thread pool for background data loading ---
        self._prefetch_executor = futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='cache_prefetch'
        )
        # Track in-progress prefetches: {file_path: {chunk_idx: threading.Event}}
        self._in_progress = {}
        # Track in-progress tile prefetches: {file_path: set((channel, block_index))}
        self._tile_in_progress = {}

    # --- NEW: Helper method for background loading ---
    def _load_and_cache_chunk(self, file_path, chunk_idx):
        """Worker function to load a single chunk and put it in the cache.

        Args:
            file_path (str): Path to the MEF file.
            chunk_idx (int): Index of the chunk to load and cache.
        """
        # Minimize lock duration - only check and mark as in-progress
        with self._lock:
            # Check if file is still open and chunk isn't already cached
            if file_path not in self._files:
                return
            
            state = self._files[file_path]
            cache = state['cache']
            
            # Quick check: already cached or invalid index
            if chunk_idx in cache:
                return
            
            chunks = state['chunks']
            if not (chunks and 0 <= chunk_idx < len(chunks)):
                return
            
            # --- In-progress event tracking ---
            in_progress = self._in_progress.setdefault(file_path, {})
            if chunk_idx in in_progress:
                # Already being prefetched
                return
            
            # Mark as in progress
            event = threading.Event()
            in_progress[chunk_idx] = event
            
            # Get references we need (outside lock, these are safe to use)
            rdr = state['reader']
            chunk_info = chunks[chunk_idx]

        # --- Data reading happens outside the main lock ---
        try:
            channels = rdr.channels
            data = rdr.get_data(channels, chunk_info['start'], chunk_info['end'])
            data = np.array(data)

            # --- Put loaded data into the cache ---
            with self._lock:
                if file_path in self._files and self._files[file_path]['cache'] is cache:
                    cache.put(chunk_idx, data)
                    logger.debug(f"Cache PREFETCHED: chunk {chunk_idx} for {file_path}")
        except Exception as e:
            logger.error(f"Error prefetching chunk {chunk_idx} for {file_path}: {e}")
        finally:
            # Signal completion and cleanup
            with self._lock:
                event.set()
                if self._in_progress.get(file_path) is in_progress:
                    in_progress.pop(chunk_idx, None)

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
                    'chunks': [],
                    'chunk_duration_s': 0,
                    # --- NEW: Initialize a dedicated LRUCache for this file ---
                    'cache': LRUCache(capacity=self.cache_capacity),
                    # --- Timestamp-based access: per-channel tile metadata ---
                    # (tiles themselves live in the shared self._tile_cache)
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

            # Prefetch initial chunks if chunks are already set (e.g., re-opening)
            state = self._files[file_path]
            if state['chunks']:
                num_to_prefetch = min(self.n_prefetch + 1, len(state['chunks']))
                for idx in range(num_to_prefetch):
                    self._prefetch_executor.submit(self._load_and_cache_chunk, file_path, idx)

            return self._get_file_info_unsafe(file_path)

    def get_signal_segment(self, file_path, chunk_idx):
        """
        Yields signal data for a given segment, streaming in chunks of ~2.5MB.
        Args:
            file_path (str): Path to the MEF file.
            chunk_idx (int): Index of the segment to retrieve.
        Yields:
            SignalChunk: Protobuf message containing a chunk of signal data.
        """
        try:
            with self._lock:
                if file_path not in self._files:
                    yield gRPCMef3Server_pb2.SignalChunk(
                        file_path=file_path,
                        error_message=f"File not open: {file_path}"
                    )
                    return
                state = self._files[file_path]
                rdr = state['reader']
                chunks = state['chunks']
                cache = state['cache']
                in_progress = self._in_progress.setdefault(file_path, {})
                if not chunks:
                    yield gRPCMef3Server_pb2.SignalChunk(
                        file_path=file_path,
                        error_message=f"No chunks available for file: {file_path}. Set a segment size to init chunks."
                    )
                    return
                active_channels = state.get('active_channels')
                if active_channels is None:
                    active_channels = list(rdr.channels)
            if not (chunks and 0 <= chunk_idx < len(chunks)):
                yield gRPCMef3Server_pb2.SignalChunk(
                    file_path=file_path,
                    error_message=f"Invalid chunk request: {chunk_idx} for {file_path}"
                )
                return
            # --- PREFETCHING: Submit background tasks to load neighbors FIRST (before waiting) ---
            # This ensures prefetching happens eagerly, even before we need the current chunk
            # Batch check all neighbors in one lock acquisition to reduce contention
            with self._lock:
                for i in range(1, self.n_prefetch + 1):
                    neighbor_before = chunk_idx - i
                    neighbor_after = chunk_idx + i
                    if neighbor_before >= 0 and neighbor_before not in cache and neighbor_before not in in_progress:
                        self._prefetch_executor.submit(self._load_and_cache_chunk, file_path, neighbor_before)
                    if neighbor_after < len(chunks) and neighbor_after not in cache and neighbor_after not in in_progress:
                        self._prefetch_executor.submit(self._load_and_cache_chunk, file_path, neighbor_after)
            
            data = cache.get(chunk_idx)
            if data is not None:
                # --- CACHE HIT ---
                logger.debug(f"Cache HIT: chunk {chunk_idx} for {file_path}")
                # Filter data to only active channels if needed
                if 'active_channels' in state and state['active_channels'] is not None:
                    all_channels = list(rdr.channels)
                    channel_indices = [all_channels.index(ch) for ch in active_channels]
                    data = data[channel_indices, :]
            else:
                # --- Check if prefetch is in progress ---
                wait_event = None
                with self._lock:
                    if chunk_idx in in_progress:
                        wait_event = in_progress[chunk_idx]
                if wait_event is not None:
                    # Wait for the prefetch to complete
                    logger.debug(f"Waiting for prefetch of chunk {chunk_idx} for {file_path}")
                    wait_event.wait()
                    data = cache.get(chunk_idx)
                    if data is not None:
                        logger.debug(f"Cache HIT after wait: chunk {chunk_idx} for {file_path}")
                        # Filter data to only active channels if needed
                        if 'active_channels' in state and state['active_channels'] is not None:
                            all_channels = list(rdr.channels)
                            channel_indices = [all_channels.index(ch) for ch in active_channels]
                            data = data[channel_indices, :]
                    else:
                        logger.warning(f"Prefetch failed for chunk {chunk_idx} for {file_path}, loading from disk.")
                if data is None:
                    logger.info(f"Cache MISS: chunk {chunk_idx} for {file_path}. Loading from disk.")
                    # --- CACHE MISS (not in progress or prefetch failed) ---
                    try:
                        chunk_info = chunks[chunk_idx]
                        data = rdr.get_data(active_channels, chunk_info['start'], chunk_info['end'])
                        data = np.array(data)
                        cache.put(chunk_idx, data)
                    except Exception as e:
                        logger.error(f"Error loading chunk {chunk_idx} for {file_path}: {e}")
                        yield gRPCMef3Server_pb2.SignalChunk(
                            file_path=file_path,
                            error_message=str(e)
                        )
                        return

            # --- Dynamic chunking for ~2.5MB ---
            shape = list(data.shape)
            num_channels = shape[0]
            dtype_size = np.dtype('float64').itemsize
            max_bytes = int(2.5 * 1024 * 1024)  # 2.5MB
            samples_per_chunk = max(int(max_bytes / (num_channels * dtype_size)), 1)
            total_samples = shape[1]
            chunk_info = chunks[chunk_idx]
            chunk_start = int(chunk_info['start'])
            chunk_end = int(chunk_info['end'])

            if active_channels:
                fs = rdr.get_property('fsamp', active_channels[0])
            else:
                fs = rdr.get_property('fsamp')[0]

            for start in range(0, total_samples, samples_per_chunk):
                end = min(start + samples_per_chunk, total_samples)
                tile = data[:, start:end]
                # Calculate tile start/end timestamps
                tile_start = chunk_start + int((start / total_samples) * (chunk_end - chunk_start))
                tile_end = chunk_start + int((end / total_samples) * (chunk_end - chunk_start))
                yield gRPCMef3Server_pb2.SignalChunk(
                    file_path=file_path,
                    array_bytes=tile.tobytes(),
                    dtype='float64',
                    shape=list(tile.shape),
                    start_uutc=tile_start,
                    end_uutc=tile_end,
                    fs=fs,
                    channel_names=active_channels,
                    error_message=""
                )
        except Exception as e:
            logger.error(f"Unexpected error in get_signal_segment: {e}")
            yield gRPCMef3Server_pb2.SignalChunk(
                file_path=file_path,
                error_message=str(e)
            )

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
            channels (list[str] or None): Channels to read; ``None``/empty uses the
                active channels (or all channels).
            start_uutc (int): Inclusive window start in microseconds (uUTC).
            end_uutc (int): Exclusive window end in microseconds (uUTC).
            prefetch (bool): Whether to prefetch neighboring tiles.

        Returns:
            dict: ``{'array': np.ndarray (n_channels, n_samples) float32,
                     'channel_names': list, 'fs': float,
                     'start_uutc': int, 'end_uutc': int}``.

        Raises:
            ValueError: If the file is not open, channels are invalid, the requested
                channels do not share a sampling rate, or the window is empty.
        """
        with self._lock:
            if file_path not in self._files:
                raise ValueError(f"File not open: {file_path}")
            state = self._files[file_path]
            rdr = state['reader']
            cache = self._tile_cache
            all_channels = list(rdr.channels)
            if not channels:
                channels = state.get('active_channels') or all_channels
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

        rows = []
        prefetch_targets = []
        for ch in channels:
            _, cs, S = metas[ch]
            i1 = int(round((start_uutc - cs) * fs / 1e6))
            i2 = i1 + n
            b1 = i1 // S
            b2 = (i2 - 1) // S
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
            if prefetch:
                for k in range(1, self.n_prefetch + 1):
                    prefetch_targets.append((ch, b1 - k))
                    prefetch_targets.append((ch, b2 + k))

        data = np.vstack(rows) if len(rows) > 1 else rows[0][np.newaxis, :]

        if prefetch and prefetch_targets:
            for ch, b in prefetch_targets:
                if b >= 0:
                    self._prefetch_executor.submit(self._prefetch_tile, file_path, ch, b)

        return {
            'array': data,
            'channel_names': channels,
            'fs': fs,
            'start_uutc': int(start_uutc),
            'end_uutc': int(end_uutc),
        }

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

    # --- NEW: Method to gracefully shut down the thread pool ---
    def shutdown(self):
        """Shuts down the background prefetch thread pool executor."""
        logger.info("Shutting down prefetch executor...")
        self._prefetch_executor.shutdown(wait=False)

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
        start_uutc = min(rdr.get_property('start_time'))
        end_uutc = max(rdr.get_property('end_time'))
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
                    # Clean up in-progress events for this file
                    self._in_progress.pop(file_path, None)
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

    def set_signal_segment_size(self, file_path, seconds):
        """Sets the segment size for signal data and updates segment metadata.

        Args:
            file_path (str): Path to the MEF file.
            seconds (float): Duration of each segment in seconds.

        Returns:
            SetSignalSegmentResponse: Protobuf response with the number of segments.
        """
        with self._lock:
            if file_path not in self._files:
                logger.warning(f"set_signal_segment_size: file not open: {file_path}")
                return gRPCMef3Server_pb2.SetSignalSegmentResponse(
                    file_path=file_path,
                    number_of_segments=0,
                    error_message=f"File not open: {file_path}"
                )

            try:
                state = self._files[file_path]
                rdr = state['reader']
                start_uutc = min(rdr.get_property('start_time'))
                end_uutc = max(rdr.get_property('end_time'))
                segment_starts = np.arange(start_uutc, end_uutc, seconds * 1e6)
                segments = []
                for s in segment_starts:
                    seg_end = min(s + seconds * 1e6, end_uutc)
                    segments.append({'start': s, 'end': seg_end})
                # Ensure last segment is included even if shorter
                if not segments or segments[-1]['end'] < end_uutc:
                    if segments:
                        last_start = segments[-1]['end']
                    else:
                        last_start = start_uutc
                    segments.append({'start': last_start, 'end': end_uutc})
                state['cache'] = LRUCache(capacity=self.cache_capacity)
                self._in_progress.pop(file_path, None)
                state['chunk_duration_s'] = seconds
                state['chunks'] = segments
                if segments:
                    logger.info(f"Set segment size to {seconds}s for {file_path}, total segments: {len(segments)}")
                    # Eagerly prefetch the first n_prefetch chunks
                    num_to_prefetch = min(self.n_prefetch + 1, len(segments))
                    for idx in range(num_to_prefetch):
                        self._prefetch_executor.submit(self._load_and_cache_chunk, file_path, idx)

                return gRPCMef3Server_pb2.SetSignalSegmentResponse(
                    file_path=file_path,
                    number_of_segments=len(segments),
                    error_message=""
                )
            except Exception as e:
                logger.error(f"Error in set_signal_segment_size for {file_path}: {e}")
                return gRPCMef3Server_pb2.SetSignalSegmentResponse(
                    file_path=file_path,
                    number_of_segments=0,
                    error_message=str(e)
                )

    def get_number_of_segments(self, file_path):
        """Returns the number of signal segments currently configured for a file.

        Args:
            file_path (str): Path to the MEF file.

        Returns:
            int: Number of segments, or 0 if the file is not open or no segment
                size has been set.
        """
        with self._lock:
            state = self._files.get(file_path)
            if state is None:
                return 0
            return len(state.get('chunks', []))

    def list_open_files(self):
        """Lists all currently open MEF files.

        Returns:
            list: List of file paths for open files.
        """
        with self._lock:
            return list(self._files.keys())

    def set_active_channels(self, file_path, channel_names):
        with self._lock:
            if file_path not in self._files:
                return gRPCMef3Server_pb2.SetActiveChannelsResponse(
                    file_path=file_path,
                    active_channels=[],
                    error_message=f"File not open: {file_path}"
                )
            state = self._files[file_path]
            rdr = state['reader']
            all_channels = set(rdr.channels)
            requested = list(channel_names)
            valid = [ch for ch in requested if ch in all_channels]
            invalid = [ch for ch in requested if ch not in all_channels]
            if not requested:
                # Default to all channels
                state['active_channels'] = list(rdr.channels)
                return gRPCMef3Server_pb2.SetActiveChannelsResponse(
                    file_path=file_path,
                    active_channels=state['active_channels'],
                    error_message=""
                )
            if not valid:
                # None of the requested channels are valid, keep previous setup
                prev = state.get('active_channels', list(rdr.channels))
                return gRPCMef3Server_pb2.SetActiveChannelsResponse(
                    file_path=file_path,
                    active_channels=prev,
                    error_message=f"No valid channels in request. Invalid: {invalid}"
                )
            # Set only valid channels
            state['active_channels'] = valid
            err_msg = f"Some channels do not exist: {invalid}" if invalid else ""
            return gRPCMef3Server_pb2.SetActiveChannelsResponse(
                file_path=file_path,
                active_channels=valid,
                error_message=err_msg
            )

    def get_active_channels(self, file_path):
        with self._lock:
            if file_path not in self._files:
                return gRPCMef3Server_pb2.GetActiveChannelsResponse(
                    file_path=file_path,
                    active_channels=[],
                    error_message=f"File not open: {file_path}"
                )
            state = self._files[file_path]
            active = state.get('active_channels')
            if active is None:
                # Default to all channels
                active = list(state['reader'].channels)
                state['active_channels'] = active
            return gRPCMef3Server_pb2.GetActiveChannelsResponse(
                file_path=file_path,
                active_channels=active,
                error_message=""
            )
