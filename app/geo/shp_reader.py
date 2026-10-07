"""Zipped Shapefile reader.

A "Shapefile" is really a set of sibling files that share one base name:

    .shp  geometry                      (required)
    .shx  index into the .shp           (optional here; pyshp can scan without it)
    .dbf  attribute table, one row per shape
    .prj  the CRS, as WKT text
    .cpg  the text encoding of the .dbf

The archive is read entirely in memory. Nothing is extracted to disk, so a
crafted entry name such as ``../../etc/passwd`` ("zip slip") has no effect.
"""

import codecs
import datetime as dt
import io
import struct
import zipfile
from decimal import Decimal
from pathlib import PurePosixPath

import shapefile  # pyshp
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import shape as to_shapely

from app.config import settings
from app.geo.kml_reader import read_kml
from app.geo.types import GeoFileError, ParsedFile, RawFeature


def read_zip(data: bytes) -> ParsedFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, NotImplementedError, ValueError, OSError) as exc:
        raise GeoFileError("invalid_zip", "The file is not a valid zip archive.") from exc

    with archive:
        members = [m for m in archive.infolist() if not m.is_dir() and not _is_junk(m.filename)]

        # Zip-bomb guard: check the declared uncompressed size before reading anything.
        if sum(m.file_size for m in members) > settings.max_uncompressed_bytes:
            raise GeoFileError("archive_too_large", "The archive expands beyond the allowed size.")

        shp_members = [m for m in members if _suffix(m.filename) == ".shp"]
        if not shp_members:
            kml_members = [m for m in members if _suffix(m.filename) == ".kml"]
            if len(kml_members) == 1:  # a .kmz, or a KML someone zipped
                return read_kml(_read(archive, kml_members[0]))
            raise GeoFileError("missing_shp", "The zip does not contain a .shp file.")
        if len(shp_members) > 1:
            names = ", ".join(sorted(m.filename for m in shp_members))
            raise GeoFileError("multiple_layers", f"The zip contains several Shapefiles ({names}). Upload one per zip.")

        # Sibling files share the .shp's path and base name; match case-insensitively.
        stem = shp_members[0].filename[:-4].lower()
        siblings = {_suffix(m.filename): m for m in members if m.filename[:-4].lower() == stem}

        def part(ext: str) -> bytes | None:
            return _read(archive, siblings[ext]) if ext in siblings else None

        shp, shx, dbf, prj, cpg = part(".shp"), part(".shx"), part(".dbf"), part(".prj"), part(".cpg")

    warnings: list[str] = []
    if dbf is None:
        warnings.append("The zip has no .dbf file, so features have no attributes.")

    features = _read_features(shp, shx, dbf, _encoding(cpg))
    crs = _read_prj(prj, warnings)
    return ParsedFile(file_format="shapefile", features=features, crs=crs, warnings=warnings)


def _read_features(shp: bytes, shx: bytes | None, dbf: bytes | None, encoding: str) -> list[RawFeature]:
    try:
        # pyshp treats a part as present if the keyword is passed at all, so
        # only pass the parts the archive really contains.
        parts = {"shp": shp, "shx": shx, "dbf": dbf}
        streams = {name: io.BytesIO(content) for name, content in parts.items() if content}
        reader = shapefile.Reader(**streams, encoding=encoding, encodingErrors="replace")
        # Geometry decides how many features there are. If the .dbf is missing
        # or has fewer rows than the .shp has shapes, the remaining features
        # are kept without attributes instead of being dropped.
        records = reader.iterRecords() if dbf else iter(())
        return [_to_feature(index, shape, next(records, None)) for index, shape in enumerate(reader.iterShapes())]
    except (shapefile.ShapefileException, struct.error, EOFError, UnicodeError, IndexError) as exc:
        raise GeoFileError("invalid_shapefile", f"The Shapefile could not be read: {exc}") from exc


def _to_feature(index: int, shape, record) -> RawFeature:
    feature = RawFeature(index=index, properties=_json_safe(record.as_dict()) if record is not None else {})

    if shape.shapeType == shapefile.NULL:
        return feature  # a record with no geometry is legal
    if shape.shapeType == shapefile.MULTIPATCH:
        feature.geometry_type = "MultiPatch"  # 3D surface type; nothing to build
        return feature

    try:
        feature.geometry = to_shapely(_with_heights(shape))
        feature.geometry_type = feature.geometry.geom_type
    except Exception as exc:  # noqa: BLE001 - a bad shape breaks this feature only
        # A corrupt record can carry a shape-type number that does not exist.
        type_name = shapefile.SHAPETYPE_LOOKUP.get(shape.shapeType, f"type {shape.shapeType}")
        feature.geometry_type = type_name.title()
        feature.error = f"Could not read {type_name} geometry: {exc}"
    return feature


def _with_heights(shape) -> dict:
    """The shape as GeoJSON, with its Z values when it is a Z type (PolygonZ, PolyLineZ, ...).

    pyshp's ``__geo_interface__`` is always 2D; the heights sit in a separate
    ``shape.z`` list that runs parallel to ``shape.points``. Each point gets
    its height back by matching coordinates, which survives pyshp regrouping
    rings into polygons.
    """
    geo = shape.__geo_interface__
    heights = getattr(shape, "z", None)
    if not heights or len(heights) != len(shape.points):
        return geo
    height_at = {tuple(point[:2]): z for point, z in zip(shape.points, heights, strict=True)}

    def add_z(coords):
        if coords and isinstance(coords[0], (int, float)):
            return (coords[0], coords[1], height_at[(coords[0], coords[1])])
        return [add_z(c) for c in coords]

    try:
        return {**geo, "coordinates": add_z(geo["coordinates"])}
    except KeyError:  # a point pyshp rewrote; measure in 2D rather than fail the feature
        return geo


def _read_prj(prj: bytes | None, warnings: list[str]) -> CRS | None:
    if prj is None:
        return None  # resolve_source_crs() adds the right warning
    try:
        # Usually WKT, but some tools write "EPSG:32643" or a PROJ string instead.
        return CRS.from_user_input(prj.decode("utf-8", errors="replace").strip())
    except CRSError:
        warnings.append("The .prj file could not be parsed, so it was ignored.")
        return None


def _encoding(cpg: bytes | None) -> str:
    if cpg:
        name = cpg.decode("ascii", errors="ignore").strip()
        try:
            return codecs.lookup(name).name
        except (LookupError, ValueError):  # unknown or garbled encoding name
            pass
    return "utf-8"


def _json_safe(record: dict) -> dict:
    """DBF fields can hold dates, decimals and raw bytes, which JSON cannot store."""

    def convert(value):
        if isinstance(value, (dt.date, dt.datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    return {key: convert(value) for key, value in record.items()}


def _read(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    try:
        return archive.read(member)
    except (RuntimeError, ValueError, OSError, EOFError, zipfile.BadZipFile, NotImplementedError) as exc:
        # encrypted, corrupt, or compressed with a method Python cannot read
        raise GeoFileError("invalid_zip", f"Could not read '{member.filename}' from the zip: {exc}") from exc


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _is_junk(name: str) -> bool:
    """macOS adds '__MACOSX/' folders and '._name' resource forks to zips."""
    path = PurePosixPath(name)
    return "__MACOSX" in path.parts or path.name.startswith(".")
