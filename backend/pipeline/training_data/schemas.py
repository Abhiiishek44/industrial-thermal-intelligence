"""Stable contracts for evidence-backed thermal-source datasets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any


SCHEMA_VERSION = "thermal-source-training-v1"


class TrainingClass(str, Enum):
    INDUSTRIAL_FIRE = "industrial_fire"
    GAS_FLARE = "gas_flare"
    MINING_ACTIVITY = "mining_activity"
    WILDFIRE = "wildfire"
    INDUSTRIAL_PROCESS_HEAT = "industrial_process_heat"
    UNKNOWN_CONFIRMED = "unknown_confirmed"
    INDUSTRIAL_THERMAL = "industrial_thermal"
    WILDFIRE_OR_VEGETATION = "wildfire_or_vegetation"
    AGRICULTURAL_BURNING = "agricultural_burning"
    OTHER_CONFIRMED = "other_confirmed"


class LabelState(str, Enum):
    LABELED = "labeled"
    UNLABELED = "unlabeled"
    AMBIGUOUS = "ambiguous"


class EvidenceTier(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class DatasetSplit(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class RegionProfile:
    region_id: str
    display_name: str
    bbox: tuple[float, float, float, float]
    start_date: str
    end_date: str
    observation_provider: str
    feature_provider: str
    label_provider: str
    geographic_group_id: str
    temporal_group_id: str
    source_event_id: int | None
    fixed_split: DatasetSplit
    exclude_from_model_fitting: bool = False
    exclusion_reason: str | None = None
    provider_options: tuple[tuple[str, Any], ...] = ()

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.provider_options)

    def as_manifest(self) -> dict[str, Any]:
        result = asdict(self)
        result["fixed_split"] = self.fixed_split.value
        result["provider_options"] = self.options
        return result


LABEL_COLUMNS = (
    "observation_id",
    "region_id",
    "label_state",
    "class_label",
    "evidence_tier",
    "evidence_source",
    "evidence_source_url",
    "evidence_record_id",
    "evidence_method",
    "labeler",
)


def validate_label_record(record: dict[str, Any]) -> None:
    """Reject heuristic or incomplete records from the supervised contract."""
    state = LabelState(record["label_state"])
    def missing(value: Any) -> bool:
        return value is None or (isinstance(value, float) and math.isnan(value))

    label = None if missing(record.get("class_label")) else record.get("class_label")
    tier = None if missing(record.get("evidence_tier")) else record.get("evidence_tier")
    if state is LabelState.LABELED:
        TrainingClass(label)
        parsed_tier = EvidenceTier(tier)
        if parsed_tier is EvidenceTier.C:
            raise ValueError("evidence tier C is candidate-only and cannot be supervised")
        required = ("evidence_source", "evidence_source_url", "evidence_record_id", "evidence_method")
        absent = [field for field in required if missing(record.get(field)) or not record.get(field)]
        if absent:
            raise ValueError(f"labeled record lacks provenance: {', '.join(absent)}")
    elif label is not None or tier is not None:
        raise ValueError("unlabeled/ambiguous records cannot carry a class or evidence tier")
