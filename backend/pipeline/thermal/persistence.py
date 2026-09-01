"""Multi-sensor aggregation and persistent thermal-source analysis."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN
from sklearn.neighbors import BallTree


EARTH_RADIUS_M = 6_371_008.8
DEFAULT_DEDUP_RADIUS_M = 500.0
DEFAULT_DEDUP_TIME_MINUTES = 90
DEFAULT_CLUSTER_RADIUS_M = 300.0
DEFAULT_CLUSTER_MIN_OBSERVATIONS = 2


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def get_persistence_settings() -> dict:
    return {
        "dedup_radius_m": float(os.getenv("THERMAL_DEDUP_RADIUS_M", DEFAULT_DEDUP_RADIUS_M)),
        "dedup_time_minutes": int(os.getenv(
            "THERMAL_DEDUP_TIME_MINUTES", DEFAULT_DEDUP_TIME_MINUTES,
        )),
        "cluster_radius_m": float(os.getenv("THERMAL_CLUSTER_RADIUS_M", DEFAULT_CLUSTER_RADIUS_M)),
        "cluster_min_observations": int(os.getenv(
            "THERMAL_CLUSTER_MIN_OBSERVATIONS", DEFAULT_CLUSTER_MIN_OBSERVATIONS,
        )),
    }


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _mode(series: pd.Series, default=None):
    values = series.dropna()
    if values.empty:
        return default
    modes = values.mode(dropna=True)
    return modes.iloc[0] if not modes.empty else values.iloc[0]


def _json_value(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _footprint_metrics(scan_km, track_km, cluster_extent_m=0.0) -> tuple[float | None, float | None]:
    """Return a transparent observation-envelope radius and area.

    FIRMS ``scan``/``track`` describe the source pixel dimensions. Cluster extent
    describes the distance from the derived source centre to its farthest
    observation. The resulting circle is a display envelope, not a burned or
    environmentally affected area.
    """
    try:
        scan = float(scan_km)
        track = float(track_km)
    except (TypeError, ValueError):
        scan = track = 0.0
    try:
        extent = max(0.0, float(cluster_extent_m or 0.0))
    except (TypeError, ValueError):
        extent = 0.0
    pixel_radius = (
        0.5 * math.hypot(scan, track) * 1000.0
        if scan > 0 and track > 0 else 0.0
    )
    radius = extent + pixel_radius
    if radius <= 0:
        return None, None
    return round(radius, 2), round(math.pi * radius * radius / 1_000_000.0, 4)


def aggregate_multisensor_observations(
    observations: pd.DataFrame,
    *,
    radius_m: float = DEFAULT_DEDUP_RADIUS_M,
    time_minutes: int = DEFAULT_DEDUP_TIME_MINUTES,
) -> pd.DataFrame:
    """Collapse spatially/temporally overlapping sensor rows into UI detections.

    Original observations are never modified. Connected components are used so
    the output remains deterministic regardless of satellite ordering.
    """
    if observations.empty:
        return observations.copy()
    required = {"latitude", "longitude", "observed_at", "satellite"}
    missing = required.difference(observations.columns)
    if missing:
        raise ValueError(f"thermal observations missing columns: {', '.join(sorted(missing))}")

    frame = observations.copy().reset_index(drop=True)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame = frame.sort_values(
        ["observed_at", "latitude", "longitude", "satellite"], kind="mergesort",
    ).reset_index(drop=True)
    parent = list(range(len(frame)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    # Query spatial candidates with a haversine BallTree. The previous nested
    # time-window loop became quadratic for Indian forest seasons containing
    # thousands of observations.
    max_delta_seconds = float(time_minutes * 60)
    # Explicit Unix seconds avoid pandas datetime storage-unit differences
    # (microseconds vs nanoseconds across supported versions).
    observed_seconds = frame["observed_at"].map(pd.Timestamp.timestamp).to_numpy()
    satellites = frame["satellite"].astype(str).to_numpy()
    coordinates = np.radians(frame[["latitude", "longitude"]].astype(float).to_numpy())
    neighbors = BallTree(coordinates, metric="haversine").query_radius(
        coordinates,
        r=radius_m / EARTH_RADIUS_M,
        return_distance=False,
    )
    for left, candidates in enumerate(neighbors):
        for right in candidates:
            right = int(right)
            if right <= left or satellites[left] == satellites[right]:
                continue
            if abs(observed_seconds[right] - observed_seconds[left]) <= max_delta_seconds:
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(frame)):
        groups.setdefault(find(index), []).append(index)

    rows = []
    for detection_number, indices in enumerate(groups.values(), start=1):
        group = frame.iloc[indices]
        frp = pd.to_numeric(group.get("frp"), errors="coerce")
        brightness = pd.to_numeric(group.get("bright_ti4"), errors="coerce")
        scan_values = pd.to_numeric(
            group.get("scan", pd.Series(index=group.index, dtype=float)), errors="coerce",
        )
        track_values = pd.to_numeric(
            group.get("track", pd.Series(index=group.index, dtype=float)), errors="coerce",
        )
        representative_index = frp.idxmax() if frp.notna().any() else group.index[0]
        representative = frame.loc[representative_index].to_dict()
        first_seen = group["observed_at"].min()
        last_seen = group["observed_at"].max()
        satellites = sorted(group.get("satellite", pd.Series(dtype="string")).dropna().astype(str).unique())
        representative.update({
            "detection_id": f"detection_{detection_number:05d}",
            "latitude": float(group["latitude"].mean()),
            "longitude": float(group["longitude"].mean()),
            "observed_at": first_seen + (last_seen - first_seen) / 2,
            "first_observed_at": first_seen,
            "last_observed_at": last_seen,
            "raw_observation_count": int(len(group)),
            "sensor_count": len(satellites),
            "satellites": ",".join(satellites),
            "acquisition_span_minutes": round((last_seen - first_seen).total_seconds() / 60.0, 3),
            "frp": float(frp.mean()) if frp.notna().any() else None,
            "frp_mean_mw": float(frp.mean()) if frp.notna().any() else None,
            "frp_max_mw": float(frp.max()) if frp.notna().any() else None,
            "brightness_ti4_mean_k": float(brightness.mean()) if brightness.notna().any() else None,
            "brightness_ti4_max_k": float(brightness.max()) if brightness.notna().any() else None,
            "scan": float(scan_values.max()) if scan_values.notna().any() else None,
            "track": float(track_values.max()) if track_values.notna().any() else None,
            "daynight": _mode(group.get("daynight", pd.Series(dtype="string"))),
            "confidence": _mode(group.get("confidence", pd.Series(dtype="string"))),
            "inside_industrial_polygon": bool(group.get(
                "inside_industrial_polygon", pd.Series(False, index=group.index),
            ).fillna(False).astype(bool).any()),
            "near_industrial_facility": bool(group.get(
                "near_industrial_facility", pd.Series(False, index=group.index),
            ).fillna(False).astype(bool).any()),
            "landcover_group": _mode(group.get("landcover_group", pd.Series(dtype="string")), "unknown"),
            "landcover_class": _mode(group.get("landcover_class", pd.Series(dtype="string")), "unknown"),
        })
        rows.append(representative)

    return pd.DataFrame(rows).sort_values(
        ["observed_at", "latitude", "longitude"], kind="mergesort",
    ).reset_index(drop=True)


def build_persistent_sources(
    detections: pd.DataFrame,
    *,
    radius_m: float = DEFAULT_CLUSTER_RADIUS_M,
    min_observations: int = DEFAULT_CLUSTER_MIN_OBSERVATIONS,
    minimum_active_days: int = 2,
) -> pd.DataFrame:
    """Cluster detections and calculate explainable temporal/source features.

    ``minimum_active_days=2`` preserves the persistent-source contract. The
    classifier deliberately uses ``1`` and separately retains isolated
    observations so a short-lived industrial fire or agricultural burn is not
    discarded before source classification.
    """
    if detections.empty:
        return pd.DataFrame()
    frame = detections.copy().reset_index(drop=True)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    coordinates = np.radians(frame[["latitude", "longitude"]].astype(float).to_numpy())
    labels = DBSCAN(
        eps=radius_m / EARTH_RADIUS_M,
        min_samples=min_observations,
        metric="haversine",
        algorithm="ball_tree",
    ).fit_predict(coordinates)
    frame["cluster_label"] = labels

    rows = []
    source_number = 0
    clustered = frame[frame["cluster_label"] >= 0]
    for _, group in clustered.groupby("cluster_label", sort=True):
        center_lat = float(group["latitude"].mean())
        center_lon = float(group["longitude"].mean())
        distances = group.apply(
            lambda row: _haversine_m(center_lat, center_lon, float(row["latitude"]), float(row["longitude"])),
            axis=1,
        )
        frp = pd.to_numeric(group.get("frp"), errors="coerce")
        first_seen = group["observed_at"].min()
        last_seen = group["observed_at"].max()
        unique_days = int(group["observed_at"].dt.date.nunique())
        if unique_days < minimum_active_days:
            continue
        source_number += 1
        duration_days = (last_seen - first_seen).total_seconds() / 86_400.0
        night_count = int(group.get("daynight", pd.Series(index=group.index, dtype="string")).eq("N").sum())
        day_count = int(group.get("daynight", pd.Series(index=group.index, dtype="string")).eq("D").sum())
        if unique_days >= 10 or (unique_days >= 7 and duration_days >= 14):
            persistence_level = "HIGH"
        elif unique_days >= 3 or len(group) >= 5:
            persistence_level = "MEDIUM"
        else:
            persistence_level = "LOW"
        satellite_values = set()
        for value in group.get("satellites", pd.Series(dtype="string")).dropna().astype(str):
            satellite_values.update(item for item in value.split(",") if item)
        nearest_distances = pd.to_numeric(
            group.get("distance_to_nearest_industry_m", pd.Series(index=group.index, dtype=float)),
            errors="coerce",
        )
        nearest_index = nearest_distances.idxmin() if nearest_distances.notna().any() else None
        median_frp = float(frp.median()) if frp.notna().any() else None
        max_frp = float(frp.max()) if frp.notna().any() else None
        mean_frp = float(frp.mean()) if frp.notna().any() else None
        frp_std = float(frp.std(ddof=0)) if frp.notna().any() else None
        peak_ratio = (
            max_frp / median_frp
            if max_frp is not None and median_frp is not None and median_frp > 0
            else None
        )

        def numeric_max(name: str):
            values = pd.to_numeric(
                group.get(name, pd.Series(index=group.index, dtype=float)), errors="coerce",
            )
            return float(values.max()) if values.notna().any() else None

        scan_max = numeric_max("scan")
        track_max = numeric_max("track")
        cluster_extent = float(distances.max())
        footprint_radius, footprint_area = _footprint_metrics(
            scan_max, track_max, cluster_extent,
        )

        rows.append({
            "cluster_id": f"cluster_{source_number:03d}",
            "latitude": center_lat,
            "longitude": center_lon,
            "detection_count": int(len(group)),
            "raw_observation_count": int(pd.to_numeric(
                group.get("raw_observation_count", pd.Series(1, index=group.index)), errors="coerce",
            ).fillna(1).sum()),
            "unique_active_days": unique_days,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "active_duration_days": round(duration_days, 3),
            "mean_frp": mean_frp,
            "max_frp": max_frp,
            "median_frp": median_frp,
            "frp_std": frp_std,
            "frp_peak_ratio": round(peak_ratio, 4) if peak_ratio is not None else None,
            "frp_coefficient_variation": (
                round(frp_std / mean_frp, 4)
                if frp_std is not None and mean_frp is not None and mean_frp > 0
                else None
            ),
            "day_detection_count": day_count,
            "night_detection_count": night_count,
            "night_ratio": round(night_count / len(group), 4),
            "sensor_count": len(satellite_values),
            "satellites": ",".join(sorted(satellite_values)),
            "coordinate_variance_m2": float(np.mean(np.square(distances))),
            "max_distance_from_center_m": cluster_extent,
            "scan_max_km": scan_max,
            "track_max_km": track_max,
            "thermal_footprint_radius_m": footprint_radius,
            "thermal_footprint_area_km2": footprint_area,
            "thermal_footprint_method": "FIRMS pixel plus observation-cluster envelope",
            "active_day_density": round(unique_days / max(duration_days + 1.0, 1.0), 4),
            "inside_industrial_polygon": bool(group.get(
                "inside_industrial_polygon", pd.Series(False, index=group.index),
            ).fillna(False).astype(bool).any()),
            "near_industrial_facility": bool(group.get(
                "near_industrial_facility", pd.Series(False, index=group.index),
            ).fillna(False).astype(bool).any()),
            "distance_to_nearest_industry_m": (
                float(nearest_distances.loc[nearest_index]) if nearest_index is not None else None
            ),
            "nearest_industry_name": (
                group.loc[nearest_index].get("nearest_industry_name") if nearest_index is not None else None
            ),
            "nearest_industry_type": (
                group.loc[nearest_index].get("nearest_industry_type") if nearest_index is not None else None
            ),
            "nearest_industry_operator": (
                group.loc[nearest_index].get("nearest_industry_operator") if nearest_index is not None else None
            ),
            "nearest_power_source": (
                group.loc[nearest_index].get("nearest_power_source") if nearest_index is not None else None
            ),
            "nearest_electricity_output": (
                group.loc[nearest_index].get("nearest_electricity_output") if nearest_index is not None else None
            ),
            "industrial_feature_count_500m": numeric_max("industrial_feature_count_500m"),
            "industrial_feature_count_1km": numeric_max("industrial_feature_count_1km"),
            "landcover_group": _mode(group.get("landcover_group", pd.Series(dtype="string")), "unknown"),
            "landcover_class": _mode(group.get("landcover_class", pd.Series(dtype="string")), "unknown"),
            "forest_fraction_500m": numeric_max("forest_fraction_500m"),
            "cropland_fraction_500m": numeric_max("cropland_fraction_500m"),
            "builtup_fraction_500m": numeric_max("builtup_fraction_500m"),
            "persistence_level": persistence_level,
        })
    return pd.DataFrame(rows)


def build_classification_candidates(
    detections: pd.DataFrame,
    *,
    radius_m: float = DEFAULT_CLUSTER_RADIUS_M,
) -> pd.DataFrame:
    """Build source candidates without dropping one-off thermal episodes.

    Multi-observation clusters use the full persistence feature builder.
    Remaining isolated detections are converted in one vectorized pass; this
    avoids thousands of one-row group operations in forest-fire seasons.
    """
    if detections.empty:
        return pd.DataFrame()
    frame = detections.copy().reset_index(drop=True)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    clustered = build_persistent_sources(
        detections,
        radius_m=radius_m,
        min_observations=2,
        minimum_active_days=1,
    )
    remaining = frame
    if not clustered.empty:
        cluster_tree = BallTree(
            np.radians(
                clustered[["latitude", "longitude"]].astype(float).to_numpy()
            ),
            metric="haversine",
        )
        nearest_distance, nearest_index = cluster_tree.query(
            np.radians(
                frame[["latitude", "longitude"]].astype(float).to_numpy()
            ),
            k=1,
        )
        cluster_extent = pd.to_numeric(
            clustered.get(
                "max_distance_from_center_m",
                pd.Series(0.0, index=clustered.index),
            ),
            errors="coerce",
        ).fillna(0.0).to_numpy()
        allowed_distance = radius_m + cluster_extent[nearest_index[:, 0]]
        used_mask = (
            nearest_distance[:, 0] * EARTH_RADIUS_M <= allowed_distance
        )
        remaining = frame.loc[~used_mask].reset_index(drop=True)
    if remaining.empty:
        return clustered.reset_index(drop=True)

    singles = remaining.copy()
    singles["cluster_id"] = [
        f"episode_{index:05d}" for index in range(1, len(singles) + 1)
    ]
    singles["detection_count"] = 1
    singles["raw_observation_count"] = pd.to_numeric(
        singles.get(
            "raw_observation_count", pd.Series(1, index=singles.index),
        ),
        errors="coerce",
    ).fillna(1).astype(int)
    singles["unique_active_days"] = 1
    singles["first_seen"] = singles["observed_at"]
    singles["last_seen"] = singles["observed_at"]
    singles["active_duration_days"] = 0.0
    frp = pd.to_numeric(singles.get("frp"), errors="coerce")
    singles["mean_frp"] = frp
    singles["max_frp"] = frp
    singles["median_frp"] = frp
    singles["frp_std"] = 0.0
    singles["frp_peak_ratio"] = 1.0
    singles["frp_coefficient_variation"] = 0.0
    is_night = singles.get(
        "daynight", pd.Series("", index=singles.index),
    ).astype(str).eq("N")
    singles["day_detection_count"] = (~is_night).astype(int)
    singles["night_detection_count"] = is_night.astype(int)
    singles["night_ratio"] = is_night.astype(float)
    singles["sensor_count"] = pd.to_numeric(
        singles.get("sensor_count", pd.Series(1, index=singles.index)),
        errors="coerce",
    ).fillna(1).astype(int)
    if "satellites" not in singles:
        singles["satellites"] = singles.get(
            "satellite", pd.Series("", index=singles.index),
        ).fillna("").astype(str)
    singles["coordinate_variance_m2"] = 0.0
    singles["max_distance_from_center_m"] = 0.0
    scan = pd.to_numeric(
        singles.get("scan", pd.Series(index=singles.index, dtype=float)), errors="coerce",
    )
    track = pd.to_numeric(
        singles.get("track", pd.Series(index=singles.index, dtype=float)), errors="coerce",
    )
    singles["scan_max_km"] = scan
    singles["track_max_km"] = track
    footprint = [
        _footprint_metrics(scan_value, track_value)
        for scan_value, track_value in zip(scan, track)
    ]
    singles["thermal_footprint_radius_m"] = [item[0] for item in footprint]
    singles["thermal_footprint_area_km2"] = [item[1] for item in footprint]
    singles["thermal_footprint_method"] = "FIRMS source-pixel envelope"
    singles["active_day_density"] = 1.0
    singles["persistence_level"] = "LOW"

    columns = list(dict.fromkeys(
        list(clustered.columns) + list(singles.columns)
    ))
    return pd.concat(
        [
            clustered.reindex(columns=columns),
            singles.reindex(columns=columns),
        ],
        ignore_index=True,
        sort=False,
    )


def thermal_frame_to_geojson(frame: pd.DataFrame) -> dict:
    features = []
    for _, row in frame.iterrows():
        properties = {
            column: _json_value(value)
            for column, value in row.items()
            if column not in {"latitude", "longitude"}
        }
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(row["longitude"]), float(row["latitude"])],
            },
            "properties": properties,
        })
    return {"type": "FeatureCollection", "features": features}


def _write_geojson(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(thermal_frame_to_geojson(frame), indent=2), encoding="utf-8")
    temporary.replace(path)


def ensure_persistence_analysis(event, study) -> dict:
    """Build derived aggregated detections and persistent-source artifacts."""
    thermal_dir = Path(study.data_processed_dir) / "thermal"
    enriched_path = thermal_dir / "firms_enriched.parquet"
    if not enriched_path.exists():
        raise FileNotFoundError(f"enriched FIRMS history not found: {enriched_path}")
    metadata_path = thermal_dir / "persistence_metadata.json"
    detections_path = thermal_dir / "detections_aggregated.parquet"
    clusters_path = thermal_dir / "persistent_clusters.parquet"
    if (
        metadata_path.exists()
        and detections_path.exists()
        and clusters_path.exists()
        and metadata_path.stat().st_mtime >= enriched_path.stat().st_mtime
    ):
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    observations = pd.read_parquet(enriched_path)
    settings = get_persistence_settings()
    dedup_radius_m = settings["dedup_radius_m"]
    dedup_time_minutes = settings["dedup_time_minutes"]
    cluster_radius_m = settings["cluster_radius_m"]
    cluster_min_observations = settings["cluster_min_observations"]
    detections = aggregate_multisensor_observations(
        observations,
        radius_m=dedup_radius_m,
        time_minutes=dedup_time_minutes,
    )
    clusters = build_persistent_sources(
        detections,
        radius_m=cluster_radius_m,
        min_observations=cluster_min_observations,
    )

    _atomic_write_parquet(detections, detections_path)
    _atomic_write_parquet(clusters, clusters_path)
    _write_geojson(detections, thermal_dir / "detections_aggregated.geojson")
    _write_geojson(clusters, thermal_dir / "persistent_clusters.geojson")

    level_counts = (
        clusters["persistence_level"].value_counts().to_dict() if not clusters.empty else {}
    )
    metadata = {
        "event_id": event.id,
        "raw_observation_count": int(len(observations)),
        "aggregated_detection_count": int(len(detections)),
        "multi_sensor_duplicates_collapsed": int(len(observations) - len(detections)),
        "persistent_source_count": int(len(clusters)),
        "persistence_level_counts": {str(key): int(value) for key, value in level_counts.items()},
        "dedup_radius_m": dedup_radius_m,
        "dedup_time_minutes": dedup_time_minutes,
        "cluster_radius_m": cluster_radius_m,
        "cluster_min_observations": cluster_min_observations,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(metadata_path, json.dumps(metadata, indent=2))
    return metadata
