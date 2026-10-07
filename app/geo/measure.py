"""The measurement engine.

``measure_feature`` is the only public function. It never raises: every
outcome, including failure, comes back as a ``Measurement`` so that one bad
feature cannot take down the processing of a whole file.

Each measurement is calculated twice, by two independent methods:

* projected  - reproject to a metre-based CRS, then plain planar geometry.
               This is the primary value the API reports.
* geodesic   - computed directly on the WGS84 ellipsoid, no projection.
               This is the reference used to cross-check the first.

The difference between them is returned as ``deviation_pct``, so the client
can see how much the chosen projection distorted this particular feature.

That difference is then checked against a third, independent number: the
distortion the projection is *predicted* to cause at the feature's location
(``expected_deviation_pct``, from the projection's scale factor). When the
observed and predicted values match, the gap between the two methods is fully
accounted for by the map projection and is not an error in either of them.
"""

import logging
import math

import numpy as np
import shapely
from pyproj import CRS, Geod
from shapely import count_coordinates, force_2d, make_valid, segmentize
from shapely.geometry import LinearRing, LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from app.geo.crs import (
    WGS84,
    crs_label,
    expected_distortion_pct,
    is_local_grid,
    linear_unit_factor,
    reproject,
    select_projection,
    transformer,
    vertical_unit_factor,
)
from app.geo.types import Measurement, RawFeature
from app.geo.types import MeasurementStatus as Status

logger = logging.getLogger(__name__)

AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString", "LinearRing"}
POINT_TYPES = {"Point", "MultiPoint"}

GEOD = Geod(ellps="WGS84")
DEVIATION_WARNING_PCT = 0.5
MAX_SEGMENT_DEGREES = 0.1  # about 11 km


class _Unmeasurable(Exception):
    """This feature cannot be measured, for a reason the client should be told as-is."""


def measure_feature(feature: RawFeature, source_crs: CRS | None) -> Measurement:
    """Measure one feature. ``source_crs`` is the CRS its coordinates are in."""
    if feature.error:
        return Measurement(Status.FAILED, feature.error)

    geometry = feature.geometry
    if geometry is None:
        if feature.geometry_type:  # the reader recognised a type it cannot build
            return _unsupported(feature.geometry_type)
        return Measurement(Status.NOT_APPLICABLE, "Feature has no geometry.")
    if geometry.is_empty:
        return Measurement(Status.NOT_APPLICABLE, "Geometry is empty.")

    kind = geometry.geom_type
    if kind in POINT_TYPES:
        return Measurement(Status.NOT_APPLICABLE, "Points have no area or length.")
    if kind not in AREA_TYPES | LENGTH_TYPES:
        return _unsupported(kind)
    if source_crs is None:
        return Measurement(Status.FAILED, "Source CRS is unknown, so coordinates cannot be converted to metres.")

    is_area = kind in AREA_TYPES
    try:
        if is_local_grid(source_crs):
            return _measure_on_local_grid(geometry, source_crs, is_area)
        return _measure(geometry, source_crs, is_area)
    except _Unmeasurable as exc:
        return Measurement(Status.FAILED, str(exc))
    except Exception as exc:  # noqa: BLE001 - isolate any failure to this one feature
        logger.exception("Measurement failed for feature %s", feature.index)
        return Measurement(Status.FAILED, f"Measurement failed: {exc}")


def _unsupported(kind: str) -> Measurement:
    return Measurement(Status.UNSUPPORTED, f"Geometry type '{kind}' is not supported for measurement.")


