"""Leakage-resistant geographic/event/temporal split assignment."""

from __future__ import annotations

import pandas as pd

from pipeline.training_data.schemas import DatasetSplit, EvidenceTier, LabelState, RegionProfile


def supervised_rows(features_and_labels: pd.DataFrame) -> pd.DataFrame:
    return features_and_labels[
        features_and_labels["label_state"].eq(LabelState.LABELED.value)
        & features_and_labels["evidence_tier"].isin([EvidenceTier.A.value, EvidenceTier.B.value])
    ].copy()


def assign_grouped_splits(
    frame: pd.DataFrame, regions: tuple[RegionProfile, ...],
) -> tuple[pd.DataFrame, dict]:
    profiles = {region.region_id: region for region in regions}
    eligible = supervised_rows(frame)
    eligible["split"] = eligible["region_id"].map(
        lambda region_id: profiles[region_id].fixed_split.value,
    )
    forbidden = eligible["region_id"].map(
        lambda region_id: profiles[region_id].exclude_from_model_fitting,
    )
    if forbidden.any() or eligible["split"].eq(DatasetSplit.EXCLUDED.value).any():
        raise ValueError("model-fitting rows include an excluded region")

    checks = {}
    split_names = [DatasetSplit.TRAIN.value, DatasetSplit.VALIDATION.value, DatasetSplit.TEST.value]
    for column in (
        "region_id", "geographic_group_id", "temporal_group_id", "origin_event_id",
        "evidence_source", "evidence_record_id",
    ):
        overlaps = []
        groups = {
            name: set(eligible.loc[eligible["split"].eq(name), column].dropna())
            if column in eligible else set()
            for name in split_names
        }
        for left_index, left in enumerate(split_names):
            for right in split_names[left_index + 1:]:
                shared = sorted(groups[left] & groups[right], key=str)
                if shared:
                    overlaps.append({"splits": [left, right], "values": [str(value) for value in shared]})
        checks[f"{column}_overlap"] = overlaps
    if any(checks.values()):
        raise ValueError(f"split leakage detected: {checks}")
    return eligible.sort_values(["split", "region_id", "observed_at", "observation_id"]).reset_index(drop=True), checks
