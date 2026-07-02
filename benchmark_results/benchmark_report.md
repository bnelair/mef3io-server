# Benchmark report

- Generated: 2026-07-02T19:07:36.988591+00:00
- Host: Apple M3 Max · Darwin · 14 CPUs
- Benchmarks: 10

Times are wall-clock seconds (lower is better). *Speedup* is the use-case native-local baseline mean divided by this benchmark's mean (>1 = faster than native, <1 = slower).

## Summary

| Use case | Scenario | Mean (s) | Median (s) | StdDev (s) | Speedup vs native |
| --- | --- | ---: | ---: | ---: | ---: |
| A | gRPC + prefetch | 7.523 | 7.523 | 0.000 | 1.39x |
| A | gRPC, no prefetch | 8.155 | 8.155 | 0.000 | 1.29x |
| A | Native local (baseline) | 10.485 | 10.485 | 0.000 | — (baseline) |
| B | gRPC shared tile cache | 3.375 | 3.375 | 0.000 | 4.43x |
| B | Native local (baseline) | 14.962 | 14.962 | 0.000 | — (baseline) |
| C | gRPC, no prefetch | 2.015 | 2.015 | 0.000 | 1.79x |
| C | gRPC + prefetch | 2.184 | 2.184 | 0.000 | 1.65x |
| C | Native local (baseline) | 3.602 | 3.602 | 0.000 | — (baseline) |
| infra | In-process tile cache, prefetch OFF | 0.002 | 0.002 | 0.000 | n/a |
| infra | In-process tile cache, prefetch ON | 0.003 | 0.003 | 0.001 | n/a |

## Use case A — Interactive data viewing (scroll/jump with think-time)

### gRPC + prefetch  
`test_grpc_sequential_forward_with_prefetch`

Same forward scroll via the server using get_signal_range; background tile prefetch loads the next tiles during the think-time so the next window is already warm.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s, prefetch_ahead=1
- Timing: mean **7.523 s**, median 7.523 s, min 7.523 s, max 7.523 s (rounds=1)
- Speedup vs native: **1.39x** (faster)

### gRPC, no prefetch  
`test_grpc_sequential_forward_no_prefetch`

Same forward scroll via the server but with prefetch off: each window pays decode + transport with nothing hidden behind think-time.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s, prefetch_ahead=0
- Timing: mean **8.155 s**, median 8.155 s, min 8.155 s, max 8.155 s (rounds=1)
- Speedup vs native: **1.29x** (faster)

### Native local (baseline)  
`test_baseline_direct_mef_reader`

Scroll forward window-by-window reading directly with MefReader, with a fixed think-time between windows. No server, no cache, no prefetch.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, think-time=0.3 s
- Timing: mean **10.485 s**, median 10.485 s, min 10.485 s, max 10.485 s (rounds=1)
- Speedup vs native: — (this is the baseline)

## Use case B — Batch: many tools, one session (shared cache)

### gRPC shared tile cache  
`test_multitool_grpc_shared_cache`

N tools are clients of ONE server with ONE shared tile cache. The first tool to touch a region decodes it (cold); every other tool is served warm — each overlapping region is decoded once, not once per tool.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, num_tools=4, overlap=1.0, compute_repeats=1, prefetch_ahead=1
- Timing: mean **3.375 s**, median 3.375 s, min 3.375 s, max 3.375 s (rounds=1)
- Speedup vs native: **4.43x** (faster)

### Native local (baseline)  
`test_multitool_native_local`

N independent tools each process the SAME session locally. Overlapping regions are decrypted+decompressed once PER TOOL (num_tools x num_chunks decodes).

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, num_tools=4, overlap=1.0, compute_repeats=1
- Timing: mean **14.962 s**, median 14.962 s, min 14.962 s, max 14.962 s (rounds=1)
- Speedup vs native: — (this is the baseline)

## Use case C — Automated single-pass processing (detector)

### gRPC, no prefetch  
`test_processing_grpc_no_prefetch`

Same single detector pass via the server with prefetch off: transport per window, no overlap (the classic 'server slower than MefReader' case).

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1, prefetch_ahead=0
- Timing: mean **2.015 s**, median 2.015 s, min 2.015 s, max 2.015 s (rounds=1)
- Speedup vs native: **1.79x** (faster)

### gRPC + prefetch  
`test_processing_grpc_with_prefetch`

Same single detector pass via get_signal_range; the server prefetches upcoming tiles so decode overlaps the client's per-window compute.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1, prefetch_ahead=1
- Timing: mean **2.184 s**, median 2.184 s, min 2.184 s, max 2.184 s (rounds=1)
- Speedup vs native: **1.65x** (faster)

### Native local (baseline)  
`test_processing_native_local`

One detector walks the whole recording once via MefReader; per-window read and compute are serialized.

- Setup: 128 ch, 256 Hz, 21600 s file, 20 x 60 s windows, compute_repeats=1
- Timing: mean **3.602 s**, median 3.602 s, min 3.602 s, max 3.602 s (rounds=1)
- Speedup vs native: — (this is the baseline)

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

