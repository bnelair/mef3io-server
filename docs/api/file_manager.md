# File manager

`FileManager` manages the state and operations for multiple MEF files in a
thread-safe manner, backing the channels-and-time access path with a shared
tile cache, parallel decode, and window prefetch.

::: mef3io_server.server.file_manager
