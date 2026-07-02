"""Integration tests for the MEF3 server."""
import pytest
import time
import subprocess
import signal
import os
from brainmaze_mef3_server.client import Mef3Client
from .conftest import mef3_file


def test_server_start_and_basic_operations(shared_test_server, mef3_file):
    """Test that server can start and handle basic operations."""
    # This test uses the fixture-based server from conftest.py
    # which is already running for all tests
    port = shared_test_server
    client = Mef3Client(f"localhost:{port}")

    try:
        # Test open + metadata
        info = client.open_file(mef3_file)
        assert info['file_opened'], "File should be opened"
        assert info['number_of_channels'] > 0, "Should have channels"
        assert len(info['channel_start_uutc']) == info['number_of_channels']

        # Test channels+time read
        channels = info['channel_names'][:2]
        t1 = info['start_uutc']
        result = client.get_signal_range(mef3_file, channels, t1, t1 + 30_000_000)
        assert result['array'] is not None, "Should get data"
        assert result['error_message'] == '', "Should not have error"
        assert result['array'].shape[0] == len(channels)

        # Test list open files
        open_files = client.list_open_files()
        assert mef3_file in open_files, "File should be in open files list"

        # Test close
        client.close_file(mef3_file)

    finally:
        client.shutdown()


def test_repeated_range_reads_varied_windows(shared_test_server, mef3_file):
    """Repeatedly reading windows of varying sizes must keep working (cache reuse)."""
    port = shared_test_server
    client = Mef3Client(f"localhost:{port}")

    try:
        info = client.open_file(mef3_file)
        channels = info['channel_names']
        t1 = info['start_uutc']

        # Varying window sizes, overlapping ranges -- exercises tile cache reuse.
        for size_s in [60, 30, 45, 20, 60]:
            result = client.get_signal_range(mef3_file, channels, t1, t1 + size_s * 1_000_000)
            assert result['array'] is not None, f"Should get data for {size_s}s window"
            assert result['error_message'] == ''
            expected = int(round(size_s * info['channel_sampling_rates'][0]))
            assert abs(result['array'].shape[1] - expected) <= 1

        client.close_file(mef3_file)

    finally:
        client.shutdown()


def test_docker_path_handling():
    """Test that Docker path detection doesn't crash."""
    from brainmaze_mef3_server.server.file_manager import is_running_in_docker, get_actual_file_path

    # These functions should not crash
    in_docker = is_running_in_docker()
    assert isinstance(in_docker, bool), "Should return a boolean"

    # Test path mapping
    test_path = "/some/absolute/path.mefd"
    mapped_path = get_actual_file_path(test_path)
    assert isinstance(mapped_path, str), "Should return a string"

    # If not in Docker, path should be unchanged
    if not in_docker:
        assert mapped_path == test_path, "Path should be unchanged when not in Docker"
