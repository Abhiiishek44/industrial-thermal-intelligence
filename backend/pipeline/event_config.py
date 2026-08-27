"""Central configuration for seeded events and their data providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pipeline.regions import REGIONS, get_auto_prepare_region_ids


WILDFIRE_MODE = "wildfire_prediction"
THERMAL_MONITORING_MODE = "thermal_monitoring"


@dataclass(frozen=True)
class EventConfig:
    event_id: int
    name: str
    year: int
    bbox: tuple[float, float, float, float]
    start_date: str
    end_date: str
    description: str
    analysis_mode: str
    country_code: str
    roads_provider: str
    population_provider: str
    actual_perimeter_provider: str
    view_bbox: tuple[float, float, float, float] | None = None
    thermal_history_start: str | None = None
    thermal_history_end: str | None = None
    firms_history_sources: tuple[str, ...] = ()
    firms_chunk_days: int = 5
    industrial_context_provider: str = "none"
    industrial_boundary_provider: str = "none"
    industrial_boundary_filter: str | None = None
    landcover_provider: str = "none"
    industrial_near_distance_m: float = 1000.0
    region_id: str | None = None
    default_view_days: int = 5
    monitoring_focus: str | None = None
    state: str | None = None
    public: bool = True

    @property
    def bbox_wkt(self) -> str:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return (
            f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, "
            f"{max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"
        )

    @property
    def thermal_history_dates(self) -> tuple[date, date] | None:
        """Return the configured monitoring-history window, when applicable."""
        if not self.thermal_history_start or not self.thermal_history_end:
            return None
        return (
            date.fromisoformat(self.thermal_history_start),
            date.fromisoformat(self.thermal_history_end),
        )


_LEGACY_EVENT_CONFIGS = (
    EventConfig(
        event_id=1,
        name="Fort McMurray Wildfire 2016",
        year=2016,
        bbox=(-112.634, 56.157, -110.002, 57.380),
        start_date="2016-05-01",
        end_date="2016-05-10",
        description=(
            "The 2016 Horse River Wildfire (MWF-009) forced the evacuation of approximately "
            "88,000 residents from Fort McMurray, Alberta. It burned approximately 590,000 "
            "hectares and is the costliest disaster in Canadian history."
        ),
        analysis_mode=WILDFIRE_MODE,
        country_code="ca",
        roads_provider="canada_static",
        population_provider="canada_census",
        actual_perimeter_provider="canada_static",
        public=False,
    ),
)


def _monitoring_event(region) -> EventConfig:
    focus_label = "industrial thermal" if region.monitoring_focus == "industrial" else "forest-fire"
    return EventConfig(
        event_id=region.event_id,
        name=f"{region.name} Monitoring",
        year=2026,
        bbox=region.bbox,
        view_bbox=region.view_bbox,
        start_date=region.history_start,
        end_date=region.history_end,
        description=(
            f"NASA FIRMS {focus_label} monitoring for {region.name}, {region.state}, India. "
            "Detections are enriched with land cover, persistence, and nearby industrial context."
        ),
        analysis_mode=THERMAL_MONITORING_MODE,
        country_code=region.country_code,
        # The India catalog prioritizes FIRMS/context analytics. Fetching a
        # second Overpass dataset for roads across every region quickly hits
        # public-service rate limits and is not required by these views.
        roads_provider="none",
        population_provider="none",
        actual_perimeter_provider="none",
        thermal_history_start=region.history_start,
        thermal_history_end=region.history_end,
        firms_history_sources=region.firms_history_sources,
        firms_chunk_days=5,
        industrial_context_provider="osm_overpass",
        industrial_boundary_provider="none",
        landcover_provider="esa_worldcover_2021",
        industrial_near_distance_m=1000.0,
        region_id=region.region_id,
        default_view_days=region.default_view_days,
        monitoring_focus=region.monitoring_focus,
        state=region.state,
    )


INDIA_EVENT_CONFIGS = tuple(_monitoring_event(region) for region in REGIONS.values())
EVENT_CONFIGS = _LEGACY_EVENT_CONFIGS + INDIA_EVENT_CONFIGS

_BY_NAME = {config.name: config for config in EVENT_CONFIGS}
_BY_ID = {config.event_id: config for config in EVENT_CONFIGS}


def get_event_config(event) -> EventConfig:
    """Return a configured profile, defaulting unknown events to safe monitoring."""
    configured = _BY_ID.get(event.id) or _BY_NAME.get(event.name)
    if configured is not None:
        return configured

    from pipeline.spatial.spatial_helpers import event_bbox

    return EventConfig(
        event_id=event.id,
        name=event.name,
        year=event.year,
        bbox=event_bbox(event),
        start_date=event.start_date.isoformat(),
        end_date=event.end_date.isoformat() if event.end_date else event.start_date.isoformat(),
        description=event.description or "",
        analysis_mode=THERMAL_MONITORING_MODE,
        country_code="",
        roads_provider="osm",
        population_provider="none",
        actual_perimeter_provider="none",
    )


def uses_wildfire_model(event) -> bool:
    return get_event_config(event).analysis_mode == WILDFIRE_MODE


def is_public_event(event) -> bool:
    """Return whether an event belongs in the India-first UI catalog."""
    return bool(get_event_config(event).public)


def should_prepare_event(event) -> bool:
    """Limit expensive startup preparation to configured India regions."""
    config = get_event_config(event)
    return bool(
        config.public
        and config.country_code == "in"
        and config.region_id in get_auto_prepare_region_ids()
    )
