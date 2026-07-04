# Python client

The package ships a high-level client, `Mef3Client`, wrapping the gRPC API. All
data access is oriented in **channels and time**: open a file, inspect its
metadata, then read any channels over any `[start_uutc, end_uutc)` window. See
the [API reference](../api/client.md) for the full method signatures.

## Connecting

```python
from mef3io_server.client import Mef3Client

client = Mef3Client("localhost:50051")
```

## Opening a file and reading metadata

`open_file` returns the file's metadata as a dict. When the server runs in
Docker, pass the absolute path **as it exists on the host** — the server maps it
under `/host_root` automatically (see [Docker deployment](docker.md)).

```python
info = client.open_file("/path/to/file.mefd")

info["channel_names"]            # list[str]
info["channel_sampling_rates"]   # per-channel fs, parallel to channel_names
info["channel_start_uutc"]       # per-channel start times (uUTC)
info["channel_end_uutc"]         # per-channel end times (uUTC)
info["start_uutc"]               # global min start
info["end_uutc"]                 # global max end
info["duration_s"]               # recording span in seconds
```

`get_file_info` returns the same metadata for an already-open file without
re-opening it.

## Reading a signal range

`get_signal_range` reads any channels over any `[start_uutc, end_uutc)` window.
Times are microseconds (uUTC); the returned array is `float32`.

```python
t0 = info["start_uutc"]
res = client.get_signal_range(
    "/path/to/file.mefd",
    channels=["Ch1", "Ch2"],           # None reads all channels
    start_uutc=t0,
    end_uutc=t0 + 10_000_000,          # +10 s
)
res["array"]          # np.ndarray, shape (n_channels, n_samples), float32
res["channel_names"]  # channel order of the rows
res["fs"]             # sampling rate
```

The requested channels must share a sampling rate; reads past end-of-file are
NaN-padded. Repeated or overlapping requests are served from the tile cache, and
neighboring windows are prefetched in the background for smooth paging.

## Cleaning up

Close a file to release its reader session and purge its tiles from the shared
cache, and shut the client down when you are done:

```python
client.close_file("/path/to/file.mefd")
client.shutdown()
```
