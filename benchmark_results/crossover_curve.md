# Crossover curve: native-local vs gRPC+prefetch

- Dataset: 128 ch, 256 Hz, 21600 s
- Workload: 20 x 60 s windows, mode=compute, host CPUs=14
- Crossover (first repeats where prefetch <= native): **1**

| repeats | native (s) | gRPC+prefetch (s) | speedup vs native | gRPC no-prefetch (s) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 3.984 | 2.138 | 1.86x | 2.065 |
| 2 | 4.000 | 2.867 | 1.40x | 2.176 |
| 4 | 5.002 | 4.739 | 1.06x | 2.540 |
| 8 | 4.937 | 4.334 | 1.14x | 3.320 |
| 16 | 6.179 | 4.287 | 1.44x | 4.330 |
