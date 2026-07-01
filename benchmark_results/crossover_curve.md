# Crossover curve: native-local vs gRPC+prefetch

- Dataset: 64 ch, 256 Hz, 21600 s
- Workload: 20 x 60 s windows, mode=compute, host CPUs=14
- Crossover (first repeats where prefetch <= native): **None**

| repeats | native (s) | gRPC+prefetch (s) | speedup vs native | gRPC no-prefetch (s) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 2.117 | 2.640 | 0.80x | 2.238 |
| 2 | 2.157 | 2.749 | 0.78x | 2.997 |
| 4 | 3.152 | 3.901 | 0.81x | 3.135 |
| 8 | 3.264 | 3.918 | 0.83x | 3.247 |
| 16 | 4.209 | 5.242 | 0.80x | 4.499 |
