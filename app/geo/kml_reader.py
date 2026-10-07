"""KML reader.

KML is XML. Each ``<Placemark>`` is one feature and may sit at any depth
inside ``<Document>`` and ``<Folder>`` elements. By specification KML
coordinates are always WGS84, written as ``longitude,latitude[,altitude]``.

Two deliberate choices:

* ``defusedxml`` parses the document. The standard-library parser is open to
  entity-expansion ("billion laughs") and external-entity attacks, which
  matters for a service that accepts files from strangers.
* Elements are matched by local name with the namespace stripped, because
  real files use several KML namespaces (OGC 2.2, the older Google Earth
  ones) or none at all.
"""

import math
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from shapely import force_2d
from shapely.errors import ShapelyError
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from app.geo.crs import WGS84
from app.geo.types import GeoFileError, ParsedFile, RawFeature

GEOMETRY_TAGS = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"}
# Real KML geometry elements that have no simple vector equivalent.
UNSUPPORTED_GEOMETRY_TAGS = {"Model", "Track", "MultiTrack"}
ROOT_TAGS = {"kml", "Document", "Folder", "Placemark"}


def read_kml(data: bytes) -> ParsedFile:
    try:
        root = SafeElementTree.fromstring(data)
    except DefusedXmlException as exc:
        raise GeoFileError("unsafe_xml", "The KML uses XML entities or a DTD, which are rejected for safety.") from exc
    except ParseError as exc:
        raise GeoFileError("invalid_kml", f"The file is not well-formed XML: {exc}") from exc

    if _local(root.tag) not in ROOT_TAGS:
        raise GeoFileError("invalid_kml", f"Root element is <{_local(root.tag)}>, expected <kml>.")

    placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
    features = [_read_placemark(index, el) for index, el in enumerate(placemarks)]
    warnings = [] if features else ["The KML contains no Placemark elements."]
    return ParsedFile(file_format="kml", features=features, crs=WGS84, warnings=warnings)


def _read_placemark(index: int, placemark: Element) -> RawFeature:
    feature = RawFeature(index=index, source_id=placemark.get("id"), properties=_read_properties(placemark))

    geometry_el = next((c for c in placemark if _local(c.tag) in GEOMETRY_TAGS | UNSUPPORTED_GEOMETRY_TAGS), None)
    if geometry_el is None:
        return feature  # a Placemark with no geometry is legal KML

    tag = _local(geometry_el.tag)
    if tag in UNSUPPORTED_GEOMETRY_TAGS:
        feature.geometry_type = tag
        return feature

    try:
        feature.geometry = _read_geometry(geometry_el)
        feature.geometry_type = feature.geometry.geom_type
    except (ValueError, TypeError, ShapelyError) as exc:
        # A bad coordinate string breaks this feature only, not the file.
        feature.geometry_type = "MultiGeometry" if tag == "MultiGeometry" else tag
        feature.error = f"Could not read <{tag}> geometry: {exc}"
    return feature


def _read_properties(placemark: Element) -> dict:
    properties: dict = {}
    for key in ("name", "description"):
        node = _child(placemark, key)
        if node is not None and node.text and node.text.strip():
            properties[key] = node.text.strip()

    extended = _child(placemark, "ExtendedData")
    if extended is not None:
        for el in extended.iter():
            tag = _local(el.tag)
            if tag == "Data" and el.get("name"):          # <Data name="k"><value>v</value></Data>
                value = _child(el, "value")
                properties[el.get("name")] = (value.text or "").strip() if value is not None else None
            elif tag == "SimpleData" and el.get("name"):  # <SimpleData name="k">v</SimpleData>
                properties[el.get("name")] = (el.text or "").strip()
    return properties


def _read_geometry(el: Element) -> BaseGeometry:
    tag = _local(el.tag)
    keep_z = _altitude_mode(el) == "absolute"
    if tag == "Point":
        return Point(_coordinates(el, keep_z)[0])
    if tag == "LineString":
        return LineString(_coordinates(el, keep_z))
    if tag == "LinearRing":
        return LinearRing(_coordinates(el, keep_z))
    if tag == "Polygon":
        outer = _child(el, "outerBoundaryIs")
        if outer is None:
            raise ValueError("Polygon has no <outerBoundaryIs>")
        shell = _ring_coordinates(outer, keep_z)
        holes = [
            _coordinates(ring, keep_z)
            for boundary in _children(el, "innerBoundaryIs")
            for ring in _children(boundary, "LinearRing")
        ]
        if len({len(point) for ring in [shell, *holes] for point in ring}) > 1:  # some rings lack heights
            shell, holes = _flat(shell), [_flat(hole) for hole in holes]
        return Polygon(shell, holes)
    if tag == "MultiGeometry":
        return _combine([_read_geometry(c) for c in el if _local(c.tag) in GEOMETRY_TAGS])
    raise ValueError(f"unexpected element <{tag}>")


def _altitude_mode(el: Element) -> str:
    """KML's default, clampToGround, means altitudes are ignored and the shape lies on the terrain.

    Only ``absolute`` makes the altitude a height above sea level. ``relativeToGround``
    is a height above the terrain, so it says nothing about how the ground rises or falls.
    """
    node = _child(el, "altitudeMode")
    return (node.text or "").strip() if node is not None else "clampToGround"


def _ring_coordinates(boundary: Element, keep_z: bool) -> list[tuple[float, ...]]:
    ring = _child(boundary, "LinearRing")
    if ring is None:
        raise ValueError("boundary has no <LinearRing>")
    return _coordinates(ring, keep_z)


def _coordinates(el: Element, keep_z: bool = False) -> list[tuple[float, ...]]:
    """Parse ``lon,lat[,alt] lon,lat[,alt] ...`` into (lon, lat) or (lon, lat, alt) tuples.

    Altitude is kept only when ``keep_z`` is set and every point has a finite
    one; otherwise it is dropped for the whole element.
    """
    node = _child(el, "coordinates")
    text = (node.text or "").strip() if node is not None else ""
    if not text:
        raise ValueError(f"<{_local(el.tag)}> has no coordinates")

    points = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError(f"malformed coordinate '{token}' (expected lon,lat)")
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            raise ValueError(
                f"malformed coordinate '{token}' (expected lon,lat[,alt] with no spaces around the commas)"
            ) from None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(f"non-numeric coordinate '{token}'")
        points.append((lon, lat, _altitude(parts)))

    if keep_z and all(point[2] is not None for point in points):
        return points
    return _flat(points)


def _altitude(parts: list[str]) -> float | None:
    if len(parts) < 3:
        return None
    try:
        value = float(parts[2])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _flat(points: list[tuple[float, ...]]) -> list[tuple[float, float]]:
    return [(point[0], point[1]) for point in points]


def _combine(parts: list[BaseGeometry]) -> BaseGeometry:
    """Turn the members of a <MultiGeometry> into the tightest matching type."""
    if len({part.has_z for part in parts}) > 1:  # mixed: keep heights only if every member has them
        parts = [force_2d(part) for part in parts]
    kinds = {part.geom_type for part in parts}
    if kinds == {"Point"}:
        return MultiPoint(parts)
    if kinds == {"LineString"}:
        return MultiLineString(parts)
    if kinds == {"Polygon"}:
        return MultiPolygon(parts)
    return GeometryCollection(parts)  # mixed types


def _local(tag) -> str:
    """'{http://www.opengis.net/kml/2.2}Placemark' -> 'Placemark'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el: Element, name: str) -> list[Element]:
    return [child for child in el if _local(child.tag) == name]


def _child(el: Element, name: str) -> Element | None:
    return next(iter(_children(el, name)), None)
