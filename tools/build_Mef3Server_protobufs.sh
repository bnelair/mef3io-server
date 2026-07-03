python -m grpc_tools.protoc \
      -I. --python_out=. \
      --pyi_out=. \
      --grpc_python_out=. \
      mef3io_server/protobufs/gRPCMef3Server.proto
