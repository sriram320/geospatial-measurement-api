"""Coordinate reference system handling.

Three jobs live here:

1. Work out which CRS a file is in (declared, supplied by the client, or
   inferred when a Shapefile ships without a .prj).
2. Convert any geometry to WGS84 longitude/latitude, the common ground every
   later step works from.
3. Choose a projected CRS in metres that suits one particular feature.
"""

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Proj, Transformer
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry

from app.geo.types import GeoFileError, ParsedFile

WGS84 = CRS.from_epsg(4326)

# A UTM zone is 6 degrees of longitude wide and is defined from 80S to 84N.
UTM_MAX_LON_SPAN = 6.0
UTM_MIN_LAT, UTM_MAX_LAT = -80.0, 84.0


@dataclass(frozen=True)
class Projection:
    crs: CRS
    label: str      # what we show the client, e.g. "EPSG:32643"
    strategy: str   # "utm_zone", "local_equal_area" or "local_equidistant"
    reason: str     # one sentence saying why this projection was chosen
    from_wgs84: Transformer
    proj: Proj      # the same projection as a Proj object, which can report scale factors


def crs_label(crs: CRS) -> str:
    """Short human-readable name: the EPSG code when one matches, else the CRS name."""
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


def parse_declared_crs(value: str) -> CRS:
    """Parse a CRS supplied by the client, e.g. "EPSG:32643"."""
    try:
        return CRS.from_user_input(value.strip())
    except CRSError as exc:
        raise GeoFileError("invalid_crs", f"'{value}' is not a recognised CRS (try e.g. EPSG:32643).") from exc


def resolve_source_crs(parsed: ParsedFile, declared: CRS | None) -> tuple[CRS | None, list[str]]:
    """Decide the CRS of a parsed file. Returns (crs, warnings).

    Order of trust: what the file says, then what the client says, then an
    inference from the coordinate values. If none of those work we return
    None and the features are stored without measurements rather than with
    wrong ones.
    """
    if parsed.crs is not None:
        if declared is not None:
            return parsed.crs, ["The file declares its own CRS, so the 'crs' form field was ignored."]
        return parsed.crs, []

    if declared is not None:
        return declared, [f"The file declares no CRS. Used the client-supplied CRS {crs_label(declared)}."]

    bounds = [f.geometry.bounds for f in parsed.features if f.geometry is not None and not f.geometry.is_empty]
    if not bounds:
        return None, ["The file declares no CRS and has no geometry to infer one from."]

    looks_like_degrees = all(
        minx >= -180 and maxx <= 180 and miny >= -90 and maxy <= 90 for minx, miny, maxx, maxy in bounds
    )
    if looks_like_degrees:
        return WGS84, [
            "The file declares no CRS. Every coordinate fits longitude/latitude ranges, so EPSG:4326 "
            "was assumed. Re-upload with the 'crs' form field if that is wrong."
        ]
    return None, [
        "The file declares no CRS and the coordinates are not longitude/latitude, so measurements "
        "were skipped. Re-upload with the 'crs' form field (e.g. crs=EPSG:32643)."
    ]


def is_local_grid(crs: CRS) -> bool:
    """A site or CAD grid: planar coordinates with no link to latitude/longitude.

    Such a CRS cannot be converted to WGS84, but its coordinates are already a
    flat grid in a known unit, so they can be measured directly.
    """
    return crs.is_engineering


def linear_unit_factor(crs: CRS) -> float:
    """Metres per horizontal coordinate unit of a projected or local grid CRS (0.3048 for feet)."""
    return crs.axis_info[0].unit_conversion_factor if crs.axis_info else 1.0


def vertical_unit_factor(crs: CRS) -> float:
    """Metres per unit of the Z values in a file that uses this CRS.

    An explicit vertical axis (a compound CRS such as EPSG:32643+5773, or a
    3D geographic CRS) settles it. Otherwise heights are assumed to share the
    horizontal unit of a projected or local grid, and to be metres for
    longitude/latitude, which is what KML specifies.
    """
    for part in crs.sub_crs_list or [crs]:
        for axis in part.axis_info:
            if axis.direction == "up":
                return axis.unit_conversion_factor
    if crs.is_geographic:
        return 1.0
    return linear_unit_factor(crs)


def wrap_lon(lon: float) -> float:
    """Bring a longitude into [-180, 180). 181 becomes -179."""
    return (lon + 180) % 360 - 180


def transformer(source: CRS, target: CRS) -> Transformer:
    """The transformer between two CRSs, built once and then reused.

    The cache is keyed on each CRS's definition text (``.srs``). Using the CRS
    objects themselves as keys works but is slow: hashing one re-serialises
    it to WKT on every lookup, and this is called once per feature.
    """
    return _transformer(source.srs, target.srs)


