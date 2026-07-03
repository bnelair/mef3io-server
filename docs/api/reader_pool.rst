Reader Pool
===========

The ``ReaderProcessPool`` decodes MEF3 windows in parallel across worker
processes (pymef decode is GIL-bound), each with its own ``MefReader`` session.

.. automodule:: brainmaze_mef3_server.server.reader_pool
   :members:
   :undoc-members:
   :show-inheritance:
