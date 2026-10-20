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

## A whole-recording overview (`get_envelope`)

```python
env = client.get_envelope(path)                  # all channels, whole recording
env["ch1"]["start_uutc"], env["ch1"]["end_uutc"]  # one row per MEF block
env["ch1"]["min"], env["ch1"]["max"]              # in the channel's units
```

The minimum and maximum of every MEF data block, read from the block index
alone: nothing is decoded, so a whole day costs reading the small index files.
The resolution is the block length (10 s for files mef3io wrote, by default).
Gaps are absent rows, and a block whose writer left its extremes unset gives
NaN. Pass `channels` and `start_uutc`/`end_uutc` to narrow it.

## Serving at a lower rate (`cache_fs`)

For viewers, a session can ask the server to serve, cache and prefetch its
reads at a lower rate:

```python
info = client.open_file("/path/to/file.mefd", cache_fs=128)
info["channel_served_sampling_rates"]  # the rate each channel is served at
data = client.get_signal_range(path, channels, t0, t1)  # data["fs"] == 128
```

- **Order-preserving min-max.** Each pair of output samples covers `2 / cache_fs`
  seconds of the original and holds that stretch's minimum and maximum, in the
  order they occurred. A spike is never lost (it is the minimum or maximum of
  its stretch), and the trace keeps its shape. Every value is a real sample.
- **Cached at that rate.** At 4096 Hz -> 128 Hz the cache holds 32 times more
  time in the same memory, and prefetch works the same way.
- **The server never upsamples.** If a channel is recorded below `cache_fs`,
  the open fails (`file_opened` is False, and `error_message` names the
  channels). Pass `allow_mixed_rates=True` to serve such channels at their own
  rate instead; `channel_served_sampling_rates` then tells you which.
- The setting belongs to this client's session: another tool reading the same
  file at another rate, or at the original rate, is unaffected. Opening again
  without `cache_fs` goes back to the original rate.
- If the session expires while the file stays open (another client still uses
  it), the next read restores it with the same rate.
- A refused open (`file_opened` is False because of the rate check) leaves an
  existing session of this client as it was; it does not close the file.
- `cache_fs` needs a client id, which `Mef3Client` always sends. A server older
  than this feature makes `open_file(..., cache_fs=...)` raise instead of
  silently serving the original rate.
- Buckets are cut per cache tile. When the original rate is not a whole
  multiple of `cache_fs`, a bucket at a tile edge can be one original sample
  shorter or longer than its neighbours; no sample is lost or counted twice.

## Exactly as many points as the view has pixels (`n_points`)

```python
data = client.get_signal_range(path, channels, t0, t1, n_points=584)
data["array"].shape   # (len(channels), 584)
data["fs"]            # 584 / window length in seconds
```

- **MinMaxLTTB.** The window is cut into `n_points` buckets. Each point is a
  real sample, its bucket's minimum or maximum, chosen to keep the line's shape
  (Largest-Triangle-Three-Buckets). One point per bucket means one of a
  bucket's two extremes is left out, but each channel's overall maximum and
  minimum in the window are always among the points, so the amplitude is never
  understated (if both fall in one bucket, the larger excursion is kept).
- The requested channels must be served at one rate, as for any read; with
  `allow_mixed_rates`, request channels of different rates separately.
- **The cheapest source with detail to spare.** If the session has a
  `cache_fs` and it holds at least `2 * n_points` samples in the window, the
  points come from the cached data with no decoding. When you zoom in further,
  they come from the original rate.
- **Never upsampled.** When the window holds `n_points` samples or fewer at
  the original rate, they are returned unchanged.
- Without `n_points`, reads come back at the session's rate, as before.

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
