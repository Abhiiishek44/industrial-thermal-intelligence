"""Deterministic, non-ML construction of versioned training tables."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from pipeline.training_data.features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, feature_schema, generate_features
from pipeline.training_data.manifests import artifact_record, sha256_file, write_manifest
from pipeline.training_data.providers import LABEL_PROVIDERS, OBSERVATION_PROVIDERS
from pipeline.training_data.regions import TRAINING_REGIONS
from pipeline.training_data.schemas import (
    LABEL_COLUMNS, SCHEMA_VERSION, DatasetSplit, EvidenceTier, LabelState, TrainingClass,
    validate_label_record,
)
from pipeline.training_data.splits import assign_grouped_splits


IDENTITY_COLUMNS = (
    "observation_id", "region_id", "origin_event_id", "geographic_group_id",
    "temporal_group_id", "observed_at",
)
SUPERVISION_COLUMNS = ("class_label", "evidence_tier", "split")


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = frame.copy()
    if "observation_id" in ordered:
        ordered = ordered.sort_values("observation_id", kind="mergesort").reset_index(drop=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    ordered.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(path)


def _cast_model_contract(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    string_columns = (
        "observation_id", "region_id", "geographic_group_id", "temporal_group_id",
        "class_label", "evidence_tier", "split", *CATEGORICAL_FEATURES,
    )
    for column in string_columns:
        result[column] = result[column].astype("string")
    result["origin_event_id"] = result["origin_event_id"].astype("Int64")
    result["observed_at"] = pd.to_datetime(result["observed_at"], utc=True)
    for column in NUMERIC_FEATURES:
        if column.startswith("inside_"):
            result[column] = result[column].fillna(False).astype(bool)
        else:
            result[column] = pd.to_numeric(result[column], errors="coerce").astype("float64")
    return result


def build_training_dataset(
    repository_root: str | Path, *, output_root: str | Path | None = None,
) -> dict:
    repository_root = Path(repository_root).resolve()
    data_root = repository_root / "data"
    root = Path(output_root).resolve() if output_root else data_root / "training" / "thermal_sources" / "v1"
    observations_dir = root / "observations"
    features_dir = root / "enriched_features"
    labels_dir = root / "labels"
    model_dir = root / "model_ready"
    manifest_dir = root / "manifests"
    artifacts = []
    combined = []
    region_summaries = []
    source_snapshots = []

    source_paths = {
        "fort_mcmurray_2016": (
            data_root / "events" / "2016_0001" / "data_raw" / "firms" / "hotspots_raw.csv",
            repository_root / "data" / "static" / "actual_perimeter" / "actual_perimeter.gpkg",
        ),
        "chakan_2024_demo": (
            data_root / "events" / "2024_0002" / "data_processed" / "thermal" / "firms_enriched.parquet",
            data_root / "events" / "2024_0002" / "data_processed" / "industrial" / "metadata.json",
            data_root / "events" / "2024_0002" / "data_processed" / "landcover" / "metadata.json",
        ),
    }

    for region in TRAINING_REGIONS:
        source_snapshots.extend({
            "region_id": region.region_id,
            "path": str(path.relative_to(repository_root)),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        } for path in source_paths[region.region_id])
        observations = OBSERVATION_PROVIDERS[region.observation_provider](region, data_root)
        features = generate_features(observations)
        labels = LABEL_PROVIDERS[region.label_provider](observations, region, repository_root)
        for record in labels.to_dict("records"):
            validate_label_record(record)
        merged = features.merge(labels, on=["observation_id", "region_id"], how="left", validate="one_to_one")
        combined.append(merged)

        observation_path = observations_dir / f"{region.region_id}.parquet"
        feature_path = features_dir / f"{region.region_id}.parquet"
        label_path = labels_dir / f"{region.region_id}.parquet"
        _write_parquet(observations, observation_path)
        _write_parquet(features, feature_path)
        _write_parquet(labels[list(LABEL_COLUMNS)], label_path)
        artifacts.extend([
            artifact_record(observation_path, len(observations)),
            artifact_record(feature_path, len(features)),
            artifact_record(label_path, len(labels)),
        ])
        label_counts = labels.groupby(["label_state", "class_label", "evidence_tier"], dropna=False).size()
        region_summaries.append({
            **region.as_manifest(),
            "observation_count": len(observations),
            "label_counts": [
                {
                    "label_state": str(key[0]),
                    "class_label": None if pd.isna(key[1]) else str(key[1]),
                    "evidence_tier": None if pd.isna(key[2]) else str(key[2]),
                    "count": int(value),
                }
                for key, value in label_counts.items()
            ],
        })

    all_rows = pd.concat(combined, ignore_index=True, sort=False)
    model_rows, overlap_checks = assign_grouped_splits(all_rows, TRAINING_REGIONS)
    model_columns = list(IDENTITY_COLUMNS + SUPERVISION_COLUMNS + NUMERIC_FEATURES + CATEGORICAL_FEATURES)
    for split in (DatasetSplit.TRAIN, DatasetSplit.VALIDATION, DatasetSplit.TEST):
        selected = _cast_model_contract(
            model_rows[model_rows["split"].eq(split.value)][model_columns],
        )
        path = model_dir / f"{split.value}.parquet"
        _write_parquet(selected, path)
        artifacts.append(artifact_record(path, len(selected)))

    schema_path = manifest_dir / "feature_schema.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(feature_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifacts.append(artifact_record(schema_path))
    label_summary = all_rows.groupby(["label_state", "class_label", "evidence_tier"], dropna=False).size()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "dataset construction only; no model was trained or integrated",
        "regions": region_summaries,
        "label_summary": [
            {
                "label_state": str(key[0]),
                "class_label": None if pd.isna(key[1]) else str(key[1]),
                "evidence_tier": None if pd.isna(key[2]) else str(key[2]),
                "count": int(value),
            }
            for key, value in label_summary.items()
        ],
        "supervised_class_evidence_counts": {
            class_label.value: {
                tier.value: int((
                    all_rows["label_state"].eq(LabelState.LABELED.value)
                    & all_rows["class_label"].eq(class_label.value)
                    & all_rows["evidence_tier"].eq(tier.value)
                ).sum())
                for tier in (EvidenceTier.A, EvidenceTier.B)
            }
            for class_label in TrainingClass
        },
        "ambiguous_count": int(all_rows["label_state"].eq(LabelState.AMBIGUOUS.value).sum()),
        "unlabeled_count": int(all_rows["label_state"].eq(LabelState.UNLABELED.value).sum()),
        "split_counts": {name: int((model_rows["split"] == name).sum()) for name in ("train", "validation", "test")},
        "overlap_checks": overlap_checks,
        "source_provenance": [
            {
                "region_id": "fort_mcmurray_2016",
                "firms_source": "NASA FIRMS event cache",
                "perimeter_source": "Canadian dated fire perimeter archive",
                "perimeter_url": "https://zenodo.org/records/19502692",
                "worldcover_temporal_mismatch": "not used; context fields unavailable",
                "osm_temporal_mismatch": "not used; context fields unavailable",
            },
            {
                "region_id": "chakan_2024_demo",
                "firms_source": "NASA FIRMS VIIRS SNPP/NOAA-20 event cache",
                "industrial_sources": "MIDC Enterprise GIS and OpenStreetMap cache",
                "landcover_source": "ESA WorldCover 2021 v200 cache",
                "worldcover_temporal_mismatch": "2021 land cover applied to 2024 observations",
                "osm_temporal_mismatch": "current cache snapshot applied to 2024 observations",
                "fitting_status": "hard excluded",
            },
        ],
        "source_snapshots": source_snapshots,
        "artifacts": artifacts,
    }
    manifest_path = manifest_dir / "dataset_manifest.json"
    write_manifest(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)
    manifest["manifest_sha256"] = sha256_file(manifest_path)
    return manifest
