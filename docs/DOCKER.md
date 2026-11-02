# CLIQ Docker Deployment & Operations Guide

This guide describes how to build, configure, deploy, and operate CLIQ using Docker and Docker Compose.

---

## 1. Overview & Container Architecture

CLIQ is packaged as a single, self-contained production container combining the FastAPI backend, the compiled React/xterm.js frontend, and the compiled SQLite command intelligence catalog.

```
┌─────────────────────────────────────────────────────────────┐
│                      Stage 1: Builder                       │
│    node:20-alpine                                           │
│    - npm ci                                                 │
│    - npm run build ──► produces /build/frontend/dist        │
└──────────────────────────────┬──────────────────────────────┘
                               │ COPY --from=frontend-builder
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                      Stage 2: Runtime                       │
│    python:3.11-slim (non-root user: cliq)                   │
│    - Python dependencies from requirements.txt              │
│    - Compiled command catalog: command_db.sqlite            │
│    - Frontend assets: /app/frontend/dist                    │
│    - Data volume: /app/.cliq (operational memory & logs)    │
│    - Single-port Uvicorn server (:8000)                     │
└─────────────────────────────────────────────────────────────┘
```

### Key Architectural Characteristics
- **Multi-Stage Build**: Keeps the runtime image clean and lightweight by excluding Node.js, `npm`, development compilers, and source `node_modules`.
- **Pre-Compiled Command Database**: The source YAML files in `command_db/` are validated and compiled into `/app/command_db.sqlite` at image build time, guaranteeing instant microsecond cold-start lookups.
- **Unified Single-Port Delivery**: FastAPI serves the compiled SPA from `/app/frontend/dist` while simultaneously handling `/api/*` REST endpoints and `/ws` multiplexed WebSockets on port 8000.
- **Non-Root Execution**: Runs under an unprivileged `cliq` system user (`UID/GID 999/999` or dynamic system pool) to maintain container isolation.
- **Air-Gapped & Offline Ready**: Core terminal, multi-device management, CML integration, and command intelligence require zero external internet access.

---

## 2. Prerequisites

- **Docker Engine**: Version 24.0 or higher
- **Docker Compose**: Version 2.20 or higher (or Docker Desktop 4.25+)
- **Operating System**: Linux (Ubuntu, Debian, RHEL, Rocky), macOS, or Windows 10/11 with Docker Desktop (WSL2 backend)

---

## 3. Quick Start

### 1. Prepare Environment Configuration

Copy the provided environment template:

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```
```bash
# Linux / macOS
cp .env.example .env
```

Review `.env` and configure any specific ports or CML parameters (all parameters have sensible defaults).

### 2. Build the Docker Image

```bash
docker compose build
```

### 3. Launch CLIQ in Background

```bash
docker compose up -d
```

### 4. Verify Application Health

Open your browser and navigate to:
```text
http://localhost:8000/
```

Check the health endpoint directly:
```bash
curl http://localhost:8000/health
```
Expected response:
```json
{"status":"healthy"}
```

---

## 4. Useful Operations & Lifecycle Management

| Action | Command |
|---|---|
| **View Service Status** | `docker compose ps` |
| **Follow Live Container Logs** | `docker compose logs -f cliq` |
| **Restart the CLIQ Service** | `docker compose restart cliq` |
| **Stop Containers (Preserve Data)** | `docker compose down` |
| **Stop Containers and Remove Volumes** | `docker compose down -v` |
| **Rebuild After Code Changes** | `docker compose build --no-cache && docker compose up -d` |
| **Execute Shell Inside Container** | `docker compose exec cliq bash` |

---

## 5. Networking & Real Device Connectivity

Network engineering tools require reliable connectivity to external routers, switches, and hypervisors. Docker networking in CLIQ is designed to support all deployment environments:

### Reaching External CML Controllers & SSH Gateways
- By default, Docker Compose deploys CLIQ on a standard bridge network.
- Outbound packets destined for physical subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) or VPN routes are translated via host NAT.
- Ensure host firewall rules permit outbound traffic on:
  - Port `443` / `80` (CML REST API)
  - Port `22` (SSH directly to devices or console servers)
  - Custom console breakout ports (e.g. `2001–2099` or `5000–5050` for telnet/console proxies)

### Reaching Services Running on the Host Machine
If your CML instance, GNS3 server, or SSH terminal server is running on the host OS itself (e.g. VMware Workstation / VirtualBox on the same PC):

- In Docker Desktop (Windows / macOS) and modern Linux Docker Compose, use:
  ```text
  host.docker.internal
  ```
  `docker-compose.yml` includes `extra_hosts: - "host.docker.internal:host-gateway"` to ensure this hostname resolves across all platforms.
- Example: In the CLIQ login dialog, enter `host.docker.internal` instead of `localhost` or `127.0.0.1`.

### Windows Docker Desktop & Corporate VPN Considerations
When running Docker Desktop on Windows in enterprise network environments:
- **Corporate VPN Routing**: Software like Cisco AnyConnect, Palo Alto GlobalProtect, or Zscaler may alter host routing tables. If the container cannot reach a lab subnet (`10.x.x.x`) while your host VPN is active, enable "Use the WSL 2 based engine" and verify that WSL 2 network mirroring or Docker Desktop "Mirrored networking mode" (`networkingMode=mirrored` in `.wslconfig`) is enabled.
- **DNS Suffix Resolution**: If CML is reachable on the host via an internal domain suffix (e.g. `cml.lab.corp`), but fails inside the container, configure the full FQDN or specify custom DNS servers in Docker Compose under `dns:` (e.g. `dns: [10.x.x.x]`).

### Linux Host Networking Mode (`network_mode: "host"`)
On native Linux servers, you can optionally switch the service to host networking to eliminate NAT overhead and permit direct layer-2 interaction:

```yaml
services:
  cliq:
    build: .
    network_mode: "host"
    # Note: 'ports' directive is omitted when using host networking
