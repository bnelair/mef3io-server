# mef3io-server

A gRPC server for efficient, concurrent access to MEF3 (Multiscale Electrophysiology Format) files. Every data call is oriented purely in **channels and time**: open a file, read its metadata, then request any channels over any `[start_uutc, end_uutc)` window. Backed by a byte-budgeted per-channel tile cache, parallel decode across worker processes, and configurable window prefetch. Designed for scalable neurophysiology data streaming and analysis.

## Features
- gRPC API for remote MEF3 file access, oriented purely in **channels and time**
- Shared, byte-budgeted per-channel **tile cache** with an idle TTL
- **Parallel MEF3 decode** across worker processes (pymef decode is GIL-bound)
- Configurable **window look-ahead/behind prefetch** for smooth paging
- Configurable via environment variables or Docker
- Ready for deployment in Docker and CI/CD pipelines

## Installation

### Requirements
- Python 3.10+ (3.12 recommended)
- (Optional) Docker for containerized deployment

### Local Setup
Clone the repository and install the package (dependencies come from `pyproject.toml`):
```sh
pip install .
```
For development (tests, benchmarks, build tooling):
```sh
pip install -e .[dev]
```

### Docker

> **Important — data mounting (`/host_root`).** 
> 
> When the server runs inside a
> container, it automatically rewrites every absolute file path you request to
> `/host_root/<that path>`. You therefore **must bind-mount your host filesystem into
> the container at `/host_root`**, or no files will be found. 
> 
> You can mount only the specific directories containing your MEF3 files, but **keep their absolute paths** so the server can resolve them correctly. For example, if your data lives in `/data/recordings` on the host, mount it as:
> 
> _-v /data/recordings:/host_root/data/recordings:ro_
> 
> 
>See [Accessing MEF3 files from the container](#accessing-mef3-files-from-the-container) below.
> 

#### Pull the prebuilt image (recommended)
Released images are published to the GitHub Container Registry (GHCR). The package
is public:
```sh
docker pull ghcr.io/bnelair/mef3io-server:latest
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  ghcr.io/bnelair/mef3io-server:latest
```

#### Build locally
The image is based on `ubuntu:24.04` with Python 3.12:
```sh
docker build -t mef3io-server .
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  mef3io-server
```

#### Accessing MEF3 files from the container
The dockerized server reads files from the host through a bind mount at `/host_root`.
The mapping is automatic:

1. **Mount the host into the container at `/host_root`.** Read-only is recommended,
   since the server only reads data:
   ```sh
   -v /:/host_root:ro
   ```
2. **Ask for files using their normal absolute path on the host** — *do not* add
   `/host_root` yourself. The server prepends it for you. For example, if your file
   lives at `/data/recordings/subj01.mefd` on the host:
   ```python
   client.open_file("/data/recordings/subj01.mefd")
   # server reads /host_root/data/recordings/subj01.mefd inside the container
   ```

To limit what the container can see, mount only the directory holding your data,
**keeping its absolute path** so the mapping still resolves:
```sh
-v /data/recordings:/host_root/data/recordings:ro
```

> Note: this `/host_root` mapping only applies when the server runs in Docker
> (detected via `/.dockerenv`). Running the server directly on the host uses paths
> as-is, with no `/host_root` prefix.

## Usage

### As a Python Module
Run the server with configurable options:
```sh
python -m mef3io_server.server
```

#### Configuration via Environment Variables
- `PORT`: gRPC server port (default: 50051)
- `TILE_DURATION_S`: Tile length in seconds for timestamp-based access (default: 60)
- `TILE_CACHE_MB`: Global tile-cache budget in MB, shared across all open files (default: 512)
- `CACHE_TTL_S`: Discard tiles not accessed within this many seconds; a finished
  session (e.g. a detector that moved on) is freed even before the byte budget is
  hit. `0` disables idle expiry (default: 1800 = 30 min)

Parallel decode (pymef MEF3 decode is GIL-bound, so real parallelism needs
separate worker processes — see below):
- `USE_PROCESS_POOL`: Decode cold reads / prefetch in worker processes (default: `true`)
- `READER_PROCESSES`: Total decode worker processes (default: auto = `cpu_count - 1`)
- `PREFETCH_PROCESSES`: How many of those form the background prefetch lane; the
  rest (always ≥ 1) are the reserved foreground lane so prefetch can never starve
  an interactive read (default: auto = half)
- `MIN_PARALLEL_TILES`: Minimum missing tiles before a cold read fans out to the
  pool; smaller reads stay in-process, where IPC is not worth it (default: 2)

Prefetch / paging for visualization (look-ahead/behind measured in *windows* of
the request's own size):
- `PREFETCH_AHEAD_WINDOWS`: Windows to prefetch after the request (page forward) (default: 1)
- `PREFETCH_BEHIND_WINDOWS`: Windows to prefetch before the request (page backward) (default: 1)
- `MAX_WORKERS`: Thread-pool size for the in-process prefetch fallback used when
  `USE_PROCESS_POOL=0` (default: 4)

Example — interactive viewing (page both ways), and a detector single-pass
(stream forward, no look-behind, deeper look-ahead):
```sh
# viewer
PORT=50052 PREFETCH_AHEAD_WINDOWS=1 PREFETCH_BEHIND_WINDOWS=1 python -m mef3io_server.server
# detector / automated single pass
PREFETCH_AHEAD_WINDOWS=3 PREFETCH_BEHIND_WINDOWS=0 python -m mef3io_server.server
```

### As a Docker Container
The `-v /:/host_root:ro` mount is required so the server can reach files on the host
(see [Accessing MEF3 files from the container](#accessing-mef3-files-from-the-container)):
```sh
docker run -e PORT=50051 -e TILE_CACHE_MB=1024 -p 50051:50051 \
  -v /:/host_root:ro \
  ghcr.io/bnelair/mef3io-server:latest
```

## Python Usage Examples

### Launching the Server from Python
You can launch the gRPC server directly from Python by importing and running the server class:

```python
from mef3io_server.server.mef3_server import gRPCMef3Server
from mef3io_server.server.file_manager import FileManager
import grpc
from concurrent import futures

# Configure the file manager (all arguments optional; see FileManager docstring)
file_manager = FileManager(tile_cache_bytes=512 * 1024 * 1024, prefetch_ahead_windows=1)

# Create the gRPC server and add the MEF3 service
server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
servicer = gRPCMef3Server(file_manager)
from mef3io_server.protobufs.gRPCMef3Server_pb2_grpc import add_gRPCMef3ServerServicer_to_server
add_gRPCMef3ServerServicer_to_server(servicer, server)

# Start the server
port = 50052
server.add_insecure_port(f"[::]:{port}")
server.start()
print(f"Server started on port {port}")
server.wait_for_termination()
```

### Using the Python Client
The package provides a high-level client for interacting with the server:

```python
from mef3io_server.client import Mef3Client

client = Mef3Client("localhost:50052")

# Open a file and inspect its metadata
info = client.open_file("/path/to/file.mefd")
print("Channels:", info["channel_names"])
print("Per-channel fs:", info["channel_sampling_rates"])
print("Per-channel start/end:", info["channel_start_uutc"], info["channel_end_uutc"])
print("Recording span:", info["start_uutc"], info["end_uutc"], info["duration_s"])

# --- Channels + time: the only data access model ---------------------------
# Read any channels over any [start_uutc, end_uutc) window (microseconds, uUTC).
# The server serves from a per-channel tile cache (reading only what is missing),
# decodes missing tiles in parallel across processes, and prefetches neighboring
# windows for smooth paging.
start_uutc = info["start_uutc"]
res = client.get_signal_range(
    "/path/to/file.mefd",
    channels=["Ch1", "Ch2"],          # or None for all channels
    start_uutc=start_uutc,
    end_uutc=start_uutc + 10_000_000,  # +10 s
)
print(f"Data shape: {res['shape']}, dtype: {res['dtype']}, fs: {res['fs']}")

# List open files
print(client.list_open_files())

# Close the file
client.close_file("/path/to/file.mefd")

client.shutdown()
```

See the [API section](#api) and the Python docstrings for more details on each method.

## API
The server exposes a gRPC API. See `mef3io_server/protobufs/gRPCMef3Server.proto` for service and message definitions.

Every data access is oriented in **channels and time** — there is no segment grid
and no server-side channel selection; each request is self-contained.

Key RPCs / client methods:
- **`GetSignalRange`** / `client.get_signal_range(file_path, channels, start_uutc, end_uutc)` —
  read any channels over any `[start_uutc, end_uutc)` window; streams `float32`.
  `channels=None` means all channels.
- **`FileInfo`** / `client.get_file_info(file_path)` — metadata: `channel_names`,
  `channel_sampling_rates`, `channel_start_uutc`, `channel_end_uutc` (parallel
  per-channel arrays) plus the global `start_uutc`/`end_uutc`/`duration_s`.
- `OpenFile`, `CloseFile`, `ListOpenFiles`.

## Testing with Large Data
For testing with real-life large MEF3 files, use the `demo/run_big_data.py` script:

```sh
# Start the server
python -m mef3io_server.server &

# Run the big data test
python demo/run_big_data.py /path/to/large_file.mefd localhost:50051
```

This script performs comprehensive tests including:
- Opening large files and reading per-channel metadata
- Reading channels over time windows of various sizes
- Sequential window retrieval (prefetch) and cache-hit re-reads
- Channel-subset selection
- Cache behavior validation

Note: This test may take a long time with very large files and is intended for manual integration testing rather than CI/CD pipelines.

## Logging
The server provides comprehensive logging for troubleshooting:

- Logs are written to both console and file (`logs/server_YYYY-MM-DDTHH-MM-SS.log`)
- Log level can be configured via `app_config.json` (default: INFO)
- Logs include:
  - File open/close operations
  - Cache hits/misses and TTL eviction
  - Prefetch operations
  - Error handling and stack traces
  - Docker environment detection

To adjust the log level, create or edit `app_config.json`:
```json
{
  "log_level": "DEBUG"
}
```

Available log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

## Development

### Running Tests
Install the dev dependencies, then run the suite. All tests are in `tests/` and use `pytest`:
```sh
pip install -e .[dev]
pytest
```
A plain `pytest` run excludes the long-running `slow` and `benchmark` suites by default
(configured via `addopts` in `pyproject.toml`), so it stays fast. Opt into them explicitly:
```sh
pytest -m slow        # long functional tests against a generated 1-hour file
pytest -m benchmark   # performance benchmarks (see below)
```

### Running Benchmarks
The package ships performance benchmarks (using `pytest-benchmark`, included in the `[dev]`
and `[test]` extras) under `tests/test_access_patterns.py` and `tests/test_file_manager.py`.
They compare direct MEF reading vs. the gRPC server with and without prefetching, over a
generated dataset (2 hours, 64 channels, 256 Hz). Because they generate large data and run a
real server, they are excluded from normal test runs — run them on demand with:
```sh
pip install -e .[dev]
pytest -m benchmark
```
Useful options:
```sh
pytest -m benchmark --benchmark-only                 # only timing, skip assertions overhead
pytest -m benchmark --benchmark-save=baseline        # save results as "baseline"
pytest -m benchmark --benchmark-compare              # compare against the last saved run
pytest -m benchmark --benchmark-histogram            # write histogram SVGs
```

**Each benchmark records the setup it ran under**, so results are self-describing:

- **File / dataset:** file name, total channels in the file, channels actually used under
  test, sampling rate, precision, and duration.
- **Server config:** whether the process pool is on, window prefetch depth
  (`prefetch_ahead_windows`), and gRPC server threads — plus the host's CPU count.

This is attached to each result's `extra_info`. To see it printed on the console, add `-s`;
to capture it to a file for later analysis, write JSON:
```sh
pytest -m benchmark -s                         # print the setup block for each benchmark
pytest -m benchmark --benchmark-json=results.json   # setup is included under "extra_info"
```
> Note: benchmarks are intended for local/manual use and are not run in CI.

### Linting
You can use any linter compatible with Google-style docstrings (e.g., `pylint`, `flake8`).

### Building Documentation
This project uses Sphinx with Google-style docstrings for API documentation.

To build the documentation locally:
```sh
# Install documentation dependencies
pip install -e ".[docs]"

# Build HTML documentation
cd docs
make html

# Or use sphinx-build directly
sphinx-build -b html docs/ docs/_build/html
```

The built documentation will be available at `docs/_build/html/index.html`.

#### Online Documentation
Documentation is automatically built and deployed to GitHub Pages on every push to the `main` branch. The documentation is available at: https://bnelair.github.io/mef3io-server/

The deployment uses GitHub Actions (see `.github/workflows/docs.yml`) and publishes to the `gh-pages` branch.

## CI/CD
Documentation is automatically built and deployed to GitHub Pages on every push to the `main` branch via GitHub Actions (see `.github/workflows/docs.yml`).

## License
Specify your license here.

## Authors
See Git history for contributors.
