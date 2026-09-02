# Industrial Thermal Intelligence

An explainable geospatial monitoring platform for identifying and investigating
thermal activity across Indian industrial corridors and forest landscapes. It
turns NASA FIRMS hotspot detections into map-ready evidence by adding land
cover, nearby industrial context, and persistence analysis, then presents the
results through a Flask API and browser dashboard.

![Dashboard showing regional thermal activity, persistent sources, and classifications](assets/Screenshot_1.png?v=1ecfa735f3c3)

The dashboard helps analysts move from a raw satellite detection to a more
useful question: is this likely industrial process heat, gas flaring,
agricultural burning, mining activity, wildfire, or an unresolved source?

## Live deployment

Deployment URL: https://industrial-thermal-intelligence-production.up.railway.app/

## Why it matters

### Detect

Collect historical and near-real-time NASA FIRMS observations across configured
regions, aggregate multi-sensor data, and keep current monitoring views fresh
when a FIRMS key is available.

### Explain

Enrich each detection with spatial context, identify persistent hotspot
clusters, and apply transparent source-type rules. Classifications include
`industrial_fire`, `gas_flare`, `agricultural_burning`, `mining_activity`,
`wildfire`, `industrial_process_heat`, and `unknown`.

### Investigate

Explore detection history, timeline playback, persistent sources,
classifications, and contextual GeoJSON layers in a Leaflet dashboard or via
the REST API.

## Monitoring coverage

The seeded public India catalog contains ten regions.

**Industrial corridors**

- Vijayanagar
- Talcher-Angul
- Dhanbad-Bokaro
- Singrauli-Sonbhadra
- Korba
- Jamnagar-Vadinar

**Forest-fire landscapes**

- Gadchiroli-Tadoba
- Kanha-Pench
- Bastar
- Mizoram

The region definitions and their programmatic IDs are maintained in
[backend/pipeline/regions.py](backend/pipeline/regions.py) and seeded through
[backend/pipeline/event_config.py](backend/pipeline/event_config.py).

## How it works

```text
NASA FIRMS detections
        |
        v
Collection, normalization, and regional filtering
        |
        v
Spatial enrichment and persistence analysis
        |
        v
Explainable source classification ----> PostgreSQL + PostGIS
                                           |
                                           v
                              Flask API and Leaflet dashboard (/demo)
```

The application can optionally schedule near-real-time refreshes and expose
GeoJSON for detections, persistent clusters, classifications, and contextual
layers.

## Repository layout

- [backend](backend) - Flask application, API routes, database models, and data pipeline
- [frontend](frontend) - Leaflet interface, templates, styles, and browser logic
- [data](data) - processed inputs, generated outputs, model assets, and uploads
- [docker](docker) - container and deployment resources
- [tests](tests) - thermal-monitoring, training-data, and dashboard regression tests
- [docs/api.yaml](docs/api.yaml) - OpenAPI contract
- [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) - detailed repository guide

## Technology choices

- **Application:** Python, Flask, SQLAlchemy
- **Geospatial data:** PostgreSQL with PostGIS, GeoPandas, Rasterio, and Shapely
- **Interface:** Leaflet and vanilla JavaScript
- **Analysis:** pandas, NumPy, scikit-learn, and XGBoost
- **Optional AI integration:** Anthropic or Google Gemini

## Run locally

### Minimum dashboard setup

You need Python 3.11+, `pip`, and a running PostgreSQL instance with PostGIS
available. A FIRMS map key is not required to start the app, but live thermal
refreshes are disabled without one.

Install dependencies from the repository root:

```bash
pip install -r requirements.txt
```

Create `.env` from the provided example, then start with the smallest useful
local configuration:

```powershell
Copy-Item .env.example .env
```

```bash
cp .env.example .env
```

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=thermal_intelligence_db
DB_USER=postgres
DB_PASSWORD=change-me-strong-password

JWT_SECRET_KEY=change-me-random-64-chars
ACTIVE_REGION=vijayanagar
AUTO_PREPARE_REGIONS=vijayanagar

FIRMS_HISTORY_AUTO_FETCH=0
THERMAL_AUTO_REFRESH=0
```

Run the server:

```bash
cd backend
python main.py
```

Open [http://localhost:5000/demo](http://localhost:5000/demo). On startup, the
app connects to or creates the configured database, enables PostGIS, seeds the
region catalog, and prepares the selected region in the background.

### Optional capabilities

Add only the settings needed for the functionality you want:

- **Live FIRMS refresh:** set `FIRMS_API_KEY`, `THERMAL_AUTO_REFRESH=1`, and an
  optional `THERMAL_REFRESH_INTERVAL_HOURS` value.
- **All public regions:** set `AUTO_PREPARE_REGIONS=all`; this can require more
  time and storage than a single-region setup.
- **AI reports and chat:** set `LLM_PROVIDER` to `claude` or `gemini` and add
  the matching `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`.
- **ERA5 wildfire preparation:** provide a Copernicus CDS token when using the
  related legacy replay tooling.
- **Seeded administrator:** set `ADMIN_USERNAME` and a non-empty
  `ADMIN_PASSWORD`.

## API

The dashboard is served at `/demo`. Primary API areas are:

- `/auth` for registration, login, token refresh, logout, and current-user details
- `/api/regions` and `/api/events` for available monitoring areas and events
- `/api/events/<event_id>/thermal/*` for overview, history, refresh status,
  detections, persistent clusters, and classifications
- `/api/events/<event_id>/field-reports` for authenticated crowd reports

The complete request and response contract is available in
[docs/api.yaml](docs/api.yaml).

## Screenshots

### Field evidence

![Field-report form for submitting a geolocated observation](assets/Screenshot_2.png?v=1a9642c0c82a)

Authenticated users can contribute location-based field reports to support
incident investigation.

### Decision-support situation report

![Thermal-monitoring decision report with priority signals, risks, and actions](assets/Screenshot_3.png?v=ee951c8be690)

A structured thermal-monitoring briefing summarizes priority signals,
classification results, key risks, and recommended immediate actions.

### Thermal analysis and verification

![Thermal-cluster classification with confidence and supporting evidence](assets/Screenshot_4.png?v=31babce79a13)

Inspect a thermal cluster's classification, confidence, detected activity,
nearby facility, and supporting spatial evidence.

## Testing

Run the test suite from the repository root:

```bash
python -m pytest tests
```

The test suite covers thermal data preparation, persistence and
classification, region configuration, training data, and dashboard behavior.

## Legacy research workflow

The repository retains a separate Fort McMurray 2016 wildfire replay for
research and demonstrations. It includes timestep-based predictions,
evacuation and road-impact analysis, synthetic crowd activity, and optional AI
situation reports. This workflow is not the primary public monitoring product.

## Operational notes

- The source classifier is rule-based and explainable; it is not a validated
  production model.
- Without `FIRMS_API_KEY`, the application uses available prepared data and
  disables scheduled live refreshes.
- Some prepared datasets are intentionally excluded from Git and are generated
  or fetched when the relevant workflow is enabled.
- External services, including FIRMS, CDS, Anthropic, and Gemini, enable only
  their respective optional capabilities; they are not all required to explore
  the core dashboard.
