# Benchmarks

This document explains **what the benchmarks are trying to prove**, how to run
them, how the dataset is configured/cached, and how to read the results.

Every benchmark compares two ways of getting the same MEF3 data to the same
processing code:

- **Native local** — read it in-process with a plain `MefReader`, in a loop.
  This is the historical baseline, and for a *single sequential consumer* it is
  hard to beat: no transport, no serialization, just decode → compute.
- **gRPC server** — read it through the server, which adds a **shared, reusable
  cache** and **background prefetch** on top of the same `MefReader`.

Every server read in the benchmarks goes through the **timestamp-based
`get_signal_range` path** — the shared per-channel `TileCache` + parallel decode +
background tile prefetch. This is the server's only data-access model: every call
is oriented in channels and time.

The whole point of the benchmarks is to be honest about **when the server helps
and when it doesn't**, because "the local reader is faster" is true for the
naive single-pass case and misleading for the cases the server actually targets.

## The three use cases we care about

| # | Use case | What it looks like | Who should win, and why | Suite |
|---|----------|--------------------|-------------------------|-------|
| **A** | **Interactive data viewing** | A dashboard scrolls/jumps through the recording, reading 10 s–5 min of a handful of channels, revisiting regions. | Roughly a **tie on cold reads**; the server wins on **revisits** (warm cache) and hides decode behind human think-time via **prefetch**. Native re-decodes every revisit. | `test_access_patterns.py` (sequential scroll w/ think-time) |
| **B** | **Batch: many tools, one session** | One iEEG session processed by several tools (spike/seizure/QC), whose data access **overlaps**. "I don't want to read + decrypt/decompress the same data multiple times." | **Server wins.** MEF3 decode is decrypt+decompress; native does it **once per tool**. The shared tile cache decodes each overlapping region **once** and serves every other tool warm. | `test_multitool_shared_session.py` |
| **C** | **Automated single-pass processing** | One detector walks the whole recording once, window by window, doing real per-window signal processing. | **Server now wins at high channel counts.** With the process pool (default) the server decodes upcoming windows **in parallel across processes** and prefetch overlaps that with compute. At 64 ch the isolated crossover sweep shows gRPC+prefetch beating native at **every** intensity (crossover = 1). With very cheap decode (few channels) native can still win — nothing to parallelize/hide. | `test_automated_processing.py`, `test_crossover_curve.py` |

The **native `MefReader` in a loop is the baseline in all three** — it is exactly
what use case B does N times and what use case C does once.

> A note on parallelism: these numbers were measured with the legacy `pymef`
> backend, whose MEF3 decode is **GIL-bound**, so *thread* prefetch did not decode
> in parallel (the current `mef3io` backend releases the GIL during reads).
> The server therefore **decodes in worker
> processes** (`ReaderProcessPool`, two disjoint lanes: a reserved foreground lane
> + a background prefetch lane), which **is now the default** (`USE_PROCESS_POOL=1`).
> This adds genuine parallel decode on top of the two older wins — *hiding* decode
> behind the client's own work (think-time in A, compute in C) and *reusing*
> already-decoded data (the cache win in A and B). It is what flips use case C in
> the server's favour at high channel counts. Set `USE_PROCESS_POOL=0` to fall
> back to the in-process thread path.

## TL;DR

```sh
# from an activated environment (e.g. conda: `conda activate bnel-mef3-server`)
./run_benchmarks.sh data         # just (re)generate/cache the dataset
./run_benchmarks.sh multitool    # use case B: many tools, one session (shared cache)
./run_benchmarks.sh benchmark    # all pytest-benchmark suites
./run_benchmarks.sh report       # run all suites, then write a Markdown scenario report
./run_benchmarks.sh crossover    # heavy crossover-curve analysis (writes artifact)
./run_benchmarks.sh all          # benchmark + crossover
```

Everything below is also runnable directly with `pytest` if you prefer.

## The benchmark report (per-scenario values + explanation)

`./run_benchmarks.sh report` runs every `benchmark`-marked suite, dumps the
`pytest-benchmark` JSON, and renders **`benchmark_results/benchmark_report.md`** —
a plain-language report that, for each benchmark, gives the scenario
explanation, its use case (A/B/C), its timing, and its **speedup vs the
native-local baseline** for that use case. It opens with a summary table:

