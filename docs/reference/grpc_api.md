# gRPC API

The server exposes a gRPC API. The service and message definitions live in
[`mef3io_server/protobufs/gRPCMef3Server.proto`](https://github.com/bnelair/mef3io-server/blob/main/mef3io_server/protobufs/gRPCMef3Server.proto).

Every data access is oriented in **channels and time** — there is no segment
grid and no server-side channel selection; each request is self-contained.

## Key RPCs

| RPC | Client method | Purpose |
|---|---|---|
| `GetSignalRange` | `get_signal_range(file_path, channels, start_uutc, end_uutc)` | Read any channels over any `[start_uutc, end_uutc)` window; streams `float32`. `channels=None` means all channels. |
| `FileInfo` | `get_file_info(file_path)` | Metadata: `channel_names`, `channel_sampling_rates`, `channel_start_uutc`, `channel_end_uutc` (parallel per-channel arrays) plus the global `start_uutc` / `end_uutc` / `duration_s`. |
| `OpenFile` | `open_file(file_path, cache_fs=None, allow_mixed_rates=False)` | Open a file for this client and return its metadata. With `cache_fs`, this client's reads are served, cached and prefetched at that rate (order-preserving min-max); `channel_served_sampling_rates` lists the rate per channel. |
| `CloseFile` | `close_file(file_path)` | End this client's session on the file. The reader is released, and the file's tiles purged from the shared cache, when no other client's session uses it. |
| `ListOpenFiles` | `list_open_files()` | List the currently open files. |

## Streaming

`GetSignalRange` streams the result in ~2.5 MB `SignalChunk` messages carrying
raw `float32` bytes plus the shape, dtype, channel names, and the uUTC span of
each chunk. The client reassembles them into a single `(n_channels, n_samples)`
array. See the [Python client guide](../guides/client.md) for usage.
