.. mef3io-server documentation master file

Welcome to mef3io-server's documentation!
==================================================

A gRPC server for efficient, concurrent access to MEF3 (Multiscale Electrophysiology Format) files. Every data call is oriented purely in **channels and time**: open a file, read its metadata, then request any channels over any ``[start_uutc, end_uutc)`` window. Backed by a per-channel tile cache, parallel decode across worker processes, and configurable window prefetch. Designed for scalable neurophysiology data streaming and analysis.

Features
--------

- gRPC API for remote MEF3 file access, oriented purely in **channels and time**
- Shared, byte-budgeted per-channel **tile cache** with an idle TTL
- **Parallel MEF3 decode** across worker processes (pymef decode is GIL-bound)
- Configurable **window look-ahead/behind prefetch** for smooth paging
- Configurable via environment variables or Docker
- Ready for deployment in Docker and CI/CD pipelines

Installation
------------

Requirements
~~~~~~~~~~~~

- Python 3.10+ (3.12 recommended)
- (Optional) Docker for containerized deployment

Local Setup
~~~~~~~~~~~

Clone the repository and install the package (dependencies come from ``pyproject.toml``):

.. code-block:: bash

   pip install .

Docker
~~~~~~

Released images are published to the GitHub Container Registry (GHCR), or you can
build locally. **A bind mount at** ``/host_root`` **is required** so the server can
reach files on the host (see `Accessing MEF3 files from the container`_):

.. code-block:: bash

   # Prebuilt image (public, no login needed)
   docker run -e PORT=50051 -p 50051:50051 \
     -v /:/host_root:ro \
     ghcr.io/bnelair/mef3io-server:latest

   # Or build locally (image is based on ubuntu:24.04 with Python 3.12)
   docker build -t mef3io-server .
   docker run -e PORT=50051 -p 50051:50051 \
     -v /:/host_root:ro \
     mef3io-server

Accessing MEF3 files from the container
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the server runs inside Docker, it automatically rewrites every absolute file
path you request to ``/host_root/<that path>``. The mapping is automatic:

1. **Mount the host into the container at** ``/host_root`` (read-only is recommended,
   since the server only reads data):

   .. code-block:: bash

      -v /:/host_root:ro

2. **Request files using their normal absolute path on the host** -- do *not* add
   ``/host_root`` yourself; the server prepends it for you. For example, a host file
   at ``/data/recordings/subj01.mefd`` is opened as:

   .. code-block:: python

      client.open_file("/data/recordings/subj01.mefd")
      # server reads /host_root/data/recordings/subj01.mefd inside the container

To limit what the container can see, mount only the directory holding your data,
**keeping its absolute path** so the mapping still resolves:

.. code-block:: bash

   -v /data/recordings:/host_root/data/recordings:ro

.. note::

   The ``/host_root`` mapping only applies when the server runs in Docker (detected
   via ``/.dockerenv``). Running the server directly on the host uses paths as-is.

Quick Start
-----------

As a Python Module
~~~~~~~~~~~~~~~~~~

Run the server with configurable options:

.. code-block:: bash

   python -m mef3io_server.server

Configuration via Environment Variables:

- ``PORT``: gRPC server port (default: 50051)
- ``TILE_DURATION_S``: Tile length in seconds for the tile cache (default: 60)
- ``TILE_CACHE_MB``: Global tile-cache budget in MB (default: 512)
- ``CACHE_TTL_S``: Discard tiles idle longer than this; ``0`` disables (default: 1800)
- ``USE_PROCESS_POOL``: Decode in worker processes for parallel decode (default: ``true``)
- ``READER_PROCESSES`` / ``PREFETCH_PROCESSES``: Total / prefetch-lane decode processes (default: auto)
- ``PREFETCH_AHEAD_WINDOWS`` / ``PREFETCH_BEHIND_WINDOWS``: Windows to prefetch forward / backward (default: 1 / 1)
- ``MIN_PARALLEL_TILES``: Min missing tiles before using the pool (default: 2)
- ``MAX_WORKERS``: Thread-pool size for the in-process prefetch fallback (default: 4)

Using the Python Client
~~~~~~~~~~~~~~~~~~~~~~~~

The package provides a high-level client for interacting with the server:

.. code-block:: python

   from mef3io_server.client import Mef3Client

   client = Mef3Client("localhost:50052")

   # Open a file. When the server runs in Docker, pass the absolute path as it
   # exists on the host (mounted at /host_root); the server maps it automatically.
   info = client.open_file("/path/to/file.mefd")
   print("Channels:", info["channel_names"])
   print("Per-channel start/end:", info["channel_start_uutc"], info["channel_end_uutc"])

   # Read any channels over any [start_uutc, end_uutc) window (microseconds, uUTC).
   # channels=None means all channels.
   t0 = info["start_uutc"]
   result = client.get_signal_range(
       "/path/to/file.mefd",
       channels=["Ch1", "Ch2"],
       start_uutc=t0,
       end_uutc=t0 + 10_000_000,  # +10 s
   )
   print("Shape:", result["shape"], "channels:", result["channel_names"])

   # Close the file
   client.close_file("/path/to/file.mefd")
   client.shutdown()

Contents
--------

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/client
   api/server
   api/file_manager
   api/tile_cache
   api/reader_pool
   api/config_manager
   api/log_manager

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
