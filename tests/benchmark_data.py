"""Config-driven, cached generation of MEF3 benchmark datasets.

The dataset parameters live in ``tests/benchmark_config.json`` so development can
use a small file while the real (e.g. 24h / 256ch) benchmark is only a change of
variables. Generated ``.mefd`` directories are *persisted* (not tempdirs) under a
``data_dir`` and reused across runs: a ``benchmark_meta.json`` is dumped inside
each ``.mefd`` recording the config it was built with, so an identical request
skips regeneration instead of rebuilding the same file over and over.

The generated file name encodes the dataset identity, so a dev-small config and
the full benchmark config coexist as separate cached files -- switching between
them is just editing the config.
"""
import os
import json
import shutil
import logging
import datetime

import numpy as np
from mef_tools import MefWriter, MefReader

logger = logging.getLogger(__name__)

# Bump when the generation logic changes in a way that invalidates cached files.
GENERATOR_VERSION = 1

DEFAULT_CONFIG = {
    "channels": 8,
    "sampling_rate_hz": 256,
    "duration_s": 300,
    "precision": 2,
    "mef_block_len": 10000,
    "start_offset_days": 100,
    "seed": 42,
    "data_dir": "benchmark_data",
}

# Workload params describe *how the file is accessed* during benchmarks (not its
# content), so they are intentionally NOT part of the dataset identity and never
# trigger regeneration. They live under the ``workload`` key of the config.
DEFAULT_WORKLOAD = {
    # --- access pattern (shared by every scenario) ---
    "num_chunks": 20,            # number of sequential windows processed
    "segment_size_s": 60,        # window length in seconds
    "processing_mode": "compute",  # "compute" (real detector work) | "sleep"
    "processing_cost_s": 0.3,    # per-window delay when processing_mode == "sleep"
    "compute_repeats": 1,        # detector intensity when processing_mode == "compute"
    # --- multi-tool shared-session benchmark (use case B) ---
    "num_tools": 4,              # independent tools all processing the SAME session
    "tool_overlap": 1.0,         # fraction [0..1] of each tool's windows shared with the others
    # --- server under test: passed straight through to the FileManager ---
    "grpc_threads": 20,          # gRPC servicer threads + thread-prefetch fallback pool (MAX_WORKERS)
    "use_process_pool": True,    # parallel decode in worker processes (USE_PROCESS_POOL)
    "reader_processes": None,    # total decode processes; None => auto cpu-1 (READER_PROCESSES)
    "prefetch_processes": None,  # background prefetch lane size; None => auto half (PREFETCH_PROCESSES)
    "min_parallel_tiles": 2,     # min missing tiles before fanning out to the pool (MIN_PARALLEL_TILES)
    "prefetch_ahead_windows": 1, # windows to prefetch ahead / page forward (PREFETCH_AHEAD_WINDOWS)
    "prefetch_behind_windows": 1,# windows to prefetch behind / page backward (PREFETCH_BEHIND_WINDOWS)
    "tile_duration_s": 60,       # tile length for the range path (TILE_DURATION_S)
    "tile_cache_mb": 512,        # global tile-cache byte budget (TILE_CACHE_MB)
    "cache_ttl_s": 1800,         # discard tiles idle longer than this (CACHE_TTL_S)
    # --- ReaderProcessPool micro-benchmark ONLY (isolated prototype path;
    #     test_reader_pool.py measures the pool directly, not the wired server) ---
    "reader_pool_workers": 4,    # worker PROCESSES for the isolated reader-pool benchmark
    "read_window_s": 300,        # FOREGROUND read size (what you request at once), e.g. 5 min
    "prefetch_chunk_s": 60,      # PREFETCH granularity each worker fetches ahead, e.g. 1 min
    "prefetch_ahead_chunks": 8,  # how many prefetch chunks to keep scheduled ahead of reading
}

# Workload keys that map 1:1 onto FileManager constructor kwargs. `grpc_threads`
# and `tile_cache_mb` are translated separately by :func:`server_kwargs`.
_SERVER_PASSTHROUGH_KEYS = (
    "use_process_pool", "reader_processes", "prefetch_processes",
    "min_parallel_tiles", "prefetch_ahead_windows", "prefetch_behind_windows",
    "tile_duration_s", "cache_ttl_s",
)


def get_workload(config):
    """Return the benchmark workload params, merging config over defaults.

    Args:
        config (dict): The loaded benchmark config.

    Returns:
        dict: Effective workload parameters.
    """
    wl = dict(DEFAULT_WORKLOAD)
    wl.update(config.get("workload", {}))
    return wl


def server_kwargs(workload, **overrides):
    """FileManager kwargs for the benchmark server, from a workload (+ overrides).

    Translates the workload's server-tuning keys into the FileManager constructor
    signature (``grpc_threads`` -> ``max_workers``, ``tile_cache_mb`` ->
    ``tile_cache_bytes``). ``overrides`` win, e.g.
    ``server_kwargs(wl, prefetch_ahead_windows=0)`` for a no-prefetch scenario.
    """
    kw = {k: workload[k] for k in _SERVER_PASSTHROUGH_KEYS if k in workload}
    kw["max_workers"] = workload.get("grpc_threads", 4)
    kw["tile_cache_bytes"] = int(workload.get("tile_cache_mb", 512) * 1024 * 1024)
    kw.update(overrides)
    return kw


# Crossover-curve analysis: sweep the per-window processing intensity to find
# where server+prefetch overtakes native-local reading. Heavy / opt-in only.
DEFAULT_CROSSOVER = {
    "compute_repeats_sweep": [1, 2, 4, 8, 16],
    "include_no_prefetch": True,
    "output_dir": "benchmark_results",
}


