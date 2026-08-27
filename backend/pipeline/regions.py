"""India-first monitoring-region catalog.

Industrial and forest regions both use the generic FIRMS thermal pipeline;
``monitoring_focus`` controls labels and the historical product/window used
for a meaningful demonstration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


INDUSTRIAL_FOCUS = "industrial"
FOREST_FOCUS = "forest"

CURRENT_FIRMS_SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
FOREST_HISTORY_SOURCES = ("VIIRS_NOAA20_SP", "VIIRS_SNPP_SP")


@dataclass(frozen=True)
class RegionConfig:
    event_id: int
    region_id: str
    name: str
    country: str
    country_code: str
    state: str
    center: tuple[float, float]
    bbox: tuple[float, float, float, float]
    view_bbox: tuple[float, float, float, float] | None
    monitoring_focus: str
    history_start: str
    history_end: str
    firms_history_sources: tuple[str, ...]
    default_history_days: int = 30
    default_view_days: int = 5

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "id": self.region_id,
            "name": self.name,
            "country": self.country,
            "country_code": self.country_code,
            "state": self.state,
            "center": list(self.center),
            "bbox": list(self.bbox),
            "view_bbox": list(self.view_bbox or self.bbox),
            "monitoring_focus": self.monitoring_focus,
            "history_start": self.history_start,
            "history_end": self.history_end,
            "firms_history_sources": list(self.firms_history_sources),
            "default_history_days": self.default_history_days,
            "default_view_days": self.default_view_days,
        }


def _industrial(event_id, region_id, name, state, center, bbox) -> RegionConfig:
    return RegionConfig(
        event_id=event_id,
        region_id=region_id,
        name=name,
        country="India",
        country_code="in",
        state=state,
        center=center,
        bbox=bbox,
        view_bbox=None,
        monitoring_focus=INDUSTRIAL_FOCUS,
        history_start="2026-07-29",
        history_end="2026-08-27",
        firms_history_sources=CURRENT_FIRMS_SOURCES,
    )


def _forest(event_id, region_id, name, state, center, bbox, view_bbox) -> RegionConfig:
    return RegionConfig(
        event_id=event_id,
        region_id=region_id,
        name=name,
        country="India",
        country_code="in",
        state=state,
        center=center,
        bbox=bbox,
        view_bbox=view_bbox,
        monitoring_focus=FOREST_FOCUS,
        # Standard products preserve a reproducible Indian fire-season window
        # after the corresponding near-real-time records expire.
        history_start="2026-03-20",
        history_end="2026-04-18",
        firms_history_sources=FOREST_HISTORY_SOURCES,
    )


REGIONS = {
    # Major industrial thermal corridors.
    "vijayanagar": _industrial(
        2, "vijayanagar", "Vijayanagar / Toranagallu Industrial Region",
        "Karnataka", (15.172, 76.677), (76.58, 15.10, 76.76, 15.24),
    ),
    "talcher_angul": _industrial(
        3, "talcher_angul", "Talcher / Angul Industrial Corridor",
        "Odisha", (20.95, 85.08), (84.75, 20.75, 85.25, 21.25),
    ),
    "dhanbad_bokaro": _industrial(
        4, "dhanbad_bokaro", "Dhanbad / Bokaro Industrial Corridor",
        "Jharkhand", (23.75, 86.10), (85.75, 23.45, 86.45, 24.05),
    ),
    "singrauli_sonbhadra": _industrial(
        5, "singrauli_sonbhadra", "Singrauli / Sonbhadra Energy Corridor",
        "Madhya Pradesh / Uttar Pradesh", (24.05, 82.70), (82.30, 23.75, 83.15, 24.40),
    ),
    "korba": _industrial(
        6, "korba", "Korba Power and Industrial Region",
        "Chhattisgarh", (22.40, 82.70), (82.40, 22.15, 83.05, 22.75),
    ),
    "jamnagar_vadinar": _industrial(
        7, "jamnagar_vadinar", "Jamnagar / Vadinar Refinery Corridor",
        "Gujarat", (22.45, 70.05), (69.65, 22.15, 70.45, 22.75),
    ),
    # Data-rich forest-fire landscapes selected from the 2026 fire season.
    "gadchiroli_tadoba": _forest(
        8, "gadchiroli_tadoba", "Gadchiroli Forest Landscape",
        "Maharashtra", (20.15, 80.00), (79.00, 19.50, 80.50, 20.80),
        (79.75, 19.85, 80.25, 20.45),
    ),
    "kanha_pench": _forest(
        9, "kanha_pench", "Kanha Forest Landscape",
        "Madhya Pradesh", (22.20, 80.65), (79.00, 21.30, 80.80, 23.20),
        (80.35, 21.80, 81.05, 22.55),
    ),
    "bastar": _forest(
        10, "bastar", "Bastar Forest Landscape",
        "Chhattisgarh", (18.80, 82.00), (81.20, 17.80, 82.80, 19.80),
        (81.55, 18.25, 82.35, 19.25),
    ),
    "mizoram": _forest(
        11, "mizoram", "Mizoram Forest Landscape",
        "Mizoram", (23.00, 92.85), (92.20, 22.00, 93.50, 24.00),
        (92.45, 22.55, 93.10, 23.65),
    ),
}


def get_active_region() -> RegionConfig:
    region_id = os.getenv("ACTIVE_REGION", "vijayanagar").strip().lower()
    try:
        return REGIONS[region_id]
    except KeyError as exc:
        available = ", ".join(sorted(REGIONS))
        raise ValueError(
            f"unknown ACTIVE_REGION {region_id!r}; available regions: {available}"
        ) from exc


def get_auto_prepare_region_ids() -> set[str]:
    """Return the configured startup-preparation subset (``all`` by default)."""
    configured = os.getenv("AUTO_PREPARE_REGIONS", "all").strip().lower()
    if configured == "all":
        return set(REGIONS)
    selected = {value.strip() for value in configured.split(",") if value.strip()}
    unknown = selected.difference(REGIONS)
    if unknown:
        raise ValueError(
            "unknown AUTO_PREPARE_REGIONS values: " + ", ".join(sorted(unknown))
        )
    return selected
