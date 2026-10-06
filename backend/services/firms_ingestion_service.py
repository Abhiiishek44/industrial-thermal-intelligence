"""Durable near-real-time FIRMS ingestion, event grouping, and alerting.

The existing thermal pipeline remains responsible for downloading and enriching
regional FIRMS history.  This service persists only newly observed pixels from
those enriched artifacts and reuses the already-loaded 2021-2024 model for
production scoring; it never fits or retrains a model.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from geoalchemy2.elements import WKTElement
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from db.connection import db
from db.models import FireAlert, FireDetection, FirmsSyncState, MonitoringFireEvent
from services.live_thermal_inference import live_thermal_inference


log = logging.getLogger(__name__)
UTC = timezone.utc
ALLOWED_WINDOW_HOURS = (1, 6, 12, 24, 48)
MODEL_FEATURES = (
    "frp_mean_24h", "frp_max_24h", "brightness_mean_24h",
    "brightness_max_24h", "frp_ratio_24h_vs_30d",
    "brightness_ratio_24h_vs_30d", "frp_zscore_24h",
    "brightness_zscore_24h", "frp_percent_change_24h",
    "brightness_percent_change_24h", "detections_24h", "detections_7d",
    "detections_30d", "active_days_30d", "persistence_30d",
    "hours_since_previous_detection",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_acquisition_time(acq_date: Any, acq_time: Any) -> datetime:
    """Combine FIRMS date/time fields into a strict timezone-aware UTC value."""
    date_text = str(acq_date).strip()
    time_text = str(acq_time).strip().removesuffix(".0").zfill(4)
    parsed = datetime.strptime(date_text + time_text, "%Y-%m-%d%H%M")
    return parsed.replace(tzinfo=UTC)


def detection_key(record: dict[str, Any], acquisition_time: datetime | None = None) -> str:
    """Return a deterministic identity based only on stable FIRMS fields."""
    acquired = acquisition_time or build_acquisition_time(
        record.get("acq_date"), record.get("acq_time")
    )
    identity = "|".join((
        str(record.get("satellite") or "").strip().upper(),
        str(record.get("instrument") or "").strip().upper(),
        f"{float(record['latitude']):.5f}",
        f"{float(record['longitude']):.5f}",
        acquired.isoformat(),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def rolling_cutoff(hours: int = 24, *, now: datetime | None = None) -> datetime:
    if hours not in ALLOWED_WINDOW_HOURS:
        raise ValueError(f"hours must be one of {ALLOWED_WINDOW_HOURS}")
    current = now or utc_now()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(UTC) - timedelta(hours=hours)


def filter_recent_records(
    records: Iterable[dict[str, Any]], hours: int = 48, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    cutoff = rolling_cutoff(hours, now=now)
    result = []
    for record in records:
        try:
            acquired = _record_acquisition_time(record)
        except (TypeError, ValueError):
            continue
        if acquired >= cutoff:
            result.append(record)
    return result


def _record_acquisition_time(record: dict[str, Any]) -> datetime:
    value = record.get("observed_at") or record.get("acquisition_time")
    if value is not None and not pd.isna(value):
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize("UTC")
        return parsed.tz_convert("UTC").to_pydatetime()
    return build_acquisition_time(record.get("acq_date"), record.get("acq_time"))


def _float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _json_number(value: Any) -> float | int | None:
    number = _float(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else number


def _boolean(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def event_matches_detection(
    event: Any,
    *,
    latitude: float,
    longitude: float,
    acquisition_time: datetime,
    radius_km: float,
    time_gap_hours: float,
) -> bool:
    """Pure spatial-temporal event membership rule used by ingestion/tests."""
    gap = acquisition_time - event.last_detected_at
    return (
        timedelta(0) <= gap <= timedelta(hours=time_gap_hours)
        and _haversine_km(
            latitude, longitude, event.centroid_latitude, event.centroid_longitude
        ) <= radius_km
    )


def should_create_alert(confidence: float | None, threshold: float) -> bool:
    return confidence is not None and confidence >= threshold


def _point(latitude: float, longitude: float) -> WKTElement:
    return WKTElement(f"POINT({longitude:.8f} {latitude:.8f})", srid=4326)


def get_ingestion_settings() -> dict[str, Any]:
    sources = tuple(
        item.strip() for item in os.getenv(
            "FIRMS_SOURCES", "VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT"
        ).split(",") if item.strip()
    )
    return {
        "poll_interval_minutes": max(1, int(os.getenv("FIRMS_POLL_INTERVAL_MINUTES", "15"))),
        "lookback_hours": int(os.getenv("FIRMS_LOOKBACK_HOURS", "48")),
        "sources": sources,
        "event_radius_km": max(0.1, float(os.getenv("FIRE_EVENT_RADIUS_KM", "3"))),
        "event_time_gap_hours": max(0.1, float(os.getenv("FIRE_EVENT_TIME_GAP_HOURS", "6"))),
        "inactive_hours": max(0.1, float(os.getenv("FIRE_EVENT_INACTIVE_HOURS", "6"))),
        "alert_confidence_threshold": min(1.0, max(0.0, float(os.getenv(
            "FIRE_ALERT_CONFIDENCE_THRESHOLD", "0.75"
        )))),
        "model_version": os.getenv("MODEL_VERSION", "v1.0"),
    }


def validate_model_schema() -> list[str]:
    """Fail before inference if the saved artifact and production schema diverge."""
    actual = list(live_thermal_inference.artifact["features"])
    expected = list(MODEL_FEATURES)
    if actual != expected:
        raise ValueError(
            "live feature schema does not match saved model: "
            f"model_expected_features={actual}; live_generated_features={expected}"
        )
    return actual


@dataclass
class IngestionStats:
    records_received: int = 0
    duplicates: int = 0
    new_records: int = 0
    predictions_completed: int = 0
    prediction_failures: int = 0
    events_created: int = 0
    events_updated: int = 0
    alerts_created: int = 0
    malformed_records: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class FirmsIngestionService:
    """Persist new enriched pixels and associate them with physical events."""

    def __init__(self) -> None:
        self.settings = get_ingestion_settings()

    def ingest_records(
        self,
        records: Iterable[dict[str, Any]],
        *,
        received_at: datetime | None = None,
        model_rows: dict[str, dict[str, Any]] | None = None,
        monitoring_region_id: int | None = None,
    ) -> IngestionStats:
        received = (received_at or utc_now()).astimezone(UTC)
        materialized = list(records)
        stats = IngestionStats(records_received=len(materialized))
        model_rows = model_rows or {}
        validate_model_schema()

        keys: list[str] = []
        normalized: list[tuple[str, dict[str, Any], datetime]] = []
        for record in materialized:
            try:
                acquired = _record_acquisition_time(record)
                latitude = float(record["latitude"])
                longitude = float(record["longitude"])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError("coordinates out of range")
                key = detection_key(record, acquired)
            except (KeyError, TypeError, ValueError, OverflowError):
                stats.malformed_records += 1
                continue
            keys.append(key)
            normalized.append((key, record, acquired))

        existing = {
            row[0] for row in db.session.query(FireDetection.detection_key).filter(
                FireDetection.detection_key.in_(keys)
            ).all()
        } if keys else set()

        for key, record, acquired in normalized:
            if key in existing:
                stats.duplicates += 1
                continue
            score = model_rows.get(key, {})
            try:
                with db.session.begin_nested():
                    detection = self._new_detection(
                        key, record, acquired, received, score,
                        monitoring_region_id=monitoring_region_id,
                    )
                    event, created = self._associate_event(detection)
                    detection.fire_event = event
                    db.session.add(detection)
                    db.session.flush()
                    alert_created = self._create_new_event_alert(event, detection, created)
                stats.new_records += 1
                stats.predictions_completed += int(detection.model_confidence is not None)
                stats.prediction_failures += int(detection.model_confidence is None)
                stats.events_created += int(created)
                stats.events_updated += int(not created)
                stats.alerts_created += alert_created
                existing.add(key)
            except IntegrityError:
                stats.duplicates += 1
            except Exception:
                stats.prediction_failures += 1
                log.exception("FIRMS record processing failed detection_key=%s", key)
        self._update_event_statuses(received)
        db.session.commit()
        return stats

    def _new_detection(
        self,
        key: str,
        record: dict[str, Any],
        acquired: datetime,
        received: datetime,
        score: dict[str, Any],
        *,
        monitoring_region_id: int | None = None,
    ) -> FireDetection:
        latitude, longitude = float(record["latitude"]), float(record["longitude"])
        model_features = {
            name: _json_number(score.get(name)) for name in MODEL_FEATURES
            if _json_number(score.get(name)) is not None
        }
        prediction = record.get("source_class") or score.get("predicted_class")
        confidence = _float(score.get("model_confidence"))
        return FireDetection(
            monitoring_region_id=monitoring_region_id,
            detection_key=key,
            latitude=latitude,
            longitude=longitude,
            location=_point(latitude, longitude),
            acquisition_time=acquired,
            received_at=received,
            data_latency_seconds=int((received - acquired).total_seconds()),
            satellite=str(record.get("satellite") or "UNKNOWN"),
            instrument=str(record.get("instrument") or "UNKNOWN"),
            source_product=str(record.get("source_product") or "") or None,
            brightness=_float(record.get("brightness") or record.get("bright_ti4")),
            bright_ti4=_float(record.get("bright_ti4")),
            bright_ti5=_float(record.get("bright_ti5")),
            frp=_float(record.get("frp")),
            firms_confidence=str(record.get("confidence") or "") or None,
            day_night=str(record.get("daynight") or "") or None,
            land_cover=str(record.get("landcover_class") or "unknown"),
            landcover_group=str(record.get("landcover_group") or "unknown"),
            inside_industrial_polygon=_boolean(
                record.get("inside_industrial_polygon")
            ),
            near_industrial_facility=_boolean(
                record.get("near_industrial_facility")
            ),
            model_features=model_features or None,
            predicted_class=str(prediction or "not_evaluated"),
            model_confidence=confidence,
            model_version=self.settings["model_version"],
        )

    def _associate_event(self, detection: FireDetection) -> tuple[MonitoringFireEvent, bool]:
        earliest = detection.acquisition_time - timedelta(
            hours=self.settings["event_time_gap_hours"]
        )
        candidates_query = MonitoringFireEvent.query.filter(
            MonitoringFireEvent.last_detected_at >= earliest,
            MonitoringFireEvent.last_detected_at <= detection.acquisition_time,
        )
        if detection.monitoring_region_id is None:
            candidates_query = candidates_query.filter(
                MonitoringFireEvent.monitoring_region_id.is_(None)
            )
        else:
            candidates_query = candidates_query.filter(
                MonitoringFireEvent.monitoring_region_id == detection.monitoring_region_id
            )
        candidates = candidates_query.with_for_update().all()
        nearby = [
            (event, _haversine_km(
                detection.latitude, detection.longitude,
                event.centroid_latitude, event.centroid_longitude,
            )) for event in candidates if event_matches_detection(
                event,
                latitude=detection.latitude,
                longitude=detection.longitude,
                acquisition_time=detection.acquisition_time,
                radius_km=self.settings["event_radius_km"],
                time_gap_hours=self.settings["event_time_gap_hours"],
            )
        ]
        if not nearby:
            key = hashlib.sha256(
                f"{detection.detection_key}|event".encode("utf-8")
            ).hexdigest()
            event = MonitoringFireEvent(
                monitoring_region_id=detection.monitoring_region_id,
                event_key=key,
                centroid_latitude=detection.latitude,
                centroid_longitude=detection.longitude,
                location=_point(detection.latitude, detection.longitude),
                first_detected_at=detection.acquisition_time,
                last_detected_at=detection.acquisition_time,
                status="NEW",
                detection_count=1,
                max_frp=detection.frp,
                average_frp=detection.frp,
                predicted_class=detection.predicted_class,
                confidence=detection.model_confidence,
            )
            db.session.add(event)
            db.session.flush()
            return event, True

        event = min(nearby, key=lambda item: item[1])[0]
        old_count = event.detection_count
        new_count = old_count + 1
        event.centroid_latitude = (
            event.centroid_latitude * old_count + detection.latitude
        ) / new_count
        event.centroid_longitude = (
            event.centroid_longitude * old_count + detection.longitude
        ) / new_count
        event.location = _point(event.centroid_latitude, event.centroid_longitude)
        event.last_detected_at = max(event.last_detected_at, detection.acquisition_time)
        event.detection_count = new_count
        event.status = "ACTIVE"
        if detection.frp is not None:
            event.max_frp = max(event.max_frp or detection.frp, detection.frp)
            event.average_frp = (
                ((event.average_frp or 0.0) * old_count) + detection.frp
            ) / new_count
        if (detection.model_confidence or -1) >= (event.confidence or -1):
            event.confidence = detection.model_confidence
            event.predicted_class = detection.predicted_class
        return event, False

    def _create_new_event_alert(
        self, event: MonitoringFireEvent, detection: FireDetection, created: bool
    ) -> int:
        if not created:
            return 0
        confidence = detection.model_confidence
        meets_threshold = should_create_alert(
            confidence, self.settings["alert_confidence_threshold"]
        )
        dedup = f"event:{event.event_key}:created"
        db.session.add(FireAlert(
            fire_event_id=event.id,
            detection_id=detection.id,
            alert_type="NEW_EVENT",
            severity=(
                "HIGH" if confidence is not None and confidence >= 0.9
                else "MEDIUM" if meets_threshold
                else "LOW"
            ),
            message=(
                f"New {detection.predicted_class} event at "
                f"{detection.latitude:.4f}, {detection.longitude:.4f}"
            ),
            deduplication_key=dedup,
        ))
        now = utc_now()
        event.alert_status = detection.alert_status = (
            "CREATED" if meets_threshold else "CREATED_LOW_PRIORITY"
        )
        event.alert_sent_at = detection.alert_sent_at = now
        return 1

    def _update_event_statuses(self, now: datetime) -> None:
        inactive_cutoff = now - timedelta(hours=self.settings["inactive_hours"])
        expired_cutoff = now - timedelta(hours=48)
        MonitoringFireEvent.query.filter(
            MonitoringFireEvent.last_detected_at < inactive_cutoff,
            MonitoringFireEvent.last_detected_at >= expired_cutoff,
            MonitoringFireEvent.status.notin_(("INACTIVE", "EXPIRED")),
        ).update({"status": "INACTIVE"}, synchronize_session=False)
        MonitoringFireEvent.query.filter(
            MonitoringFireEvent.last_detected_at < expired_cutoff,
            MonitoringFireEvent.status != "EXPIRED",
        ).update({"status": "EXPIRED"}, synchronize_session=False)

    def sync_event_artifacts(self, event: Any, *, now: datetime | None = None) -> IngestionStats:
        """Persist newly enriched pixels produced by the existing regional pipeline."""
        started_at = time.monotonic()
        thermal_dir = (
            Path(__file__).resolve().parents[2] / "data" / "events"
            / f"{event.year}_{event.id:04d}" / "data_processed" / "thermal"
        )
        enriched_path = thermal_dir / "firms_enriched.parquet"
        aggregated_path = thermal_dir / "detections_aggregated.parquet"
        if not enriched_path.exists() or not aggregated_path.exists():
            return IngestionStats()
        current = now or utc_now()
        enriched_history = pd.read_parquet(enriched_path)
        enriched_history["observed_at"] = pd.to_datetime(
            enriched_history["observed_at"], utc=True, errors="coerce"
        )
        cutoff = current - timedelta(hours=self.settings["lookback_hours"])
        enriched = enriched_history[
            enriched_history["observed_at"].ge(cutoff)
        ].copy()
        if enriched.empty:
            return IngestionStats()

        classified_path = thermal_dir / "classified_sources.parquet"
        if classified_path.exists():
            enriched = self._add_existing_classifications(
                enriched, pd.read_parquet(classified_path)
            )

        # Database uniqueness is the final guard, but this preflight ensures
        # already-persisted pixels never reach expensive model inference.
        candidate_records = enriched.replace({np.nan: None}).to_dict("records")
        candidate_keys = []
        keyed_records = []
        records_by_key: dict[str, dict[str, Any]] = {}
        for record in candidate_records:
            try:
                key = detection_key(record, _record_acquisition_time(record))
            except (KeyError, TypeError, ValueError):
                keyed_records.append((None, record))
                continue
            candidate_keys.append(key)
            keyed_records.append((key, record))
            records_by_key[key] = record
        existing_detections = FireDetection.query.options(
            selectinload(FireDetection.fire_event)
        ).filter(
            FireDetection.detection_key.in_(candidate_keys)
        ).all() if candidate_keys else []
        existing_keys = {row.detection_key for row in existing_detections}
        new_records = [
            record for key, record in keyed_records
            if key is None or key not in existing_keys
        ]

        score_rows: dict[str, dict[str, Any]] = {}
        if new_records or any(
            detection.model_confidence is None for detection in existing_detections
        ):
            score_started_at = time.monotonic()
            # Every engineered baseline uses at most 30 days. Retaining one
            # extra day gives boundary-safe context without recomputing months
            # of irrelevant history for a 48-hour ingestion window.
            score_cutoff = enriched["observed_at"].min() - pd.Timedelta(days=31)
            model_history = enriched_history[
                enriched_history["observed_at"].ge(score_cutoff)
            ].copy()
            # Score the raw enriched history rather than spatially aggregated
            # rows so each result retains the exact satellite/time identity
            # used by the durable detection key.
            scores = self._score_rows(model_history, str(event.name))
            for row in scores.to_dict("records"):
                try:
                    score_rows[detection_key(row, _record_acquisition_time(row))] = row
                except (KeyError, TypeError, ValueError):
                    continue
            log.info(
                "FIRMS model scoring event_id=%s rows=%d duration_ms=%d",
                event.id, len(scores), int((time.monotonic() - score_started_at) * 1000),
            )

        # Older rows predate region-aware monitoring and complete model scoring.
        # Backfill both while reading the region's own artifact so existing
        # installations do not require a destructive re-import.
        backfilled = False
        for detection in existing_detections:
            context_record = records_by_key.get(detection.detection_key, {})
            if detection.monitoring_region_id is None:
                detection.monitoring_region_id = event.id
                backfilled = True
            if (
                detection.monitoring_region_id == event.id
                and detection.fire_event is not None
                and detection.fire_event.monitoring_region_id is None
            ):
                detection.fire_event.monitoring_region_id = event.id
                backfilled = True
            if detection.landcover_group is None:
                detection.landcover_group = str(
                    context_record.get("landcover_group") or "unknown"
                )
                backfilled = True
            if detection.inside_industrial_polygon is None:
                detection.inside_industrial_polygon = _boolean(
                    context_record.get("inside_industrial_polygon")
                )
                backfilled = True
            if detection.near_industrial_facility is None:
                detection.near_industrial_facility = _boolean(
                    context_record.get("near_industrial_facility")
                )
                backfilled = True
            score = score_rows.get(detection.detection_key)
            confidence = _float(score.get("model_confidence")) if score else None
            if detection.model_confidence is None and confidence is not None:
                detection.model_confidence = confidence
                detection.model_features = {
                    name: value for name in MODEL_FEATURES
                    if (value := _json_number(score.get(name))) is not None
                } or None
                if detection.predicted_class in (None, "not_evaluated"):
                    detection.predicted_class = str(
                        score.get("predicted_class") or "industrial_normal"
                    )
                detection.model_version = self.settings["model_version"]
                if detection.fire_event is not None and (
                    detection.fire_event.confidence is None
                    or confidence >= detection.fire_event.confidence
                ):
                    detection.fire_event.confidence = confidence
                    if detection.fire_event.predicted_class in (None, "not_evaluated"):
                        detection.fire_event.predicted_class = detection.predicted_class
                backfilled = True
        if backfilled:
            db.session.commit()
        if not new_records:
            log.info(
                "FIRMS artifact sync event_id=%s records=%d duration_ms=%d",
                event.id, len(candidate_records),
                int((time.monotonic() - started_at) * 1000),
            )
            return IngestionStats(
                records_received=len(candidate_records), duplicates=len(existing_keys)
            )

        stats = self.ingest_records(
            new_records,
            received_at=current,
            model_rows=score_rows,
            monitoring_region_id=event.id,
        )
        stats.records_received = len(candidate_records)
        stats.duplicates += len(existing_keys)
        log.info(
            "FIRMS artifact sync event_id=%s records=%d duration_ms=%d",
            event.id, len(candidate_records), int((time.monotonic() - started_at) * 1000),
        )
        return stats

    @staticmethod
    def _add_existing_classifications(
        detections: pd.DataFrame, sources: pd.DataFrame
    ) -> pd.DataFrame:
        """Reuse the current source classifier without recalculating context."""
        if detections.empty or sources.empty:
            return detections
        required = {"latitude", "longitude", "source_class"}
        if not required.issubset(sources.columns):
            return detections
        from sklearn.neighbors import BallTree

        valid = sources.dropna(subset=["latitude", "longitude"]).copy()
        if valid.empty:
            return detections
        tree = BallTree(
            np.radians(valid[["latitude", "longitude"]].astype(float).to_numpy()),
            metric="haversine",
        )
        distances, indexes = tree.query(
            np.radians(detections[["latitude", "longitude"]].astype(float).to_numpy()),
            k=1,
        )
        output = detections.copy()
        output["source_class"] = None
        output["classification_confidence"] = None
        for position, (distance, source_position) in enumerate(
            zip(distances[:, 0], indexes[:, 0])
        ):
            source = valid.iloc[int(source_position)]
            radius_m = max(
                1000.0, _float(source.get("thermal_footprint_radius_m")) or 0.0
            )
            if distance * 6371008.8 <= radius_m:
                row_index = output.index[position]
                output.at[row_index, "source_class"] = source.get("source_class")
                output.at[row_index, "classification_confidence"] = source.get(
                    "classification_confidence"
                )
        return output

    def _score_rows(self, raw: pd.DataFrame, region: str) -> pd.DataFrame:
        """Apply the exact production feature builder and saved model pipeline."""
        frame = live_thermal_inference._prepare_observations(raw, region)
        if frame.empty:
            return frame
        frame = live_thermal_inference._add_model_features(frame)
        frame = live_thermal_inference._score_iforest(frame)

        # The operational risk view keeps its strict eligibility flag, but the
        # persisted NRT feed needs a model score for every valid FIRMS pixel.
        # The saved sklearn pipeline includes the same missing-value handling
        # used during training, so score rows lacking a mature 30-day baseline
        # without fitting or altering the production artifact.
        missing = frame["industrial_anomaly_score"].isna()
        if missing.any():
            artifact = live_thermal_inference.artifact
            features = list(artifact["features"])
            inputs = frame.loc[missing, features].replace([np.inf, -np.inf], np.nan)
            raw_scores = artifact["pipeline"].score_samples(inputs)
            predictions = artifact["pipeline"].predict(inputs)
            low = float(artifact["normalization"]["low"])
            high = float(artifact["normalization"]["high"])
            strength = np.clip(-np.asarray(raw_scores), low, high)
            normalized = (
                np.zeros(len(strength))
                if high <= low else (strength - low) / (high - low)
            )
            frame.loc[missing, "iforest_raw_score"] = raw_scores
            frame.loc[missing, "industrial_anomaly_score"] = normalized
            frame.loc[missing, "industrial_is_anomaly"] = predictions == -1
        frame["model_confidence"] = frame["industrial_anomaly_score"]
        frame["predicted_class"] = np.where(
            frame["industrial_is_anomaly"], "industrial_anomaly", "industrial_normal",
        )
        return frame


def record_sync_status(stats: IngestionStats | None, *, error: str | None = None) -> None:
    state = db.session.get(FirmsSyncState, 1) or FirmsSyncState(id=1)
    now = utc_now()
    state.last_attempt_at = now
    if error:
        state.status = "failed"
        state.error = error
    else:
        state.status = "succeeded"
        state.last_successful_sync = now
        state.error = None
        if stats:
            state.records_received = stats.records_received
            state.new_records = stats.new_records
            state.duplicates = stats.duplicates
            state.prediction_failures = stats.prediction_failures
    db.session.add(state)
    db.session.commit()


firms_ingestion = FirmsIngestionService()
