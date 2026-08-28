"""Industrial thermal-monitoring data preparation."""

from pipeline.thermal.history import (
    collect_latest_firms,
    collect_firms_history,
    ensure_thermal_history,
    load_history_metadata,
    normalize_firms_history,
)
from pipeline.thermal.context import (
    collect_industrial_context,
    collect_landcover,
    enrich_thermal_history,
    ensure_thermal_context,
    load_context_metadata,
)
from pipeline.thermal.persistence import (
    aggregate_multisensor_observations,
    build_classification_candidates,
    build_persistent_sources,
    ensure_persistence_analysis,
    get_persistence_settings,
    thermal_frame_to_geojson,
)
from pipeline.thermal.classification import (
    classification_metadata,
    classify_persistent_sources,
    classify_source,
    ensure_source_classification,
)

__all__ = [
    "collect_latest_firms",
    "collect_firms_history",
    "ensure_thermal_history",
    "load_history_metadata",
    "normalize_firms_history",
    "collect_industrial_context",
    "collect_landcover",
    "enrich_thermal_history",
    "ensure_thermal_context",
    "load_context_metadata",
    "aggregate_multisensor_observations",
    "build_classification_candidates",
    "build_persistent_sources",
    "ensure_persistence_analysis",
    "get_persistence_settings",
    "thermal_frame_to_geojson",
    "classification_metadata",
    "classify_persistent_sources",
    "classify_source",
    "ensure_source_classification",
]
