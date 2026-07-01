# Benchmark report

- Generated: 2026-07-01T17:17:16.706691+00:00
- Host: Apple M3 Max · Darwin · 14 CPUs
- Benchmarks: 10

Times are wall-clock seconds (lower is better). *Speedup* is the use-case native-local baseline mean divided by this benchmark's mean (>1 = faster than native, <1 = slower).

## Summary

| Use case | Scenario | Mean (s) | Median (s) | StdDev (s) | Speedup vs native |
| --- | --- | ---: | ---: | ---: | ---: |
| A | Native local (baseline) | 8.646 | 8.646 | 0.000 | — (baseline) |
| A | gRPC, no prefetch | 8.691 | 8.691 | 0.000 | 0.99x |
| A | gRPC + prefetch | 8.719 | 8.719 | 0.000 | 0.99x |
| B | gRPC shared tile cache | 4.428 | 4.428 | 0.000 | 2.17x |
| B | Native local (baseline) | 9.610 | 9.610 | 0.000 | — (baseline) |
| C | Native local (baseline) | 2.095 | 2.095 | 0.000 | — (baseline) |
| C | gRPC, no prefetch | 2.667 | 2.667 | 0.000 | 0.79x |
| C | gRPC + prefetch | 3.294 | 3.294 | 0.000 | 0.64x |
| infra | In-process tile cache, prefetch OFF | 0.002 | 0.002 | 0.000 | n/a |
| infra | In-process tile cache, prefetch ON | 0.022 | 0.024 | 0.004 | n/a |

## Use case A — Interactive data viewing (scroll/jump with think-time)

### Native local (baseline)  
`test_baseline_direct_mef_reader`

Scroll forward window-by-window reading directly with MefReader, with a fixed think-time between windows. No server, no cache, no prefetch.

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s
- Timing: mean **8.646 s**, median 8.646 s, min 8.646 s, max 8.646 s (rounds=1)
- Speedup vs native: — (this is the baseline)

### gRPC, no prefetch  
`test_grpc_sequential_forward_no_prefetch`

Same forward scroll via the server but with prefetch off: each window pays decode + transport with nothing hidden behind think-time.

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s, n_prefetch=0
- Timing: mean **8.691 s**, median 8.691 s, min 8.691 s, max 8.691 s (rounds=1)
- Speedup vs native: **0.99x** (slower)

### gRPC + prefetch  
`test_grpc_sequential_forward_with_prefetch`

Same forward scroll via the server using get_signal_range; background tile prefetch loads the next tiles during the think-time so the next window is already warm.

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s, n_prefetch=1
- Timing: mean **8.719 s**, median 8.719 s, min 8.719 s, max 8.719 s (rounds=1)
- Speedup vs native: **0.99x** (slower)

## Use case B — Batch: many tools, one session (shared cache)

### gRPC shared tile cache  
`test_multitool_grpc_shared_cache`

N tools are clients of ONE server with ONE shared tile cache. The first tool to touch a region decodes it (cold); every other tool is served warm — each overlapping region is decoded once, not once per tool.

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, num_tools=4, overlap=1.0, compute_repeats=1, n_prefetch=5
- Timing: mean **4.428 s**, median 4.428 s, min 4.428 s, max 4.428 s (rounds=1)
- Speedup vs native: **2.17x** (faster)

### Native local (baseline)  
`test_multitool_native_local`

N independent tools each process the SAME session locally. Overlapping regions are decrypted+decompressed once PER TOOL (num_tools x num_chunks decodes).

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, num_tools=4, overlap=1.0, compute_repeats=1
- Timing: mean **9.610 s**, median 9.610 s, min 9.610 s, max 9.610 s (rounds=1)
- Speedup vs native: — (this is the baseline)

## Use case C — Automated single-pass processing (detector)

### Native local (baseline)  
`test_processing_native_local`

One detector walks the whole recording once via MefReader; per-window read and compute are serialized.

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1
- Timing: mean **2.095 s**, median 2.095 s, min 2.095 s, max 2.095 s (rounds=1)
- Speedup vs native: — (this is the baseline)

### gRPC, no prefetch  
`test_processing_grpc_no_prefetch`

Same single detector pass via the server with prefetch off: transport per window, no overlap (the classic 'server slower than MefReader' case).

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1, n_prefetch=0
- Timing: mean **2.667 s**, median 2.667 s, min 2.667 s, max 2.667 s (rounds=1)
- Speedup vs native: **0.79x** (slower)

### gRPC + prefetch  
`test_processing_grpc_with_prefetch`

Same single detector pass via get_signal_range; the server prefetches upcoming tiles so decode overlaps the client's per-window compute.

- Setup: 64 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1, n_prefetch=5
- Timing: mean **3.294 s**, median 3.294 s, min 3.294 s, max 3.294 s (rounds=1)
- Speedup vs native: **0.64x** (slower)

## Infrastructure micro-benchmarks (not a user-facing use case)

### In-process tile cache, prefetch OFF  
`test_no_prefetch_real_file`

Same in-process tile-cache access with prefetch disabled — a floor for how fast the raw read path is with no background help.

- Setup: 64 ch, 256 Hz, 300 s file, 5 x 60 s windows, n_prefetch=0
- Timing: mean **0.002 s**, median 0.002 s, min 0.001 s, max 0.002 s (rounds=5)

### In-process tile cache, prefetch ON  
`test_with_prefetch_real_file`

FileManager.read_signal_range over a few sequential windows, in-process (no gRPC), with tile prefetch enabled.

- Setup: 64 ch, 256 Hz, 300 s file, 5 x 60 s windows, n_prefetch=10
- Timing: mean **0.022 s**, median 0.024 s, min 0.015 s, max 0.026 s (rounds=5)

