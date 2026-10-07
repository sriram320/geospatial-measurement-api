"""Builders for test input. Every test file is generated in memory, so the
tests need no fixture files on disk and each test shows exactly what it feeds in."""

import io
import zipfile

import shapefile  # pyshp
from pyproj import CRS, Transformer
from shapely.geometry import box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

KML_NS = "http://www.opengis.net/kml/2.2"
UTM_43N = CRS.from_epsg(32643)
UTM_43N_PRJ = UTM_43N.to_wkt("WKT1_ESRI")
WGS84_PRJ = CRS.from_epsg(4326).to_wkt("WKT1_ESRI")


# ---------------------------------------------------------------- KML

def kml(*placemarks: str, namespace: str | None = KML_NS) -> bytes:
    xmlns = f' xmlns="{namespace}"' if namespace else ""
    body = "".join(placemarks)
    return f'<?xml version="1.0" encoding="UTF-8"?><kml{xmlns}><Document>{body}</Document></kml>'.encode()


def placemark(name: str, geometry: str = "", extra: str = "", pm_id: str | None = None) -> str:
    id_attr = f' id="{pm_id}"' if pm_id else ""
    return f"<Placemark{id_attr}><name>{name}</name>{extra}{geometry}</Placemark>"


def _coords(points) -> str:
    return " ".join(",".join(str(v) for v in point) for point in points)


def point_xml(lon: float, lat: float) -> str:
    return f"<Point><coordinates>{lon},{lat}</coordinates></Point>"


def line_xml(points) -> str:
    return f"<LineString><coordinates>{_coords(points)}</coordinates></LineString>"


def polygon_xml(shell, holes=()) -> str:
    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{_coords(h)}</coordinates></LinearRing></innerBoundaryIs>"
        for h in holes
    )
    return (
        "<Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{_coords(shell)}</coordinates>"
        f"</LinearRing></outerBoundaryIs>{inner}</Polygon>"
    )


# A small square near Bengaluru, roughly 110 m on a side.
SQUARE = [(77.59, 12.97), (77.591, 12.97), (77.591, 12.971), (77.59, 12.971), (77.59, 12.97)]
LINE = [(77.59, 12.97), (77.60, 12.97), (77.60, 12.98)]


# ---------------------------------------------------------------- Shapefile

def shapefile_zip(
    geometries: list[BaseGeometry],
    records: list[dict] | None = None,
    prj: str | None = None,
    folder: str = "",
    base: str = "layer",
    include: tuple[str, ...] = (".shp", ".shx", ".dbf"),
    extra_files: dict[str, bytes] | None = None,
    upper_case: bool = False,
) -> bytes:
    """Build a zipped Shapefile from shapely geometries."""
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf)
    writer.field("NAME", "C", 40)
    writer.field("VALUE", "N", 12, 2)
    writer.field("SURVEYED", "D")
    for i, geometry in enumerate(geometries):
        record = (records[i] if records else None) or {}
        if geometry is None:
            writer.null()
        else:
            writer.shape(mapping(geometry))
        writer.record(record.get("NAME", f"feature-{i}"), record.get("VALUE", i), record.get("SURVEYED"))
    writer.close()

    parts = {".shp": shp.getvalue(), ".shx": shx.getvalue(), ".dbf": dbf.getvalue()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for ext in include:
            name = f"{folder}{base}{ext}"
            archive.writestr(name.upper() if upper_case else name, parts[ext])
        if prj:
            name = f"{folder}{base}.prj"
            archive.writestr(name.upper() if upper_case else name, prj)
        for name, content in (extra_files or {}).items():
            archive.writestr(name, content)
    return out.getvalue()


def zip_of(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


# ---------------------------------------------------------------- geometry

def utm_box(easting: float, northing: float, width: float, height: float) -> BaseGeometry:
    """A rectangle whose sides are exact metres on the UTM 43N grid."""
    return box(easting, northing, easting + width, northing + height)


def to_lonlat(geometry: BaseGeometry, source: CRS = UTM_43N) -> BaseGeometry:
    transformer = Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True)
    return transform(transformer.transform, geometry)
