# Install

## Requirements

- Python 3.10+ (3.12 recommended)
- (Optional) Docker for containerized deployment

Decode is provided by [mef3io](https://github.com/bnelair/mef3io), pulled in
automatically as a dependency.

## From source

Clone the repository and install the package (dependencies come from
`pyproject.toml`):

```bash
git clone https://github.com/bnelair/mef3io-server && cd mef3io-server
pip install .
```

For development (tests, benchmarks, build tooling):

```bash
pip install -e ".[dev]"
```

To build these docs locally:

```bash
pip install -e ".[docs]"
mkdocs serve      # live preview at http://127.0.0.1:8000
```

## Docker (prebuilt image)

Released images are published to the GitHub Container Registry (GHCR); the
package is public, so no login is needed:

```bash
docker pull ghcr.io/bnelair/mef3io-server:latest
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  ghcr.io/bnelair/mef3io-server:latest
```

!!! important "The `/host_root` bind mount is required"
    When the server runs in a container it rewrites every absolute path you
    request to `/host_root/<that path>`, so you **must** bind-mount your host
    filesystem at `/host_root` or no files will be found. See
    [Docker deployment](guides/docker.md) for the full mapping.

## Docker (build locally)

The image is based on `ubuntu:24.04` with Python 3.12:

```bash
docker build -t mef3io-server .
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  mef3io-server
```
