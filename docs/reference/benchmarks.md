# Performance & benchmarks

The repository ships a benchmark suite (`pytest-benchmark`) that compares a
direct MEF read against the gRPC server, with and without prefetch, over a
generated dataset. The full write-up — methodology, access patterns, and
results — lives in
[`BENCHMARKS.md`](https://github.com/bnelair/mef3io-server/blob/main/BENCHMARKS.md).

## The three access patterns

| | Pattern | When the server wins |
|---|---|---|
| **A** | Interactive viewing (page around, think between reads) | Prefetch overlaps decode with the user's think-time, and the cache serves re-reads. |
| **B** | Repeated reads of overlapping ranges | The shared tile cache serves what has already been decoded. |
| **C** | Automated single-pass processing (a detector walks the whole recording once) | At high channel counts, parallel decode across worker processes overlaps decode with per-window compute. |

The **native reader in a loop is the baseline** in all three.

## Latest results

From the bundled run of 2026-07-05 (Apple M3 Max, 14 CPUs; 128 ch, 256 Hz,
21600 s file, 20 × 60 s windows). Results are machine-specific — regenerate for
your host with `./run_benchmarks.sh report`. The full per-scenario report lives
in
[`benchmark_results/benchmark_report.md`](https://github.com/bnelair/mef3io-server/blob/main/benchmark_results/benchmark_report.md).

| Use case | Scenario | Mean (s) | Speedup vs native |
| --- | --- | ---: | ---: |
| A | gRPC + prefetch | 7.27 | **1.02x** |
| A | Native local (baseline) | 7.43 | — |
| A | gRPC, no prefetch | 7.50 | 0.99x |
| B | gRPC shared tile cache | 3.08 | **2.04x** |
| B | Native local (baseline) | 6.28 | — |
| C | Native local (baseline) | 1.64 | — |
| C | gRPC, no prefetch | 1.76 | 0.93x |
| C | gRPC + prefetch | 2.02 | 0.81x |

!!! warning "Use case C: trust the crossover sweep, not the bundled single shot"
    The use-case-C rows above are single-shot (`rounds=1`) and include one-time
    process-pool spawn cost; when the full suite runs several servers
    back-to-back their worker pools can oversubscribe the CPUs, so this figure
    can swing ~2x run-to-run and even flip sign. The isolated crossover sweep
    (fresh server per level) is the authoritative use-case-C result — see the
    crossover-curve section of
    [`BENCHMARKS.md`](https://github.com/bnelair/mef3io-server/blob/main/BENCHMARKS.md).

## Running the benchmarks

Benchmarks generate large data and run a real server, so they are excluded from
normal test runs. Opt in explicitly:

```bash
pip install -e ".[dev]"
pytest -m benchmark
```

Useful options:

```bash
pytest -m benchmark --benchmark-only            # timing only
pytest -m benchmark --benchmark-save=baseline   # save a baseline
pytest -m benchmark --benchmark-compare         # compare to the last saved run
pytest -m benchmark -s                          # print the setup block per benchmark
```

Each benchmark records the file/dataset and server config it ran under (attached
to the result's `extra_info`), so results are self-describing. Results are
machine-specific — record the host when publishing numbers.
