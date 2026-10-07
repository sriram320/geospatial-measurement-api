"""Tests for the measurement engine.

Most of these are known-answer tests: the input is built so that the right
answer is obvious without trusting our own code. A rectangle drawn with
1,000 m sides on the UTM grid has to come back as 1,000,000 m2.
"""

import math

import pytest
from pyproj import CRS, Geod
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.geo.crs import WGS84, reproject, select_projection, transformer
from app.geo.measure import measure_feature
from app.geo.types import MeasurementStatus as Status
from app.geo.types import RawFeature
from tests.helpers import UTM_43N, to_lonlat, utm_box


def measure(geometry, crs=WGS84):
    kind = geometry.geom_type if geometry is not None else None
    return measure_feature(RawFeature(index=0, geometry=geometry, geometry_type=kind), crs)


# ------------------------------------------------------------ known answers

def test_one_km_square_measures_one_square_km():
    square = to_lonlat(utm_box(780_000, 1_440_000, 1_000, 1_000))  # near Bengaluru

    result = measure(square)

    assert result.status is Status.MEASURED
    assert result.area_m2 == pytest.approx(1_000_000, abs=0.5)
    assert result.perimeter_m == pytest.approx(4_000, abs=0.01)
    assert result.projected_crs == "EPSG:32643"
    assert result.projection_strategy == "utm_zone"


def test_one_km_line_measures_one_km():
    line = to_lonlat(LineString([(780_000, 1_440_000), (781_000, 1_440_000)]))

    result = measure(line)

    assert result.status is Status.MEASURED
    assert result.length_m == pytest.approx(1_000, abs=0.01)
    assert result.area_m2 is None


def test_holes_are_subtracted_from_polygon_area():
    outer = utm_box(780_000, 1_440_000, 1_000, 1_000)
    hole = utm_box(780_400, 1_440_400, 200, 200)
    polygon = to_lonlat(Polygon(outer.exterior.coords, [hole.exterior.coords]))

    result = measure(polygon)

    assert result.area_m2 == pytest.approx(1_000_000 - 40_000, abs=0.5)
    # The geodesic cross-check must subtract the hole as well.
    assert result.geodesic_area_m2 == pytest.approx(960_000, rel=0.002)


def test_multipolygon_area_is_the_sum_of_its_parts():
    parts = MultiPolygon([utm_box(780_000, 1_440_000, 100, 50), utm_box(780_500, 1_440_000, 40, 25)])

    result = measure(to_lonlat(parts))

    assert result.area_m2 == pytest.approx(5_000 + 1_000, abs=0.05)


def test_multilinestring_and_linear_ring_are_measured_as_length():
    lines = MultiLineString(
        [[(780_000, 1_440_000), (780_300, 1_440_000)], [(780_000, 1_440_100), (780_000, 1_440_500)]]
    )
    ring = LinearRing(utm_box(780_000, 1_440_000, 100, 100).exterior.coords)

    assert measure(to_lonlat(lines)).length_m == pytest.approx(700, abs=0.01)
    assert measure(to_lonlat(ring)).length_m == pytest.approx(400, abs=0.01)


def test_projected_source_gives_same_answer_as_its_lonlat_equivalent():
    native = utm_box(780_000, 1_440_000, 250, 400)

    from_projected = measure(native, crs=UTM_43N)
    from_lonlat = measure(to_lonlat(native))

    assert from_projected.area_m2 == pytest.approx(100_000, abs=0.05)
    assert from_projected.area_m2 == pytest.approx(from_lonlat.area_m2, abs=0.05)


# ------------------------------------------------------------ why CRS handling matters

def test_same_size_in_degrees_is_not_the_same_size_on_the_ground():
    """A 0.01 x 0.01 degree box is half as wide at 60N as at the equator,
    because meridians converge. Degrees are angles, not distances."""
    at_equator = measure(box(10.0, 0.0, 10.01, 0.01))
    at_60_north = measure(box(10.0, 60.0, 10.01, 60.01))

    assert at_60_north.area_m2 / at_equator.area_m2 == pytest.approx(math.cos(math.radians(60)), rel=0.01)


