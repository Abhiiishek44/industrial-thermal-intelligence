# Data Directory Structure

```
data/
├── static/                        ← Global static data (shared across all events)
│   ├── population.gpkg            ← Canadian census dissemination areas (GeoPackage)
│   ├── roads_canada.gpkg          ← Canadian major road network (GeoPackage)
│   └── models/
│       ├── feature_cols.json      ← ML model feature column list
│       ├── model_full_lr.pkl      ← Logistic Regression model (active)
│       ├── model_full_rf.pkl      ← Random Forest model
│       ├── model_full_xgb.pkl     ← XGBoost model
│       └── model_full_thresholds.json  ← Per-model Youden's J classification thresholds
│
└── events/
    └── <year>_<id:04d>/           ← e.g. 2016_0001 (Fort McMurray 2016)
        ├── landmarks.json         ← Named landmarks (used in cut_location descriptions)
        ├── data_raw/              ← Raw downloaded data (do not modify manually)
        │   ├── firms/
        │   │   └── hotspots_raw.csv      ← NASA FIRMS raw hotspot CSV
        │   ├── terrain/
        │   │   ├── dtm.tif               ← Digital terrain model (raw)
        │   │   ├── slope.tif             ← Slope (raw)
        │   │   └── aspect.tif            ← Aspect (raw)
        │   ├── landcover/
        │   │   └── fuel_type.tif         ← Fuel type (raw)
        │   ├── weather/
        │   │   └── era5.nc               ← ERA5 meteorological data (NetCDF)
        │   └── clouds/                   ← Cloud cover data (reserved, currently empty)
        │
        ├── data_processed/        ← Pipeline-processed outputs
        │   ├── grid_static.parquet       ← Static 500m grid features (terrain + fuel combined)
        │   ├── firms/
        │   │   └── hotspots.parquet      ← Cleaned hotspots (x_proj, y_proj, overpass_time)
        │   ├── terrain/
        │   │   ├── dtm.tif               ← Processed DEM (EPSG:3978)
        │   │   ├── slope.tif             ← Slope (degrees)
        │   │   └── aspect.tif            ← Aspect (degrees)
        │   ├── landcover/
        │   │   └── fuel_type.tif         ← Processed fuel type
        │   ├── weather/
        │   │   ├── era5.parquet          ← ERA5 tabular (temp, humidity, wind, etc.)
        │   │   ├── ffmc_daily.parquet    ← Daily FFMC (Fine Fuel Moisture Code)
        │   │   ├── isi_hourly.parquet    ← Hourly ISI (Initial Spread Index)
        │   │   └── ros_hourly.parquet    ← Hourly ROS (Rate of Spread, m/min)
        │   └── training/
        │       └── fire_state.pkl        ← Fire state object (boundary_after, steps, cluster meta)
        │
        ├── data_render/           ← Render image cache (currently empty)
        ├── models/                ← Event-specific models (currently empty; uses static/models/)
        ├── predictions/           ← Prediction cache (currently empty)
        │
        └── timesteps/             ← Per-timestep outputs (3-hour intervals)
            └── <YYYY-MM-DDTHHMM>/  ← e.g. 2016-05-01T1200
                ├── prediction/
                │   ├── perimeter.geojson      ← Current fire perimeter (from fire_state.boundary_after)
                │   ├── hotspots.geojson       ← Satellite hotspot points (FIRMS, at this overpass)
                │   ├── risk_zones_3h.geojson  ← ML-predicted high-risk zone at +3h
                │   ├── risk_zones_6h.geojson  ← ML-predicted high-risk zone at +6h
                │   ├── risk_zones_12h.geojson ← ML-predicted high-risk zone at +12h
                │   └── fire_context.json      ← Fire metrics summary (area, wind, FWI, road impact)
                ├── spatial_analysis/
        │   ├── roads.geojson          ← Affected major roads (status: burned/at_risk_Xh/clear)
        │   └── ai_summary.json        ← AI situation report (generated on demand)
        └── weather/
            ├── forecast.json          ← Hourly area-avg forecast [{hour,temp_c,rh,wind_speed_kmh,max_wind_speed_kmh,wind_dir}]
            └── wind_field.json        ← Leaflet-velocity data [{hour,data:[u_obj,v_obj]}] (13h × 26×12 grid)
```

## Timestep Coverage

- **Event 2016_0001**: 2016-05-01T1200 to 2016-05-17T1800
- Interval: every 3 hours
- Total: ~68 timesteps

## Coordinate Systems

- Internal processing: **EPSG:3978** (Canada Atlas Lambert, unit: metres)
- GeoJSON output: **EPSG:4326** (WGS84, lat/lon)

## Pipeline Flow

```
data_raw/ → [whp pipeline] → data_processed/
                ↓
        fire_state.pkl  (boundary_after, steps, cluster metadata)
                ↓
        [predict stage] → timestep/prediction/*.geojson + fire_context.json
                ↓
        [spatial stage] → timestep/spatial_analysis/roads.geojson + DB population counts
                ↓
        [on-demand]     → timestep/spatial_analysis/ai_summary.json
```

## Active Model

**Logistic Regression (steps)** (`model_full_lr_steps.pkl`)  
Threshold: `0.42211` — loaded from `model_full_thresholds.json` key `"lr_steps"`.  
To switch models, update `model_name` in `backend/pipeline/check/builder.py` (`_load_predictor`)
and the default in `backend/pipeline/predict/risk_zones.py` (`load_youden_threshold`).

## Population Count Fields

Stored in DB (`EventTimestep`). Retrieved via:

```
GET /api/events/{event_id}/timesteps/{ts_id}/population
GET /api/events/{event_id}/timesteps/{ts_id}/weather
GET /api/events/{event_id}/timesteps/{ts_id}/wind-field          ← all hours
GET /api/events/{event_id}/timesteps/{ts_id}/wind-field?hour=N   ← single hour [u,v]
```

```json
{
  "affected_population": 45200,
  "at_risk_3h":          12400,
  "at_risk_6h":          18700,
  "at_risk_12h":         27100
}
```

| Field | Description |
|---|---|
| `affected_population` | Population inside the fire perimeter |
| `at_risk_3h` | Inside +3h high-risk zone, excluding perimeter |
| `at_risk_6h` | Inside +6h high-risk zone, excluding perimeter and +3h zone |
| `at_risk_12h` | Inside +12h high-risk zone, excluding perimeter, +3h, and +6h zones |

Each ring counts only the *additional* population not already counted in a closer horizon.