def get_crossover(config):
    """Return the crossover-sweep params, merging config over defaults.

    Args:
        config (dict): The loaded benchmark config.

    Returns:
        dict: Effective crossover-analysis parameters.
    """
    cx = dict(DEFAULT_CROSSOVER)
    cx.update(config.get("crossover", {}))
    return cx


# Params that determine the *content* of the file. If any of these change, the
# cached file is considered stale and regenerated. ``data_dir`` is a location,
# not part of the dataset identity.
_IDENTITY_KEYS = (
    "channels", "sampling_rate_hz", "duration_s", "precision",
    "mef_block_len", "start_offset_days", "seed",
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)


def load_benchmark_config(path=None):
    """Load the benchmark dataset config, merging a JSON file over the defaults.

    Path resolution order: explicit ``path`` argument, then the ``BENCHMARK_CONFIG``
    environment variable, then ``tests/benchmark_config.json``. A missing file
    simply falls back to :data:`DEFAULT_CONFIG`.

    Args:
        path (str, optional): Explicit path to a JSON config file.

    Returns:
        dict: The effective benchmark configuration.
    """
    if path is None:
        path = os.environ.get(
            "BENCHMARK_CONFIG", os.path.join(_HERE, "benchmark_config.json")
        )
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


def _identity(config):
    """Return the subset of config that defines the dataset content."""
    return {k: config[k] for k in _IDENTITY_KEYS}


def _dataset_filename(config):
    """Deterministic ``.mefd`` name so distinct configs cache independently."""
    i = _identity(config)
    return (
        f"bench_{i['channels']}ch_{i['sampling_rate_hz']}hz_"
        f"{i['duration_s']}s_p{i['precision']}.mefd"
    )


def _resolve_data_dir(config):
    """Resolve ``data_dir`` to an absolute path (relative to the repo root)."""
    d = config.get("data_dir", DEFAULT_CONFIG["data_dir"])
    if not os.path.isabs(d):
        d = os.path.join(_REPO_ROOT, d)
    return d


def _meta_path(mefd_path):
    return os.path.join(mefd_path, "benchmark_meta.json")


def read_benchmark_meta(mefd_path):
    """Return the ``benchmark_meta.json`` dumped inside a generated ``.mefd``.

    Args:
        mefd_path (str): Path to the ``.mefd`` directory.

    Returns:
        dict or None: Parsed metadata, or ``None`` if absent/unreadable.
    """
    meta_path = _meta_path(mefd_path)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _is_valid_cache(mefd_path, config):
    """True if an existing ``.mefd`` matches the requested identity and opens."""
    if not os.path.isdir(mefd_path):
        return False
    meta = read_benchmark_meta(mefd_path)
    if meta is None:
        return False
    if meta.get("generator_version") != GENERATOR_VERSION:
        return False
    if meta.get("identity") != _identity(config):
        return False
    try:
        MefReader(mefd_path)  # sanity check: the file is actually readable
    except Exception as e:  # noqa: BLE001 - any reader failure means regenerate
        logger.warning("Cached benchmark file failed to open, regenerating: %s", e)
        return False
    return True


def get_or_create_benchmark_file(config=None, force=False):
    """Return the path to a benchmark ``.mefd``, generating it once if needed.

    If a previously generated file with a matching identity already exists it is
    reused (no regeneration). Otherwise the file is built from ``config`` and a
    ``benchmark_meta.json`` is written inside it for future runs.

    Args:
        config (dict, optional): Dataset config; defaults to
            :func:`load_benchmark_config`.
        force (bool): Regenerate even if a valid cached file exists.

    Returns:
        str: Absolute path to the ``.mefd`` directory.
    """
    if config is None:
        config = load_benchmark_config()
    data_dir = _resolve_data_dir(config)
    os.makedirs(data_dir, exist_ok=True)
    mefd_path = os.path.join(data_dir, _dataset_filename(config))

    if not force and _is_valid_cache(mefd_path, config):
        logger.info("Reusing cached benchmark file: %s", mefd_path)
        return mefd_path

    logger.info("Generating benchmark file: %s", mefd_path)
    _generate(mefd_path, config)
    return mefd_path


def _generate(mefd_path, config):
    """Write a fresh MEF3 dataset from ``config`` and dump its metadata."""
    # Remove any stale/corrupt artifacts so regeneration starts clean.
    shutil.rmtree(mefd_path, ignore_errors=True)

    channels = config["channels"]
    fs = config["sampling_rate_hz"]
    duration_s = config["duration_s"]
    precision = config["precision"]

    rng = np.random.default_rng(config.get("seed", 42))
    start_uutc = int(
        (datetime.datetime.now().timestamp()
         - 3600 * 24 * config["start_offset_days"]) * 1e6
    )

    wrt = MefWriter(mefd_path, overwrite=True)
    wrt.mef_block_len = config["mef_block_len"]
    wrt.max_nans_written = 0

    n_samples = int(duration_s * fs)
    for idx in range(channels):
        chname = f"chan_{idx + 1:03d}"
        x = rng.standard_normal(n_samples)
        wrt.write_data(x, chname, start_uutc, fs, precision=precision)
    del wrt  # MefWriter flushes/closes on GC (no explicit close method)

    meta = {
        "generator_version": GENERATOR_VERSION,
        "identity": _identity(config),
        "config": config,
        "start_uutc": start_uutc,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(_meta_path(mefd_path), "w") as f:
        json.dump(meta, f, indent=2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = load_benchmark_config()
    path = get_or_create_benchmark_file(cfg)
    print(f"Benchmark file ready: {path}")
    print(json.dumps(read_benchmark_meta(path), indent=2))
