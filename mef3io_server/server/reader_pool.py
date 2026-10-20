"""Multi-process reader pool for parallel MEF3 decode.

Decode runs in separate worker processes, each with its own ``MefReader``
session, so decodes never contend on shared session state. (The pool predates
``mef3io``: the legacy ``pymef`` backend was GIL-bound, so threads could not
decode in parallel; ``mef3io`` releases the GIL during reads, but per-process
sessions remain the isolation model here.) ``FileManager`` runs two disjoint
instances of this pool -- a foreground lane for interactive/cold reads and a
prefetch lane for background look-ahead -- so prefetch can never occupy a worker
that a foreground read needs.

A window is split **by channel** across workers (MEF3 stores channels separately,
so this is embarrassingly parallel). Each worker process lazily opens and reuses
its own ``MefReader`` per file (one mef3io session per process). Results are
returned as ``float32`` arrays.
"""
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from mef3io import MefReader

from mef3io_server.server.tiles import read_tile

# Per-worker-process cache of open readers: {actual_path: MefReader}. Each worker
# process keeps its OWN readers -- a separate mef3io session per process keeps
# decode fully independent across workers.
_WORKER_READERS = {}


def _worker_get_reader(actual_path):
    reader = _WORKER_READERS.get(actual_path)
    if reader is None:
        reader = MefReader(actual_path)
        _WORKER_READERS[actual_path] = reader
    return reader


def _worker_read(actual_path, channels, t_start, t_end):
    """Worker entrypoint: decode ``channels`` over ``[t_start, t_end)`` as float32."""
    reader = _worker_get_reader(actual_path)
    data = reader.get_data(list(channels), int(t_start), int(t_end))
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[np.newaxis, :]
    return arr


def _worker_read_tiles(actual_path, channel, block_indices, fs, channel_start_uutc,
                       samples_per_tile, native_fs=None):
    """Worker entrypoint: decode tiles for one channel (``tiles.read_tile``).

    The same function the in-process path uses, so a tile is byte-identical
    whichever produced it. ``native_fs`` above ``fs`` means a downsampled tile.
    Returns ``{block_index: np.ndarray(samples_per_tile) float32}``.
    """
    reader = _worker_get_reader(actual_path)
    nat = fs if native_fs is None else native_fs
    return {int(b): read_tile(reader, channel, int(b), fs, channel_start_uutc,
                              samples_per_tile, nat)
            for b in block_indices}


def _split_contiguous(items, n_groups):
    """Split a list into up to ``n_groups`` contiguous, order-preserving groups."""
    n_groups = max(1, min(n_groups, len(items)))
    k, r = divmod(len(items), n_groups)
    groups = []
    start = 0
    for i in range(n_groups):
        size = k + (1 if i < r else 0)
        groups.append(items[start:start + size])
        start += size
    return [g for g in groups if g]


class ReaderProcessPool:
    """A pool of worker processes that decode MEF3 windows in parallel.

    Args:
        max_workers (int): Number of worker processes.
    """

    def __init__(self, max_workers=4):
        self.max_workers = max(1, int(max_workers))
        # Use the "spawn" start method regardless of platform. The default on
        # Linux is "fork", which is unsafe here: this pool is created inside a
        # multi-threaded gRPC server, and forking while other threads are inside
        # gRPC's C extension aborts the child ("Other threads are currently
        # calling into gRPC" -> Fatal Python error). "spawn" starts a clean
        # interpreter and avoids that entirely.
        self._executor = ProcessPoolExecutor(
            max_workers=self.max_workers, mp_context=mp.get_context("spawn")
        )
        # Background prefetch of time-chunks: {chunk_key: Future}. A chunk is a
        # fixed-duration slice of the requested channels, fetched ahead of reading.
        self._prefetched = {}

    def submit_read_tiles(self, actual_path, channel, block_indices, fs,
                          channel_start_uutc, samples_per_tile, native_fs=None):
        """Submit a decode of the given tiles for one channel; returns a Future.

        The Future resolves to ``{block_index: float32 tile}``. Used both for
        foreground cold reads (results gathered synchronously) and for background
        prefetch (via ``Future.add_done_callback``).
        """
        return self._executor.submit(
            _worker_read_tiles, actual_path, channel, list(block_indices),
            fs, channel_start_uutc, samples_per_tile, native_fs,
        )

    def warmup(self, actual_path, channel):
        """Pre-spawn workers and open a reader in each (amortizes first-call cost)."""
        futs = [
            self._executor.submit(_worker_read, actual_path, [channel], 0, 1)
            for _ in range(self.max_workers)
        ]
        for f in futs:
            try:
                f.result()
            except Exception:
                pass

    def read_window(self, actual_path, channels, t_start, t_end, n_splits=None):
        """Read ``channels`` over ``[t_start, t_end)``, split across workers by channel.

        Returns:
            np.ndarray: ``(len(channels), n_samples)`` float32, channel order preserved.
        """
        channels = list(channels)
        if not channels:
            raise ValueError("channels must be non-empty")
        n_splits = n_splits or self.max_workers
        groups = _split_contiguous(channels, n_splits)
        futures = [
            self._executor.submit(_worker_read, actual_path, g, t_start, t_end)
            for g in groups
        ]
        parts = [f.result() for f in futures]
        # Trim to the shortest part so ragged boundary reads still stack.
        min_len = min(p.shape[1] for p in parts)
        parts = [p[:, :min_len] for p in parts]
        return np.vstack(parts)

    # --- Time-chunked prefetch (prefetch chunk size is independent of the read
    #     window size: you can read 5-min windows while prefetching 1-min chunks) ---
    def prefetch_chunk(self, chunk_key, actual_path, channels, t_start, t_end):
        """Schedule a background prefetch of one time-chunk (all given channels).

        Idempotent per ``chunk_key`` -- scheduling the same key twice is a no-op.
        """
        if chunk_key in self._prefetched:
            return
        self._prefetched[chunk_key] = self._executor.submit(
            _worker_read, actual_path, list(channels), t_start, t_end
        )

    def is_scheduled(self, chunk_key):
        return chunk_key in self._prefetched

    def take_chunk(self, chunk_key):
        """Return a prefetched chunk (blocking until ready) or ``None`` if unknown."""
        future = self._prefetched.pop(chunk_key, None)
        if future is None:
            return None
        return future.result()

    def shutdown(self):
        # Cancel queued work, then wait for in-flight decodes and join the
        # workers. Tile decodes are short and bounded, so waiting is cheap and
        # avoids leaking worker processes past teardown (which flakes tests).
        self._prefetched.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)