def test_web_mercator_would_overstate_area_but_we_do_not():
    """EPSG:3857 is the projection people reach for first. It stretches both
    axes by 1/cos(latitude), so areas grow by 1/cos^2(latitude): about 6% at
    Bengaluru and 300% at 60N. This test documents the trap we avoid."""
    square = to_lonlat(utm_box(780_000, 1_440_000, 1_000, 1_000))
    latitude = square.centroid.y

    naive_area = reproject(square, transformer(WGS84, CRS.from_epsg(3857))).area
    result = measure(square)

    # Within 1%: the 1/cos^2 rule is for a sphere, and the ellipsoid adds a little more.
    assert naive_area / result.geodesic_area_m2 == pytest.approx(1 / math.cos(math.radians(latitude)) ** 2, rel=0.01)
    assert naive_area / result.geodesic_area_m2 > 1.05
    assert abs(result.deviation_pct) < 0.2


# ------------------------------------------------------------ the geodesic cross-check

def test_cross_check_exposes_the_utm_scale_factor():
    """On a UTM zone's central meridian the scale factor is exactly 0.9996, so
    grid areas are 0.9996^2 of true areas: a deviation of -0.08%. Seeing that
    number come out is good evidence both methods are implemented correctly."""
    square = to_lonlat(utm_box(499_500, 1_440_000, 1_000, 1_000))  # easting 500,000 is the central meridian

    result = measure(square)

    assert result.deviation_pct == pytest.approx((0.9996**2 - 1) * 100, abs=0.002)


def test_line_cross_check_agrees_within_utm_tolerance():
    result = measure(to_lonlat(LineString([(780_000, 1_440_000), (790_000, 1_445_000)])))

    assert result.geodesic_length_m == pytest.approx(result.length_m, rel=0.002)


# ------------------------------------------------------------ predicting the deviation
#
# Three numbers come from three separate calculations:
#   projected   - reproject, then planar geometry
#   geodesic    - pyproj.Geod on the ellipsoid, no projection
#   expected    - the projection's scale factor at the feature's centre
# deviation_pct compares the first two. If it equals the third, the gap between
# the methods is the projection's known distortion and nothing else.

def test_expected_deviation_is_the_known_utm_scale_factor_on_the_central_meridian():
    """A known answer that needs no code to work out: UTM's scale factor on a
    central meridian is 0.9996 by definition, so lengths are predicted to be
    0.04% short and areas 0.9996^2 = 0.08% small."""
    square = to_lonlat(utm_box(499_500, 1_440_000, 1_000, 1_000))  # easting 500,000 is the central meridian
    line = to_lonlat(LineString([(500_000, 1_440_000), (500_000, 1_441_000)]))

    assert measure(square).expected_deviation_pct == pytest.approx((0.9996**2 - 1) * 100, abs=0.0002)
    assert measure(line).expected_deviation_pct == pytest.approx((0.9996 - 1) * 100, abs=0.0002)


@pytest.mark.parametrize(
    ("lon", "lat"),
    [
        (75.0, 12.97),    # on a central meridian: the projection shrinks
        (77.59, 12.97),   # Bengaluru, 280 km east of it: the projection stretches
        (77.98, 12.97),   # the edge of the zone, where UTM is at its worst
        (18.9, 69.6),     # northern Norway
        (151.2, -33.9),   # Sydney, southern hemisphere
    ],
)
def test_observed_deviation_matches_the_prediction_anywhere_in_a_zone(lon, lat):
    plot = measure(box(lon, lat, lon + 0.01, lat + 0.01))  # roughly 1 km across
    road = measure(LineString([(lon, lat), (lon + 0.01, lat), (lon + 0.01, lat + 0.01)]))

    assert plot.deviation_pct == pytest.approx(plot.expected_deviation_pct, abs=0.0005)
    assert road.deviation_pct == pytest.approx(road.expected_deviation_pct, abs=0.0005)


