"""Region registry built on provider names rather than region conditionals."""

from __future__ import annotations

from pipeline.training_data.schemas import DatasetSplit, RegionProfile
from pipeline.regions import get_active_region


_ACTIVE_REGION = get_active_region()


TRAINING_REGIONS = (
    RegionProfile(
        region_id="fort_mcmurray_2016",
        display_name="Fort McMurray / Horse River Fire 2016",
        bbox=(-112.634, 56.157, -110.002, 57.380),
        start_date="2016-05-01",
        end_date="2016-05-10",
        observation_provider="event_firms_csv",
        feature_provider="generic_enriched_features",
        label_provider="dated_fire_perimeter",
        geographic_group_id="ca_alberta_fort_mcmurray",
        temporal_group_id="ca_alberta_fort_mcmurray_2016",
        source_event_id=1,
        fixed_split=DatasetSplit.TRAIN,
        provider_options=(("perimeter_path", "data/static/actual_perimeter/actual_perimeter.gpkg"),),
    ),
    RegionProfile(
        region_id="vijayanagar_2026_demo",
        display_name=f"{_ACTIVE_REGION.name} 2026 demo",
        bbox=_ACTIVE_REGION.bbox,
        start_date="2026-07-29",
        end_date="2026-08-27",
        observation_provider="event_enriched_parquet",
        feature_provider="generic_enriched_features",
        label_provider="unlabeled",
        geographic_group_id="in_karnataka_vijayanagar",
        temporal_group_id="in_karnataka_vijayanagar_2026",
        source_event_id=2,
        fixed_split=DatasetSplit.EXCLUDED,
        exclude_from_model_fitting=True,
        exclusion_reason=(
            "Inference/demo only; requires independent manual ground truth before external evaluation."
        ),
    ),
)

_BY_ID = {region.region_id: region for region in TRAINING_REGIONS}


def get_training_region(region_id: str) -> RegionProfile:
    try:
        return _BY_ID[region_id]
    except KeyError as exc:
        raise KeyError(f"unknown training region: {region_id}") from exc
