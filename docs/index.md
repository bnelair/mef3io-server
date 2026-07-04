# mef3io-server

**A gRPC server for efficient, concurrent access to MEF 3.0 recordings —
oriented purely in channels and time.**

[MEF 3.0](https://msel.mayo.edu/codes.html) (Multiscale Electrophysiology
Format) is a compressed, encryptable format for long-term electrophysiology
recordings. mef3io-server puts a network service in front of it: open a file,
read its metadata, then request any channels over any `[start_uutc, end_uutc)`
window. Decode is backed by [mef3io](https://github.com/bnelair/mef3io), served
from a shared per-channel tile cache with parallel decode across worker
processes and configurable window prefetch.

## Why mef3io-server

- **Channels-and-time only** — there is no segment grid and no server-side
  channel selection state; each request is self-contained and self-describing.
- **Shared tile cache** — a single byte-budgeted, per-channel cache across all
  open files, with an idle TTL so finished sessions free memory before the
  budget is hit.
- **Parallel decode** — cold reads and prefetch fan out to worker processes,
  each holding its own [mef3io](https://github.com/bnelair/mef3io) session, so
  decode is genuinely parallel across channels.
- **Smooth paging** — configurable look-ahead/behind prefetch warms the next
  and previous window so navigation is a cache hit.
- **Deployable** — configured entirely via environment variables, with a
  prebuilt Docker image published to GHCR.

## Quick taste (Python)

```python
from mef3io_server.client import Mef3Client

client = Mef3Client("localhost:50051")

info = client.open_file("/path/to/file.mefd")
t0 = info["start_uutc"]

res = client.get_signal_range(
    "/path/to/file.mefd",
    channels=["Ch1", "Ch2"],           # or None for all channels
    start_uutc=t0,
    end_uutc=t0 + 10_000_000,          # +10 s
)
print("Shape:", res["shape"], "fs:", res["fs"])

client.close_file("/path/to/file.mefd")
client.shutdown()
```

## Where to go

| | |
|---|---|
| [Install](install.md) | pip install, the prebuilt Docker image, building locally |
| [Quick start](quickstart.md) | launch the server and read your first window |
| [Python client](guides/client.md) | the `Mef3Client` interface end to end |
| [Configuration](guides/configuration.md) | every environment variable, explained |
| [Docker deployment](guides/docker.md) | the `/host_root` mount and container setup |
| [gRPC API](reference/grpc_api.md) | RPCs and message shapes |
| [Performance & benchmarks](reference/benchmarks.md) | when the server beats a direct read |

## Status and scope

Read access (metadata + signal ranges) is complete and released. Decode is
handled by [mef3io](https://github.com/bnelair/mef3io), the C++-backed
successor to the legacy pymef/mef_tools stack, imported through its drop-in
`MefReader` compatibility layer.

mef3io-server is developed at the Mayo Clinic BNEL (Bioelectronics
Neurophysiology and Engineering Lab).
