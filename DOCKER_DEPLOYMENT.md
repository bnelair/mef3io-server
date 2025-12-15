# Docker Deployment Guide

This document describes how to build and deploy the BNEL MEF3 Server using Docker.

## Available Docker Images

The project provides Docker images through two container registries:

### GitHub Container Registry (Recommended for GitHub users)
- **Registry**: `ghcr.io`
- **Image**: `ghcr.io/bnelair/brainmaze-mef3-server`
- **Tags**: 
  - `latest` - Latest stable release from main branch
  - `v*.*.*` - Specific version tags (e.g., `v0.0.2`)
  - `main` - Latest from main branch
  - `<branch-name>` - Development branches

### GitLab Container Registry
- **Registry**: `registry.gitlab.com`
- **Image**: `registry.gitlab.com/bnelair/brainmaze-mef3-server`
- **Tags**: Same as GitHub

## Quick Start

### Using GitHub Container Registry

```bash
# Pull the latest image
docker pull ghcr.io/bnelair/brainmaze-mef3-server:latest

# Run the server
docker run -d \
  --name mef3-server \
  -p 50051:50051 \
  -v /path/to/your/mef/files:/data:ro \
  ghcr.io/bnelair/brainmaze-mef3-server:latest
```

### Using GitLab Container Registry

```bash
# Pull the latest image
docker pull registry.gitlab.com/bnelair/brainmaze-mef3-server:latest

# Run the server
docker run -d \
  --name mef3-server \
  -p 50051:50051 \
  -v /path/to/your/mef/files:/data:ro \
  registry.gitlab.com/bnelair/brainmaze-mef3-server:latest
```

## Configuration

### Environment Variables

The server supports the following environment variables:

- `N_PROCESS_WORKERS` - Number of worker processes for parallel I/O (default: 2)
  ```bash
  docker run -e N_PROCESS_WORKERS=4 ghcr.io/bnelair/brainmaze-mef3-server:latest
  ```

### Volume Mounts

Mount your MEF3 data directory:

```bash
docker run -d \
  -v /path/to/mef/data:/data:ro \
  -p 50051:50051 \
  ghcr.io/bnelair/brainmaze-mef3-server:latest
```

**Important**: Use `:ro` (read-only) flag for data volumes for security.

## Production Deployment

### Docker Compose

Create a `docker-compose.yml`:

```yaml
version: '3.8'

services:
  mef3-server:
    image: ghcr.io/bnelair/brainmaze-mef3-server:latest
    container_name: mef3-server
    restart: unless-stopped
    ports:
      - "50051:50051"
    volumes:
      - /path/to/mef/data:/data:ro
    environment:
      - N_PROCESS_WORKERS=2
      - PYTHONUNBUFFERED=1
    healthcheck:
      test: ["CMD", "python", "-c", "import grpc; channel = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(channel).result(timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 4G
        reservations:
          cpus: '2'
          memory: 2G
```

Run with:
```bash
docker-compose up -d
```

### Kubernetes Deployment

Example Kubernetes deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mef3-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mef3-server
  template:
    metadata:
      labels:
        app: mef3-server
    spec:
      containers:
      - name: mef3-server
        image: ghcr.io/bnelair/brainmaze-mef3-server:latest
        ports:
        - containerPort: 50051
          protocol: TCP
        env:
        - name: N_PROCESS_WORKERS
          value: "2"
        volumeMounts:
        - name: mef-data
          mountPath: /data
          readOnly: true
        resources:
          requests:
            memory: "2Gi"
            cpu: "2"
          limits:
            memory: "4Gi"
            cpu: "4"
        livenessProbe:
          exec:
            command:
            - python
            - -c
            - "import grpc; channel = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(channel).result(timeout=5)"
          initialDelaySeconds: 10
          periodSeconds: 30
      volumes:
      - name: mef-data
        hostPath:
          path: /path/to/mef/data
          type: Directory
---
apiVersion: v1
kind: Service
metadata:
  name: mef3-server
spec:
  selector:
    app: mef3-server
  ports:
  - port: 50051
    targetPort: 50051
    protocol: TCP
  type: ClusterIP
```

## Building Images Locally

### Using the Production Dockerfile

```bash
# Build for production
docker build -t mef3-server:local -f Dockerfile .

# Run locally built image
docker run -d -p 50051:50051 -v /path/to/data:/data:ro mef3-server:local
```

### Using the Local Development Dockerfile

For development with live code:

```bash
# Build local development image
docker build -t mef3-server:dev -f Dockerfile_local .

# Run with code mounted
docker run -d -p 50051:50051 -v $(pwd):/app mef3-server:dev
```

## CI/CD Integration

### GitHub Actions

The project automatically builds and publishes Docker images on:
- **Push to main**: Creates `latest` and `main` tags
- **Push tags** (e.g., `v1.0.0`): Creates version tags and updates `latest`
- **Pull requests**: Builds but doesn't publish (for testing)

See `.github/workflows/docker-publish.yml` for details.

### GitLab CI/CD

The project automatically builds and publishes Docker images on:
- **Push to branches**: Creates branch-specific tags
- **Push to main**: Creates `latest` and `main` tags
- **Push tags** (e.g., `v1.0.0`): Creates version tags (major, minor, patch)

See `.gitlab-ci.yml` for details.

## Security Best Practices

1. **Run as non-root user**: The production image runs as user `mefserver` (UID 1000)
2. **Read-only data volumes**: Always mount MEF data as read-only (`:ro`)
3. **Resource limits**: Set CPU and memory limits in production
4. **Health checks**: Enable health checks to monitor server availability
5. **Network isolation**: Use Docker networks to isolate services
6. **Regular updates**: Pull latest images regularly for security updates

## Troubleshooting

### Check server logs
```bash
docker logs mef3-server
```

### Test server connectivity
```bash
# From host
python -c "import grpc; channel = grpc.insecure_channel('localhost:50051'); print('Connected' if grpc.channel_ready_future(channel).result(timeout=5) else 'Failed')"
```

### Inspect running container
```bash
docker exec -it mef3-server /bin/bash
```

### Check resource usage
```bash
docker stats mef3-server
```

## Performance Tuning

### Adjust worker processes
```bash
# For machines with more CPUs
docker run -e N_PROCESS_WORKERS=4 ghcr.io/bnelair/brainmaze-mef3-server:latest
```

### Memory considerations
- Base memory: ~500MB
- Per worker process: ~100-200MB
- Cache memory: Depends on segment size and cache capacity

See the main [README.md](README.md) for detailed performance tuning guidelines.

## Support

For issues, questions, or contributions:
- GitHub: https://github.com/bnelair/brainmaze-mef3-server
- GitLab: https://gitlab.com/bnelair/brainmaze-mef3-server
