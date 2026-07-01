# Crossover curve: native-local vs gRPC+prefetch

- Dataset: 8 ch, 256 Hz, 1320 s
- Workload: 20 x 60 s windows, mode=compute, host CPUs=14
- Crossover (first repeats where prefetch <= native): **None**

| repeats | native (s) | gRPC+prefetch (s) | speedup vs native | gRPC no-prefetch (s) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 0.332 | 0.381 | 0.87x | 0.392 |
| 2 | 0.336 | 0.374 | 0.90x | 0.433 |
| 4 | 0.355 | 0.447 | 0.79x | 0.417 |
| 8 | 0.398 | 0.432 | 0.92x | 0.492 |
| 16 | 0.474 | 0.538 | 0.88x | 0.562 |
