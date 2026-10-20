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

### Sessions

Each `Mef3Client` sends a client id with every call (a random one, or
`Mef3Client(address, client_id="...")`). The server keeps **one session per
client and file**:

- opening the same file again from the same client reuses its session, so a
  tool that re-opens on every page view does not pile up sessions;
- two clients (two tools, two users) have separate sessions on the same file,
  while sharing the server's open reader and its decoded tiles;
- a client can read a file another client opened, so tools can share a file
  one of them opened;
- a session idle longer than `SESSION_TTL_S` (30 min by default) is closed.

The id is a name tag, not authentication. A web app serving several users can
pass one id per user. Create one `Mef3Client` per tool or user and keep it: each new
client object has a new random id, so creating one per request leaves a
session per request until they expire.

Closing a file ends this client's session (`file_opened` in the reply says
whether the file is still open for other clients). The reader is released, and the
file's tiles purged from the shared cache, when no other session uses the file.
Shut the client down when you are done:

```python
client.close_file("/path/to/file.mefd")
client.shutdown()
```
