"""Integration tests for the MEF3 server."""
import pytest
import time
import subprocess
import signal
import os
from bnel_mef3_server.client import Mef3Client
from .conftest import mef3_file


def test_server_start_and_basic_operations(grpc_server, mef3_file):
    """Test that server can start and handle basic operations."""
    # This test uses the fixture-based server from conftest.py
    # which is already running for all tests
    port = grpc_server["port"]
    client = Mef3Client(f"localhost:{port}")
    
    try:
        # Test open
        info = client.open_file(mef3_file)
        assert info['file_opened'], "File should be opened"
        assert info['number_of_channels'] > 0, "Should have channels"
        
        # Test set segment size
        resp = client.set_signal_segment_size(mef3_file, 30)
        assert resp['number_of_segments'] > 0, "Should have segments"
        
        # Test get number of segments
        seg_resp = client.get_number_of_segments(mef3_file)
        assert seg_resp['number_of_segments'] == resp['number_of_segments'], "Segment counts should match"
        
        # Test get segment
        result = client.get_signal_segment(mef3_file, 0)
        assert result['array'] is not None, "Should get data"
        assert result['error_message'] == '', "Should not have error"
        
        # Test list open files
        open_files = client.list_open_files()
        assert mef3_file in open_files, "File should be in open files list"
        
        # Test close
        client.close_file(mef3_file)
        
    finally:
        client.shutdown()


def test_repeated_segment_size_changes(grpc_server, mef3_file):
    """Test that setting segment size repeatedly works correctly."""
    port = grpc_server["port"]
    client = Mef3Client(f"localhost:{port}")
    
    try:
        # Open file
        client.open_file(mef3_file)
        
        # Set segment size multiple times
        sizes = [60, 30, 45, 20, 60]
        for size in sizes:
            resp = client.set_signal_segment_size(mef3_file, size)
            assert resp['number_of_segments'] > 0, f"Should have segments for size {size}"
            
            # Verify we can query the segment count
            seg_resp = client.get_number_of_segments(mef3_file)
            assert seg_resp['number_of_segments'] == resp['number_of_segments'], \
                f"Segment count mismatch for size {size}"
            
            # Verify we can still get data
            result = client.get_signal_segment(mef3_file, 0)
            assert result['array'] is not None, f"Should get data after setting size to {size}"
        
        client.close_file(mef3_file)
        
    finally:
        client.shutdown()


def test_docker_path_handling():
    """Test that Docker path detection doesn't crash."""
    from bnel_mef3_server.server.file_manager import is_running_in_docker, get_actual_file_path
    
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
