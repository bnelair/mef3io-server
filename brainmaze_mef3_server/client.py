import grpc
import numpy as np

import brainmaze_mef3_server.protobufs.gRPCMef3Server_pb2 as pb2
import brainmaze_mef3_server.protobufs.gRPCMef3Server_pb2_grpc as pb2_grpc

class Mef3Client:
    """
    Client for interacting with the MEF3 gRPC server.

    Every data call is oriented purely in **channels and time**: open a file,
    inspect its metadata (channel names, per-channel sampling rates and
    per-channel start/end timestamps via :meth:`get_file_info`), then read any
    channels over any ``[start_uutc, end_uutc)`` window with
    :meth:`get_signal_range`.

    Example::

        client = Mef3Client("localhost:50052")
        client.open_file("/path/to/file.mefd")
        info = client.get_file_info("/path/to/file.mefd")
        data = client.get_signal_range("/path/to/file.mefd",
                                       info["channel_names"][:4],
                                       info["start_uutc"],
                                       info["start_uutc"] + 60_000_000)
        client.shutdown()
    """

    def __init__(self, address="localhost:50052"):
        """
        Initialize the Mef3Client and connect to the gRPC server.

        Args:
            address (str): Address of the gRPC server (default: "localhost:50052").
        """
        self.channel = grpc.insecure_channel(address)
        self.stub = pb2_grpc.gRPCMef3ServerStub(self.channel)

    def open_file(self, file_path):
        """
        Open a MEF3 file on the server.

        Args:
            file_path (str): Path to the MEF3 file.
        Returns:
            dict: File info including file_path, file_opened, number_of_channels, channel_names, etc.
        """
        resp = self.stub.OpenFile(pb2.OpenFileRequest(file_path=file_path))
        return self._file_info_to_dict(resp)

    @staticmethod
    def _file_info_to_dict(resp):
        """Convert a FileInfoResponse into a plain metadata dict.

        ``channel_names``, ``channel_sampling_rates``, ``channel_start_uutc`` and
        ``channel_end_uutc`` are parallel lists (index i describes channel i);
        ``start_uutc``/``end_uutc`` are the global recording span.
        """
        return {
            "file_path": resp.file_path,
            "file_opened": resp.file_opened,
            "number_of_channels": getattr(resp, "number_of_channels", None),
            "channel_names": list(getattr(resp, "channel_names", [])),
            "channel_sampling_rates": list(getattr(resp, "channel_sampling_rates", [])),
            "channel_start_uutc": list(getattr(resp, "channel_start_uutc", [])),
            "channel_end_uutc": list(getattr(resp, "channel_end_uutc", [])),
            "start_uutc": getattr(resp, "start_uutc", None),
            "end_uutc": getattr(resp, "end_uutc", None),
            "duration_s": getattr(resp, "duration_s", None),
            "error_message": getattr(resp, "error_message", ""),
        }

    def close_file(self, file_path):
        """
        Close a MEF3 file on the server.

        Args:
            file_path (str): Path to the MEF3 file.
        Returns:
            dict: Contains file_path and file_opened status.
        """
        resp = self.stub.CloseFile(pb2.FileInfoRequest(file_path=file_path))
        return {
            "file_path": resp.file_path,
            "file_opened": resp.file_opened,
        }

    def get_file_info(self, file_path):
        """
        Get metadata for an open MEF3 file.

        Args:
            file_path (str): Path to the MEF3 file.
        Returns:
            dict: File metadata -- ``channel_names``, ``channel_sampling_rates``,
            ``channel_start_uutc`` and ``channel_end_uutc`` (parallel per-channel
            lists), plus the global ``start_uutc``/``end_uutc``/``duration_s``.
        """
        resp = self.stub.FileInfo(pb2.FileInfoRequest(file_path=file_path))
        return self._file_info_to_dict(resp)

    def get_signal_range(self, file_path, channels, start_uutc, end_uutc):
        """
        Read arbitrary channels over an arbitrary ``[start_uutc, end_uutc)`` window.

        This is the server's data-access method -- every read is oriented in
        channels and time. The server serves the data from its per-channel tile
        cache (reading only what is missing, decoding missing tiles in parallel
        across worker processes) and prefetches neighboring windows for smooth
        forward/backward paging.

        Args:
            file_path (str): Path to the MEF3 file.
            channels (list[str] or None): Channels to read. ``None``/empty means
                all channels in the file.
            start_uutc (int): Inclusive window start in microseconds (uUTC).
            end_uutc (int): Exclusive window end in microseconds (uUTC).

        Returns:
            dict: A dict with keys ``array`` (``np.ndarray`` of shape
            ``(n_channels, n_samples)``, ``float32``), ``channel_names``,
            ``fs``, ``start_uutc``, ``end_uutc``, ``dtype``, ``shape`` and
            ``error_message``.
        """
        req = pb2.SignalRangeRequest(
            file_path=file_path,
            channel_names=list(channels) if channels else [],
            start_uutc=int(start_uutc),
            end_uutc=int(end_uutc),
        )
        arrays = []
        channel_names = None
        fs = None
        dtype = None
        got_start = None
        got_end = None
        error_message = None
        for chunk in self.stub.GetSignalRange(req):
            if chunk.error_message:
                error_message = chunk.error_message
                break
            arr = np.frombuffer(chunk.array_bytes, dtype=chunk.dtype).reshape(chunk.shape)
            arrays.append(arr)
            channel_names = list(chunk.channel_names)
            dtype = chunk.dtype
            fs = chunk.fs
            got_start = chunk.start_uutc if got_start is None else min(got_start, chunk.start_uutc)
            got_end = chunk.end_uutc if got_end is None else max(got_end, chunk.end_uutc)
        if not arrays:
            return {
                'array': None,
                'channel_names': channel_names,
                'fs': fs,
                'start_uutc': got_start,
                'end_uutc': got_end,
                'dtype': dtype,
                'shape': None,
                'error_message': error_message or 'No data returned.',
            }
        array = np.concatenate(arrays, axis=1) if len(arrays) > 1 else arrays[0]
        return {
            'array': array,
            'channel_names': channel_names,
            'fs': fs,
            'start_uutc': got_start,
            'end_uutc': got_end,
            'dtype': dtype,
            'shape': array.shape,
            'error_message': error_message or '',
        }

    def list_open_files(self):
        """
        List all currently open MEF3 files on the server.

        Returns:
            list: List of file paths for open files.
        """
        resp = self.stub.ListOpenFiles(pb2.ListOpenFilesRequest())
        return list(resp.file_paths)

    def shutdown(self):
        """
        Close the gRPC channel and clean up resources.
        """
        self.channel.close()
