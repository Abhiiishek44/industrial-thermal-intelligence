"""Reusable observation and evidence providers for registered regions."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

import geopandas as gpd
import pandas as pd

from pipeline.thermal.history import normalize_firms_frames
from pipeline.training_data.schemas import EvidenceTier, LabelState, RegionProfile, TrainingClass


def _event_dir(data_root: Path, event_id: int, year: int) -> Path:
    return data_root / "events" / f"{year}_{event_id:04d}"


def _observation_id(region_id: str, row: pd.Series) -> str:
    observed = pd.Timestamp(row["observed_at"]).tz_convert("UTC").isoformat()
    identity = "|".join((
        region_id,
        f"{float(row['latitude']):.6f}",
        f"{float(row['longitude']):.6f}",
        observed,
        str(row.get("satellite", "")),
        str(row.get("instrument", "")),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _finalize_observations(frame: pd.DataFrame, region: RegionProfile) -> pd.DataFrame:
    result = frame.copy()
    result["observed_at"] = pd.to_datetime(result["observed_at"], utc=True)
    result = result[
        result["observed_at"].between(
            pd.Timestamp(region.start_date, tz="UTC"),
            pd.Timestamp(region.end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1),
        )
    ].copy()
    result["region_id"] = region.region_id
    result["origin_event_id"] = region.source_event_id
    result["geographic_group_id"] = region.geographic_group_id
    result["temporal_group_id"] = region.temporal_group_id
    result["observation_id"] = result.apply(lambda row: _observation_id(region.region_id, row), axis=1)
    return result.sort_values(
        ["observed_at", "latitude", "longitude", "observation_id"], kind="mergesort",
    ).reset_index(drop=True)


def load_event_firms_csv(region: RegionProfile, data_root: Path) -> pd.DataFrame:
    event_dir = _event_dir(data_root, region.source_event_id, int(region.start_date[:4]))
    path = event_dir / "data_raw" / "firms" / "hotspots_raw.csv"
    raw = pd.read_csv(path, dtype={"acq_date": "string", "acq_time": "string"})
    raw["source_file"] = path.name
    raw["source_product"] = "existing_event_feed"
    normalized, _ = normalize_firms_frames([raw])
    return _finalize_observations(normalized, region)


def load_event_enriched_parquet(region: RegionProfile, data_root: Path) -> pd.DataFrame:
    event_dir = _event_dir(data_root, region.source_event_id, int(region.start_date[:4]))
    path = event_dir / "data_processed" / "thermal" / "firms_enriched.parquet"
    return _finalize_observations(pd.read_parquet(path), region)


def unlabeled_records(observations: pd.DataFrame, region: RegionProfile, _: Path) -> pd.DataFrame:
    return pd.DataFrame({
        "observation_id": observations["observation_id"],
        "region_id": region.region_id,
        "label_state": LabelState.UNLABELED.value,
        "class_label": None,
        "evidence_tier": None,
        "evidence_source": None,
        "evidence_source_url": None,
        "evidence_record_id": None,
        "evidence_method": None,
        "labeler": None,
    })


def dated_fire_perimeter_labels(
    observations: pd.DataFrame, region: RegionProfile, repository_root: Path,
) -> pd.DataFrame:
    """Tier-A labels only where a detection intersects a dated perimeter."""
    perimeter_path = repository_root / region.options["perimeter_path"]
    perimeters = gpd.read_file(perimeter_path)
    perimeters["date"] = pd.to_datetime(perimeters["date"], errors="coerce").dt.date
    points = gpd.GeoDataFrame(
        observations[["observation_id", "observed_at", "latitude", "longitude"]].copy(),
        geometry=gpd.points_from_xy(observations["longitude"], observations["latitude"]),
        crs="EPSG:4326",
    ).to_crs(perimeters.crs)
    points["date"] = points["observed_at"].dt.date
    matches: dict[str, str] = {}
    for day, day_points in points.groupby("date", sort=True):
        day_perimeters = perimeters[perimeters["date"] == day]
        if day_perimeters.empty:
            continue
        joined = gpd.sjoin(
            day_points[["observation_id", "geometry"]],
            day_perimeters[["uid", "geometry"]],
            how="inner",
            predicate="intersects",
        )
        for observation_id, records in joined.groupby("observation_id", sort=True):
            matches[observation_id] = ",".join(sorted(records["uid"].astype(str).unique()))

    rows = []
    for observation_id in observations["observation_id"]:
        record_id = matches.get(observation_id)
        if record_id:
            rows.append({
                "observation_id": observation_id,
                "region_id": region.region_id,
                "label_state": LabelState.LABELED.value,
                "class_label": TrainingClass.WILDFIRE_OR_VEGETATION.value,
                "evidence_tier": EvidenceTier.A.value,
                "evidence_source": "Canadian dated fire perimeter archive",
                "evidence_source_url": "https://zenodo.org/records/19502692",
                "evidence_record_id": record_id,
                "evidence_method": "FIRMS point intersects authoritative perimeter on observation date",
                "labeler": "dated_fire_perimeter_provider_v1",
            })
        else:
            rows.append({
                "observation_id": observation_id,
                "region_id": region.region_id,
                "label_state": LabelState.UNLABELED.value,
                "class_label": None,
                "evidence_tier": None,
                "evidence_source": None,
                "evidence_source_url": None,
                "evidence_record_id": None,
                "evidence_method": None,
                "labeler": None,
            })
    return pd.DataFrame(rows)


OBSERVATION_PROVIDERS: dict[str, Callable] = {
    "event_firms_csv": load_event_firms_csv,
    "event_enriched_parquet": load_event_enriched_parquet,
}

LABEL_PROVIDERS: dict[str, Callable] = {
    "unlabeled": unlabeled_records,
    "dated_fire_perimeter": dated_fire_perimeter_labels,
}
