# ==============================================================================
# CLIQ — Production Multi-Stage Dockerfile
# Stage 1: Build React/Vite/Tailwind frontend bundle
# Stage 2: Minimal Python 3.11 runtime with Uvicorn & compiled command DB
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Frontend Build
# ------------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /build/frontend

# Install dependencies using clean reproducible install
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Copy frontend source files
COPY frontend/ ./

# Compile production bundle to /build/frontend/dist
RUN npm run build

# ------------------------------------------------------------------------------
# Stage 2: Runtime Image
# ------------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Label metadata
LABEL maintainer="CLIQ Engineering Team" \
      description="CLIQ — Networking Device CLI Intelligence & Multi-Device Terminal Platform" \
      version="0.1.0"

# Set environment defaults
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    CLIQ_HOST=0.0.0.0 \
    CLIQ_PORT=8000 \
    CLIQ_FRONTEND_DIR=/app/frontend/dist \
    CLIQ_SESSION_DB=/app/.cliq/session_log.sqlite

# Install minimal system runtime packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root system user and group
RUN groupadd -r cliq && useradd -r -g cliq -d /app -s /sbin/nologin cliq

WORKDIR /app

# Install Python dependencies
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy source-of-truth command definitions and compiler
COPY command_db/ ./command_db/
COPY build_db.py ./
COPY src/ ./src/

# Compile SQLite command intelligence database from YAML definitions
RUN python build_db.py

# Install CLIQ package
RUN pip install --no-cache-dir -e .

# Copy compiled frontend from Stage 1
COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist

# Create runtime directories for data persistence and logging
RUN mkdir -p /app/.cliq/logs /app/.cliq/memory /app/logs \
    && chown -R cliq:cliq /app

# Switch to non-root execution
USER cliq

# Expose HTTP and WebSocket port
EXPOSE 8000

# Container healthcheck using standard library urllib against /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).getcode() == 200 else 1)"

# Start FastAPI application with Uvicorn
CMD ["python", "-m", "uvicorn", "cliq.web.server:app", "--host", "0.0.0.0", "--port", "8000"]