| Use case | Scenario | Mean (s) | Speedup vs native |
| --- | --- | ---: | ---: |
| B | gRPC shared tile cache | ~2.7–4.4 | **2.2–2.8x** |
| B | Native local (baseline) | ~7.7–9.6 | — |
| C | Native local (baseline) | ~2.0 | — |
| C | gRPC + prefetch (isolated crossover) | ~1.5 | **~1.2–1.34x** |
| A | Native local (baseline) | ~8.5 | — |
| A | gRPC ± prefetch | ~8.7 | ~0.99–1.06x |

(Numbers above are illustrative runs on an Apple M3 Max / 14 CPUs — not canonical;
regenerate them for your host.) The takeaways match the use-case model: **B wins
big** (decode once, serve many); **A is a wash** (think-time dominates and the
cache/prefetch just keeps pace); **C now favours the server** at 64 ch once decode
runs in parallel across processes — see the crossover curve below.

> ⚠️ The single-shot `test_processing_*` benchmarks (use case C, `rounds=1`) are
> **high variance**: they include one-time process-pool spawn cost, and when the
> full `-m benchmark` suite runs it starts *several* servers back-to-back, so their
> worker pools can oversubscribe the host CPUs. Run-to-run this single figure can
> swing ~2x and even flip sign. Trust the **crossover sweep** (fresh server per
> level, isolated) for the use-case-C verdict, not the bundled single shot. A real
> detector doing a full-recording pass amortizes the spawn cost to nothing.

Regenerate it directly with:

```sh
pytest -m benchmark --benchmark-json=benchmark_results/benchmark.json
python -m tests.benchmark_report benchmark_results/benchmark.json
```

## Suites & markers

