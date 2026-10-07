"""Response shapes (Pydantic). These define the public JSON contract and keep
it separate from the database tables, so either can change without the other."""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import Feature


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_format: str | None
    size_bytes: int
    feature_count: int
    crs: str | None
    status: str
    warnings: list[str]
    error: str | None
    processing_ms: int | None
    created_at: dt.datetime

    @field_validator("created_at")
    @classmethod
    def _as_utc(cls, value: dt.datetime) -> dt.datetime:
        # SQLite stores datetimes without a timezone. They are always written
        # in UTC, so label them as UTC on the way out.
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


class UploadOut(FileOut):
    duplicate: bool = Field(False, description="True when an identical file had already been processed.")


class FileList(BaseModel):
    total: int
    limit: int
    offset: int
    results: list[FileOut]


class FeatureOut(BaseModel):
    index: int
    source_id: str | None
    geometry_type: str | None
    crs: str | None
    properties: dict
    geometry: dict | None = Field(description="GeoJSON geometry, in the file's own CRS.")

    @classmethod
    def from_row(cls, row: Feature) -> "FeatureOut":
        return cls(
            index=row.feature_index,
            source_id=row.source_id,
            geometry_type=row.geometry_type,
            crs=row.crs,
            properties=row.properties,
            geometry=row.geometry,
        )


