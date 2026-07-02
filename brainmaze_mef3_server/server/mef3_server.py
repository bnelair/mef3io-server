# mef
import grpc
from concurrent import futures

from brainmaze_mef3_server.server.log_manager import get_logger, setup_logging
from brainmaze_mef3_server.server.config_manager import read_app_config, get_log_level_from_config
import os

import brainmaze_mef3_server.protobufs.gRPCMef3Server_pb2 as gRPCMef3Server_pb2
import brainmaze_mef3_server.protobufs.gRPCMef3Server_pb2_grpc as gRPCMef3Server_pb2_grpc

from brainmaze_mef3_server.server.file_manager import FileManager

# Setup logging before anything else
def _init_logging():
    config = read_app_config()
    log_level = get_log_level_from_config(config)
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../logs')
    log_file = setup_logging(log_dir, log_level)
    print(f"Logging to: {log_file}")
    return log_file

_init_logging()
logger = get_logger("brainmaze_mef3_server")


class gRPCMef3Server(gRPCMef3Server_pb2_grpc.gRPCMef3ServerServicer):
    """gRPC servicer for MEF3 files. Stateless, delegates all requests to FileManager."""

    def __init__(self, file_manager):
        """Initializes the gRPC servicer.

        Args:
            file_manager (FileManager): The FileManager instance to delegate requests to.
        """
        self.manager = file_manager

    def OpenFile(self, request, context):
        logger.info(f"Received OpenFile request for: {request.file_path}")
        try:
            return self.manager.open_file(request.file_path)
        except Exception as e:
            logger.error(f"Exception in OpenFile: {e}")
            return gRPCMef3Server_pb2.FileInfoResponse(
                file_path=request.file_path,
                file_opened=False,
                error_message=str(e)
            )

    def CloseFile(self, request, context):
        logger.info(f"Received CloseFile request for: {request.file_path}")
        try:
            return self.manager.close_file(request.file_path)
        except Exception as e:
            logger.error(f"Exception in CloseFile: {e}")
            return gRPCMef3Server_pb2.FileInfoResponse(
                file_path=request.file_path,
                file_opened=False,
                error_message=str(e)
            )

    def FileInfo(self, request, context):
        logger.info(f"Received FileInfo request for: {request.file_path}")
        try:
            return self.manager.get_file_info(request.file_path)
        except Exception as e:
            logger.error(f"Exception in FileInfo: {e}")
            return gRPCMef3Server_pb2.FileInfoResponse(
                file_path=request.file_path,
                file_opened=False,
                error_message=str(e)
            )

    def GetSignalRange(self, request, context):
        logger.info(
            f"Received GetSignalRange for [{request.start_uutc}, {request.end_uutc}) "
            f"from: {request.file_path}"
        )
        try:
            yield from self.manager.stream_signal_range(
                request.file_path,
                list(request.channel_names),
                request.start_uutc,
                request.end_uutc,
            )
        except Exception as e:
            logger.error(f"Exception in GetSignalRange: {e}")
            yield gRPCMef3Server_pb2.SignalChunk(
                file_path=request.file_path,
                error_message=str(e)
            )

    def ListOpenFiles(self, request, context):
        logger.info("Received ListOpenFiles request")
        try:
            file_paths = self.manager.list_open_files()
            return gRPCMef3Server_pb2.ListOpenFilesResponse(file_paths=file_paths, error_message="")
        except Exception as e:
            logger.error(f"Exception in ListOpenFiles: {e}")
            return gRPCMef3Server_pb2.ListOpenFilesResponse(file_paths=[], error_message=str(e))


class gRPCMef3ServerHandler:
    """Handler to launch and manage the gRPC MEF3 server lifecycle."""

    def __init__(self, port, max_workers=4,
                 tile_duration_s=60, tile_cache_bytes=512 * 1024 * 1024,
                 use_process_pool=True, reader_processes=None, prefetch_processes=None,
                 min_parallel_tiles=2, prefetch_ahead_windows=1,
                 prefetch_behind_windows=1, cache_ttl_s=1800):
        """Initializes the gRPC server and FileManager.

        Args:
            port (int): Port to listen on.
            max_workers (int): Max worker threads for the prefetch thread fallback.
            tile_duration_s (float): Tile length (seconds) for timestamp-based access.
            tile_cache_bytes (int): Global tile cache byte budget.
            use_process_pool (bool): Decode in worker processes for parallel decode.
            reader_processes (int or None): Total decode worker processes (auto cpu-1).
            prefetch_processes (int or None): Background prefetch lane size (auto half).
            min_parallel_tiles (int): Min missing tiles before fanning out to the pool.
            prefetch_ahead_windows (int): Windows to prefetch ahead (page forward).
            prefetch_behind_windows (int): Windows to prefetch behind (page backward).
            cache_ttl_s (float or None): Discard tiles idle longer than this.
        """
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

        # Pass parameters to FileManager
        self.file_manager = FileManager(
            max_workers=max_workers,
            tile_duration_s=tile_duration_s,
            tile_cache_bytes=tile_cache_bytes,
            use_process_pool=use_process_pool,
            reader_processes=reader_processes,
            prefetch_processes=prefetch_processes,
            min_parallel_tiles=min_parallel_tiles,
            prefetch_ahead_windows=prefetch_ahead_windows,
            prefetch_behind_windows=prefetch_behind_windows,
            cache_ttl_s=cache_ttl_s,
        )

        gRPCMef3Server_pb2_grpc.add_gRPCMef3ServerServicer_to_server(
            gRPCMef3Server(self.file_manager), self.server
        )
        self.server.add_insecure_port(f"0.0.0.0:{port}")
        self.server.start()
        logger.info(f"gRPC server started on port {port}.")

    def stop(self, grace=0.1):
        logger.info("Stopping gRPC server...")
        # --- NEW: Shut down the file manager's resources ---
        self.file_manager.shutdown()
        self.server.stop(grace)