```

### WebSocket Streaming Connectivity
CLIQ’s terminal multiplexer uses WebSockets on `/ws`.
- No secondary ports are required.
- If placing CLIQ behind an external reverse proxy (NGINX, Traefik, HAProxy), ensure `Upgrade` and `Connection` HTTP headers are forwarded:
  ```nginx
  location /ws {
      proxy_pass http://127.0.0.1:8000/ws;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "Upgrade";
      proxy_read_timeout 86400;
  }
  ```

---

## 6. Data Persistence & Volume Management

CLIQ cleanly separates immutable image assets from mutable runtime state:

| Resource | Storage Location | Persistence Strategy |
|---|---|---|
| **Command Catalog** | `/app/command_db.sqlite` | **Baked into image** (Compiled from YAML at build time) |
| **Frontend Static Files** | `/app/frontend/dist` | **Baked into image** (Compiled by Node.js at build time) |
| **Source Code** | `/app/src` | **Baked into image** |
| **Operational Memory** | `/app/.cliq/memory/` | **Persisted via `cliq_data` named volume** |
| **Learned Commands** | `/app/.cliq/learned_commands.sqlite` | **Persisted via `cliq_data` named volume** |
| **Audit Logs** | `/app/.cliq/audit.log` | **Persisted via `cliq_data` named volume** |
| **Diagnostic Traces** | `/app/.cliq/logs/` | **Persisted via `cliq_data` named volume** |
| **Session Memory DB** | `/app/.cliq/session_log.sqlite` | **Persisted via `cliq_data` named volume** (Configured via `CLIQ_SESSION_DB`) |

### Volume Backup and Restore
The persistent volume `cliq_data` can be backed up or inspected using standard Docker tools:

```bash
# Export persistent volume to a tarball
docker run --rm -v cliq_app_data:/data -v ${PWD}:/backup alpine tar czf /backup/cliq_data_backup.tar.gz -C /data .

# Restore persistent volume from a tarball
docker run --rm -v cliq_app_data:/data -v ${PWD}:/backup alpine tar xzf /backup/cliq_data_backup.tar.gz -C /data
```

---

## 7. Configuration Reference

All settings can be configured via environment variables in `.env` or passed through Compose:

| Variable | Default | Purpose |
|---|---|---|
| `CLIQ_HOST` | `0.0.0.0` | Container bind address |
| `CLIQ_PORT` | `8000` | Internal listening port |
| `CLIQ_FRONTEND_DIR` | `/app/frontend/dist` | Path to compiled web assets |
| `CLIQ_RESERVATION_POLICY` | `warn` | Policy for NetSim reservation checks (`warn`, `enforce`, `disabled`) |
| `CLIQ_NETSIM_API_KEY` | *(empty)* | Personal NetSim reservation verification key |
| `CML_HOST` | *(empty)* | Default CML controller host/IP |
| `CML_PORT` | `443` | Default CML REST API port |
| `CML_VERIFY_SSL` | `false` | Enable/disable TLS certificate validation for CML |
| `CLIQ_LOG_MAX_BYTES` | `10485760` | Maximum size in bytes before log files rotate (10 MB) |
| `CLIQ_LOG_MAX_BACKUPS` | `5` | Maximum number of rotated log files to retain |
| `HF_HUB_OFFLINE` | `1` | Forces Hugging Face tooling into strict offline mode |
| `TRANSFORMERS_OFFLINE` | `1` | Disables remote model download attempts |

---

## 8. Optional Advisory Dependencies & Air-Gapped Deployments

The core CLIQ platform — multi-device terminal sessions, CML integration, SSH consoles, abbreviation expansion, typo correction, and destructive command gates — functions fully with the base dependencies in `requirements.txt`.

The Phase 6 semantic retrieval stack (`sentence-transformers` and offline vector storage) is an optional advisory component:
- When models are not pre-cached, CLIQ logs an informative notice and runs in standard deterministic mode without error.
- For air-gapped environments wishing to enable semantic natural-language Q&A, pre-cache the Hugging Face model (`sentence-transformers/all-MiniLM-L6-v2`) in your user cache directory or mount it into `/home/cliq/.cache/huggingface`.

---

## 9. Security & Hardening

1. **Unprivileged Container Execution**: The application runs under user `cliq` (`USER cliq`), preventing container breakouts or filesystem privilege escalations.
2. **No Credentials in Images**: The Dockerfile copies only application code and public YAML definitions. Passwords, bearer tokens, and private keys are never included in the image layers.
3. **Volatile Session Tokens**: Terminal session tokens and device passwords exist exclusively in memory for the life of the active WebSocket connection.
4. **Log Sanitization**: Sensitive authentication tokens (JWTs) and Cisco enable passwords are automatically redacted before logs are written to disk.

---

## 10. Troubleshooting

### Port 8000 Already Bound
If host port 8000 is occupied by another service:
```powershell
# Set CLIQ_PORT in .env or run:
$env:CLIQ_PORT=8080
docker compose up -d
```
Access the application at `http://localhost:8080/`.

### Cannot Connect to CML at `localhost`
Inside a container, `localhost` refers to the container itself. To reach a CML server or gateway running on the host machine, use:
```text
host.docker.internal
```

### Container Fails Healthcheck
Inspect container logs to identify startup issues:
```bash
docker compose logs cliq
```
Verify that the internal endpoint responds:
```bash
docker compose exec cliq python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
```