def _measure(geometry: BaseGeometry, source_crs: CRS, is_area: bool) -> Measurement:
    warnings: list[str] = []

    # Step 1: bring everything to WGS84 lon/lat. Area and length are
    # planimetric, so Z values are dropped here (heights are used separately,
    # for the surface length, at the end).
    lonlat = force_2d(geometry)
    if source_crs is not WGS84:  # KML is already there
        lonlat = reproject(lonlat, transformer(source_crs, WGS84))
    if not _within_lonlat_range(lonlat):
        return Measurement(Status.FAILED, _out_of_range_message(lonlat))

    # Step 2: make a feature that crosses the 180th meridian continuous.
    lonlat, crosses_antimeridian = _unwrap_antimeridian(lonlat)
    if crosses_antimeridian:
        warnings.append(
            "This feature spans the 180th meridian. Its longitudes were unwrapped (e.g. -179.9 read as 180.1) "
            "so it is measured as one continuous shape, not as one stretching the long way around the globe."
        )

    # Step 3: repair broken polygons (e.g. a self-intersecting "bow-tie") and say so.
    repaired = False
    if is_area:
        lonlat, repaired = _repair_if_invalid(lonlat, warnings)

    # Step 4: project to metres and measure with planar geometry.
    # Reprojection only moves vertices, so a very long edge would be drawn as a
    # straight line in the target CRS and drift from the edge it represents.
    # Adding vertices along long edges keeps both methods on the same path.
    # Survey-scale features have no edge this long, so they pass through unchanged.
    vertices_before = count_coordinates(lonlat)
    lonlat = segmentize(lonlat, MAX_SEGMENT_DEGREES)
    projection = select_projection(lonlat.bounds, for_area=is_area)
    projected = reproject(lonlat, projection.from_wgs84)

    result = Measurement(
        Status.MEASURED,
        projected_crs=projection.label,
        projection_strategy=projection.strategy,
        projection_reason=projection.reason,
        repaired=repaired,
        vertices_added=count_coordinates(lonlat) - vertices_before,
        crosses_antimeridian=crosses_antimeridian,
        warnings=warnings,
    )

    # Step 5: measure again on the ellipsoid and compare.
    if is_area:
        result.area_m2 = round(projected.area, 3)
        result.perimeter_m = round(projected.length, 3)
        result.geodesic_area_m2 = round(_geodesic_area(lonlat), 3)
        result.deviation_pct = _deviation_pct(result.area_m2, result.geodesic_area_m2)
    else:
        result.length_m = round(projected.length, 3)
        result.geodesic_length_m = round(GEOD.geometry_length(lonlat), 3)
        result.deviation_pct = _deviation_pct(result.length_m, result.geodesic_length_m)

    # Step 6: what does the projection itself predict for a feature at this spot?
    # Sampled at the feature's centre, which is exact for survey-scale features
    # and an approximation for one large enough that the scale varies across it.
    centre = lonlat.centroid
    expected = expected_distortion_pct(projection, centre.x, centre.y, for_area=is_area)
    result.expected_deviation_pct = None if expected is None else round(expected, 4)

    if result.deviation_pct is not None and abs(result.deviation_pct) > DEVIATION_WARNING_PCT:
        warnings.append(
            f"Projected value differs from the geodesic value by {result.deviation_pct}%. "
            "Prefer the geodesic figure for this feature."
        )

    # Step 7: if the file has real heights, the length along the ground.
    _add_elevation(result, geometry, source_crs)
    return result


def _measure_on_local_grid(geometry: BaseGeometry, crs: CRS, is_area: bool) -> Measurement:
    """Measure a feature drawn on a site or CAD grid that has no link to latitude/longitude.

    The coordinates are already a flat grid in a known unit, so planar
    geometry gives the answer directly once the unit is converted to metres.
    With no way to place the grid on the globe there is no geodesic
    cross-check, and the response says so instead of inventing one.
    """
    warnings: list[str] = []
    flat = force_2d(geometry)
    factor = linear_unit_factor(crs)
    if factor != 1.0:
        flat = shapely.transform(flat, lambda coords: coords * factor)

    repaired = False
    if is_area:
        flat, repaired = _repair_if_invalid(flat, warnings)

    result = Measurement(
        Status.MEASURED,
        projected_crs=crs_label(crs),
        projection_strategy="source_grid",
        projection_reason=(
            f"The file uses a local grid ('{crs.name}') that is not tied to latitude/longitude, so it was "
            "measured directly on that grid. A geodesic cross-check is not possible for such a grid."
        ),
        repaired=repaired,
        vertices_added=0,
        crosses_antimeridian=False,
        warnings=warnings,
    )
    if is_area:
        result.area_m2 = round(flat.area, 3)
        result.perimeter_m = round(flat.length, 3)
    else:
        result.length_m = round(flat.length, 3)
    _add_elevation(result, geometry, crs)
    return result


def _repair_if_invalid(geometry: BaseGeometry, warnings: list[str]) -> tuple[BaseGeometry, bool]:
    """Repair an invalid polygon and add a warning saying what was wrong. Returns (geometry, repaired)."""
    if geometry.is_valid:
        return geometry, False
    reason = explain_validity(geometry)
    fixed = _polygonal_part(make_valid(geometry))
    if fixed is None:
        raise _Unmeasurable(f"Invalid polygon could not be repaired ({reason}).")
    warnings.append(f"Invalid polygon was repaired before measuring ({reason}).")
    return fixed, True


