# Production Dockerfile for bnel-mef3-server
# This Dockerfile builds a production-ready image from the current source code

FROM python:3.12-slim

# Set metadata labels
LABEL org.opencontainers.image.title="BNEL MEF3 Server"
LABEL org.opencontainers.image.description="A gRPC server for efficient, concurrent access to MEF3 files"
LABEL org.opencontainers.image.vendor="BNEL Team"
LABEL org.opencontainers.image.source="https://github.com/bnelair/brainmaze-mef3-server"

# Set the working directory
WORKDIR /app

# Install system dependencies if needed (minimal for production)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy only necessary files for installation
COPY pyproject.toml ./
COPY bnel-mef3-server.sh ./
COPY bnel_mef3_server ./bnel_mef3_server

# Make the startup script executable
RUN chmod +x /app/bnel-mef3-server.sh

# Install only production Python dependencies (not test dependencies)
RUN pip install --no-cache-dir \
    numpy~=2.3.3 \
    grpcio~=1.75.0 \
    protobuf~=6.32.1 \
    grpcio-tools~=1.75.0 \
    mef_tools==1.2.3 && \
    rm -rf ~/.cache/pip

# Create a non-root user for security
RUN useradd -m -u 1000 mefserver && \
    chown -R mefserver:mefserver /app

# Switch to non-root user
USER mefserver

# Expose the gRPC server port
EXPOSE 50051

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import grpc; channel = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(channel).result(timeout=5)" || exit 1

# Set environment variables for production
ENV PYTHONUNBUFFERED=1
ENV N_PROCESS_WORKERS=2

# Run the server
CMD ["/app/bnel-mef3-server.sh"]
