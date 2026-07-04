# Docker deployment

## The `/host_root` mount

When the server runs inside a container it automatically rewrites every absolute
file path you request to `/host_root/<that path>` (Docker is detected via
`/.dockerenv`). You therefore **must bind-mount your host filesystem into the
container at `/host_root`**, or no files will be found.

!!! important
    The `/host_root` mapping only applies inside Docker. Running the server
    directly on the host uses paths as-is, with no prefix.

## Running the prebuilt image

Released images are published to GHCR (the package is public):

```bash
docker run -e PORT=50051 -e TILE_CACHE_MB=1024 -p 50051:50051 \
  -v /:/host_root:ro \
  ghcr.io/bnelair/mef3io-server:latest
```

Read-only (`:ro`) is recommended, since the server only reads data. See
[Configuration](configuration.md) for the environment variables you can pass
with `-e`.

## Accessing files from the container

1. **Mount the host at `/host_root`:**

   ```bash
   -v /:/host_root:ro
   ```

2. **Ask for files using their normal absolute host path** — do *not* add
   `/host_root` yourself; the server prepends it. For a host file at
   `/data/recordings/subj01.mefd`:

   ```python
   client.open_file("/data/recordings/subj01.mefd")
   # server reads /host_root/data/recordings/subj01.mefd inside the container
   ```

To limit what the container can see, mount only the directory holding your data,
**keeping its absolute path** so the mapping still resolves:

```bash
-v /data/recordings:/host_root/data/recordings:ro
```

## Building locally

The image is based on `ubuntu:24.04` with Python 3.12:

```bash
docker build -t mef3io-server .
docker run -e PORT=50051 -p 50051:50051 \
  -v /:/host_root:ro \
  mef3io-server
```