def test_equal_area_fallback_is_predicted_to_add_nothing():
    result = measure(box(60, 10, 80, 20))

    assert result.expected_deviation_pct == pytest.approx(0, abs=0.0001)
    assert result.deviation_pct == pytest.approx(0, abs=0.01)


def test_equidistant_fallback_has_no_single_prediction():
    """Its scale depends on the direction a line runs, so no number is reported."""
    result = measure(LineString([(60, 10), (80, 25)]))

    assert result.projection_strategy == "local_equidistant"
    assert result.deviation_pct is not None
    assert result.expected_deviation_pct is None


def test_prediction_is_approximate_for_a_feature_that_fills_much_of_a_zone():
    """The prediction is sampled at one point. Across a 3-degree block the UTM
    scale factor changes from one side to the other, so one sample cannot
    equal the average and a small remainder is left. This test documents that
    limit and keeps it bounded."""
    result = measure(box(75.0, 12.0, 78.0, 15.0))

    remainder = abs(result.deviation_pct - result.expected_deviation_pct)
    assert 0.005 < remainder < 0.1


# ------------------------------------------------------------ what was done to the geometry

def test_small_clean_geometry_is_measured_untouched():
    result = measure(to_lonlat(utm_box(780_000, 1_440_000, 1_000, 1_000)))

    assert (result.repaired, result.vertices_added) == (False, 0)


def test_long_edges_get_extra_vertices_and_the_count_is_reported():
    result = measure(LineString([(77.0, 13.0), (78.0, 13.0)]))  # one edge, 1 degree long

    assert result.vertices_added == 9  # cut into ten pieces of 0.1 degrees


def test_nothing_is_reported_for_a_feature_that_was_not_measured():
    result = measure(Point(77.59, 12.97))

    assert result.projection_reason is None and result.expected_deviation_pct is None
    assert (result.repaired, result.vertices_added) == (None, None)


# ------------------------------------------------------------ choosing the projection

@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (77.59, 12.97, "EPSG:32643"),    # Bengaluru -> zone 43 north
        (-122.42, 37.77, "EPSG:32610"),  # San Francisco -> zone 10 north
        (151.21, -33.87, "EPSG:32756"),  # Sydney -> zone 56 south
        (115.86, -31.95, "EPSG:32750"),  # Perth -> zone 50 south
        (-179.9, 10.0, "EPSG:32601"),    # first zone
        (179.9, 10.0, "EPSG:32660"),     # last zone
    ],
)
def test_utm_zone_follows_the_feature(lon, lat, expected):
    bounds = (lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001)

    assert select_projection(bounds, for_area=True).label == expected


@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (77.59, 12.97, "UTM zone 43N, which covers 72E to 78E"),
        (-122.42, 37.77, "UTM zone 10N, which covers 126W to 120W"),
        (151.21, -33.87, "UTM zone 56S, which covers 150E to 156E"),
        (-0.13, 51.51, "UTM zone 30N, which covers 6W to 0E"),  # London, just west of Greenwich
    ],
)
def test_the_chosen_zone_is_explained(lon, lat, expected):
    result = measure(box(lon, lat, lon + 0.001, lat + 0.001))

    assert result.projection_reason.startswith(expected)


def test_feature_wider_than_a_utm_zone_falls_back_to_equal_area():
    result = measure(box(60, 10, 80, 20))  # 20 degrees wide

    assert result.projection_strategy == "local_equal_area"
    assert "+proj=laea" in result.projected_crs
    assert abs(result.deviation_pct) < 0.01  # equal-area projection agrees with the ellipsoid
    assert "spans 20.0 degrees of longitude" in result.projection_reason
    assert "an equal-area projection" in result.projection_reason


