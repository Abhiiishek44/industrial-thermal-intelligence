# Wildfire Decision Support — Local Setup (Without Docker)

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| PostgreSQL | 16+ | Must include PostGIS extension |
| Python | 3.11+ | |
| GDAL | system | Required by rasterio / geopandas |

**Install PostGIS:**
- **Windows**: Download PostgreSQL from [postgresql.org](https://www.postgresql.org/download/windows/) and select PostGIS in Stack Builder during installation
- **Mac**: `brew install postgresql postgis`
- **Ubuntu**: `sudo apt install postgresql postgresql-contrib postgis`

**Install GDAL (required for rasterio / geopandas):**
- **Windows**: Install [OSGeo4W](https://trac.osgeo.org/osgeo4w/) or use the [GDAL wheel from Christoph Gohlke](https://github.com/cgohlke/geospatial-wheels/releases). Docker is strongly recommended on Windows to avoid GDAL/PROJ compatibility issues.
- **Mac**: `brew install gdal`
- **Ubuntu**: `sudo apt install gdal-bin libgdal-dev libproj-dev proj-data proj-bin libgeos-dev libspatialindex-dev`

---

## Step 1: Configure Environment Variables

Copy the example file and update your credentials:

```bash
# Run from the project root (wildfire-decision-support/)
cp .env.example .env
```

For local development, ensure `DB_HOST=localhost` in your `.env`:

```env
# Admin account (seeded on first startup)
ADMIN_PASSWORD=change-me

# Flask Backend
DB_HOST=localhost
DB_PORT=5432
DB_NAME=wildfire_db
DB_USER=postgres
DB_PASSWORD=your_password
SECRET_KEY=some_long_random_str

# PostgreSQL Container (not used locally, can be left as-is)
POSTGRES_DB=wildfire_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

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

## Step 2: Create the Database

```bash
psql -U postgres -c "CREATE DATABASE wildfire_db;"
```

---

## Step 3: Install Python Dependencies

```bash
# Run from the project root (wildfire-decision-support/)
pip install -r requirements.txt
```

> **Windows note:** If rasterio or geopandas fail to install, install GDAL first via OSGeo4W or use pre-built wheels. Docker is the easier path on Windows.

---

## Step 4: Run Flask

```bash
cd backend
python main.py
```

On startup, Flask will:
1. Create all database tables via SQLAlchemy (PostGIS extension, `users`, `fire_events`, `event_timesteps`, `themes`, `field_reports`, `field_report_comments`, `theme_comments`)
2. Seed the admin account using `ADMIN_PASSWORD` from `.env`
3. Start the pipeline in a background thread
4. Serve at `http://localhost:5000`

---

## Common Commands

```bash
# Open database shell
psql -U postgres -d wildfire_db

# Reset database (drop and recreate — tables are recreated on next startup)
psql -U postgres -c "DROP DATABASE wildfire_db;"
psql -U postgres -c "CREATE DATABASE wildfire_db;"
```

---

## Troubleshooting

**`could not load library "postgis"` or `extension "postgis" is not available`**  
PostGIS is not installed or not linked to this PostgreSQL instance. Reinstall PostgreSQL with PostGIS via Stack Builder (Windows) or `brew install postgis` (Mac).

**`psql: command not found`**  
Add PostgreSQL `bin/` to your PATH.  
Windows example: `C:\Program Files\PostgreSQL\16\bin`

**`password authentication failed`**  
Check that `DB_PASSWORD` in `.env` matches your local PostgreSQL password.

**`Port 5000 already in use`**  
Another process is using port 5000. Change the port in `backend/main.py`:
```python
app.run(host='0.0.0.0', debug=False, port=5001)
```

**rasterio / PROJ errors on Windows**  
Use Docker instead. See `DOCKER_SETUP.md`. GDAL/PROJ version mismatches are common on Windows and Docker eliminates them entirely.
