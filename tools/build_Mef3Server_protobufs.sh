python -m grpc_tools.protoc \
      -I. --python_out=. \
      --pyi_out=. \
      --grpc_python_out=. \
      brainmaze_mef3_server/protobufs/gRPCMef3Server.proto