def test_wide_line_falls_back_to_equidistant():
    result = measure(LineString([(60, 10), (80, 25)]))

    assert result.projection_strategy == "local_equidistant"
    assert "+proj=aeqd" in result.projected_crs
    assert "an equidistant projection" in result.projection_reason


def test_feature_beyond_utm_latitudes_falls_back():
    result = measure(box(10, 85, 12, 86))  # UTM stops at 84N

    assert result.status is Status.MEASURED
    assert result.projection_strategy == "local_equal_area"
    assert "latitude limits" in result.projection_reason


# ------------------------------------------------------------ bad and unsupported input

def test_self_intersecting_polygon_is_repaired_and_flagged():
    bow_tie = Polygon([(77.0, 13.0), (77.01, 13.01), (77.01, 13.0), (77.0, 13.01), (77.0, 13.0)])
    assert not bow_tie.is_valid

    result = measure(bow_tie)

    assert result.status is Status.MEASURED
    assert result.area_m2 > 0
    assert result.repaired is True
    assert any("repaired" in warning for warning in result.warnings)


def test_points_have_nothing_to_measure():
    result = measure(Point(77.59, 12.97))

    assert result.status is Status.NOT_APPLICABLE
    assert result.area_m2 is None and result.length_m is None


def test_mixed_geometry_collection_is_reported_as_unsupported():
    collection = GeometryCollection([Point(77.59, 12.97), box(77.59, 12.97, 77.60, 12.98)])

    result = measure(collection)

    assert result.status is Status.UNSUPPORTED
    assert "GeometryCollection" in result.message


def test_type_the_reader_could_not_build_is_unsupported():
    feature = RawFeature(index=0, geometry=None, geometry_type="MultiPatch")

    assert measure_feature(feature, WGS84).status is Status.UNSUPPORTED


def test_feature_without_geometry_is_not_applicable():
    assert measure_feature(RawFeature(index=0), WGS84).status is Status.NOT_APPLICABLE


def test_reader_error_is_carried_through_as_failed():
    feature = RawFeature(index=0, geometry_type="LineString", error="Could not read <LineString> geometry")

    result = measure_feature(feature, WGS84)

    assert result.status is Status.FAILED
    assert result.message == "Could not read <LineString> geometry"


def test_unknown_crs_fails_instead_of_guessing():
    result = measure(utm_box(780_000, 1_440_000, 100, 100), crs=None)

    assert result.status is Status.FAILED
    assert "CRS is unknown" in result.message


def test_impossible_latitude_is_caught():
    """A y value of 113 cannot be a latitude, so the pair was probably written
    lat,lon. Only impossible values can be caught this way: a swap that still
    lands inside valid ranges looks like a legitimate location elsewhere."""
    swapped = LineString([(12.97, 113.59), (12.98, 113.60)])

    result = measure(swapped)

    assert result.status is Status.FAILED
    assert "longitude/latitude" in result.message
    # Swapping 12.97 and 113.59 gives a real place, so the message can say so outright.
    assert "almost certainly stored as latitude,longitude" in result.message


def test_out_of_range_coordinates_that_a_swap_would_not_fix_get_the_general_message():
    result = measure(LineString([(200.0, 95.0), (201.0, 96.0)]))  # neither order is valid

    assert result.status is Status.FAILED
    assert "swapped" not in result.message
    assert "Check the CRS" in result.message


def test_three_dimensional_geometry_is_measured_in_plan():
    flat = to_lonlat(LineString([(780_000, 1_440_000), (781_000, 1_440_000)]))
    with_z = LineString([(x, y, 900.0 + i * 50) for i, (x, y) in enumerate(flat.coords)])

    assert measure(with_z).length_m == pytest.approx(1_000, abs=0.01)


# ------------------------------------------------------------ the antimeridian (180th meridian)

GEOD = Geod(ellps="WGS84")


def lon_box(west: float, east: float, south: float = -17.0, north: float = -16.99) -> Polygon:
    """A box from ``west`` to ``east``. Across 180 that means west=179.995, east=-179.995."""
    return Polygon([(west, south), (east, south), (east, north), (west, north)])


