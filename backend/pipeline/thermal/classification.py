"""Explainable baseline classification for persistent thermal sources."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.thermal.persistence import thermal_frame_to_geojson


CLASSIFICATION_METHOD = "explainable_rules_v1"


def _truthy(value) -> bool:
    return bool(value) if value is not None and not pd.isna(value) else False


def classify_source(source: pd.Series | dict) -> dict:
    """Classify one persistent source from contextual, not locational, evidence."""
    get = source.get
    inside_industrial = _truthy(get("inside_industrial_polygon"))
    near_facility = _truthy(get("near_industrial_facility"))
    distance = pd.to_numeric(pd.Series([get("distance_to_nearest_industry_m")]), errors="coerce").iloc[0]
    distance = float(distance) if pd.notna(distance) else None
    landcover = str(get("landcover_group") or "unknown").lower()
    landcover_class = str(get("landcover_class") or "unknown").lower()
    persistence = str(get("persistence_level") or "LOW").upper()
    night_ratio = float(get("night_ratio") or 0.0)
    active_days = int(get("unique_active_days") or 0)

    industrial_score = 0
    natural_score = 0
    industrial_evidence = []
    natural_evidence = []

    if inside_industrial:
        industrial_score += 4
        industrial_evidence.append("inside mapped industrial land-use polygon")
    if near_facility:
        industrial_score += 3
        industrial_evidence.append("within configured distance of industrial infrastructure")
    if distance is not None and distance <= 500:
        industrial_score += 2
        industrial_evidence.append(f"nearest mapped industrial facility is {distance:.0f} m away")
    elif distance is not None and distance <= 1000:
        industrial_score += 1
        industrial_evidence.append(f"nearest mapped industrial facility is {distance:.0f} m away")
    elif distance is not None and distance > 2000:
        natural_score += 1
        natural_evidence.append("more than 2 km from the nearest mapped industrial facility")

    if persistence == "HIGH":
        industrial_score += 2
        industrial_evidence.append(f"high persistence across {active_days} active days")
    elif persistence == "MEDIUM":
        industrial_score += 1
        industrial_evidence.append(f"recurring activity across {active_days} active days")
    if night_ratio >= 0.5:
        industrial_score += 1
        industrial_evidence.append(f"{night_ratio:.0%} of detections occur at night")
    if landcover == "built_up" or landcover_class == "built_up":
        industrial_score += 1
        industrial_evidence.append("built-up land-cover context")

    if landcover in {"vegetation", "forest"}:
        natural_score += 4
        natural_evidence.append(f"{landcover_class.replace('_', ' ')} land cover")
    elif landcover == "agricultural":
        natural_score += 3
        natural_evidence.append("agricultural land-cover context")
    if persistence == "LOW" and not inside_industrial:
        natural_score += 1
        natural_evidence.append("low temporal persistence outside industrial land use")

    industrial_context = inside_industrial or near_facility or (
        distance is not None and distance <= 1000
    )
    natural_context = landcover in {"vegetation", "forest", "agricultural"}
    if industrial_context and industrial_score >= 4 and industrial_score >= natural_score + 2:
        class_name = "industrial"
        subtype = "persistent_industrial_source"
        evidence = industrial_evidence
        top_score, other_score = industrial_score, natural_score
    elif natural_context and natural_score >= 4 and natural_score >= industrial_score + 2:
        class_name = "natural"
        subtype = "natural_or_vegetation_source"
        evidence = natural_evidence
        top_score, other_score = natural_score, industrial_score
    else:
        class_name = "unknown"
        subtype = "other_unknown"
        evidence = []
        if industrial_evidence:
            evidence.append("industrial signals: " + "; ".join(industrial_evidence))
        if natural_evidence:
            evidence.append("natural signals: " + "; ".join(natural_evidence))
        if not evidence:
            evidence.append("insufficient mapped industrial or natural context")
        top_score, other_score = max(industrial_score, natural_score), min(industrial_score, natural_score)

    margin = max(0, top_score - other_score)
    if class_name == "unknown":
        confidence = min(0.65, 0.40 + 0.04 * top_score + 0.02 * margin)
    else:
        confidence = min(0.95, 0.52 + 0.035 * top_score + 0.025 * margin)
    return {
        "source_class": class_name,
        "source_subtype": subtype,
        "classification_confidence": round(float(confidence), 4),
        "classification_evidence": evidence,
        "industrial_score": int(industrial_score),
        "natural_score": int(natural_score),
        "classification_method": CLASSIFICATION_METHOD,
    }


def classify_persistent_sources(clusters: pd.DataFrame) -> pd.DataFrame:
    if clusters.empty:
        result = clusters.copy()
        for column in (
            "source_class", "source_subtype", "classification_confidence",
            "classification_evidence", "industrial_score", "natural_score",
            "classification_method",
        ):
            result[column] = pd.Series(dtype="object")
        return result
    classifications = pd.DataFrame([
        classify_source(row) for _, row in clusters.iterrows()
    ])
    return pd.concat([clusters.reset_index(drop=True), classifications], axis=1)


def classification_metadata(event_id: int, classified: pd.DataFrame) -> dict:
    counts = (
        classified["source_class"].value_counts().to_dict() if not classified.empty else {}
    )
    return {
        "event_id": event_id,
        "classified_source_count": int(len(classified)),
        "class_counts": {str(key): int(value) for key, value in counts.items()},
        "mean_confidence": (
            round(float(classified["classification_confidence"].mean()), 4)
            if not classified.empty else None
        ),
        "method": CLASSIFICATION_METHOD,
        "is_trained_model": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def ensure_source_classification(event, study) -> dict:
    """Classify the stored full-window persistent-source artifact."""
    thermal_dir = Path(study.data_processed_dir) / "thermal"
    clusters_path = thermal_dir / "persistent_clusters.parquet"
    if not clusters_path.exists():
        raise FileNotFoundError(f"persistent source data not found: {clusters_path}")
    classified_path = thermal_dir / "classified_sources.parquet"
    metadata_path = thermal_dir / "classification_metadata.json"
    if (
        classified_path.exists()
        and metadata_path.exists()
        and metadata_path.stat().st_mtime >= clusters_path.stat().st_mtime
    ):
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    clusters = pd.read_parquet(clusters_path)
    classified = classify_persistent_sources(clusters)
    classified.to_parquet(classified_path, index=False)
    (thermal_dir / "classified_sources.geojson").write_text(
        json.dumps(thermal_frame_to_geojson(classified), indent=2), encoding="utf-8",
    )
    metadata = classification_metadata(event.id, classified)
    metadata_path.write_text(
        json.dumps(metadata, indent=2), encoding="utf-8",
    )
    return metadata
