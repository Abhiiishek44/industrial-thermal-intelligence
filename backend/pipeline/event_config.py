"""Central configuration for seeded events and their data providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
    thermal_history_start: str | None = None
    thermal_history_end: str | None = None
    firms_history_sources: tuple[str, ...] = ()
    firms_chunk_days: int = 5
    industrial_context_provider: str = "none"
    industrial_boundary_provider: str = "none"
    industrial_boundary_filter: str | None = None
    landcover_provider: str = "none"
    industrial_near_distance_m: float = 1000.0

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


EVENT_CONFIGS = (
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
    ),
    EventConfig(
        event_id=2,
        name="Chakan Industrial Thermal Monitoring",
        year=2024,
        # Aggregate extent of the Chakan MIDC phases from the official MIDC GIS.
        bbox=(73.7358129924, 18.7095245857, 73.8649120643, 18.8080439189),
        # A 30-day replay window exposes the available January observations.
        start_date="2024-01-01",
        end_date="2024-01-30",
        description=(
            "Satellite thermal-anomaly monitoring for the Chakan MIDC industrial area in "
            "Pune, Maharashtra. Wildfire spread classification is intentionally disabled."
        ),
        analysis_mode=THERMAL_MONITORING_MODE,
        country_code="in",
        roads_provider="osm",
        population_provider="none",
        actual_perimeter_provider="none",
        thermal_history_start="2024-01-01",
        thermal_history_end="2024-12-31",
        firms_history_sources=("VIIRS_SNPP_SP", "VIIRS_NOAA20_SP"),
        firms_chunk_days=5,
        industrial_context_provider="osm_overpass",
        industrial_boundary_provider="midc_arcgis",
        industrial_boundary_filter="Chakan",
        landcover_provider="esa_worldcover_2021",
        industrial_near_distance_m=1000.0,
    ),
)

_BY_NAME = {config.name: config for config in EVENT_CONFIGS}


def get_event_config(event) -> EventConfig:
    """Return a configured profile, defaulting unknown events to safe monitoring."""
    configured = _BY_NAME.get(event.name)
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