def _within_lonlat_range(geometry: BaseGeometry) -> bool:
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    if not all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat)):
        return False
    return min_lon >= -180 and max_lon <= 180 and min_lat >= -90 and max_lat <= 90


def _out_of_range_message(geometry: BaseGeometry) -> str:
    """Say what is wrong, and say "swapped" outright when that can be proved.

    If the x values would all be legal latitudes and the y values legal
    longitudes, swapping the two would make every coordinate valid, which is
    what a file written latitude-first looks like.
    """
    message = "Coordinates fall outside valid longitude/latitude ranges. "
    min_x, min_y, max_x, max_y = geometry.bounds
    values_are_finite = all(math.isfinite(v) for v in (min_x, min_y, max_x, max_y))
    if values_are_finite and min_x >= -90 and max_x <= 90 and min_y >= -180 and max_y <= 180:
        return message + (
            "They would all be valid with the two values swapped, so this feature is almost certainly "
            "stored as latitude,longitude. The expected order is longitude,latitude."
        )
    return message + "Check the CRS, or the coordinate order (the expected order is longitude,latitude)."


# ---------------------------------------------------------------- antimeridian

def _unwrap_antimeridian(geometry: BaseGeometry) -> tuple[BaseGeometry, bool]:
    """Make a feature that crosses the 180th meridian continuous. Returns (geometry, crosses).

    Longitude jumps from +180 to -180 at the antimeridian. An edge from 179.9
    to -179.9 is 0.2 degrees long on the ground but 359.8 degrees apart as
    numbers, so the bounding box, the projection choice and the densifying
    step would all treat the feature as wrapping the long way round the
    globe. Both measuring methods would then agree on the same wrong shape,
    which the cross-check cannot catch.

    Following the GeoJSON convention that an edge takes the shorter way round,
    longitudes are unwrapped (179.9, -179.9 becomes 179.9, 180.1), and the
    parts of a multi-part feature are moved next to each other when that gives
    a narrower longitude extent (a feature split at 180 into two halves).
    The result is shifted so its centre is back in [-180, 180]; PROJ accepts
    the few longitudes left beyond 180.
    """
    min_lon, _, max_lon, _ = geometry.bounds
    if max_lon - min_lon <= 180:
        return geometry, False  # no edge or gap can be the short way across 180

    jumps = any(
        np.any(np.abs(np.diff(shapely.get_coordinates(path)[:, 0])) > 180) for path in _paths(geometry)
    )
    parts = list(getattr(geometry, "geoms", [geometry]))
    unwrapped = [_unwrap_part(part) for part in parts]
    reference = shapely.get_coordinates(unwrapped[0])[0, 0]
    unwrapped = [unwrapped[0]] + [_moved_near(part, reference) for part in unwrapped[1:]]
    result = type(geometry)(unwrapped) if hasattr(geometry, "geoms") else unwrapped[0]

    new_min, _, new_max, _ = result.bounds
    if not jumps and new_max - new_min >= max_lon - min_lon:
        return geometry, False  # e.g. parts far apart on opposite sides of the world: leave as drawn
    if isinstance(result, MultiPolygon) and geometry.is_valid and not result.is_valid:
        # Two halves stored either side of 180 now share an edge; join them
        # here so the repair step does not report a defect the file never had.
        result = _polygonal_part(shapely.union_all(result.geoms)) or result

    shift = 360 * round((new_min + new_max) / 2 / 360)
    if shift:
        result = shapely.transform(result, lambda coords: coords - np.array([shift, 0.0]))
    return result, True


def _unwrap_part(part: BaseGeometry) -> BaseGeometry:
    if isinstance(part, Polygon):
        shell = _unwrap_ring(part.exterior)
        holes = [_shift_coords(_unwrap_ring(hole), shell[0, 0]) for hole in part.interiors]
        return Polygon(shell, holes)
    coords = _unwrap_coords(shapely.get_coordinates(part))
    if isinstance(part, LinearRing):
        return LinearRing(_closed(coords))
    return LineString(coords)


def _unwrap_ring(ring: LinearRing) -> np.ndarray:
    return _closed(_unwrap_coords(shapely.get_coordinates(ring)))


