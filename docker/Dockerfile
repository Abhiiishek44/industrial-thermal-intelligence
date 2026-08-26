FROM python:3.11-slim

WORKDIR /app

# System dependencies:
#   libpq-dev + gcc      → psycopg2
#   gdal-bin + libgdal-dev + libproj-dev + proj-data + proj-bin → rasterio / pyproj / geopandas
#   libgeos-dev          → shapely (C extension)
#   libspatialindex-dev  → rtree (spatial indexing used by geopandas)
RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Tell pip/GDAL where the headers are
ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend and frontend (data/ is mounted at runtime)
COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

EXPOSE 5000

CMD ["python", "main.py"]
