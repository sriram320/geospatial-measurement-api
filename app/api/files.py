"""HTTP endpoints. This layer only translates between HTTP and the service:
it validates the request, calls the service or runs a query, and shapes the
response. No geospatial logic lives here."""

from pathlib import PureWindowsPath

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.errors import ApiError
from app.config import settings
from app.db import get_db, get_session_factory
from app.geo import SUPPORTED_EXTENSIONS
from app.geo.types import GeoFileError, MeasurementStatus
from app.models import Feature, GeoFile
from app.schemas import (
    CrossCheckReport,
    FeatureList,
    FeatureOut,
    FileList,
    FileOut,
    FileReport,
    MeasurementList,
    MeasurementOut,
    MeasurementSummary,
    ProblemGroup,
    ProcessingReport,
    ProjectionUse,
    UploadOut,
)
from app.services import ProcessingFailed, process_in_background, process_upload, register_upload

router = APIRouter(prefix="/api/files", tags=["files"])

CHUNK = 1024 * 1024


# These are plain ``def`` endpoints on purpose. Parsing and measuring are
# CPU-bound, and FastAPI runs sync endpoints in a thread pool, so a large file
# does not block the event loop for other requests.
@router.post(
    "/",
    response_model=UploadOut,
    status_code=201,
    summary="Upload and process a file",
    responses={202: {"model": UploadOut, "description": "Accepted with ?async=true; poll the file for its status."}},
)
def upload_file(
    response: Response,
    background: BackgroundTasks,
    file: UploadFile = File(description="A .kml file, or a .zip containing one Shapefile."),
    crs: str | None = Form(None, description="Source CRS, used only if the file declares none. E.g. EPSG:32643."),
    run_async: bool = Query(
        False,
        alias="async",
        description="Return 202 at once with status PROCESSING and process in the background. "
        "Poll GET /api/files/{id}/ until the status is COMPLETED or FAILED.",
    ),
    db: Session = Depends(get_db),
    session_factory: sessionmaker = Depends(get_session_factory),
) -> UploadOut:
    # Keep only the last path component. PureWindowsPath splits on both / and \,
    # so "../../x.kml" and "C:\\data\\x.kml" both become "x.kml".
    filename = PureWindowsPath(file.filename or "").name[:255]
    if PureWindowsPath(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ApiError(
            415,
            "unsupported_file_type",
            f"Upload a file ending in one of: {', '.join(SUPPORTED_EXTENSIONS)}.",
        )

    data = _read_limited(file)
    if not data:
        raise ApiError(400, "empty_file", "The uploaded file is empty.")

    if run_async:
        try:
            record, duplicate = register_upload(db, filename, data, crs)
        except GeoFileError as exc:  # bad 'crs' value
            raise ApiError(400, exc.code, exc.message) from exc
        if duplicate:
            response.status_code = 200
        else:
            background.add_task(process_in_background, session_factory, record.id, data, crs)
            response.status_code = 202
            response.headers["Location"] = f"{router.prefix}/{record.id}/"
        return UploadOut.model_validate(record).model_copy(update={"duplicate": duplicate})

    try:
        record, duplicate = process_upload(db, filename, data, crs)
    except ProcessingFailed as exc:
        # The upload is stored with status FAILED, so the id is returned too.
        raise ApiError(422, exc.code, exc.message, file_id=exc.file_id) from exc
    except GeoFileError as exc:  # rejected before anything was stored (bad 'crs' value)
        raise ApiError(400, exc.code, exc.message) from exc

    if duplicate:
        response.status_code = 200  # nothing new was created
    return UploadOut.model_validate(record).model_copy(update={"duplicate": duplicate})


@router.get("/", response_model=FileList, summary="List uploaded files")
def list_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> FileList:
    total = db.scalar(select(func.count()).select_from(GeoFile)) or 0
    rows = db.scalars(select(GeoFile).order_by(GeoFile.created_at.desc()).limit(limit).offset(offset)).all()
    return FileList(total=total, limit=limit, offset=offset, results=rows)


@router.get("/{file_id}/", response_model=FileOut, summary="File information")
def get_file(file_id: str, db: Session = Depends(get_db)) -> GeoFile:
    return _get_file_or_404(db, file_id)


@router.get("/{file_id}/features/", response_model=FeatureList, summary="Features with geometry and attributes")
def list_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> FeatureList:
    record = _get_file_or_404(db, file_id)
    rows = db.scalars(
        select(Feature).where(Feature.file_id == file_id).order_by(Feature.feature_index).limit(limit).offset(offset)
    ).all()
    return FeatureList(
        file_id=file_id,
        total=record.feature_count,
        limit=limit,
        offset=offset,
        results=[FeatureOut.from_row(row) for row in rows],
    )


@router.get("/{file_id}/measurements/", response_model=MeasurementList, summary="Measurements for every feature")
def list_measurements(
    file_id: str,
    measurement_status: MeasurementStatus | None = Query(
        None, alias="status", description="Only return features with this measurement status."
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> MeasurementList:
    record = _get_file_or_404(db, file_id)

    query = select(Feature).where(Feature.file_id == file_id)
    if measurement_status is not None:
        query = query.where(Feature.status == measurement_status.value)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Feature.feature_index).limit(limit).offset(offset)).all()

    return MeasurementList(
        file_id=file_id,
        filename=record.filename,
        crs=record.crs,
        summary=_summarise(db, file_id),
        total=total,
        limit=limit,
        offset=offset,
        results=[MeasurementOut.from_row(row) for row in rows],
    )


@router.get("/{file_id}/report/", response_model=FileReport, summary="Quality report for the whole file")
def file_report(file_id: str, db: Session = Depends(get_db)) -> FileReport:
    """One page that answers "can I trust this file's numbers, and what went wrong?"

    The measurements endpoint lists features one by one. This one aggregates
    them, so a file with 20,000 features still gives a short answer. Every
    figure is computed by the database, not by loading the rows into Python.
    """
    record = _get_file_or_404(db, file_id)
    in_file = Feature.file_id == file_id

    projections = db.execute(
        select(Feature.projected_crs, Feature.projection_strategy, func.count())
        .where(in_file, Feature.projected_crs.is_not(None))
        .group_by(Feature.projected_crs, Feature.projection_strategy)
        .order_by(func.count().desc())
    ).all()

    # The gap the projection does not account for: observed minus predicted.
    unexplained = Feature.deviation_pct - Feature.expected_deviation_pct
    checked, max_deviation, max_unexplained, repaired, densified, antimeridian, with_elevation = db.execute(
        select(
            func.count(Feature.deviation_pct),  # count(column) skips nulls
            func.max(func.abs(Feature.deviation_pct)),
            func.max(func.abs(unexplained)),
            func.count().filter(Feature.repaired),
            func.count().filter(Feature.vertices_added > 0),
            func.count().filter(Feature.crosses_antimeridian),
            func.count(Feature.surface_length_m),
        ).where(in_file)
    ).one()

    largest = db.scalar(
        select(Feature.feature_index)
        .where(in_file, Feature.deviation_pct.is_not(None))
        .order_by(func.abs(Feature.deviation_pct).desc(), Feature.feature_index)
        .limit(1)
    )

    problems = db.execute(
        select(Feature.status, Feature.message, func.count(), func.min(Feature.feature_index))
        .where(in_file, Feature.status.in_([MeasurementStatus.FAILED.value, MeasurementStatus.UNSUPPORTED.value]))
        .group_by(Feature.status, Feature.message)
        .order_by(func.count().desc(), func.min(Feature.feature_index))
        .limit(50)
    ).all()

    return FileReport(
        file_id=file_id,
        filename=record.filename,
        status=record.status,
        crs=record.crs,
        error=record.error,
        warnings=record.warnings or [],
        processing_ms=record.processing_ms,
        summary=_summarise(db, file_id),
        projections=[
            ProjectionUse(projected_crs=crs, strategy=strategy, features=n) for crs, strategy, n in projections
        ],
        cross_check=CrossCheckReport(
            features_checked=checked,
            max_abs_deviation_pct=max_deviation,
            max_abs_unexplained_pct=None if max_unexplained is None else round(max_unexplained, 4),
            largest_deviation_index=largest,
        ),
        processing=ProcessingReport(
            repaired=repaired,
            densified=densified,
            crosses_antimeridian=antimeridian,
            with_elevation=with_elevation,
        ),
        problems=[
            ProblemGroup(status=status, message=message or "", features=n, first_index=first)
            for status, message, n, first in problems
        ],
    )


@router.delete("/{file_id}/", status_code=204, summary="Delete a file and its features")
def delete_file(file_id: str, db: Session = Depends(get_db)) -> Response:
    record = _get_file_or_404(db, file_id)
    db.execute(delete(Feature).where(Feature.file_id == file_id))
    db.delete(record)
    db.commit()
    return Response(status_code=204)


def _read_limited(file: UploadFile) -> bytes:
    """Read the upload in chunks and stop as soon as it exceeds the limit,
    instead of loading an arbitrarily large body and checking afterwards."""
    chunks, size = [], 0
    while chunk := file.file.read(CHUNK):
        size += len(chunk)
        if size > settings.max_upload_bytes:
            raise ApiError(
                413,
                "file_too_large",
                f"The file exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB upload limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _get_file_or_404(db: Session, file_id: str) -> GeoFile:
    record = db.get(GeoFile, file_id)
    if record is None:
        raise ApiError(404, "file_not_found", f"No file with id '{file_id}'.")
    return record


def _summarise(db: Session, file_id: str) -> MeasurementSummary:
    """Totals for the whole file, computed in SQL so they do not depend on paging."""
    rows = db.execute(
        select(Feature.status, func.count(), func.sum(Feature.area_m2), func.sum(Feature.length_m))
        .where(Feature.file_id == file_id)
        .group_by(Feature.status)
    ).all()

    summary = MeasurementSummary()
    for row_status, count, area, length in rows:
        setattr(summary, row_status.lower(), count)
        summary.total_area_m2 += area or 0.0
        summary.total_length_m += length or 0.0
    summary.total_area_m2 = round(summary.total_area_m2, 3)
    summary.total_length_m = round(summary.total_length_m, 3)
    summary.total_area_hectares = round(summary.total_area_m2 / 10_000, 6)
    summary.total_length_km = round(summary.total_length_m / 1_000, 6)
    return summary