| Marker       | What it measures                                                        | Use case | Files |
|--------------|-------------------------------------------------------------------------|----------|-------|
| `benchmark`  | Multi-tool shared-cache (many tools, one session)                       | **B** | `test_multitool_shared_session.py` |
| `benchmark`  | Sequential access with think-time; detector native vs gRPC ±prefetch    | A, C | `test_access_patterns.py`, `test_automated_processing.py` |
| `benchmark`  | In-process cache micro-benchmark; multiprocess reader-pool decode       | (infra) | `test_file_manager.py`, `test_reader_pool.py` |
| `crossover`  | Sweep of per-window processing intensity to find where prefetch wins    | C | `test_crossover_curve.py` |
| `slow`       | Long functional tests (not performance)                                 | — | `test_real_life_data.py`, ... |

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
  // All keys are optional; anything omitted falls back to DEFAULT_WORKLOAD in
  // tests/benchmark_data.py. Server keys are passed straight through to the
  // FileManager under test via server_kwargs() (their env-var equivalents are in
  // parentheses).
  "workload": {
    // access pattern (shared by every scenario)
    "num_chunks": 20,          // sequential windows processed
    "segment_size_s": 60,      // window length (needs num_chunks*segment_size <= duration_s)
    "processing_mode": "compute", // "compute" = real detector work; "sleep" = fixed delay
    "processing_cost_s": 0.5,  // per-window delay when processing_mode == "sleep"
    "compute_repeats": 1,      // detector intensity when processing_mode == "compute"
    // use case B: many tools, one session
    "num_tools": 4,            // independent tools all processing the SAME session
    "tool_overlap": 1.0,       // fraction [0..1] of each tool's windows shared with others
    // server under test (passed to the FileManager)
    "grpc_threads": 20,        // gRPC servicer threads + thread-prefetch fallback (MAX_WORKERS)
    "use_process_pool": true,  // parallel decode in worker processes (USE_PROCESS_POOL)
    "reader_processes": null,  // total decode processes; null => auto cpu-1 (READER_PROCESSES)
    "prefetch_processes": null,// background prefetch lane; null => auto half (PREFETCH_PROCESSES)
    "min_parallel_tiles": 2,   // min missing tiles before using the pool (MIN_PARALLEL_TILES)
    "prefetch_ahead_windows": 1,  // windows prefetched ahead / page fwd (PREFETCH_AHEAD_WINDOWS)
    "prefetch_behind_windows": 1, // windows prefetched behind / page back (PREFETCH_BEHIND_WINDOWS)
    "tile_duration_s": 60,     // range-path tile length (TILE_DURATION_S)
    "tile_cache_mb": 512,      // global tile-cache budget (TILE_CACHE_MB)
    "cache_ttl_s": 1800,       // discard tiles idle longer than this (CACHE_TTL_S)
    // ReaderProcessPool micro-benchmark ONLY (isolated prototype, test_reader_pool.py)
    "reader_pool_workers": 16, // worker PROCESSES for the isolated reader-pool benchmark
    "read_window_s": 300,      // FOREGROUND read size (what you request at once), e.g. 5 min
    "prefetch_chunk_s": 60,    // PREFETCH granularity each worker fetches ahead, e.g. 1 min
    "prefetch_ahead_chunks": 2 // how many prefetch chunks to keep scheduled ahead of reading
  },

  // --- crossover analysis only ---
  "crossover": {
    "compute_repeats_sweep": [1, 2, 4, 8, 16], // intensities to sweep
    "include_no_prefetch": true,
    "output_dir": "benchmark_results"          // where the curve artifact is written
  }
}
```

### ReaderProcessPool micro-benchmark knobs (isolated prototype only)

These four keys drive **only** `test_reader_pool.py`, which times the
`ReaderProcessPool` directly (not the wired-in server). They are the pool's own
foreground-read-size vs. prefetch-chunk-size knobs — deliberately separate, and
they mirror the concepts the server now exposes as first-class config:

| Knob | Meaning | Server equivalent |
|------|---------|-------------------|
| `read_window_s` | size of each **foreground** read (e.g. 5 min) | the client's `get_signal_range(t1, t2)` request size (per call) |
| `prefetch_chunk_s` | **prefetch** granularity — how big each background fetch is (e.g. 1 min) | the tile size, `TILE_DURATION_S` |
| `prefetch_ahead_chunks` | how many prefetch chunks to keep scheduled ahead of reading | the prefetch horizon, `PREFETCH_AHEAD_WINDOWS` |
| `reader_pool_workers` | worker **processes** doing the decode | `READER_PROCESSES` / `PREFETCH_PROCESSES` |

For the **wired-in server** benchmarks (use cases A/B/C) the equivalents are set
directly under `workload` (`prefetch_ahead_windows`, `tile_duration_s`,
`reader_processes`, ...) and passed through by `server_kwargs()`; the four keys
above are for the isolated pool measurement:
`pytest -m benchmark tests/test_reader_pool.py::test_reader_pool_streaming_prefetch -s`.

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
pytest -m benchmark tests/test_multitool_shared_session.py -s  # use case B: many tools, one session
pytest -m benchmark tests/test_file_manager.py        # in-process cache micro-benchmark
pytest -m benchmark tests/test_access_patterns.py     # sequential access (with sleep floor)
pytest -m benchmark tests/test_automated_processing.py# detector: native vs gRPC±prefetch
pytest -m crossover                                   # crossover-curve analysis

pytest -m benchmark --benchmark-json=benchmark_results/benchmark.json  # capture all suites
python -m tests.benchmark_report benchmark_results/benchmark.json      # -> benchmark_report.md

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

## Use case B: many tools, one session (`test_multitool_shared_session.py`)

This is the benchmark that shows the server's clearest win, and the one that
matches the pain point "I don't want to read + decrypt/decompress the same data
multiple times."

`num_tools` independent tools each process the **same** session. Their data
access overlaps by `tool_overlap` (1.0 = every tool reads the identical window
set; 0.0 = fully disjoint). Two scenarios run on the same plan:

- **`multitool_native_local`** — the baseline. Every tool decodes its own
  windows. An overlapping region is decrypted+decompressed **once per tool**, so
  the machine does `num_tools × num_chunks` decodes. (We give native its *best*
  case: all tools share one in-process `MefReader` and the OS page cache, so only
  the decode is duplicated — not the file open or the disk read.)
- **`multitool_grpc_shared_cache`** — all tools are clients of **one** server
  with **one** shared tile cache. The first tool to touch a region decodes it
  (cold); every other tool is served warm. Distinct decodes drop to
  `shared + num_tools × (num_chunks − shared)` where `shared = round(overlap ×
  num_chunks)` — i.e. **once** per overlapping region.

Run it and read the printed per-tool breakdown (`-s`):

```sh
./run_benchmarks.sh multitool -s
# or: pytest -m benchmark tests/test_multitool_shared_session.py -s
```

```
[multitool:grpc] per-tool wall time (s): 2.99, 0.36, 0.37, 0.37
[multitool:grpc] tool 0 (cold): 2.99s | tools 1..n (warm) mean: 0.37s
[multitool:grpc] distinct windows decoded once = 20 (native decodes 80)
```

The story: tool 0 pays the full cold decode; tools 1..N ride the shared cache
(only transport + their own compute remain). The advantage grows with
`num_tools` and with `tool_overlap`. **At `tool_overlap = 0` there is nothing to
reuse**, so the server only adds transport overhead and native wins — the same
benchmark reports this honestly if you set it.

## The crossover curve

`pytest -m crossover` sweeps `compute_repeats` and, at each level, times a **cold
single detector pass** for native-local, gRPC+prefetch, and (optionally) gRPC
no-prefetch. A fresh server is created per level so no cache carries over. It
writes an artifact to `crossover.output_dir` (default `benchmark_results/`):

- `crossover_curve.json` — full data + metadata
- `crossover_curve.md` — a table you can drop into the README

The **crossover point** is the first intensity where gRPC+prefetch ≤ native. With
few channels, decode is so cheap there is nothing to parallelize/hide and native
local can win at every intensity (crossover = `None`); the server+prefetch
advantage appears at high channel counts, where per-window decode is large enough
that decoding it **in parallel across processes** and overlapping it with
processing outweighs the gRPC transport cost.

With the process pool now the default, a representative 64 ch / 256 Hz / 21600 s
run (Apple M3 Max, 14 CPUs) crosses over immediately — gRPC+prefetch beats native
at **every** intensity:

| repeats | native (s) | gRPC+prefetch (s) | speedup |
| ---: | ---: | ---: | ---: |
| 1  | 2.01 | 1.51 | **1.34x** |
| 2  | 2.28 | 1.78 | 1.28x |
| 4  | 2.49 | 1.94 | 1.28x |
| 8  | 3.00 | 2.44 | 1.23x |
| 16 | 4.18 | 3.52 | 1.19x |

(Crossover = 1.) This is the use-case-C result the redesign targeted: real
parallel decode, hidden behind compute, tips a whole single pass in the server's
favour. Because each level is still a single cold pass, read the whole column
rather than any one row.

## Interpreting results

Map every number back to a use case (A/B/C above):

- **Native local** is the baseline everywhere: one process, read and compute
  serialized. For a *single* sequential pass it is the one to beat.
- **Shared cache (use case B)** is the server's strongest result: decode once,
  serve many. Look at the per-tool breakdown — cold tool vs warm tools — and at
  `distinct windows decoded` vs `native decodes`. The win scales with
  `num_tools × tool_overlap`.
- **gRPC + prefetch (use cases A, C)** decodes upcoming windows in the
  background, overlapping decode with the client's work (think-time or compute).
  It helps when that work is large relative to decode; otherwise it roughly ties
  or slightly loses to native.
- **gRPC no-prefetch** pays transport per window with no overlap and no reuse —
  expected to be the slowest; this is the classic "server slower than MefReader"
  case, kept as a reference point.
- MEF3 decode was **GIL-bound** in the legacy `pymef` backend used for these
  measurements, so thread prefetch could not decode in parallel (the current
  `mef3io` backend releases the GIL). The server decodes in **worker processes** by default
  (`ReaderProcessPool`); this is what lets prefetch overlap *parallel* decode with
  compute and is what tips use case C. `USE_PROCESS_POOL=0` reverts to the
  in-process thread path (hides latency + reuses cache only, no parallel decode).
- Results are **machine-specific** (CPU count, disk, OS page cache). Record the
  host when publishing numbers; `record_benchmark_setup` already attaches CPU
  count and the full setup to each result's `extra_info`.
