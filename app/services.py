"""Upload processing: the one place that ties the geospatial core to the database.

Flow for one upload:

    hash the bytes -> return the earlier result if this exact file was seen
    create the file row (PROCESSING)
    [with ?async=true the response is sent here, and the rest runs in the background]
    parse the file into features
    resolve the source CRS
    measure every feature
    store the features, mark the file COMPLETED (or FAILED with the reason)
"""

import hashlib
import logging
import time

from pyproj import CRS
from shapely.geometry import mapping
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.geo import parse_upload
from app.geo.crs import crs_label, parse_declared_crs, resolve_source_crs
from app.geo.measure import measure_feature
from app.geo.types import GeoFileError, RawFeature
from app.models import Feature, FileStatus, GeoFile

logger = logging.getLogger(__name__)


class ProcessingFailed(Exception):
    """The upload was recorded but its content could not be processed."""

    def __init__(self, file_id: str, code: str, message: str):
        super().__init__(message)
        self.file_id = file_id
        self.code = code
        self.message = message


def process_upload(db: Session, filename: str, data: bytes, declared_crs: str | None) -> tuple[GeoFile, bool]:
    """Register and process one uploaded file. Returns (file row, was_duplicate)."""
    record, duplicate = register_upload(db, filename, data, declared_crs)
    if not duplicate:
        process_file(db, record, data, declared_crs)
    return record, duplicate


def register_upload(db: Session, filename: str, data: bytes, declared_crs: str | None) -> tuple[GeoFile, bool]:
    """Put the upload on record with status PROCESSING. Returns (file row, was_duplicate).

    A file identical to one already processed returns that earlier row instead.
    """
    # Parse the optional CRS first so a typo is rejected before anything is stored.
    declared = parse_declared_crs(declared_crs) if declared_crs else None

    digest = hashlib.sha256(data).hexdigest()
    if declared is None:
        duplicate = db.scalar(
            select(GeoFile)
            .where(GeoFile.sha256 == digest, GeoFile.status == FileStatus.COMPLETED.value)
            .order_by(GeoFile.created_at.desc())
        )
        if duplicate is not None:
            return duplicate, True

    record = GeoFile(filename=filename, size_bytes=len(data), sha256=digest, status=FileStatus.PROCESSING.value)
    db.add(record)
    db.commit()  # the upload is on record from here, whatever happens next
    return record, False


def process_in_background(session_factory: sessionmaker, file_id: str, data: bytes, declared_crs: str | None) -> None:
    """Process a registered upload after the response has been sent.

    Runs with its own database session, because the request's session is
    closed by then. The outcome, success or failure, is recorded on the file
    row for the client to poll.
    """
    with session_factory() as db:
        record = db.get(GeoFile, file_id)
        try:
            process_file(db, record, data, declared_crs)
        except ProcessingFailed as exc:
            logger.info("File %s could not be processed: %s", file_id, exc.message)
        except Exception:  # noqa: BLE001 - process_file logged it and marked the file FAILED; nobody to raise to
            pass


def process_file(db: Session, record: GeoFile, data: bytes, declared_crs: str | None) -> None:
    """Parse, measure and store a registered upload, then mark it COMPLETED or FAILED."""
    declared = parse_declared_crs(declared_crs) if declared_crs else None
    filename = record.filename
    started = time.perf_counter()
    try:
        parsed = parse_upload(filename, data)
        if len(parsed.features) > settings.max_features:
            raise GeoFileError("too_many_features", f"The file has more than {settings.max_features} features.")

        source_crs, crs_warnings = resolve_source_crs(parsed, declared)
        label = crs_label(source_crs) if source_crs is not None else None
        rows = [_to_row(record.id, feature, source_crs, label) for feature in parsed.features]

        if rows:
            db.execute(insert(Feature), rows)  # one bulk INSERT, not one ORM object per feature
        record.file_format = parsed.file_format
        record.crs = label
        record.feature_count = len(rows)
        record.warnings = parsed.warnings + crs_warnings
        record.status = FileStatus.COMPLETED.value
        record.processing_ms = _elapsed_ms(started)
        db.commit()  # features and the COMPLETED status land together, or not at all
    except GeoFileError as exc:
        _mark_failed(db, record, exc.message, started)
        raise ProcessingFailed(record.id, exc.code, exc.message) from exc
    except Exception:
        # A bug or a database error. Record the failure so the file is not
        # left in PROCESSING forever, then let it surface as a 500.
        logger.exception("Unexpected failure processing file %s", record.id)
        _mark_failed(db, record, "Unexpected error while processing the file.", started)
        raise

    logger.info("Processed %s (%s): %d features in %d ms", filename, record.id, len(rows), record.processing_ms)


def _to_row(file_id: str, feature: RawFeature, source_crs: CRS | None, label: str | None) -> dict:
    """Measure one feature and return it as a row for the features table."""
    result = measure_feature(feature, source_crs)
    return dict(
        file_id=file_id,
        feature_index=feature.index,
        source_id=feature.source_id,
        geometry_type=feature.geometry_type,
        geometry=mapping(feature.geometry) if feature.geometry is not None else None,
        crs=label,
        properties=feature.properties,
        status=result.status.value,
        message=result.message,
        area_m2=result.area_m2,
        perimeter_m=result.perimeter_m,
        length_m=result.length_m,
        projected_crs=result.projected_crs,
        projection_strategy=result.projection_strategy,
        projection_reason=result.projection_reason,
        geodesic_area_m2=result.geodesic_area_m2,
        geodesic_length_m=result.geodesic_length_m,
        deviation_pct=result.deviation_pct,
        expected_deviation_pct=result.expected_deviation_pct,
        warnings=result.warnings,
        repaired=result.repaired,
        vertices_added=result.vertices_added,
        crosses_antimeridian=result.crosses_antimeridian,
        surface_length_m=result.surface_length_m,
        elevation_min_m=result.elevation_min_m,
        elevation_max_m=result.elevation_max_m,
    )


def _mark_failed(db: Session, record: GeoFile, message: str, started: float) -> None:
    db.rollback()
    record.status = FileStatus.FAILED.value
    record.error = message
    record.processing_ms = _elapsed_ms(started)
    db.commit()


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
