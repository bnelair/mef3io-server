"""
Demo: Testing the Mef3Client with real-life big data.

This script demonstrates opening a large MEF3 file, inspecting its metadata,
reading channels over time windows of various sizes, and testing edge cases
that may occur with real-life data.

This test is designed to work with large files and may take a long time to run.
It should be run manually for integration testing but not as part of regular CI/CD.

If the server runs in Docker, start it with the host mounted at /host_root, e.g.
    docker run -p 50051:50051 -v /:/host_root:ro ghcr.io/bnelair/brainmaze-mef3-server:latest
and pass <path_to_mef3_file> as the absolute path AS IT EXISTS ON THE HOST. The server
maps it to /host_root/<path> automatically -- do not add /host_root yourself.
"""
import sys
import os
import time
from brainmaze_mef3_server.client import Mef3Client

def test_big_data():
    """Test the MEF3 server with big data."""

    # Path to the big MEF3 file - this should be provided as a command line argument
    if len(sys.argv) < 2:
        print("Usage: python run_big_data.py <path_to_mef3_file> [server_address]")
        print("Example: python run_big_data.py /path/to/big_file.mefd localhost:50051")
        print("Note: with a dockerized server, give the absolute HOST path; the server")
        print("      reads it via the /host_root mount (-v /:/host_root:ro).")
        sys.exit(1)

    MEF3_FILE = sys.argv[1]
    server_address = sys.argv[2] if len(sys.argv) > 2 else "localhost:50051"

    if not os.path.exists(MEF3_FILE):
        print(f"ERROR: File does not exist: {MEF3_FILE}")
        sys.exit(1)

    print(f"Testing with file: {MEF3_FILE}")
    print(f"Connecting to server: {server_address}")

    # Connect to the server
    try:
        client = Mef3Client(server_address)
        print("✓ Connected to server")
    except Exception as e:
        print(f"ERROR: Failed to connect to server: {e}")
        sys.exit(1)

    try:
        # Test 1: Open the file and inspect metadata
        print("\n--- Test 1: Opening file ---")
        start_time = time.time()
        info = client.open_file(MEF3_FILE)
        elapsed = time.time() - start_time
        print(f"✓ File opened in {elapsed:.2f}s")
        print(f"  Channels: {info['number_of_channels']}")
        print(f"  Duration: {info['duration_s']:.2f}s")
        print(f"  Start: {info['start_uutc']}")
        print(f"  End: {info['end_uutc']}")
        print(f"  Per-channel fs: {sorted(set(info['channel_sampling_rates']))} Hz")

        if not info['file_opened']:
            print(f"ERROR: Failed to open file: {info.get('error_message', 'Unknown error')}")
            sys.exit(1)

        channels = info["channel_names"]
        t0 = info["start_uutc"]
        win_us = 60 * 1_000_000  # 60 s windows

        # Test 2: Retrieve the first 60 s of all channels
        print("\n--- Test 2: Reading first 60 s of all channels ---")
        start_time = time.time()
        result = client.get_signal_range(MEF3_FILE, channels, t0, t0 + win_us)
        elapsed = time.time() - start_time
        if result['array'] is not None:
            print(f"✓ First window retrieved in {elapsed:.2f}s")
            print(f"  Shape: {result['shape']}")
            print(f"  Channels: {len(result['channel_names'])}")
            print(f"  Sampling rate: {result['fs']} Hz")
        else:
            print(f"ERROR: Failed to retrieve window: {result.get('error_message', 'Unknown error')}")

        # Test 3: Retrieve multiple sequential windows (prefetch should warm them)
        print("\n--- Test 3: Reading first 5 windows sequentially ---")
        num_windows = 5
        start_time = time.time()
        for i in range(num_windows):
            s = t0 + i * win_us
            result = client.get_signal_range(MEF3_FILE, channels, s, s + win_us)
            if result['array'] is None:
                print(f"  ERROR on window {i}: {result.get('error_message', 'Unknown error')}")
            else:
                print(f"  Window {i}: shape={result['shape']}, retrieved successfully")
        elapsed = time.time() - start_time
        print(f"✓ Retrieved {num_windows} windows in {elapsed:.2f}s ({elapsed/num_windows:.2f}s per window)")

        # Test 4: Re-read the first window (must be a cache hit -- much faster)
        print("\n--- Test 4: Re-reading the first window (cache hit) ---")
        start_time = time.time()
        result = client.get_signal_range(MEF3_FILE, channels, t0, t0 + win_us)
        elapsed = time.time() - start_time
        print(f"✓ Re-read in {elapsed:.2f}s, shape: {result['shape']}")

        # Test 5: A different window size over the same data (30 s)
        print("\n--- Test 5: Reading a 30 s window ---")
        start_time = time.time()
        result = client.get_signal_range(MEF3_FILE, channels, t0, t0 + 30 * 1_000_000)
        elapsed = time.time() - start_time
        if result['array'] is not None:
            print(f"✓ Window retrieved in {elapsed:.2f}s")
            print(f"  Shape: {result['shape']}")
        else:
            print(f"ERROR: Failed to retrieve window: {result.get('error_message', 'Unknown error')}")

        # Test 6: Channel subset in a chosen order
        print("\n--- Test 6: Reading a channel subset ---")
        if len(channels) >= 3:
            selected = [channels[0], channels[1], channels[2]]
            print(f"  Selecting channels: {selected}")
            result = client.get_signal_range(MEF3_FILE, selected, t0, t0 + win_us)
            if result['array'] is not None:
                print(f"✓ Subset window: shape={result['shape']}")
                if result['shape'][0] != len(selected):
                    print(f"ERROR: Expected {len(selected)} channels, got {result['shape'][0]}")
            else:
                print(f"ERROR: Failed to retrieve window: {result.get('error_message', 'Unknown error')}")
        else:
            print(f"  Skipping (only {len(channels)} channels available)")

        # Test 7: List open files
        print("\n--- Test 7: Listing open files ---")
        open_files = client.list_open_files()
        print(f"✓ Open files: {open_files}")
        if MEF3_FILE not in open_files:
            print(f"WARNING: Expected {MEF3_FILE} in open files list")

        # Test 8: Close file
        print("\n--- Test 8: Closing file ---")
        client.close_file(MEF3_FILE)
        print(f"✓ File closed")

        print("\n" + "="*60)
        print("ALL TESTS PASSED!")
        print("="*60)

    except Exception as e:
        print(f"\nERROR: Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        client.shutdown()

if __name__ == "__main__":
    test_big_data()
