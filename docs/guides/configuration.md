# Configuration

The server is configured entirely through environment variables. Run it with:

```bash
python -m mef3io_server.server
```

## Server

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `50051` | gRPC server port. |

## Tile cache

| Variable | Default | Meaning |
|---|---|---|
| `TILE_DURATION_S` | `60` | Tile length in seconds for timestamp-based access. |
| `TILE_CACHE_MB` | `512` | Global tile-cache budget in MB, shared across all open files. |
| `CACHE_TTL_S` | `1800` | Discard tiles not accessed within this many seconds; a finished session (e.g. a detector that moved on) is freed even before the byte budget is hit. `0` disables idle expiry. |

## Parallel decode

Cold reads and prefetch decode in worker processes, each holding its own
[mef3io](https://github.com/bnelair/mef3io) session, so decode is genuinely
parallel across channels.

| Variable | Default | Meaning |
|---|---|---|
| `USE_PROCESS_POOL` | `true` | Decode cold reads / prefetch in worker processes. Set `0` for the in-process thread path. |
| `READER_PROCESSES` | auto (`cpu_count - 1`) | Total decode worker processes. |
| `PREFETCH_PROCESSES` | auto (half) | How many of those form the background prefetch lane; the rest (always ≥ 1) are the reserved foreground lane, so prefetch can never starve an interactive read. |
| `MIN_PARALLEL_TILES` | `2` | Minimum missing tiles before a cold read fans out to the pool; smaller reads stay in-process, where IPC is not worth it. |

## Prefetch / paging

Look-ahead and look-behind are measured in **windows** of the request's own size.

| Variable | Default | Meaning |
|---|---|---|
| `PREFETCH_AHEAD_WINDOWS` | `1` | Windows to prefetch after the request (page forward). |
| `PREFETCH_BEHIND_WINDOWS` | `1` | Windows to prefetch before the request (page backward). |
| `MAX_WORKERS` | `4` | Thread-pool size for the in-process prefetch fallback used when `USE_PROCESS_POOL=0`. |

## Examples

```bash
# Interactive viewing — page both ways.
PORT=50052 PREFETCH_AHEAD_WINDOWS=1 PREFETCH_BEHIND_WINDOWS=1 \
  python -m mef3io_server.server

# Detector / automated single pass — stream forward, no look-behind, deeper look-ahead.
PREFETCH_AHEAD_WINDOWS=3 PREFETCH_BEHIND_WINDOWS=0 \
  python -m mef3io_server.server
```

## Logging

Logs are written to both the console and a file
(`logs/server_YYYY-MM-DDTHH-MM-SS.log`). The level is set in `app_config.json`
(default: `INFO`):

```json
{
  "log_level": "DEBUG"
}
```

Available levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
