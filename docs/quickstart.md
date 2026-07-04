# Quick start

## 1. Launch the server

Run the server as a module (see [Configuration](guides/configuration.md) for the
full set of environment variables):

```bash
python -m mef3io_server.server
```

Or launch it programmatically:

```python
import grpc
from concurrent import futures

from mef3io_server.server.mef3_server import gRPCMef3Server
from mef3io_server.server.file_manager import FileManager
from mef3io_server.protobufs.gRPCMef3Server_pb2_grpc import (
    add_gRPCMef3ServerServicer_to_server,
)

file_manager = FileManager(tile_cache_bytes=512 * 1024 * 1024, prefetch_ahead_windows=1)
server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
add_gRPCMef3ServerServicer_to_server(gRPCMef3Server(file_manager), server)
server.add_insecure_port("[::]:50051")
server.start()
server.wait_for_termination()
```

## 2. Read your first window

Every data call is oriented in **channels and time**: open a file, inspect its
metadata, then request any channels over any `[start_uutc, end_uutc)` window
(microseconds, uUTC).

```python
from mef3io_server.client import Mef3Client

client = Mef3Client("localhost:50051")

# Open a file and inspect its metadata.
info = client.open_file("/path/to/file.mefd")
print("Channels:", info["channel_names"])
print("Per-channel fs:", info["channel_sampling_rates"])
print("Recording span:", info["start_uutc"], info["end_uutc"], info["duration_s"])

# Read the first 10 s of two channels (channels=None reads all channels).
t0 = info["start_uutc"]
res = client.get_signal_range(
    "/path/to/file.mefd",
    channels=["Ch1", "Ch2"],
    start_uutc=t0,
    end_uutc=t0 + 10_000_000,
)
print(f"Data shape: {res['shape']}, dtype: {res['dtype']}, fs: {res['fs']}")

client.close_file("/path/to/file.mefd")
client.shutdown()
```

The server serves from a per-channel tile cache (reading only what is missing),
decodes missing tiles in parallel across processes, and prefetches neighboring
windows so paging forward and backward is a cache hit.

## Next steps

- [Python client](guides/client.md) — the full `Mef3Client` interface.
- [Configuration](guides/configuration.md) — tune the cache, prefetch, and
  decode pool.
- [Docker deployment](guides/docker.md) — run it in a container against host data.