def test_square_across_the_antimeridian_measures_like_the_same_square_beside_it():
    """Before the fix, this 1.2 km2 square near Fiji measured as 42,000 km2, by both methods."""
    across = measure(lon_box(179.995, -179.995))
    beside = measure(lon_box(179.985, 179.995))  # same size, same latitude, just west of 180

    assert across.status is Status.MEASURED
    assert across.crosses_antimeridian is True and beside.crosses_antimeridian is False
    assert across.geodesic_area_m2 == pytest.approx(beside.geodesic_area_m2, abs=0.01)
    assert across.area_m2 == pytest.approx(across.geodesic_area_m2, rel=0.003)
    assert abs(across.deviation_pct - across.expected_deviation_pct) < 0.001  # still fully explained
    assert any("180th meridian" in warning for warning in across.warnings)
    assert not beside.warnings


def test_line_across_the_antimeridian_takes_the_short_way():
    result = measure(LineString([(179.99, -17.0), (-179.99, -17.0)]))
    _, _, short_way = GEOD.inv(179.99, -17.0, -179.99, -17.0)  # about 2.1 km

    assert result.crosses_antimeridian is True
    assert result.geodesic_length_m == pytest.approx(short_way, abs=0.01)
    assert result.length_m == pytest.approx(short_way, rel=0.003)


def test_feature_split_into_halves_at_180_is_measured_as_one():
    halves = MultiPolygon([lon_box(179.995, 180.0), lon_box(-180.0, -179.995)])

    result = measure(halves)

    assert result.crosses_antimeridian is True
    assert result.repaired is False  # the halves are rejoined, not reported as a defect
    assert result.geodesic_area_m2 == pytest.approx(measure(lon_box(179.995, -179.995)).geodesic_area_m2, abs=0.01)
    assert result.projection_strategy == "utm_zone"  # not a fallback for a "359-degree-wide" feature


def test_hole_across_the_antimeridian_is_subtracted():
    shell = [(179.93, -17.07), (-179.93, -17.07), (-179.93, -16.93), (179.93, -16.93)]
    hole = [(179.99, -17.01), (-179.99, -17.01), (-179.99, -16.99), (179.99, -16.99)]

    def west(x: float) -> float:  # the same shape moved 0.2 degrees west, entirely clear of 180
        return x - 0.2 if x > 0 else x + 359.8

    moved = Polygon([(west(x), y) for x, y in shell], [[(west(x), y) for x, y in hole]])

    result = measure(Polygon(shell, [hole]))

    assert result.geodesic_area_m2 == pytest.approx(measure(moved).geodesic_area_m2, abs=0.1)
    assert result.geodesic_area_m2 < measure(Polygon(shell)).geodesic_area_m2 - 4_000_000  # hole is ~4.7 km2


def test_projected_source_spilling_across_180_is_handled():
    """UTM zone 60S runs to 180E; a 1 km square drawn past its edge has eastings beyond 180."""
    square = box(818_500, 8_118_000, 819_500, 8_119_000)

    result = measure(square, CRS.from_epsg(32760))

    assert result.crosses_antimeridian is True
    assert result.geodesic_area_m2 == pytest.approx(1_000_000, rel=0.005)


def test_polygon_around_a_pole_fails_with_a_reason():
    around_south_pole = Polygon([(-170, -85), (-60, -85), (60, -85), (170, -85)])

    result = measure(around_south_pole)

    assert result.status is Status.FAILED
    assert "encircles a pole" in result.message


# ------------------------------------------------------------ heights (Z values)

def test_surface_length_includes_the_climb():
    """A haul road 300 m long on the map that climbs 400 m is about 500 m long on the ground."""
    road = LineString([(780_000, 1_440_000, 100.0), (780_300, 1_440_000, 500.0)])

    result = measure(road, UTM_43N)

    assert result.length_m == pytest.approx(300, abs=0.01)  # the plan length is unchanged
    assert result.surface_length_m == pytest.approx(math.hypot(result.geodesic_length_m, 400), abs=0.01)
    assert (result.elevation_min_m, result.elevation_max_m) == (100.0, 500.0)