@lru_cache(maxsize=64)
def _transformer(source: str, target: str) -> Transformer:
    # always_xy=True pins the axis order to (x=lon/easting, y=lat/northing).
    # Without it pyproj follows each CRS's official axis order, and EPSG:4326
    # is officially (lat, lon), which silently swaps coordinates.
    return Transformer.from_crs(source, target, always_xy=True)


def reproject(geometry: BaseGeometry, using: Transformer) -> BaseGeometry:
    """Move every vertex of a geometry through a transformer.

    shapely hands over all the vertices as one (N, 2) array, so pyproj
    converts them in a single call instead of one Python call per ring.
    """

    def apply(coords: np.ndarray) -> np.ndarray:
        x, y = using.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geometry, apply)


@lru_cache(maxsize=128)
def _utm_projection(epsg: int) -> Projection:
    crs = CRS.from_epsg(epsg)
    zone, hemisphere = epsg % 100, "N" if epsg < 32700 else "S"
    west = zone * 6 - 186  # zone 1 starts at 180W, and each zone is 6 degrees wide
    reason = (
        f"UTM zone {zone}{hemisphere}, which covers {_lon(west)} to {_lon(west + 6)} "
        "and contains the centre of this feature."
    )
    return Projection(crs, f"EPSG:{epsg}", "utm_zone", reason, transformer(WGS84, crs), Proj(crs))


def _lon(degrees: int) -> str:
    return f"{abs(degrees)}{'E' if degrees >= 0 else 'W'}"


def select_projection(bounds: tuple[float, float, float, float], for_area: bool) -> Projection:
    """Choose a metre-based CRS for a feature from its WGS84 bounding box.

    Default: the UTM zone under the centre of the feature. UTM is conformal,
    is the standard for survey deliverables, and keeps linear distortion to
    roughly 0.1% inside a zone.

    Fallback: a feature wider than one zone, or beyond UTM's latitude limits,
    gets a projection built around its own centre. For areas that is Lambert
    Azimuthal Equal-Area (areas are preserved by construction); for lengths it
    is Azimuthal Equidistant.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    # A feature unwrapped across the antimeridian can have longitudes past 180.
    centre_lon = wrap_lon((min_lon + max_lon) / 2)
    centre_lat = (min_lat + max_lat) / 2

    fits_utm = (max_lon - min_lon) <= UTM_MAX_LON_SPAN and min_lat >= UTM_MIN_LAT and max_lat <= UTM_MAX_LAT
    if fits_utm:
        zone = min(int((centre_lon + 180) // 6) + 1, 60)
        epsg = (32600 if centre_lat >= 0 else 32700) + zone  # 326xx = north, 327xx = south
        return _utm_projection(epsg)  # built once per zone, then reused

    lon_span = max_lon - min_lon
    if lon_span > UTM_MAX_LON_SPAN:
        why = f"This feature spans {lon_span:.1f} degrees of longitude, more than one 6-degree UTM zone"
    else:
        why = "This feature reaches beyond UTM's latitude limits (80S to 84N)"

    name, strategy, kind = (
        ("laea", "local_equal_area", "an equal-area") if for_area else ("aeqd", "local_equidistant", "an equidistant")
    )
    proj4 = f"+proj={name} +lat_0={centre_lat:.6f} +lon_0={centre_lon:.6f} +datum=WGS84 +units=m +no_defs"
    crs = CRS.from_proj4(proj4)
    reason = f"{why}, so {kind} projection centred on the feature was used."
    return Projection(crs, proj4, strategy, reason, Transformer.from_crs(WGS84, crs, always_xy=True), Proj(crs))


def expected_distortion_pct(projection: Projection, lon: float, lat: float, for_area: bool) -> float | None:
    """How much this projection is predicted to change a measurement at one point, in percent.

    No flat map can show a curved surface at true size everywhere. Each
    projection stretches or shrinks the ground by a known amount that depends
    on where you are, and PROJ reports that amount as scale factors:

    * areas    the areal scale factor (map area / true area)
    * lengths  the linear scale factor (map length / true length). UTM is
               conformal, so this is the same in every direction.

    A scale factor of 1.0006 means the projection overstates by 0.06%.

    The azimuthal equidistant fallback is not conformal: its scale depends on
    the direction a line runs, so there is no single number to predict and
    the result is None.
    """
    if projection.strategy == "local_equidistant":
        return None
    factors = projection.proj.get_factors(wrap_lon(lon), lat)
    scale = factors.areal_scale if for_area else factors.meridional_scale
    return (scale - 1) * 100 if math.isfinite(scale) else None