def _unwrap_coords(coords: np.ndarray) -> np.ndarray:
    lons = coords[:, 0]
    unwrapped = np.unwrap(lons, period=360)  # each step becomes the shorter way round
    out = coords.copy()
    # Add exact multiples of 360 to the original values; np.unwrap's running sum
    # turns 179.9 into 179.89999999999995, which can tip an edge over the densify threshold.
    out[:, 0] = lons + 360 * np.round((unwrapped - lons) / 360)
    return out


def _closed(ring: np.ndarray) -> np.ndarray:
    """A ring that circles a pole crosses 180 once, so after unwrapping it no longer closes."""
    if abs(ring[0, 0] - ring[-1, 0]) > 1e-9:
        raise _Unmeasurable(
            "This polygon encircles a pole, so it has no continuous form in longitude/latitude. "
            "Polar polygons are not supported."
        )
    return ring


def _moved_near(part: BaseGeometry, lon: float) -> BaseGeometry:
    """Shift a whole part by a multiple of 360 degrees so it starts within 180 degrees of ``lon``."""
    turns = round((shapely.get_coordinates(part)[0, 0] - lon) / 360)
    if not turns:
        return part
    return shapely.transform(part, lambda coords: coords - np.array([360.0 * turns, 0.0]))


def _shift_coords(coords: np.ndarray, lon: float) -> np.ndarray:
    out = coords.copy()
    out[:, 0] -= 360 * round((coords[0, 0] - lon) / 360)
    return out


def _paths(geometry: BaseGeometry) -> list[BaseGeometry]:
    """Every line or ring in a geometry, holes included."""
    parts = shapely.get_parts(geometry)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return list(shapely.get_rings(parts))
    return list(parts)


# ---------------------------------------------------------------- elevation

def _add_elevation(result: Measurement, geometry: BaseGeometry, source_crs: CRS) -> None:
    """Fill in the surface length and height range when the file has usable heights.

    The surface length follows the file's own vertices. Each step combines the
    horizontal distance between two vertices (geodesic, or grid distance on a
    local grid) with the change in height between them, so a road climbing a
    pit wall comes out longer than its map length. For a polygon this is the
    3D perimeter; a surface *area* would need a terrain model, not just the
    heights of the boundary.
    """
    if not geometry.has_z:
        return
    paths = [shapely.get_coordinates(path, include_z=True) for path in _paths(geometry)]
    if not paths or not all(np.isfinite(coords).all() for coords in paths):
        return

    z_factor = vertical_unit_factor(source_crs)
    local = is_local_grid(source_crs)
    total = 0.0
    for coords in paths:
        x, y, z = coords[:, 0], coords[:, 1], coords[:, 2] * z_factor
        if local:
            horizontal = np.hypot(np.diff(x), np.diff(y)) * linear_unit_factor(source_crs)
        else:
            if source_crs is not WGS84:
                x, y = transformer(source_crs, WGS84).transform(x, y)
            _, _, horizontal = GEOD.inv(x[:-1], y[:-1], x[1:], y[1:])
        total += float(np.sum(np.hypot(horizontal, np.diff(z))))
    if not math.isfinite(total):
        return

    heights = np.concatenate([coords[:, 2] for coords in paths]) * z_factor
    result.surface_length_m = round(total, 3)
    result.elevation_min_m = round(float(heights.min()), 3)
    result.elevation_max_m = round(float(heights.max()), 3)


# ---------------------------------------------------------------- helpers

def _polygonal_part(geometry: BaseGeometry) -> BaseGeometry | None:
    """Keep only the polygons from a repair result (make_valid can also emit lines)."""
    parts = getattr(geometry, "geoms", [geometry])
    polygons: list[Polygon] = []
    for part in parts:
        if isinstance(part, Polygon):
            polygons.append(part)
        elif isinstance(part, MultiPolygon):
            polygons.extend(part.geoms)
    polygons = [p for p in polygons if not p.is_empty]
    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def _geodesic_area(geometry: BaseGeometry) -> float:
    """Area on the WGS84 ellipsoid: each outer ring minus its holes."""
    polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else [geometry]
    total = 0.0
    for polygon in polygons:
        total += _ring_area(polygon.exterior)
        total -= sum(_ring_area(hole) for hole in polygon.interiors)
    return total


def _ring_area(ring) -> float:
    lons, lats = ring.xy
    area, _perimeter = GEOD.polygon_area_perimeter(lons, lats)
    return abs(area)  # the sign only encodes winding direction


def _deviation_pct(value: float, reference: float) -> float | None:
    if not reference:
        return None
    return round((value - reference) / reference * 100, 4)
