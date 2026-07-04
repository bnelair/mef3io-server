# Tile cache

`TileCache` is the shared, byte-budgeted per-channel cache backing the
timestamp-based access path, with an idle TTL for freeing memory.

::: mef3io_server.server.tile_cache
