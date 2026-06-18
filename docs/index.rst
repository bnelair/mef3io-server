.. brainmaze-mef3-server documentation master file

Welcome to brainmaze-mef3-server's documentation!
=============================================

A gRPC server for efficient, concurrent access to MEF3 (Multiscale Electrophysiology Format) files, with LRU caching and background prefetching. Designed for scalable neurophysiology data streaming and analysis.

Features
--------

- gRPC API for remote MEF3 file access
- Thread-safe LRU cache for signal chunks
- Asynchronous prefetching for low-latency streaming
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
     ghcr.io/bnelair/brainmaze-mef3-server:latest

   # Or build locally (image is based on ubuntu:24.04 with Python 3.12)
   docker build -t brainmaze-mef3-server .
   docker run -e PORT=50051 -p 50051:50051 \
     -v /:/host_root:ro \
     brainmaze-mef3-server

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

   python -m brainmaze_mef3_server.server

Configuration via Environment Variables:

- ``PORT``: gRPC server port (default: 50051)
- ``N_PREFETCH``: Number of chunks to prefetch (default: 3)
- ``CACHE_CAPACITY_MULTIPLIER``: Extra cache slots (default: 3)
- ``MAX_WORKERS``: Prefetch thread pool size (default: 4)

Using the Python Client
~~~~~~~~~~~~~~~~~~~~~~~~

The package provides a high-level client for interacting with the server:

.. code-block:: python

   from brainmaze_mef3_server.client import Mef3Client

   client = Mef3Client("localhost:50052")

   # Open a file. When the server runs in Docker, pass the absolute path as it
   # exists on the host (mounted at /host_root); the server maps it automatically.
   info = client.open_file("/path/to/file.mefd")
   print("Opened file:", info)

   # Set the segment size (in seconds)
   resp = client.set_signal_segment_size("/path/to/file.mefd", 60)
   print("Number of segments:", resp["number_of_segments"])

   # Get a segment of signal data (returns a dict with a single numpy array)
   result = client.get_signal_segment("/path/to/file.mefd", chunk_idx=0)
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
   api/cache
   api/file_manager
   api/config_manager
   api/log_manager

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