def test_level_line_is_as_long_on_the_ground_as_on_the_ellipsoid():
    road = LineString([(780_000, 1_440_000, 920.0), (780_300, 1_440_000, 920.0), (780_300, 1_440_400, 920.0)])

    result = measure(road, UTM_43N)

    assert result.surface_length_m == pytest.approx(result.geodesic_length_m, abs=0.001)


def test_heights_in_feet_are_converted_to_metres():
    """California State Plane zone 5 in US survey feet, with NAVD88 heights in feet."""
    crs = CRS.from_user_input("EPSG:2229+6360")
    road = LineString([(6_500_000, 1_850_000, 0.0), (6_501_000, 1_850_000, 1_000.0)])

    result = measure(road, crs)

    assert result.elevation_max_m == pytest.approx(304.8006, abs=0.001)
    assert result.surface_length_m == pytest.approx(math.hypot(result.geodesic_length_m, 304.8006), abs=0.01)


def test_polygon_with_heights_gets_a_surface_perimeter():
    flat = utm_box(780_000, 1_440_000, 100, 100)
    # The east side is 50 m higher than the west side, so the north and south edges each slope up 50 m.
    sloped = Polygon([(x, y, 50.0 if x > 780_050 else 0.0) for x, y in flat.exterior.coords])

    result = measure(sloped, UTM_43N)

    assert result.area_m2 == pytest.approx(10_000, abs=0.01)  # area stays planimetric
    expected = 2 * 100 + 2 * math.hypot(100, 50)  # two level sides, two sloping sides
    assert result.surface_length_m == pytest.approx(expected, rel=0.002)  # within UTM's scale at this spot


def test_unusable_heights_are_ignored():
    road = LineString([(780_000, 1_440_000, float("nan")), (780_300, 1_440_000, 10.0)])

    result = measure(road, UTM_43N)

    assert result.status is Status.MEASURED
    assert result.surface_length_m is None and result.elevation_min_m is None


def test_feature_without_heights_has_no_elevation():
    assert measure(LineString([(77.59, 12.97), (77.60, 12.97)])).surface_length_m is None


# ------------------------------------------------------------ local site grids

def local_grid(unit: str = "Meter", factor: float = 1.0) -> CRS:
    return CRS.from_wkt(
        f'LOCAL_CS["Quarry site grid",LOCAL_DATUM["Arbitrary",0],UNIT["{unit}",{factor}],'
        'AXIS["X",EAST],AXIS["Y",NORTH]]'
    )


def test_local_site_grid_is_measured_directly():
    """A grid with no link to latitude/longitude used to fail every feature."""
    result = measure(box(1_000, 2_000, 1_100, 2_050), local_grid())

    assert result.status is Status.MEASURED
    assert result.area_m2 == 5_000.0
    assert result.perimeter_m == 300.0
    assert result.projection_strategy == "source_grid"
    assert "Quarry site grid" in result.projection_reason
    # There is nothing to cross-check against, and the result says so rather than inventing a value.
    assert result.geodesic_area_m2 is None and result.deviation_pct is None and result.expected_deviation_pct is None


def test_local_grid_in_feet_is_converted_to_metres():
    result = measure(LineString([(0, 0, 0), (100, 0, 0), (100, 50, 0)]), local_grid("Foot", 0.3048))

    assert result.length_m == pytest.approx(150 * 0.3048)
    assert result.surface_length_m == pytest.approx(150 * 0.3048)


def test_invalid_polygon_on_a_local_grid_is_repaired():
    bow_tie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])

    result = measure(bow_tie, local_grid())

    assert result.repaired is True
    assert result.area_m2 == pytest.approx(50.0)
