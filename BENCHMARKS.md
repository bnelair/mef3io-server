# Benchmarks

This document explains how to run the benchmark suites, how the benchmark
dataset is configured and cached, and how to interpret the results.

All benchmarks compare **reading MEF3 data through the gRPC server** against
**reading it locally with a plain `MefReader`** (the historical baseline). They
are excluded from normal `pytest` runs and are opt-in via pytest markers.

## TL;DR

```sh
# from an activated environment (e.g. conda: `conda activate bnel-mef3-server`)
./run_benchmarks.sh data         # just (re)generate/cache the dataset
./run_benchmarks.sh benchmark    # all pytest-benchmark suites
./run_benchmarks.sh crossover    # heavy crossover-curve analysis (writes artifact)
./run_benchmarks.sh all          # benchmark + crossover
```

Everything below is also runnable directly with `pytest` if you prefer.

## Suites & markers

| Marker       | What it measures                                                        | Files |
|--------------|-------------------------------------------------------------------------|-------|
| `benchmark`  | Micro (in-process cache), sequential access, and detector/processing    | `test_file_manager.py`, `test_access_patterns.py`, `test_automated_processing.py` |
| `crossover`  | Sweep of per-window processing intensity to find where prefetch wins    | `test_crossover_curve.py` |
| `slow`       | Long functional tests (not performance)                                 | `test_real_life_data.py`, ... |

Marker gating (see `pyproject.toml`):
- Plain `pytest` runs **none** of these (`addopts = -m 'not slow and not benchmark and not crossover'`).
- `-m benchmark` runs the benchmark suites but **not** `crossover`.
- `crossover` is heavy and only runs with `-m crossover`.

## The benchmark dataset (config-driven & cached)

Dataset and workload parameters live in **`tests/benchmark_config.json`**. The
file is generated **once** into `benchmark_data/` (gitignored) and reused across
runs: a `benchmark_meta.json` is written *inside* the `.mefd` recording the
config it was built with, so an identical request skips regeneration. The file
name encodes the dataset identity (e.g. `bench_8ch_256hz_1320s_p2.mefd`), so a
small dev dataset and the full benchmark dataset **coexist** as separate caches —
switching between them is just editing the config.

Point at a different config file with the `BENCHMARK_CONFIG` env var:

```sh
BENCHMARK_CONFIG=/path/to/my_config.json ./run_benchmarks.sh benchmark
```

### Config reference

```jsonc
{
  // --- dataset identity (changing any of these regenerates the file) ---
  "channels": 8,               // number of channels
  "sampling_rate_hz": 256,
  "duration_s": 1320,          // recording length in seconds
  "precision": 2,
  "mef_block_len": 10000,      // MEF3 internal block length (align tiles to this later)
  "start_offset_days": 100,    // timestamp placed this many days in the past
  "seed": 42,                  // RNG seed for reproducible data
  "data_dir": "benchmark_data",// where generated .mefd files are cached (not identity)

  // --- workload: HOW the file is accessed (not part of identity) ---
  "workload": {
    "num_chunks": 20,          // sequential windows processed
    "segment_size_s": 60,      // window length (needs num_chunks*segment_size <= duration_s)
    "n_prefetch": 1,           // server prefetch depth
    "cache_capacity_multiplier": 30,
    "max_workers": 20,         // server prefetch/gRPC threads
    "processing_mode": "compute", // "compute" = real detector work; "sleep" = fixed delay
    "processing_cost_s": 0.3,  // per-window delay when processing_mode == "sleep"
    "compute_repeats": 1       // detector intensity when processing_mode == "compute"
  },

  // --- crossover analysis only ---
  "crossover": {
    "compute_repeats_sweep": [1, 2, 4, 8, 16], // intensities to sweep
    "include_no_prefetch": true,
    "output_dir": "benchmark_results"          // where the curve artifact is written
  }
}
```

### Switching to the full "real" benchmark

Edit `tests/benchmark_config.json` (or supply your own via `BENCHMARK_CONFIG`):

```jsonc
{ "channels": 256, "sampling_rate_hz": 256, "duration_s": 86400, "precision": 2, ... }
```

> ⚠️ The 24 h / 256 ch dataset takes a while to generate and needs several GB of
> disk. It is generated **once** and then cached, so subsequent runs are fast.

## Running individual suites (direct pytest)

```sh
pytest -m benchmark                                   # all benchmark suites
pytest -m benchmark tests/test_file_manager.py        # in-process cache micro-benchmark
pytest -m benchmark tests/test_access_patterns.py     # sequential access (with sleep floor)
pytest -m benchmark tests/test_automated_processing.py# detector: native vs gRPC±prefetch
pytest -m crossover                                   # crossover-curve analysis

python -m tests.benchmark_data                        # just generate/cache the dataset
```

Useful options (pytest-benchmark):

```sh
pytest -m benchmark -s                       # print the per-benchmark setup block
pytest -m benchmark --benchmark-json=out.json# capture results (incl. extra_info) to JSON
pytest -m benchmark --benchmark-columns=min,mean,max,rounds
pytest -m benchmark --benchmark-save=baseline# save a named baseline
pytest -m benchmark --benchmark-compare      # compare against the last saved run
```

## The crossover curve

`pytest -m crossover` sweeps `compute_repeats` and, at each level, times a **cold
single detector pass** for native-local, gRPC+prefetch, and (optionally) gRPC
no-prefetch. A fresh server is created per level so no cache carries over. It
writes an artifact to `crossover.output_dir` (default `benchmark_results/`):

- `crossover_curve.json` — full data + metadata
- `crossover_curve.md` — a table you can drop into the README

The **crossover point** is the first intensity where gRPC+prefetch ≤ native. With
few channels, decode is so cheap there is nothing to hide and native local wins at
every intensity (crossover = `None`); the server+prefetch advantage appears at
high channel counts, where per-window decode is large enough that overlapping it
with processing outweighs the gRPC transport cost.

## Interpreting results

- **Native local** is the baseline: one process, read and compute serialized.
- **gRPC + prefetch** decodes upcoming windows in the background, overlapping
  decode with the client's processing. It wins only when per-window processing is
  large relative to decode.
- **gRPC no-prefetch** pays transport per window with no overlap — expected to be
  the slowest; this is the classic "server slower than MefReader" case.
- MEF3 decode is **GIL-bound** in `pymef`, so the current server's prefetch does
  not decode in parallel — it only hides latency behind client work. (Real
  parallel decode needs multiple processes; a planned change.)
- Results are **machine-specific** (CPU count, disk, OS page cache). Record the
  host when publishing numbers; `record_benchmark_setup` already attaches CPU
  count and the full setup to each result's `extra_info`.
