FROM python:3.11-slim-bookworm

WORKDIR /app

# Native dependencies used by PostgreSQL/PostGIS and the geospatial Python stack.
RUN apt-get update \
        -o Acquire::ForceIPv4=true \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
    && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    g++ \
    git \
    gdal-bin \
    libgdal-dev \
    libproj-dev \
    proj-data \
    proj-bin \
    libgeos-dev \
    libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config \
    CPLUS_INCLUDE_PATH=/usr/include/gdal \
    C_INCLUDE_PATH=/usr/include/gdal \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime datasets are deliberately excluded. Mount persistent storage at
# /app/data so generated observations, reports and uploads survive redeploys.
COPY backend/ ./backend/
COPY frontend/ ./frontend/
# Immutable presentation artifacts live outside /app/data so an empty Railway
# volume mounted at /app/data cannot hide them on first deploy.
COPY data/processed/final_fused_events.parquet ./runtime_artifacts/final_fused_events.parquet
COPY data/processed/thermal_sources.parquet ./runtime_artifacts/thermal_sources.parquet
COPY models/industrial_iforest_train_2021_2024.joblib ./models/industrial_iforest_train_2021_2024.joblib

WORKDIR /app/backend

EXPOSE 5000

CMD ["python", "main.py"]
