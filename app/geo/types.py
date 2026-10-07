"""Plain data types shared by the readers and the measurement engine.

Nothing in ``app.geo`` imports FastAPI or SQLAlchemy. The geospatial logic can
be used and tested on its own, and the web layer depends on it, never the
other way round.
"""

from dataclasses import dataclass, field
from enum import Enum

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


class GeoFileError(Exception):
    """The upload cannot be read as the format it claims to be.

    Raised for problems with the file as a whole (corrupt zip, broken XML,
    no .shp inside the archive). Problems with a single feature never raise:
    they are recorded on that feature so the rest of the file still processes.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MeasurementStatus(str, Enum):
    MEASURED = "MEASURED"              # area or length was calculated
    NOT_APPLICABLE = "NOT_APPLICABLE"  # nothing to measure (points, no geometry)
    UNSUPPORTED = "UNSUPPORTED"        # geometry type we do not measure
    FAILED = "FAILED"                  # should be measurable, but could not be


@dataclass
class RawFeature:
    """One feature as read from the file, before any measuring."""

    index: int
    geometry: BaseGeometry | None = None
    geometry_type: str | None = None
    source_id: str | None = None
    properties: dict = field(default_factory=dict)
    error: str | None = None  # set when this feature's geometry could not be read


@dataclass
class ParsedFile:
    file_format: str  # "kml" or "shapefile"
    features: list[RawFeature]
    crs: CRS | None = None  # None when the file does not declare one
    warnings: list[str] = field(default_factory=list)


@dataclass
class Measurement:
    status: MeasurementStatus
    message: str | None = None
    area_m2: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None
    projection_strategy: str | None = None
    projection_reason: str | None = None       # one sentence: why this projection
    geodesic_area_m2: float | None = None
    geodesic_length_m: float | None = None
    deviation_pct: float | None = None           # observed: projected vs geodesic
    expected_deviation_pct: float | None = None  # predicted from the projection's scale factor
    # What was done to the geometry before it was measured
    repaired: bool | None = None
    vertices_added: int | None = None
    crosses_antimeridian: bool | None = None
    # From the Z values, when the file has real heights (None otherwise)
    surface_length_m: float | None = None  # 3D length of a line, or 3D perimeter of a polygon
    elevation_min_m: float | None = None
    elevation_max_m: float | None = None
    warnings: list[str] = field(default_factory=list)
