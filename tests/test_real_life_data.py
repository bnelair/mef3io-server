
from brainmaze_mef3_server.client import Mef3Client
from tests.conftest import MEF3_TEST_FS, MEF3_TEST_CHANNELS, MEF3_FUNCTIONAL_TEST_DURATION_S

import pytest
from tqdm import tqdm


@pytest.mark.slow
def test_real_life_data(functional_test_mef3_file, launch_server_process, request):
    """
    Test real-world usage patterns on the channels+time API.
    Reads varying channel subsets and window sizes over a full recording.
    """
    pth_mef = functional_test_mef3_file
    fs = MEF3_TEST_FS
    n_channels = MEF3_TEST_CHANNELS
    data_len_s = MEF3_FUNCTIONAL_TEST_DURATION_S

    cl = Mef3Client("localhost:50051")
    request.addfinalizer(cl.shutdown)
    cl.open_file(pth_mef)
    fi = cl.get_file_info(pth_mef)

    channels = fi["channel_names"]
    assert len(channels) == n_channels, f"Expected {n_channels} channels, got {len(channels)}"
    # Explicit per-channel metadata.
    assert len(fi["channel_sampling_rates"]) == n_channels
    assert len(fi["channel_start_uutc"]) == n_channels
    assert len(fi["channel_end_uutc"]) == n_channels

    # Verify timestamps are in microseconds
    duration_us = fi["end_uutc"] - fi["start_uutc"]
    duration_s = duration_us / 1e6
    assert abs(duration_s - data_len_s) < 1, f"Duration mismatch: expected ~{data_len_s}s, got {duration_s}s"

    t0 = fi["start_uutc"]

    # Test 1: 32-channel subset over a 5-minute window
    x = cl.get_signal_range(pth_mef, channels[:32], t0, t0 + 5 * 60 * 1_000_000)
    assert x['array'] is not None, "No data returned"
    assert x['array'].shape[0] == 32, f"Expected 32 channels, got {x['array'].shape[0]}"
    expected_samples = 5 * 60 * fs
    assert abs(x['array'].shape[1] - expected_samples) <= 1, \
        f"Expected ~{expected_samples} samples, got {x['array'].shape[1]}"

    # Test 2: 1-minute windows across the whole recording
    expected_samples = 1 * 60 * fs
    n_windows = data_len_s // 60
    win_us = 60 * 1_000_000
    for w in tqdm(range(n_windows), desc="Verifying all windows"):
        x = cl.get_signal_range(pth_mef, channels[:32], t0 + w * win_us, t0 + (w + 1) * win_us)
        arr = x['array']
        assert arr.shape[0] == 32, f"Window {w}: expected 32 channels, got {arr.shape[0]}"
        assert abs(arr.shape[1] - expected_samples) <= 1, \
            f"Window {w}: expected ~{expected_samples} samples, got {arr.shape[1]}"


@pytest.mark.slow
def test_dynamic_parameter_changes(functional_test_mef3_file, launch_server_process, request):
    """
    Test that the server handles frequently changing window sizes and channel
    selections gracefully -- with channels+time there is no server-side state to
    reconfigure, every request is self-contained.
    """
    pth_mef = functional_test_mef3_file
    fs = MEF3_TEST_FS

    cl = Mef3Client("localhost:50051")
    request.addfinalizer(cl.shutdown)
    cl.open_file(pth_mef)
    fi = cl.get_file_info(pth_mef)
    channels = fi["channel_names"]
    t0 = fi["start_uutc"]

    # Test rapid window size changes
    window_sizes = [30, 60, 120, 300, 60, 30]  # seconds
    for window_size in window_sizes:
        x = cl.get_signal_range(pth_mef, channels, t0, t0 + window_size * 1_000_000)
        assert x['array'] is not None, f"No data returned for window size {window_size}s"
        expected_samples = window_size * fs
        assert abs(x['array'].shape[1] - expected_samples) <= 1, \
            f"Window size {window_size}s: expected ~{expected_samples} samples, got {x['array'].shape[1]}"

    # Test rapid channel-selection changes (per request, no server state)
    channel_configs = [
        channels[:16],   # First 16
        channels[16:32], # Next 16
        channels[:32],   # First 32
        channels[32:],   # Last 32
        channels,        # All channels
    ]

    for i, channel_list in enumerate(channel_configs):
        x = cl.get_signal_range(pth_mef, channel_list, t0, t0 + 60 * 1_000_000)
        assert x['array'].shape[0] == len(channel_list), \
            f"Config {i}: expected {len(channel_list)} channels in data"
        assert x['channel_names'] == list(channel_list), \
            f"Config {i}: channel order must match the request"


@pytest.mark.slow
def test_error_handling(functional_test_mef3_file, launch_server_process, request):
    """
    Test that server errors are properly caught and returned to the client.
    No server crashes should occur.
    """
    pth_mef = functional_test_mef3_file

    cl = Mef3Client("localhost:50051")
    request.addfinalizer(cl.shutdown)

    # Test 1: Request data from unopened file
    x = cl.get_signal_range(pth_mef, None, 0, 1_000_000)
    assert 'error_message' in x, "Expected error_message in response"
    assert x['error_message'] != '', "Expected non-empty error message for unopened file"

    # Now open the file
    cl.open_file(pth_mef)
    fi = cl.get_file_info(pth_mef)
    t0 = fi["start_uutc"]

    # Test 2: Empty window (end <= start)
    x = cl.get_signal_range(pth_mef, None, t0, t0)
    assert x['error_message'] != '', "Expected error for empty window"

    # Test 3: Unknown channel name
    x = cl.get_signal_range(pth_mef, ["invalid_channel_name"], t0, t0 + 1_000_000)
    assert x['error_message'] != '', "Expected error for unknown channel"

    # Test 4: Verify server is still responsive after errors
    fi = cl.get_file_info(pth_mef)
    assert fi["file_opened"] == True, "Server should still be responsive after errors"

    # Test 5: A valid read still works after the error barrage
    x = cl.get_signal_range(pth_mef, fi["channel_names"][:2], t0, t0 + 1_000_000)
    assert x['error_message'] == ''
    assert x['array'] is not None
