"""
Demo: Using the Mef3Client to interact with a running MEF3 gRPC server (in Docker).

This script demonstrates opening a file, setting the segment size, selecting active
channels, retrieving signal data, and closing the file using the high-level client.

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

print("Setting segment size to 60 seconds...")
seg_resp = client.set_signal_segment_size(MEF3_FILE, 60)
print("Segment response:", seg_resp)

print("Getting all channel names...")
all_channels = info["channel_names"]
print("All channels:", all_channels)

# Select a subset of channels (if at least 3 exist)
if len(all_channels) >= 3:
    selected = [all_channels[2], all_channels[0], all_channels[1]]
    print(f"Setting active channels to: {selected}")
    resp_set = client.set_active_channels(MEF3_FILE, selected)
    print("Set active channels response:", resp_set)
    print("Getting active channels...")
    resp_get = client.get_active_channels(MEF3_FILE)
    print("Active channels:", resp_get["active_channels"])
else:
    print("Not enough channels to demonstrate active channel selection.")

print("Retrieving first segment of signal data...")
result = client.get_signal_segment(MEF3_FILE, 0)
if result["array"] is not None:
    arr = result["array"]
    print(f"Segment shape={result['shape']}, dtype={arr.dtype}, channels={result['channel_names']}")
    print("First samples (truncated):", arr.flatten()[:10])
else:
    print("No data returned:", result.get("error_message"))

print("Listing open files:")
print(client.list_open_files())

print("Closing file...")
client.close_file(MEF3_FILE)

client.shutdown()
print("Demo complete.")
