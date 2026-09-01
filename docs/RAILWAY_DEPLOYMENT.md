# Railway deployment (Docker)

The application is deployed as a Docker service. Railway builds the root
`Dockerfile`; Railway replaces Docker Compose orchestration with an application
service, a PostGIS service, private networking and a persistent volume.

## Resulting architecture

```text
Railway public HTTPS domain
          |
          v
wildfire-app (Dockerfile, one replica)
      |                  |
      v                  v
PostGIS service     volume at /app/data
```

Caddy is not deployed on Railway because Railway terminates HTTPS. Do not set a
Railway root directory: the Docker build context must remain the repository root.

## 1. Push the source

Commit the deployment files and push the branch to GitHub. Never commit `.env`
or real credentials.

```bash
git add Dockerfile .dockerignore backend/main.py .env.example \
  docker/docker-compose.yml docker/docker-compose.prod.yml \
  docs/RAILWAY_DEPLOYMENT.md README.md
git commit -m "Prepare Docker application for Railway"
git push origin main
```

## 2. Create the Railway project and application service

1. In Railway, create an **Empty Project**.
2. Select **New > GitHub Repo** and choose this repository.
3. Name the service `wildfire-app`.
4. Leave **Root Directory** empty. Railway detects the root `Dockerfile`.
5. Keep the Dockerfile start command; do not add a Railway start-command override.

The container starts `python main.py`, binds to `0.0.0.0`, and reads Railway's
injected `PORT` variable.

## 3. Add PostGIS

The application uses GeoAlchemy geometry columns and enables the `postgis`
extension during startup. Add a **PostGIS** template from Railway's template
marketplace in the same project. A plain PostgreSQL image is insufficient when
the PostGIS extension files are not installed.

Assuming the database service is named `PostGIS`, add these reference variables
to `wildfire-app` using Railway's variable autocomplete:

```env
DB_HOST=${{PostGIS.PGHOST}}
DB_PORT=${{PostGIS.PGPORT}}
DB_NAME=${{PostGIS.PGDATABASE}}
DB_USER=${{PostGIS.PGUSER}}
DB_PASSWORD=${{PostGIS.PGPASSWORD}}
DB_STARTUP_MAX_ATTEMPTS=12
DB_STARTUP_RETRY_SECONDS=5
```

The startup retry settings cover the case where Railway starts the application
before the database is ready. Docker Compose's `depends_on` is not used by
Railway.

## 4. Add a persistent volume

Attach a Railway Volume to `wildfire-app` and set its mount path to:

```text
/app/data
```

This is required for downloaded datasets, generated regional artifacts, FIRMS
caches, uploaded field-report images and cached reports to survive redeploys.
The local data directory is currently about 1.9 GB, so choose a volume with
comfortable growth capacity.

An empty volume is supported: the background preparation pipeline downloads or
rebuilds configured assets after startup. Limit `AUTO_PREPARE_REGIONS` during an
initial trial if you do not want to prepare the complete catalog immediately.

## 5. Configure application variables

Add the following to the `wildfire-app` service. Replace every placeholder.

```env
JWT_SECRET_KEY=<long-random-secret>
JWT_ACCESS_TOKEN_MINUTES=15
JWT_REFRESH_TOKEN_DAYS=30
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<strong-admin-password>

ACTIVE_REGION=vijayanagar
AUTO_PREPARE_REGIONS=all

FIRMS_API_KEY=<nasa-firms-map-key>
FIRMS_HISTORY_AUTO_FETCH=1
THERMAL_AUTO_REFRESH=1
THERMAL_REFRESH_INTERVAL_HOURS=4
THERMAL_FAILURE_RETRY_MINUTES=15
THERMAL_LIVE_LOOKBACK_DAYS=2

THERMAL_DEDUP_RADIUS_M=500
THERMAL_DEDUP_TIME_MINUTES=90
THERMAL_CLUSTER_RADIUS_M=300
THERMAL_CLUSTER_MIN_OBSERVATIONS=2
WORLDPOP_AUTO_DOWNLOAD=1
LOG_LEVEL=INFO
```

Generate a JWT secret locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Configure one AI provider:

```env
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=<key>
```

or:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=<key>
```

`CDS_KEY`, `EARTHDATA_TOKEN`, Sentinel Hub credentials and other variables in
`.env.example` are optional unless their corresponding data features are used.
Seal API keys and passwords in Railway after adding them.

## 6. Configure health and networking

In the application service settings:

1. Set **Healthcheck Path** to `/health`.
2. Keep the health-check timeout at 300 seconds.
3. Under **Networking**, select **Generate Domain**.
4. Keep exactly **one replica**. Multiple web replicas would each start the
   in-process thermal refresh scheduler.

The health endpoint returns HTTP 200 only after the application can query the
database. The data-preparation pipeline continues in the background, so some
regional layers can appear progressively on the first run.

## 7. Deploy and verify

Apply Railway's staged changes and watch the deployment logs. Expected messages
include:

```text
=== Setting up database ===
=== Database ready ===
=== Starting Flask ===
```

Verify the deployment, replacing the domain:

```bash
curl https://wildfire-app-production.up.railway.app/health
curl https://wildfire-app-production.up.railway.app/api/regions/
```

Open the interface at:

```text
https://wildfire-app-production.up.railway.app/demo
```

## Updating the deployment

When GitHub autodeploy is enabled, every push to the configured branch triggers
a new Docker build. The `/app/data` volume and PostGIS database survive those
application redeployments.

Because a volume can be mounted by only one active deployment, expect a short
restart window during application redeploys. Enable Railway backups for both the
PostGIS service and application volume before production use.

## Troubleshooting

- **Railway uses Railpack instead of Docker:** confirm `Dockerfile` is present at
  the repository root and the service Root Directory is empty.
- **Application failed to respond:** confirm logs show the server listening on
  `0.0.0.0` and Railway's `PORT`; do not override `PORT`.
- **PostGIS extension unavailable:** replace plain PostgreSQL with a PostGIS
  template or install/enable PostGIS in the selected database image.
- **Database connection refused:** verify the five `DB_*` reference variables
  point to the PostGIS service, then redeploy.
- **Regional layers are initially missing:** monitor logs while the background
  preparation completes and verify data-source API keys.
- **No space left on device:** expand the `/app/data` volume; do not move runtime
  data into the Docker image.

Railway references:

- <https://docs.railway.com/builds/dockerfiles>
- <https://docs.railway.com/guides/docker-compose>
- <https://docs.railway.com/databases/postgresql>
- <https://docs.railway.com/volumes>
- <https://docs.railway.com/deployments/healthchecks>

