"""Entrypoint for running the gRPC MEF3 server as a Python module.

This script reads configuration from environment variables and launches the gRPC server.
"""
import os
import signal
import sys
import time
from brainmaze_mef3_server.server.mef3_server import gRPCMef3ServerHandler

print("[MEF3 SERVER] Starting server from __main__.py...")


def main():
    """Main entrypoint for the gRPC MEF3 server.

    Reads configuration from environment variables and starts the server.
    Handles graceful shutdown on SIGTERM/SIGINT.
    """
    def _env_bool(name, default):
        return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

    def _env_int_or_none(name):
        val = os.environ.get(name)
        return int(val) if val not in (None, "") else None

    # Read configuration from environment variables (with defaults)
    port = int(os.environ.get("PORT", 50051))
    max_workers = int(os.environ.get("MAX_WORKERS", 4))
    tile_duration_s = float(os.environ.get("TILE_DURATION_S", 60))
    tile_cache_bytes = int(float(os.environ.get("TILE_CACHE_MB", 512)) * 1024 * 1024)
    # --- Parallel-decode + prefetch + TTL configuration ---
    use_process_pool = _env_bool("USE_PROCESS_POOL", True)
    reader_processes = _env_int_or_none("READER_PROCESSES")
    prefetch_processes = _env_int_or_none("PREFETCH_PROCESSES")
    min_parallel_tiles = int(os.environ.get("MIN_PARALLEL_TILES", 2))
    prefetch_ahead_windows = int(os.environ.get("PREFETCH_AHEAD_WINDOWS", 1))
    prefetch_behind_windows = int(os.environ.get("PREFETCH_BEHIND_WINDOWS", 1))
    cache_ttl_s = float(os.environ.get("CACHE_TTL_S", 1800))

    print(f"Starting gRPC MEF3 server on port {port} with FileManager config: "
          f"max_workers={max_workers}, tile_duration_s={tile_duration_s}, "
          f"tile_cache_mb={tile_cache_bytes // (1024 * 1024)}, "
          f"use_process_pool={use_process_pool}, reader_processes={reader_processes}, "
          f"prefetch_processes={prefetch_processes}, min_parallel_tiles={min_parallel_tiles}, "
          f"prefetch_ahead_windows={prefetch_ahead_windows}, "
          f"prefetch_behind_windows={prefetch_behind_windows}, cache_ttl_s={cache_ttl_s}")

    handler = gRPCMef3ServerHandler(
        port=port,
        max_workers=max_workers,
        tile_duration_s=tile_duration_s,
        tile_cache_bytes=tile_cache_bytes,
        use_process_pool=use_process_pool,
        reader_processes=reader_processes,
        prefetch_processes=prefetch_processes,
        min_parallel_tiles=min_parallel_tiles,
        prefetch_ahead_windows=prefetch_ahead_windows,
        prefetch_behind_windows=prefetch_behind_windows,
        cache_ttl_s=cache_ttl_s,
    )

    def handle_sigterm(signum, frame):
        """Handles SIGTERM/SIGINT for graceful shutdown."""
        print("Received termination signal, shutting down...")
        handler.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_sigterm(None, None)


if __name__ == "__main__":
    main()
