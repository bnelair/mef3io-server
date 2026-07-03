"""
Demo: Using the Mef3Client to interact with a running MEF3 gRPC server (in Docker).

This script demonstrates opening a file, inspecting its metadata (channels,
per-channel sampling rates and start/end timestamps), reading arbitrary channels
over an arbitrary time window, and closing the file using the high-level client.

Prerequisite: the server is running in Docker with the host mounted at /host_root, e.g.

    docker run -p 50051:50051 -v /:/host_root:ro ghcr.io/bnelair/brainmaze-mef3-server:latest

When the server runs in Docker it reads files via /host_root, so pass MEF3_FILE as the
absolute path AS IT EXISTS ON THE HOST (e.g. /data/recordings/x.mefd). The server maps
it to /host_root/data/recordings/x.mefd automatically -- do not add /host_root yourself.
"""
from brainmaze_mef3_server.client import Mef3Client
import os

# Absolute path to the MEF3 file on the HOST. Edit this to point at your own file.
MEF3_FILE = os.environ.get("MEF3_FILE", "/data/recordings/test_file.mefd")
print("File (host path):", MEF3_FILE)

# Connect to the server. Edit host:port if the server runs elsewhere.
SERVER = os.environ.get("MEF3_SERVER", "localhost:50051")
client = Mef3Client(SERVER)

print("Opening file:", MEF3_FILE)
info = client.open_file(MEF3_FILE)
print("File info:", info)

print("Channel metadata (name, fs, start, end):")
for name, fs, cs, ce in zip(info["channel_names"], info["channel_sampling_rates"],
                            info["channel_start_uutc"], info["channel_end_uutc"]):
    print(f"  {name}: {fs} Hz, [{cs}, {ce}]")

all_channels = info["channel_names"]
t0 = info["start_uutc"]

# Read a subset of channels, in a chosen order, over the first 60 seconds.
if len(all_channels) >= 3:
    selected = [all_channels[2], all_channels[0], all_channels[1]]
else:
    selected = all_channels
print(f"Reading channels {selected} over [{t0}, {t0 + 60_000_000})...")
result = client.get_signal_range(MEF3_FILE, selected, t0, t0 + 60_000_000)
if result["array"] is not None:
    arr = result["array"]
    print(f"Range shape={result['shape']}, dtype={arr.dtype}, channels={result['channel_names']}")
    print("First samples (truncated):", arr.flatten()[:10])
else:
    print("No data returned:", result.get("error_message"))

# Any window works -- e.g. jump 5 minutes in and read 10 seconds of everything.
print("Reading 10 s of ALL channels 5 minutes in...")
result = client.get_signal_range(MEF3_FILE, None, t0 + 300_000_000, t0 + 310_000_000)
print("Shape:", result["shape"], "error:", result["error_message"] or "none")

print("Listing open files:")
print(client.list_open_files())

print("Closing file...")
client.close_file(MEF3_FILE)

client.shutdown()
print("Demo complete.")
