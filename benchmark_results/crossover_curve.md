# Crossover curve: native-local vs gRPC+prefetch

- Dataset: 64 ch, 256 Hz, 21600 s
- Workload: 20 x 60 s windows, mode=compute, host CPUs=14
- Crossover (first repeats where prefetch <= native): **1**

| repeats | native (s) | gRPC+prefetch (s) | speedup vs native | gRPC no-prefetch (s) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 2.011 | 1.506 | 1.34x | 1.584 |
| 2 | 2.281 | 1.783 | 1.28x | 1.766 |
| 4 | 2.485 | 1.940 | 1.28x | 2.035 |
| 8 | 3.002 | 2.436 | 1.23x | 3.015 |
| 16 | 4.182 | 3.517 | 1.19x | 3.862 |
