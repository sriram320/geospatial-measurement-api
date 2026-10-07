"""Database tables.

``files``     one row per upload.
``features``  one row per feature, holding both what was read from the file
              and what was measured. A feature has exactly one measurement, so
              a separate measurements table would only add a join.
"""

import datetime as dt
import uuid
from enum import Enum

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FileStatus(str, Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class GeoFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    filename: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str | None] = mapped_column(String(16))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64), index=True)  # for duplicate detection
    status: Mapped[str] = mapped_column(String(16), default=FileStatus.PROCESSING.value)
    crs: Mapped[str | None] = mapped_column(String(255))
    feature_count: Mapped[int] = mapped_column(default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    processing_ms: Mapped[int | None]
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("file_id", "feature_index"),
        Index("ix_features_file_status", "file_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)

    # What the file said
    feature_index: Mapped[int]                               # position in the file, from 0
    source_id: Mapped[str | None] = mapped_column(String(255))  # the file's own id, if any
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    geometry: Mapped[dict | None] = mapped_column(JSON)      # GeoJSON, in the file's CRS
    crs: Mapped[str | None] = mapped_column(String(255))
    properties: Mapped[dict] = mapped_column(JSON, default=dict)

    # What we measured
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(Text)
    area_m2: Mapped[float | None]
    perimeter_m: Mapped[float | None]
    length_m: Mapped[float | None]
    projected_crs: Mapped[str | None] = mapped_column(String(255))
    projection_strategy: Mapped[str | None] = mapped_column(String(32))
    projection_reason: Mapped[str | None] = mapped_column(Text)
    geodesic_area_m2: Mapped[float | None]
    geodesic_length_m: Mapped[float | None]
    deviation_pct: Mapped[float | None]           # observed: projected vs geodesic
    expected_deviation_pct: Mapped[float | None]  # predicted from the projection's scale factor
    warnings: Mapped[list] = mapped_column(JSON, default=list)

    # What was done to the geometry before measuring (null when it was not measured)
    repaired: Mapped[bool | None]
    vertices_added: Mapped[int | None]
    crosses_antimeridian: Mapped[bool | None]

    # From Z values, when the file has real heights
    surface_length_m: Mapped[float | None]  # 3D length of a line, 3D perimeter of a polygon
    elevation_min_m: Mapped[float | None]
    elevation_max_m: Mapped[float | None]
