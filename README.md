# brainmaze-mef3-server

A gRPC server for efficient, concurrent access to MEF3 (Multiscale Electrophysiology Format) files, with LRU caching and background prefetching. Designed for scalable neurophysiology data streaming and analysis.

## Features
- gRPC API for remote MEF3 file access
- Thread-safe LRU cache for signal chunks
- Asynchronous prefetching for low-latency streaming
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
docker pull ghcr.io/bnelair/brainmaze-mef3-server:latest
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  ghcr.io/bnelair/brainmaze-mef3-server:latest
```

#### Build locally
The image is based on `ubuntu:24.04` with Python 3.12:
```sh
docker build -t brainmaze-mef3-server .
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  brainmaze-mef3-server
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
python -m brainmaze_mef3_server.server
```

#### Configuration via Environment Variables
- `PORT`: gRPC server port (default: 50051)
- `N_PREFETCH`: Number of chunks to prefetch (default: 3)
- `CACHE_CAPACITY_MULTIPLIER`: Extra cache slots (default: 3)
- `MAX_WORKERS`: Prefetch thread pool size (default: 4)

Example:
```sh
PORT=50052 N_PREFETCH=2 python -m brainmaze_mef3_server.server
```

### As a Docker Container
The `-v /:/host_root:ro` mount is required so the server can reach files on the host
(see [Accessing MEF3 files from the container](#accessing-mef3-files-from-the-container)):
```sh
docker run -e PORT=50051 -e N_PREFETCH=2 -p 50051:50051 \
  -v /:/host_root:ro \
  ghcr.io/bnelair/brainmaze-mef3-server:latest
```

## Python Usage Examples

### Launching the Server from Python
You can launch the gRPC server directly from Python by importing and running the server class:

```python
from brainmaze_mef3_server.server.mef3_server import gRPCMef3Server
from brainmaze_mef3_server.server.file_manager import FileManager
import grpc
from concurrent import futures

# Configure file manager (optional arguments: n_prefetch, cache_capacity_multiplier, max_workers)
file_manager = FileManager(n_prefetch=3, cache_capacity_multiplier=3, max_workers=4)

# Create the gRPC server and add the MEF3 service
server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
servicer = gRPCMef3Server(file_manager)
from brainmaze_mef3_server.protobufs.gRPCMef3Server_pb2_grpc import add_gRPCMef3ServerServicer_to_server
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
from brainmaze_mef3_server.client import Mef3Client

client = Mef3Client("localhost:50052")

# Open a file
info = client.open_file("/path/to/file.mefd")
print("Opened file:", info)

# Set chunk size (in seconds)
resp = client.set_signal_segment_size("/path/to/file.mefd", 60)
print(f"Number of segments: {resp['number_of_segments']}")

# Query number of segments
seg_info = client.get_number_of_segments("/path/to/file.mefd")
print(f"File has {seg_info['number_of_segments']} segments")

# Set active channels (optional)
client.set_active_channels("/path/to/file.mefd", ["Ch1", "Ch2"])  # Use your channel names

# Get a chunk of signal data (as a single numpy array)
result = client.get_signal_segment("/path/to/file.mefd", chunk_idx=0)
print(f"Data shape: {result['shape']}, channels: {result['channel_names']}")

# List open files
print(client.list_open_files())

# Close the file
client.close_file("/path/to/file.mefd")

client.shutdown()
```

See the [API section](#api) and the Python docstrings for more details on each method.

## API
The server exposes a gRPC API. See `brainmaze_mef3_server/protobufs/gRPCMef3Server.proto` for service and message definitions.

## Testing with Large Data
For testing with real-life large MEF3 files, use the `demo/run_big_data.py` script:

```sh
# Start the server
python -m brainmaze_mef3_server.server &

# Run the big data test
python demo/run_big_data.py /path/to/large_file.mefd localhost:50051
```

This script performs comprehensive tests including:
- Opening large files
- Setting and resetting segment sizes
- Querying segment counts
- Sequential segment retrieval
- Channel filtering
- Cache behavior validation

Note: This test may take a long time with very large files and is intended for manual integration testing rather than CI/CD pipelines.

## Logging
The server provides comprehensive logging for troubleshooting:

- Logs are written to both console and file (`logs/server_YYYY-MM-DDTHH-MM-SS.log`)
- Log level can be configured via `app_config.json` (default: INFO)
- Logs include:
  - File open/close operations
  - Segment size changes
  - Cache hits/misses
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
- **Server config:** prefetch depth (`n_prefetch`), cache capacity, prefetch worker threads,
  and gRPC server threads — plus the host's CPU count.

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
Documentation is automatically built and deployed to GitHub Pages on every push to the `main` branch. The documentation is available at: https://bnelair.github.io/brainmaze-mef3-server/

The deployment uses GitHub Actions (see `.github/workflows/docs.yml`) and publishes to the `gh-pages` branch.

## CI/CD
Documentation is automatically built and deployed to GitHub Pages on every push to the `main` branch via GitHub Actions (see `.github/workflows/docs.yml`).

## License
Specify your license here.

## Authors
See Git history for contributors.
