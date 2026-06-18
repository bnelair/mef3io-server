"""Tests for get_number_of_segments functionality."""
import pytest
from bnel_mef3_server.server.file_manager import FileManager
from .conftest import mef3_file


def test_get_number_of_segments(mef3_file):
    """Test getting the number of segments for a file."""
    fm = FileManager()
    
    # Initially, no segments should exist
    num_segments = fm.get_number_of_segments(mef3_file)
    assert num_segments == 0, "Should return 0 for file that's not open"
    
    # Open the file
    fm.open_file(mef3_file)
    
    # Still no segments until we set segment size
    num_segments = fm.get_number_of_segments(mef3_file)
    assert num_segments == 0, "Should return 0 when no segments are set"
    
    # Set segment size
    resp = fm.set_signal_segment_size(mef3_file, 60)
    expected_segments = resp.number_of_segments
    
    # Now we should get the correct number of segments
    num_segments = fm.get_number_of_segments(mef3_file)
    assert num_segments == expected_segments, f"Expected {expected_segments} segments, got {num_segments}"
    
    # Change segment size
    resp2 = fm.set_signal_segment_size(mef3_file, 30)
    expected_segments2 = resp2.number_of_segments
    
    # Verify the number of segments changed
    num_segments2 = fm.get_number_of_segments(mef3_file)
    assert num_segments2 == expected_segments2, f"Expected {expected_segments2} segments, got {num_segments2}"
    assert num_segments2 != num_segments, "Number of segments should change when segment size changes"
    
    # Close the file
    fm.close_file(mef3_file)
    
    # After closing, should return 0
    num_segments = fm.get_number_of_segments(mef3_file)
    assert num_segments == 0, "Should return 0 for closed file"
    
    fm.shutdown()


def test_get_number_of_segments_via_grpc(grpc_stub_1, mef3_file):
    """Test getting the number of segments via gRPC."""
    import bnel_mef3_server.protobufs.gRPCMef3Server_pb2 as pb2
    
    # Open the file
    open_resp = grpc_stub_1.OpenFile(pb2.OpenFileRequest(file_path=mef3_file))
    assert open_resp.file_opened
    
    # Set segment size
    seg_resp = grpc_stub_1.SetSignalSegmentSize(pb2.SetSignalSegmentRequest(file_path=mef3_file, seconds=60))
    expected_segments = seg_resp.number_of_segments
    
    # Get number of segments
    num_resp = grpc_stub_1.GetNumberOfSegments(pb2.FileInfoRequest(file_path=mef3_file))
    assert num_resp.number_of_segments == expected_segments
    assert num_resp.error_message == ""
    
    # Close the file
    grpc_stub_1.CloseFile(pb2.FileInfoRequest(file_path=mef3_file))