class FeatureList(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    results: list[FeatureOut]


class MethodOut(BaseModel):
    source_crs: str | None
    projected_crs: str = Field(description="The metre-based CRS the feature was measured in.")
    strategy: str = Field(
        description="utm_zone, local_equal_area, local_equidistant, or source_grid for a local site/CAD grid "
        "measured directly."
    )
    reason: str | None = Field(description="Why this projection was chosen for this feature.")


class CrossCheckOut(BaseModel):
    method: str = "geodesic (WGS84 ellipsoid)"
    area_m2: float | None
    length_m: float | None
    deviation_pct: float | None = Field(description="Observed gap: (projected - geodesic) / geodesic * 100.")
    expected_pct: float | None = Field(
        description="Predicted gap: what the projection's scale factor at the feature's centre says the "
        "deviation should be. Null for the equidistant fallback, whose scale depends on direction."
    )
    unexplained_pct: float | None = Field(
        description="deviation_pct - expected_pct. Near zero means the whole gap is the projection's known "
        "distortion. It grows for features large enough that the scale varies across them."
    )


class ProcessingOut(BaseModel):
    repaired: bool = Field(description="The polygon was invalid and was repaired before measuring.")
    vertices_added: int = Field(description="Vertices added along edges longer than 0.1 degrees before projecting.")
    crosses_antimeridian: bool = Field(
        description="The feature spans the 180th meridian and its longitudes were unwrapped before measuring."
    )


class ElevationOut(BaseModel):
    surface_length_m: float | None = Field(
        description="Lines: length along the ground, including climbs and descents between vertices."
    )
    surface_perimeter_m: float | None = Field(description="Polygons: the same, around the boundary.")
    min_m: float
    max_m: float


class MeasurementOut(BaseModel):
    index: int
    source_id: str | None
    geometry_type: str | None
    properties: dict = Field(description="The feature's attributes, so each result can be identified.")
    status: str
    message: str | None
    area_m2: float | None
    area_hectares: float | None
    perimeter_m: float | None
    length_m: float | None
    length_km: float | None
    method: MethodOut | None
    cross_check: CrossCheckOut | None
    processing: ProcessingOut | None = Field(description="What was done to the geometry before it was measured.")
    elevation: ElevationOut | None = Field(
        description="From the file's Z values. Null unless the file has real heights (a Z-type Shapefile, or KML "
        "with altitudeMode 'absolute')."
    )
    warnings: list[str]

    @classmethod
    def from_row(cls, row: Feature) -> "MeasurementOut":
        measured = row.projected_crs is not None
        unexplained = None
        if row.deviation_pct is not None and row.expected_deviation_pct is not None:
            unexplained = round(row.deviation_pct - row.expected_deviation_pct, 4)
        elevation = None
        if row.surface_length_m is not None:
            is_area = row.area_m2 is not None
            elevation = ElevationOut(
                surface_length_m=None if is_area else row.surface_length_m,
                surface_perimeter_m=row.surface_length_m if is_area else None,
                min_m=row.elevation_min_m,
                max_m=row.elevation_max_m,
            )
        return cls(
            index=row.feature_index,
            source_id=row.source_id,
            geometry_type=row.geometry_type,
            properties=row.properties,
            status=row.status,
            message=row.message,
            area_m2=row.area_m2,
            area_hectares=_scaled(row.area_m2, 10_000),
            perimeter_m=row.perimeter_m,
            length_m=row.length_m,
            length_km=_scaled(row.length_m, 1_000),
            method=MethodOut(
                source_crs=row.crs,
                projected_crs=row.projected_crs,
                strategy=row.projection_strategy,
                reason=row.projection_reason,
            ) if measured else None,
            cross_check=CrossCheckOut(
                area_m2=row.geodesic_area_m2,
                length_m=row.geodesic_length_m,
                deviation_pct=row.deviation_pct,
                expected_pct=row.expected_deviation_pct,
                unexplained_pct=unexplained,
            ) if measured else None,
            processing=ProcessingOut(
                repaired=bool(row.repaired),
                vertices_added=row.vertices_added or 0,
                crosses_antimeridian=bool(row.crosses_antimeridian),
            ) if measured else None,
            elevation=elevation,
            warnings=row.warnings or [],
        )


class MeasurementSummary(BaseModel):
    measured: int = 0
    not_applicable: int = 0
    unsupported: int = 0
    failed: int = 0
    total_area_m2: float = 0.0
    total_area_hectares: float = 0.0
    total_length_m: float = 0.0
    total_length_km: float = 0.0


class MeasurementList(BaseModel):
    file_id: str
    filename: str
    crs: str | None
    summary: MeasurementSummary = Field(description="Totals across the whole file, not just this page.")
    total: int
    limit: int
    offset: int
    results: list[MeasurementOut]


class ProjectionUse(BaseModel):
    projected_crs: str
    strategy: str
    features: int


class CrossCheckReport(BaseModel):
    features_checked: int = Field(description="Measured features that have a geodesic value to compare with.")
    max_abs_deviation_pct: float | None = Field(description="Largest observed projected-vs-geodesic gap.")
    max_abs_unexplained_pct: float | None = Field(
        description="Largest part of any gap that the projection's scale factor does not account for."
    )
    largest_deviation_index: int | None = Field(description="Index of the feature with the largest observed gap.")


class ProcessingReport(BaseModel):
    repaired: int = Field(description="Invalid polygons that were repaired before measuring.")
    densified: int = Field(description="Features that had vertices added along long edges.")
    crosses_antimeridian: int = Field(description="Features that span the 180th meridian and were unwrapped.")
    with_elevation: int = Field(description="Features whose heights gave a surface length.")


class ProblemGroup(BaseModel):
    status: str
    message: str
    features: int = Field(description="How many features share this status and message.")
    first_index: int = Field(description="The first feature affected, to look it up.")


class FileReport(BaseModel):
    file_id: str
    filename: str
    status: str
    crs: str | None
    error: str | None
    warnings: list[str]
    processing_ms: int | None
    summary: MeasurementSummary
    projections: list[ProjectionUse] = Field(description="Which projected CRSs were used, and for how many features.")
    cross_check: CrossCheckReport
    processing: ProcessingReport
    problems: list[ProblemGroup] = Field(
        description="FAILED and UNSUPPORTED features grouped by reason, most common first (at most 50 groups)."
    )


def _scaled(value: float | None, divisor: float) -> float | None:
    return None if value is None else round(value / divisor, 6)
