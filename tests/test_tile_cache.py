"""Unit tests for the byte-budgeted, key-based TileCache."""
import time

import numpy as np
import pytest

from brainmaze_mef3_server.server.tile_cache import TileCache, CACHE_DTYPE


def _tile(n, fill=1.0):
    return np.full(n, fill, dtype=np.float64)


def test_put_get_roundtrip_and_dtype():
    c = TileCache(max_bytes=10_000)
    c.put(("f", "ch0", 0), _tile(4, 2.0))
    got = c.get(("f", "ch0", 0))
    assert got is not None
    assert got.dtype == CACHE_DTYPE  # stored as float32
    np.testing.assert_array_equal(got, np.full(4, 2.0, dtype=CACHE_DTYPE))


def test_miss_returns_none_and_presence():
    c = TileCache(max_bytes=10_000)
    assert c.get(("f", "ch0", 0)) is None
    assert not c.contains(("f", "ch0", 0))
    c.put(("f", "ch0", 0), _tile(4))
    assert c.contains(("f", "ch0", 0))
    assert ("f", "ch0", 0) in c


def test_byte_budget_eviction_is_lru():
    # Each tile: 4 samples * 4 bytes = 16 bytes. Budget holds 2 tiles.
    c = TileCache(max_bytes=32)
    c.put(("f", "ch0", 0), _tile(4))
    c.put(("f", "ch0", 1), _tile(4))
    assert len(c) == 2 and c.current_bytes == 32
    # Touch tile 0 so tile 1 becomes LRU.
    assert c.get(("f", "ch0", 0)) is not None
    c.put(("f", "ch0", 2), _tile(4))  # evicts LRU == tile 1
    assert c.contains(("f", "ch0", 0))
    assert c.contains(("f", "ch0", 2))
    assert not c.contains(("f", "ch0", 1))
    assert c.current_bytes == 32


def test_oversized_tile_not_retained():
    c = TileCache(max_bytes=16)
    c.put(("f", "ch0", 0), _tile(4))  # 16 bytes, fits exactly
    c.put(("f", "ch0", 1), _tile(100))  # 400 bytes, cannot fit
    assert not c.contains(("f", "ch0", 1))
    # The pre-existing, fitting tile must survive a doomed oversized insert.
    assert c.contains(("f", "ch0", 0))


def test_replace_updates_byte_count():
    c = TileCache(max_bytes=1_000)
    c.put(("f", "ch0", 0), _tile(4))
    b1 = c.current_bytes
    c.put(("f", "ch0", 0), _tile(8))  # replace with larger
    assert c.current_bytes == b1 * 2
    assert len(c) == 1


def test_gap_nan_is_preserved_distinct_from_absence():
    c = TileCache(max_bytes=1_000)
    arr = _tile(4)
    arr[1:3] = np.nan  # a real MEF3 gap inside a loaded tile
    c.put(("f", "ch0", 0), arr)
    got = c.get(("f", "ch0", 0))
    assert c.contains(("f", "ch0", 0))  # loaded...
    assert np.isnan(got[1:3]).all()  # ...but contains gap NaNs


def test_evict_matching_purges_one_files_tiles():
    c = TileCache(max_bytes=10_000)
    c.put(("A", "ch0", 0), _tile(4))
    c.put(("A", "ch1", 0), _tile(4))
    c.put(("B", "ch0", 0), _tile(4))
    dropped = c.evict_matching(lambda k: k[0] == "A")
    assert dropped == 2
    assert not c.contains(("A", "ch0", 0))
    assert c.contains(("B", "ch0", 0))
    assert c.current_bytes == 16  # only file B's single tile remains


def test_global_budget_shared_across_files():
    # 2-tile budget shared globally: inserting a 3rd tile (any file) evicts LRU.
    c = TileCache(max_bytes=32)
    c.put(("A", "ch0", 0), _tile(4))
    c.put(("B", "ch0", 0), _tile(4))
    c.put(("C", "ch0", 0), _tile(4))  # evicts LRU == file A
    assert not c.contains(("A", "ch0", 0))
    assert c.contains(("B", "ch0", 0))
    assert c.contains(("C", "ch0", 0))


def test_ttl_evicts_idle_tiles():
    # A deterministic clock is injected via evict_expired(now=...), no sleeping.
    c = TileCache(max_bytes=10_000, ttl_seconds=100)
    c.put(("f", "ch0", 0), _tile(4))
    t0 = time.monotonic()
    # Within TTL -> retained.
    assert c.evict_expired(now=t0 + 50) == 0
    assert c.contains(("f", "ch0", 0))
    # Past TTL -> evicted and bytes reclaimed.
    assert c.evict_expired(now=t0 + 150) == 1
    assert not c.contains(("f", "ch0", 0))
    assert c.current_bytes == 0


def test_ttl_access_resets_idle_timer():
    c = TileCache(max_bytes=10_000, ttl_seconds=100)
    c.put(("f", "ch0", 0), _tile(4))
    assert c.get(("f", "ch0", 0)) is not None  # access refreshes last-access time
    t_access = time.monotonic()
    assert c.evict_expired(now=t_access + 50) == 0
    assert c.contains(("f", "ch0", 0))


def test_ttl_none_disables_expiry():
    c = TileCache(max_bytes=10_000, ttl_seconds=None)
    c.put(("f", "ch0", 0), _tile(4))
    assert c.evict_expired(now=1e18) == 0  # never expires
    assert c.contains(("f", "ch0", 0))


def test_clear_and_invalid_budget():
    c = TileCache(max_bytes=100)
    c.put(("f", "ch0", 0), _tile(4))
    c.clear()
    assert len(c) == 0 and c.current_bytes == 0
    with pytest.raises(ValueError):
        TileCache(max_bytes=0)
