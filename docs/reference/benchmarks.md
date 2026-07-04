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
