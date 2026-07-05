# Benchmark report

- Generated: 2026-07-05T04:39:34.727199+00:00
- Host: Apple M3 Max · Darwin · 14 CPUs
- Benchmarks: 10

Times are wall-clock seconds (lower is better). *Speedup* is the use-case native-local baseline mean divided by this benchmark's mean (>1 = faster than native, <1 = slower).

## Summary

| Use case | Scenario | Mean (s) | Median (s) | StdDev (s) | Speedup vs native |
| --- | --- | ---: | ---: | ---: | ---: |
| A | gRPC + prefetch | 7.273 | 7.273 | 0.000 | 1.02x |
| A | Native local (baseline) | 7.429 | 7.429 | 0.000 | — (baseline) |
| A | gRPC, no prefetch | 7.499 | 7.499 | 0.000 | 0.99x |
| B | gRPC shared tile cache | 3.082 | 3.082 | 0.000 | 2.04x |
| B | Native local (baseline) | 6.277 | 6.277 | 0.000 | — (baseline) |
| C | Native local (baseline) | 1.638 | 1.638 | 0.000 | — (baseline) |
| C | gRPC, no prefetch | 1.760 | 1.760 | 0.000 | 0.93x |
| C | gRPC + prefetch | 2.016 | 2.016 | 0.000 | 0.81x |
| infra | In-process tile cache, prefetch OFF | 0.002 | 0.002 | 0.000 | n/a |
| infra | In-process tile cache, prefetch ON | 0.003 | 0.003 | 0.001 | n/a |

## Use case A — Interactive data viewing (scroll/jump with think-time)

### gRPC + prefetch  
`test_grpc_sequential_forward_with_prefetch`

Same forward scroll via the server using get_signal_range; background tile prefetch loads the next tiles during the think-time so the next window is already warm.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s, prefetch_ahead=1
- Timing: mean **7.273 s**, median 7.273 s, min 7.273 s, max 7.273 s (rounds=1)
- Speedup vs native: **1.02x** (faster)

### Native local (baseline)  
`test_baseline_direct_mef_reader`

Scroll forward window-by-window reading directly with MefReader, with a fixed think-time between windows. No server, no cache, no prefetch.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s
- Timing: mean **7.429 s**, median 7.429 s, min 7.429 s, max 7.429 s (rounds=1)
- Speedup vs native: — (this is the baseline)

### gRPC, no prefetch  
`test_grpc_sequential_forward_no_prefetch`

Same forward scroll via the server but with prefetch off: each window pays decode + transport with nothing hidden behind think-time.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s, prefetch_ahead=0
- Timing: mean **7.499 s**, median 7.499 s, min 7.499 s, max 7.499 s (rounds=1)
- Speedup vs native: **0.99x** (slower)

## Use case B — Batch: many tools, one session (shared cache)

### gRPC shared tile cache  
`test_multitool_grpc_shared_cache`

N tools are clients of ONE server with ONE shared tile cache. The first tool to touch a region decodes it (cold); every other tool is served warm — each overlapping region is decoded once, not once per tool.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, num_tools=4, overlap=1.0, compute_repeats=1, prefetch_ahead=1
- Timing: mean **3.082 s**, median 3.082 s, min 3.082 s, max 3.082 s (rounds=1)
- Speedup vs native: **2.04x** (faster)

### Native local (baseline)  
`test_multitool_native_local`

N independent tools each process the SAME session locally. Overlapping regions are decrypted+decompressed once PER TOOL (num_tools x num_chunks decodes).

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, num_tools=4, overlap=1.0, compute_repeats=1
- Timing: mean **6.277 s**, median 6.277 s, min 6.277 s, max 6.277 s (rounds=1)
- Speedup vs native: — (this is the baseline)

## Use case C — Automated single-pass processing (detector)

### Native local (baseline)  
`test_processing_native_local`

One detector walks the whole recording once via MefReader; per-window read and compute are serialized.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1
- Timing: mean **1.638 s**, median 1.638 s, min 1.638 s, max 1.638 s (rounds=1)
- Speedup vs native: — (this is the baseline)

### gRPC, no prefetch  
`test_processing_grpc_no_prefetch`

Same single detector pass via the server with prefetch off: transport per window, no overlap (the classic 'server slower than MefReader' case).

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1, prefetch_ahead=0
- Timing: mean **1.760 s**, median 1.760 s, min 1.760 s, max 1.760 s (rounds=1)
- Speedup vs native: **0.93x** (slower)

### gRPC + prefetch  
`test_processing_grpc_with_prefetch`

Same single detector pass via get_signal_range; the server prefetches upcoming tiles so decode overlaps the client's per-window compute.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1, prefetch_ahead=1
- Timing: mean **2.016 s**, median 2.016 s, min 2.016 s, max 2.016 s (rounds=1)
- Speedup vs native: **0.81x** (slower)

## Infrastructure micro-benchmarks (not a user-facing use case)

### In-process tile cache, prefetch OFF  
`test_no_prefetch_real_file`

Same in-process tile-cache access with prefetch disabled — a floor for how fast the raw read path is with no background help.

- Setup: 64 ch, 256 Hz, 300 s file, 5 x 60 s windows, prefetch_ahead=0
- Timing: mean **0.002 s**, median 0.002 s, min 0.002 s, max 0.002 s (rounds=5)

### In-process tile cache, prefetch ON  
`test_with_prefetch_real_file`

FileManager.read_signal_range over a few sequential windows, in-process (no gRPC), with tile prefetch enabled.

- Setup: 64 ch, 256 Hz, 300 s file, 5 x 60 s windows, prefetch_ahead=1
- Timing: mean **0.003 s**, median 0.003 s, min 0.003 s, max 0.004 s (rounds=5)

