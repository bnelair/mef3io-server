from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class OpenFileRequest(_message.Message):
    __slots__ = ("file_path", "cache_fs", "allow_mixed_rates")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    CACHE_FS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_MIXED_RATES_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    cache_fs: float
    allow_mixed_rates: bool
    def __init__(self, file_path: _Optional[str] = ..., cache_fs: _Optional[float] = ..., allow_mixed_rates: _Optional[bool] = ...) -> None: ...

class FileInfoRequest(_message.Message):
    __slots__ = ("file_path",)
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    def __init__(self, file_path: _Optional[str] = ...) -> None: ...

class FileInfoResponse(_message.Message):
    __slots__ = ("file_path", "file_opened", "number_of_channels", "channel_names", "channel_sampling_rates", "start_uutc", "end_uutc", "duration_s", "channel_start_uutc", "channel_end_uutc", "channel_served_sampling_rates", "cache_fs", "error_message")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    FILE_OPENED_FIELD_NUMBER: _ClassVar[int]
    NUMBER_OF_CHANNELS_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_NAMES_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_SAMPLING_RATES_FIELD_NUMBER: _ClassVar[int]
    START_UUTC_FIELD_NUMBER: _ClassVar[int]
    END_UUTC_FIELD_NUMBER: _ClassVar[int]
    DURATION_S_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_START_UUTC_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_END_UUTC_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_SERVED_SAMPLING_RATES_FIELD_NUMBER: _ClassVar[int]
    CACHE_FS_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    file_opened: bool
    number_of_channels: int
    channel_names: _containers.RepeatedScalarFieldContainer[str]
    channel_sampling_rates: _containers.RepeatedScalarFieldContainer[float]
    start_uutc: int
    end_uutc: int
    duration_s: float
    channel_start_uutc: _containers.RepeatedScalarFieldContainer[int]
    channel_end_uutc: _containers.RepeatedScalarFieldContainer[int]
    channel_served_sampling_rates: _containers.RepeatedScalarFieldContainer[float]
    cache_fs: float
    error_message: str
    def __init__(self, file_path: _Optional[str] = ..., file_opened: _Optional[bool] = ..., number_of_channels: _Optional[int] = ..., channel_names: _Optional[_Iterable[str]] = ..., channel_sampling_rates: _Optional[_Iterable[float]] = ..., start_uutc: _Optional[int] = ..., end_uutc: _Optional[int] = ..., duration_s: _Optional[float] = ..., channel_start_uutc: _Optional[_Iterable[int]] = ..., channel_end_uutc: _Optional[_Iterable[int]] = ..., channel_served_sampling_rates: _Optional[_Iterable[float]] = ..., cache_fs: _Optional[float] = ..., error_message: _Optional[str] = ...) -> None: ...

class SignalRangeRequest(_message.Message):
    __slots__ = ("file_path", "channel_names", "start_uutc", "end_uutc", "n_points")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_NAMES_FIELD_NUMBER: _ClassVar[int]
    START_UUTC_FIELD_NUMBER: _ClassVar[int]
    END_UUTC_FIELD_NUMBER: _ClassVar[int]
    N_POINTS_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    channel_names: _containers.RepeatedScalarFieldContainer[str]
    start_uutc: int
    end_uutc: int
    n_points: int
    def __init__(self, file_path: _Optional[str] = ..., channel_names: _Optional[_Iterable[str]] = ..., start_uutc: _Optional[int] = ..., end_uutc: _Optional[int] = ..., n_points: _Optional[int] = ...) -> None: ...

class SignalChunk(_message.Message):
    __slots__ = ("file_path", "array_bytes", "dtype", "shape", "start_uutc", "end_uutc", "fs", "channel_names", "error_message")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    ARRAY_BYTES_FIELD_NUMBER: _ClassVar[int]
    DTYPE_FIELD_NUMBER: _ClassVar[int]
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    START_UUTC_FIELD_NUMBER: _ClassVar[int]
    END_UUTC_FIELD_NUMBER: _ClassVar[int]
    FS_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_NAMES_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    array_bytes: bytes
    dtype: str
    shape: _containers.RepeatedScalarFieldContainer[int]
    start_uutc: int
    end_uutc: int
    fs: float
    channel_names: _containers.RepeatedScalarFieldContainer[str]
    error_message: str
    def __init__(self, file_path: _Optional[str] = ..., array_bytes: _Optional[bytes] = ..., dtype: _Optional[str] = ..., shape: _Optional[_Iterable[int]] = ..., start_uutc: _Optional[int] = ..., end_uutc: _Optional[int] = ..., fs: _Optional[float] = ..., channel_names: _Optional[_Iterable[str]] = ..., error_message: _Optional[str] = ...) -> None: ...

class ListOpenFilesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListOpenFilesResponse(_message.Message):
    __slots__ = ("file_paths", "error_message")
    FILE_PATHS_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    file_paths: _containers.RepeatedScalarFieldContainer[str]
    error_message: str
    def __init__(self, file_paths: _Optional[_Iterable[str]] = ..., error_message: _Optional[str] = ...) -> None: ...

class EnvelopeRequest(_message.Message):
    __slots__ = ("file_path", "channel_names", "start_uutc", "end_uutc")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_NAMES_FIELD_NUMBER: _ClassVar[int]
    START_UUTC_FIELD_NUMBER: _ClassVar[int]
    END_UUTC_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    channel_names: _containers.RepeatedScalarFieldContainer[str]
    start_uutc: int
    end_uutc: int
    def __init__(self, file_path: _Optional[str] = ..., channel_names: _Optional[_Iterable[str]] = ..., start_uutc: _Optional[int] = ..., end_uutc: _Optional[int] = ...) -> None: ...

class ChannelEnvelope(_message.Message):
    __slots__ = ("file_path", "channel_name", "start_uutc", "end_uutc", "min", "max", "error_message")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_NAME_FIELD_NUMBER: _ClassVar[int]
    START_UUTC_FIELD_NUMBER: _ClassVar[int]
    END_UUTC_FIELD_NUMBER: _ClassVar[int]
    MIN_FIELD_NUMBER: _ClassVar[int]
    MAX_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    channel_name: str
    start_uutc: _containers.RepeatedScalarFieldContainer[int]
    end_uutc: _containers.RepeatedScalarFieldContainer[int]
    min: _containers.RepeatedScalarFieldContainer[float]
    max: _containers.RepeatedScalarFieldContainer[float]
    error_message: str
    def __init__(self, file_path: _Optional[str] = ..., channel_name: _Optional[str] = ..., start_uutc: _Optional[_Iterable[int]] = ..., end_uutc: _Optional[_Iterable[int]] = ..., min: _Optional[_Iterable[float]] = ..., max: _Optional[_Iterable[float]] = ..., error_message: _Optional[str] = ...) -> None: ...
