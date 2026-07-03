"""Signal tile cache with a global byte budget.

This is the caching substrate for timestamp-based access. The client asks for any
``(channels, t1, t2)``; internally the recording is addressed as a sparse grid of
**tiles**, where each tile holds a fixed number of samples for a *single* channel
over a fixed time window. MEF3 stores each channel independently, so tiling per
channel (rather than per channel-block) avoids read amplification when only a few
of many channels are requested.

The cache is **keyed by an arbitrary hashable** (the FileManager uses
``(file_path, channel, block_index)``) so a single instance can be shared across
all open files under one global budget -- many open files/clients therefore share
one memory ceiling instead of each holding an independent per-file cache.

Key properties:

* **Value** = a ``float32`` array of samples (``float32`` halves memory vs
  ``float64``).
* **Presence vs. gaps**: a key being present means the tile is loaded; genuine
  MEF3 discontinuities inside a loaded tile are represented as ``NaN`` values.
  "Not loaded" and "loaded but contains a gap" are therefore distinct.
* **Byte-budgeted LRU**: eviction is driven by a total byte budget, not a fixed
  item count, since tiles can differ in size across channels/sampling rates.
* **Idle TTL**: tiles that have not been accessed for ``ttl_seconds`` are
  discarded, so a finished session (e.g. a detector that moved on) does not pin
  memory even if the byte budget is never hit.
* **Thread-safe**: all operations are guarded by a single lock.
"""
import collections
import threading
import time

import numpy as np

CACHE_DTYPE = np.float32


class TileCache:
    """A thread-safe, byte-budgeted LRU cache of signal tiles.

    Args:
        max_bytes (int): Maximum total size of cached tiles, in bytes. Must be
            positive. Individual tiles larger than this are never retained.
        ttl_seconds (float or None): Discard a tile after this many seconds without
            an access (``get``/``put``). ``None`` or ``<= 0`` disables idle expiry.
    """

    def __init__(self, max_bytes: int, ttl_seconds=None):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = float(ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
        self._store = collections.OrderedDict()  # key -> np.ndarray
        self._atime = {}  # key -> last-access monotonic timestamp
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _nbytes(array):
        return int(array.nbytes)

    def get(self, key):
        """Return the cached tile for ``key`` or ``None``; a hit is marked MRU."""
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            self._atime[key] = time.monotonic()
            return self._store[key]

    def put(self, key, array):
        """Insert or replace a tile, evicting LRU tiles to honor the budget.

        The array is stored as ``float32`` (copied only if a cast is needed).
        Tiles larger than the whole budget are dropped rather than cached.
        """
        array = np.ascontiguousarray(array, dtype=CACHE_DTYPE)
        nbytes = self._nbytes(array)
        with self._lock:
            if key in self._store:
                self._bytes -= self._nbytes(self._store[key])
                del self._store[key]
            if nbytes > self.max_bytes:
                # Cannot ever fit; do not evict everything for a doomed insert.
                self._atime.pop(key, None)
                return
            self._store[key] = array
            self._bytes += nbytes
            self._store.move_to_end(key)
            self._atime[key] = time.monotonic()
            self._evict_to_budget()

    def _evict_to_budget(self):
        """Evict least-recently-used tiles until within the byte budget."""
        while self._bytes > self.max_bytes and self._store:
            k, evicted = self._store.popitem(last=False)
            self._bytes -= self._nbytes(evicted)
            self._atime.pop(k, None)

    def evict_expired(self, now=None):
        """Drop tiles not accessed within ``ttl_seconds``.

        A no-op when TTL is disabled. Intended to be called periodically by a
        background sweeper so idle sessions release memory even without traffic.

        Returns:
            int: Number of tiles evicted.
        """
        if self.ttl_seconds is None:
            return 0
        if now is None:
            now = time.monotonic()
        cutoff = now - self.ttl_seconds
        with self._lock:
            to_drop = [k for k, t in self._atime.items() if t < cutoff]
            for k in to_drop:
                self._bytes -= self._nbytes(self._store[k])
                del self._store[k]
                del self._atime[k]
            return len(to_drop)

    def evict_matching(self, predicate):
        """Drop every tile whose key satisfies ``predicate(key)``.

        Used to purge a file's tiles when it is closed.

        Returns:
            int: Number of tiles evicted.
        """
        with self._lock:
            to_drop = [k for k in self._store if predicate(k)]
            for k in to_drop:
                self._bytes -= self._nbytes(self._store[k])
                del self._store[k]
                self._atime.pop(k, None)
            return len(to_drop)

    def __contains__(self, key):
        with self._lock:
            return key in self._store

    def contains(self, key):
        """Return whether a tile is cached (without touching LRU order)."""
        with self._lock:
            return key in self._store

    @property
    def current_bytes(self):
        with self._lock:
            return self._bytes

    def __len__(self):
        with self._lock:
            return len(self._store)

    def clear(self):
        """Drop all cached tiles."""
        with self._lock:
            self._store.clear()
            self._atime.clear()
            self._bytes = 0
