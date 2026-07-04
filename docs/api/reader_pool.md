# Reader pool

`ReaderProcessPool` decodes MEF3 windows in parallel across worker processes,
each with its own [mef3io](https://github.com/bnelair/mef3io) `MefReader`
session.

::: mef3io_server.server.reader_pool
