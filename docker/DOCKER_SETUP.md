# Wildfire Decision Support — Docker Setup Guide

## Prerequisites

- Install [Docker Desktop](https://www.docker.com/products/docker-desktop) (Windows/Mac) or Docker Engine (Linux)
- Launch Docker Desktop and confirm the icon in the system tray shows **Running** (green)

---

## Step 1: Configure Environment Variables

The `.env` file must be in the **project root** (not inside `docker/`).  
Copy the example file to create your own:

```bash
# Run from the project root (wildfire-decision-support/)
cp .env.example .env
```

Open `.env` and update the following:

```env
# Admin account (seeded on first startup)
ADMIN_PASSWORD=change-me-before-deploy

# Flask Backend
DB_NAME=wildfire_db
DB_USER=postgres
DB_PASSWORD=your_password        # change this
SECRET_KEY=some_long_random_str  # always change for production

# PostgreSQL Container (must match DB_* above)
POSTGRES_DB=wildfire_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password  # must be identical to DB_PASSWORD

# LLM Provider — "claude" (default) or "gemini"
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=your_anthropic_api_key
GEMINI_API_KEY=your_gemini_api_key   # only needed if LLM_PROVIDER=gemini

# Optional integrations
SENTINELHUB_CLIENT_ID=...       # Sentinel-2 satellite basemap
SENTINELHUB_CLIENT_SECRET=...
FIRMS_API_KEY=...               # NASA FIRMS active fire data
CDS_KEY=...                     # Copernicus ERA5-Land weather data
EARTHDATA_TOKEN=...             # NASA VIIRS cloud mask (optional)
```

> See `.env.example` for full documentation on each key and where to obtain them.

---

## Development Setup

`docker-compose.yml` runs two services: `db` and `backend`.  
Backend and frontend source files are mounted as volumes so edits take effect without rebuilding.

```bash
cd docker
docker compose up --build
```

> `--build` is required the first time. Subsequent restarts don't need it unless you change `Dockerfile` or `requirements.txt`.

On first startup Docker will:
1. Download `postgis/postgis:16-3.4` and `python:3.11-slim` (~500 MB, requires internet)
2. Run `init.sql` to create the database schema automatically
3. Seed the admin account using `ADMIN_PASSWORD` from `.env`

When everything is ready you should see:
```
backend-1  |  * Running on http://0.0.0.0:5000
```

### Dev Services

| Service | URL |
|---|---|
| Flask backend | http://localhost:5000 |
| PostgreSQL | localhost:5432 (credentials from `.env`) |

---

## Production Setup

Production uses `docker-compose.prod.yml`, which adds a **Caddy** reverse proxy for HTTPS.

### 1. Edit Caddyfile

Replace `YOUR_DOMAIN` in `docker/Caddyfile` with your actual domain or server IP:

```
# Domain (Caddy auto-obtains Let's Encrypt TLS certificate)
wildfire.yourdomain.com { ... }

# IP only (no TLS)
http://1.2.3.4 { ... }
```

### 2. Deploy with deploy.sh

Run from your **local machine**:

```bash
bash docker/deploy.sh <server-ip> [ssh-user]
# e.g.
bash docker/deploy.sh 1.2.3.4 root
```

The script will:
1. Rsync `data/events/2016_0001/` to the server (skips files already present)
2. Clone or pull the latest code on the server
3. Upload your local `.env` to the server
4. Rebuild and restart the Docker stack with `docker-compose.prod.yml`

### Production Services

| Service | Details |
|---|---|
| Caddy | Ports 80 / 443, handles TLS + reverse proxy |
| Flask backend | Internal only (not exposed to host) |
| PostgreSQL | Internal only |

---

## Common Commands

> All commands run from `wildfire-decision-support/docker/`

```bash
# Run in background (detached mode)
docker compose up -d --build

# View logs
docker compose logs -f
docker compose logs -f backend        # backend only

# Stop all services
docker compose down

# Stop and delete all data (full reset)
docker compose down -v

# Reset database and rebuild from scratch (e.g. after changing .env)
docker compose down -v && docker compose up --build

# Rebuild a single service (e.g. after editing Dockerfile)
docker compose up --build backend

# Open a database shell
docker compose exec db psql -U postgres -d wildfire_db

# Production equivalents — append -f docker-compose.prod.yml
docker compose -f docker-compose.prod.yml logs -f backend
docker compose -f docker-compose.prod.yml down -v
```

---

## Troubleshooting

**`docker compose: command not found`**  
Older Docker versions use `docker-compose` (with hyphen). Try: `docker-compose up --build`

**`Port 5432 already in use`**  
A local PostgreSQL instance is running on the same port. Stop it, or change the host port in `docker-compose.yml`:
```yaml
ports: "5433:5432"
```

**`backend: Connection refused (cannot reach db)`**  
The DB may still be initializing. Wait a few seconds — the backend will retry. Check: `docker compose logs db`

**`relation "..." does not exist` (init.sql not executed)**  
PostgreSQL skips `init.sql` when a volume already exists. Run:
```bash
docker compose down -v && docker compose up --build
```

**`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` errors**  
Ensure the correct key is set in `.env` and `LLM_PROVIDER` matches the key you provided.
