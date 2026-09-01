"""Explainable multi-class classification for thermal-source candidates.

This remains a rules baseline, not a trained model. It deliberately exposes
its scores and evidence so analysts can review every decision and so the same
contract can later be served by a calibrated supervised model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.thermal.persistence import (
    build_classification_candidates,
    get_persistence_settings,
    thermal_frame_to_geojson,
)


CLASSIFICATION_METHOD = "explainable_rules_v3"
SOURCE_CLASSES = (
    "industrial_fire",
    "gas_flare",
    "agricultural_burning",
    "mining_activity",
    "wildfire",
    "industrial_process_heat",
    "unknown",
)


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

_FLARE_TERMS = {
    "flare", "refinery", "petroleum", "petrochemical", "oil", "gas", "lng",
    "hydrocarbon", "terminal",
}
_MINING_TERMS = {
    "mine", "mining", "coal", "colliery", "quarry", "opencast", "open cast",
    "mineral",
}


def _truthy(value) -> bool:
    return bool(value) if value is not None and not pd.isna(value) else False


def _number(value, default=None):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(*values) -> str:
    return " ".join(
        str(value).lower()
        for value in values
        if value is not None and not pd.isna(value)
    )


def _has_term(value: str, terms: set[str]) -> bool:
    return any(term in value for term in terms)


def classify_source(source: pd.Series | dict) -> dict:
    """Classify one source candidate using auditable contextual evidence."""
    get = source.get
    inside_industrial = _truthy(get("inside_industrial_polygon")) or _truthy(
        get("inside_midc")
    )
    near_facility = _truthy(get("near_industrial_facility"))
    distance = _number(get("distance_to_nearest_industry_m"))
    landcover = str(get("landcover_group") or "unknown").lower()
    landcover_class = str(get("landcover_class") or "unknown").lower()
    persistence = str(get("persistence_level") or "LOW").upper()
    active_days = int(_number(get("unique_active_days"), 0) or 0)
    duration_days = _number(get("active_duration_days"), 0.0) or 0.0
    night_ratio = _number(get("night_ratio"), 0.0) or 0.0
    max_frp = _number(get("max_frp"), _number(get("frp")))
    peak_ratio = _number(get("frp_peak_ratio"))
    spatial_radius = _number(get("max_distance_from_center_m"), 0.0) or 0.0
    forest_fraction = _number(get("forest_fraction_500m"), 0.0) or 0.0
    crop_fraction = _number(get("cropland_fraction_500m"), 0.0) or 0.0
    built_fraction = _number(get("builtup_fraction_500m"), 0.0) or 0.0
    facility_text = _text(
        get("nearest_industry_type"), get("nearest_industry_name")
    )
    flare_context = _has_term(facility_text, _FLARE_TERMS)
    mining_context = _has_term(facility_text, _MINING_TERMS)
    industrial_context = inside_industrial or near_facility or (
        distance is not None and distance <= 1000
    )
    transient = active_days <= 2 and duration_days <= 2.5
    persistent = persistence in {"MEDIUM", "HIGH"} or active_days >= 3
    stationary = spatial_radius <= 350
    frp_anomaly = (peak_ratio is not None and peak_ratio >= 2.5) or (
        transient and max_frp is not None and max_frp >= 50
    )

    scores = {name: 0 for name in SOURCE_CLASSES if name != "unknown"}
    evidence = {name: [] for name in scores}

    def add(name: str, points: int, message: str) -> None:
        scores[name] += points
        evidence[name].append(message)

    if industrial_context:
        context_message = (
            "inside mapped industrial land use"
            if inside_industrial
            else (
                f"{distance:.0f} m from mapped industrial infrastructure"
                if distance is not None
                else "near mapped industrial infrastructure"
            )
        )
        add("industrial_fire", 4, context_message)
        add("industrial_process_heat", 4, context_message)
    if flare_context:
        add("gas_flare", 6, f"oil/gas facility context: {facility_text.strip()}")
    if mining_context:
        add("mining_activity", 7, f"mine/coal facility context: {facility_text.strip()}")

    if transient:
        add(
            "industrial_fire", 2,
            f"short-lived episode across {max(active_days, 1)} active day(s)",
        )
        add("agricultural_burning", 2, "short-lived thermal episode")
        add("wildfire", 1, "non-persistent thermal episode")
    if persistent:
        add("gas_flare", 3, f"recurs across {active_days} active days")
        add("mining_activity", 2, f"recurs across {active_days} active days")
        add("industrial_process_heat", 2, f"recurs across {active_days} active days")
    if stationary:
        add("gas_flare", 2, f"stationary source radius is {spatial_radius:.0f} m")
        add("mining_activity", 1, f"stationary source radius is {spatial_radius:.0f} m")
        add(
            "industrial_process_heat", 1,
            f"stationary source radius is {spatial_radius:.0f} m",
        )
    elif spatial_radius >= 500:
        add("industrial_fire", 1, f"thermal cluster spans {spatial_radius:.0f} m")
        add("wildfire", 2, f"thermal cluster spans {spatial_radius:.0f} m")

    if frp_anomaly:
        detail = (
            f"peak FRP is {peak_ratio:.1f}× the cluster median"
            if peak_ratio is not None and peak_ratio >= 2.5
            else f"short-lived peak FRP reaches {max_frp:.1f} MW"
        )
        add("industrial_fire", 5, detail)
    if max_frp is not None and max_frp >= 100:
        add("industrial_fire", 2, f"maximum FRP is {max_frp:.1f} MW")
        add("wildfire", 1, f"maximum FRP is {max_frp:.1f} MW")
    if night_ratio >= 0.5:
        add("gas_flare", 1, f"{night_ratio:.0%} of detections occur at night")
        add(
            "industrial_process_heat", 1,
            f"{night_ratio:.0%} of detections occur at night",
        )

    if landcover == "agricultural" or landcover_class == "cropland":
        add("agricultural_burning", 6, "cropland land-cover context")
    if crop_fraction >= 0.5:
        add(
            "agricultural_burning", 3,
            f"cropland covers {crop_fraction:.0%} of the 500 m neighborhood",
        )
    elif crop_fraction >= 0.2:
        add(
            "agricultural_burning", 1,
            f"cropland covers {crop_fraction:.0%} of the 500 m neighborhood",
        )
    if transient and (landcover == "agricultural" or crop_fraction >= 0.5):
        add(
            "agricultural_burning", 2,
            "cropland signal is consistent with a brief field burn",
        )

    if landcover in {"vegetation", "forest"}:
        add("wildfire", 5, f"{landcover_class.replace('_', ' ')} land-cover context")
    if forest_fraction >= 0.5:
        add(
            "wildfire", 3,
            f"forest covers {forest_fraction:.0%} of the 500 m neighborhood",
        )
    elif forest_fraction >= 0.2:
        add(
            "wildfire", 1,
            f"forest covers {forest_fraction:.0%} of the 500 m neighborhood",
        )
    if distance is not None and distance > 2000:
        add("wildfire", 1, "more than 2 km from mapped industrial infrastructure")

    if landcover == "bare" or landcover_class == "bare_sparse_vegetation":
        add("mining_activity", 2, "bare or sparse land-cover context")
    if (
        landcover == "built_up"
        or landcover_class == "built_up"
        or built_fraction >= 0.5
    ):
        add("industrial_process_heat", 1, "built-up land-cover context")

    if flare_context and not frp_anomaly:
        scores["industrial_process_heat"] = max(
            0, scores["industrial_process_heat"] - 2
        )
    if mining_context:
        scores["industrial_process_heat"] = max(
            0, scores["industrial_process_heat"] - 2
        )

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_class, top_score = ranked[0]
    second_score = ranked[1][1]
    margin = top_score - second_score
    if top_score < 5 or margin < 2:
        class_name = "unknown"
        selected_evidence = []
        if top_score:
            selected_evidence.append(
                f"conflicting evidence: {top_class.replace('_', ' ')} scored "
                f"{top_score}, next alternative scored {second_score}"
            )
            selected_evidence.extend(evidence[top_class][:3])
        else:
            selected_evidence.append(
                "insufficient mapped context or temporal evidence"
            )
        confidence = min(
            0.64, 0.38 + 0.025 * top_score + 0.02 * max(margin, 0)
        )
    else:
        class_name = top_class
        selected_evidence = evidence[class_name]
        confidence = min(0.94, 0.48 + 0.03 * top_score + 0.025 * margin)

    if class_name in {
        "industrial_fire", "wildfire", "agricultural_burning",
    }:
        # FIRMS establishes a satellite-observed thermal anomaly, not a
        # ground-verified active fire. Keep fire-like classifications explicitly
        # provisional until an independent incident/perimeter or field report
        # confirms active burning.
        operational_state = "fire_candidate"
    elif class_name == "unknown":
        operational_state = "uncertain"
    else:
        operational_state = (
            "persistent_source" if persistent else "thermal_activity"
        )
    if class_name == "industrial_fire":
        alert_level = "high"
    elif class_name in {"wildfire", "agricultural_burning"}:
        alert_level = "medium"
    else:
        alert_level = "review" if class_name == "unknown" else "low"

    source_type = {
        "industrial_fire": "industrial_facility",
        "gas_flare": "oil_gas_flare",
        "agricultural_burning": "cropland",
        "mining_activity": "mine_or_coal_area",
        "wildfire": "vegetation",
        "industrial_process_heat": "industrial_facility",
        "unknown": "unknown",
    }[class_name]
    return {
        "source_class": class_name,
        "source_type": source_type,
        "source_subtype": class_name,
        "operational_state": operational_state,
        "alert_level": alert_level,
        "is_emergency_candidate": class_name == "industrial_fire",
        "classification_confidence": round(float(confidence), 4),
        "classification_evidence": selected_evidence,
        "class_scores": {name: int(score) for name, score in scores.items()},
        "classification_method": CLASSIFICATION_METHOD,
    }


def classify_persistent_sources(clusters: pd.DataFrame) -> pd.DataFrame:
    """Backward-compatible name: classify any supplied source candidates."""
    if clusters.empty:
        result = clusters.copy()
        for column in (
            "source_class", "source_type", "source_subtype",
            "operational_state", "alert_level", "is_emergency_candidate",
            "classification_confidence", "classification_evidence",
            "class_scores", "classification_method",
        ):
            result[column] = pd.Series(dtype="object")
        return result
    classifications = pd.DataFrame(
        [classify_source(row) for row in clusters.to_dict("records")]
    )
    return pd.concat([clusters.reset_index(drop=True), classifications], axis=1)


def classification_metadata(event_id: int, classified: pd.DataFrame) -> dict:
    counts = (
        classified["source_class"].value_counts().to_dict()
        if not classified.empty
        else {}
    )
    emergency_count = (
        int(
            classified.get(
                "is_emergency_candidate", pd.Series(dtype=bool)
            ).fillna(False).sum()
        )
        if not classified.empty
        else 0
    )
    return {
        "event_id": event_id,
        "classified_source_count": int(len(classified)),
        "class_counts": {
            name: int(counts.get(name, 0)) for name in SOURCE_CLASSES
        },
        "emergency_candidate_count": emergency_count,
        "mean_confidence": (
            round(float(classified["classification_confidence"].mean()), 4)
            if not classified.empty
            else None
        ),
        "method": CLASSIFICATION_METHOD,
        "class_labels": list(SOURCE_CLASSES),
        "is_trained_model": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def ensure_source_classification(event, study) -> dict:
    """Classify the full-window persistent-source artifact for overview maps."""
    thermal_dir = Path(study.data_processed_dir) / "thermal"
    detections_path = thermal_dir / "detections_aggregated.parquet"
    clusters_path = thermal_dir / "persistent_clusters.parquet"
    source_path = clusters_path if clusters_path.exists() else detections_path
    if not source_path.exists():
        raise FileNotFoundError(f"thermal source data not found: {source_path}")
    classified_path = thermal_dir / "classified_sources.parquet"
    metadata_path = thermal_dir / "classification_metadata.json"
    if classified_path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("method") == CLASSIFICATION_METHOD
            and metadata_path.stat().st_mtime >= source_path.stat().st_mtime
        ):
            return metadata

    source_frame = pd.read_parquet(source_path)
    if source_path == detections_path:
        settings = get_persistence_settings()
        candidates = build_classification_candidates(
            source_frame, radius_m=settings["cluster_radius_m"]
        )
    else:
        candidates = source_frame
    classified = classify_persistent_sources(candidates)
    _atomic_write_parquet(classified, classified_path)
    _atomic_write_text(
        thermal_dir / "classified_sources.geojson",
        json.dumps(thermal_frame_to_geojson(classified), indent=2),
    )
    metadata = classification_metadata(event.id, classified)
    _atomic_write_text(metadata_path, json.dumps(metadata, indent=2))
    return metadata
