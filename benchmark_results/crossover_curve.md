# Crossover curve: native-local vs gRPC+prefetch

- Dataset: 128 ch, 256 Hz, 21600 s
- Workload: 20 x 60 s windows, mode=compute, host CPUs=12
- Crossover (first repeats where prefetch <= native): **8**

| repeats | native (s) | gRPC+prefetch (s) | speedup vs native | gRPC no-prefetch (s) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 2.955 | 3.460 | 0.85x | 4.001 |
| 2 | 4.268 | 5.240 | 0.81x | 5.367 |
| 4 | 7.053 | 7.511 | 0.94x | 8.135 |
| 8 | 12.738 | 12.265 | 1.04x | 12.927 |
| 16 | 24.810 | 26.514 | 0.94x | 23.948 |
