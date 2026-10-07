"""Tests for the KML and Shapefile readers and for CRS resolution."""

import datetime as dt
import io

import pytest
import shapefile  # pyshp
from pyproj import CRS
from shapely.geometry import LineString, Point, box

from app.config import settings
from app.geo import parse_upload
from app.geo.crs import WGS84, crs_label, resolve_source_crs
from app.geo.kml_reader import read_kml
from app.geo.shp_reader import read_zip
from app.geo.types import GeoFileError
from tests.helpers import (
    LINE,
    SQUARE,
    UTM_43N_PRJ,
    WGS84_PRJ,
    kml,
    line_xml,
    placemark,
    point_xml,
    polygon_xml,
    shapefile_zip,
    utm_box,
    zip_of,
)

# ================================================================ KML


def test_kml_reads_point_line_and_polygon():
    data = kml(
        placemark("Marker", point_xml(77.59, 12.97)),
        placemark("Road", line_xml(LINE)),
        placemark("Plot", polygon_xml(SQUARE)),
    )

    parsed = read_kml(data)

    assert parsed.file_format == "kml"
    assert parsed.crs == WGS84  # KML is always WGS84 by specification
    assert [f.geometry_type for f in parsed.features] == ["Point", "LineString", "Polygon"]
    assert [f.index for f in parsed.features] == [0, 1, 2]
    assert parsed.features[2].properties == {"name": "Plot"}


@pytest.mark.parametrize("namespace", ["http://www.opengis.net/kml/2.2", "http://earth.google.com/kml/2.1", None])
def test_kml_namespace_variants_are_all_accepted(namespace):
    parsed = read_kml(kml(placemark("Plot", polygon_xml(SQUARE)), namespace=namespace))

    assert parsed.features[0].geometry_type == "Polygon"


def test_kml_finds_placemarks_nested_in_folders():
    nested = f"<Folder><Folder>{placemark('Deep', point_xml(77.59, 12.97))}</Folder></Folder>"

    parsed = read_kml(kml(placemark("Top", point_xml(77.5, 12.9)), nested))

    assert [f.properties["name"] for f in parsed.features] == ["Top", "Deep"]


def test_kml_reads_extended_data_and_placemark_id():
    extra = (
        "<description>North side</description>"
        "<ExtendedData>"
        '<Data name="owner"><value>Site team</value></Data>'
        '<SchemaData schemaUrl="#s"><SimpleData name="grade">A</SimpleData></SchemaData>'
        "</ExtendedData>"
    )

    feature = read_kml(kml(placemark("Plot", polygon_xml(SQUARE), extra=extra, pm_id="plot-7"))).features[0]

    assert feature.source_id == "plot-7"
    assert feature.properties == {"name": "Plot", "description": "North side", "owner": "Site team", "grade": "A"}


def test_kml_polygon_keeps_its_holes():
    hole = [(77.5903, 12.9703), (77.5906, 12.9703), (77.5906, 12.9706), (77.5903, 12.9706), (77.5903, 12.9703)]

    geometry = read_kml(kml(placemark("Plot", polygon_xml(SQUARE, holes=[hole])))).features[0].geometry

    assert len(geometry.interiors) == 1


def test_kml_altitude_is_dropped_and_coordinates_are_lon_lat():
    with_altitude = "<Point><coordinates>77.59,12.97,912.5</coordinates></Point>"

    geometry = read_kml(kml(placemark("GCP", with_altitude))).features[0].geometry

    assert (geometry.x, geometry.y) == (77.59, 12.97)
    assert not geometry.has_z


def test_kml_absolute_altitudes_are_kept_as_heights():
    road = (
        "<LineString><altitudeMode>absolute</altitudeMode>"
        "<coordinates>77.59,12.97,900 77.60,12.97,950.5</coordinates></LineString>"
    )

    geometry = read_kml(kml(placemark("Haul road", road))).features[0].geometry

    assert geometry.has_z
    assert [z for _, _, z in geometry.coords] == [900.0, 950.5]


@pytest.mark.parametrize("mode", ["clampToGround", "relativeToGround", "clampToSeaFloor"])
def test_kml_altitudes_that_are_not_elevations_are_dropped(mode):
    """clampToGround ignores altitude by definition; relativeToGround is height above the terrain."""
    road = (
        f"<LineString><altitudeMode>{mode}</altitudeMode>"
        "<coordinates>77.59,12.97,10 77.6,12.97,20</coordinates></LineString>"
    )

    assert not read_kml(kml(placemark("Road", road))).features[0].geometry.has_z


def test_kml_heights_are_dropped_when_some_points_lack_one():
    road = (
        "<LineString><altitudeMode>absolute</altitudeMode>"
        "<coordinates>77.59,12.97,900 77.6,12.97</coordinates></LineString>"
    )

    assert not read_kml(kml(placemark("Road", road))).features[0].geometry.has_z


def test_kml_polygon_with_absolute_altitudes_keeps_heights():
    shell = [(x, y, 800.0) for x, y in SQUARE]
    polygon = polygon_xml(shell).replace("<Polygon>", "<Polygon><altitudeMode>absolute</altitudeMode>")

    geometry = read_kml(kml(placemark("Pad", polygon))).features[0].geometry

    assert geometry.has_z


def test_kml_multigeometry_of_one_type_becomes_a_multi_geometry():
    multi = f"<MultiGeometry>{polygon_xml(SQUARE)}{polygon_xml([(x + 0.01, y) for x, y in SQUARE])}</MultiGeometry>"

    feature = read_kml(kml(placemark("Dumps", multi))).features[0]

    assert feature.geometry_type == "MultiPolygon"
    assert len(feature.geometry.geoms) == 2


def test_kml_multigeometry_of_mixed_types_becomes_a_collection():
    mixed = f"<MultiGeometry>{point_xml(77.59, 12.97)}{polygon_xml(SQUARE)}</MultiGeometry>"

    assert read_kml(kml(placemark("Mixed", mixed))).features[0].geometry_type == "GeometryCollection"


def test_kml_placemark_without_geometry_is_kept():
    feature = read_kml(kml(placemark("Note"))).features[0]

    assert feature.geometry is None and feature.geometry_type is None and feature.error is None


def test_kml_track_is_recognised_but_not_built():
    track = '<gx:Track xmlns:gx="http://www.google.com/kml/ext/2.2"><gx:coord>77.59 12.97 0</gx:coord></gx:Track>'

    feature = read_kml(kml(placemark("Flight path", track))).features[0]

    assert feature.geometry is None
    assert feature.geometry_type == "Track"


@pytest.mark.parametrize(
    "bad_geometry",
    [
        "<LineString><coordinates>77.59,12.97 77.60;12.98</coordinates></LineString>",  # wrong separator
        "<LineString><coordinates>77.59,abc 77.60,12.98</coordinates></LineString>",    # not a number
        "<LineString><coordinates>77.59,12.97</coordinates></LineString>",              # one point is not a line
        "<LineString><coordinates>  </coordinates></LineString>",                       # empty
        "<Polygon><outerBoundaryIs></outerBoundaryIs></Polygon>",                       # no ring
        "<Point><coordinates>nan,nan</coordinates></Point>",                            # not finite
    ],
)
def test_kml_bad_geometry_fails_that_feature_only(bad_geometry):
    data = kml(placemark("Broken", bad_geometry), placemark("Fine", point_xml(77.59, 12.97)))

    broken, fine = read_kml(data).features

    assert broken.geometry is None and broken.error
    assert fine.geometry is not None and fine.error is None


def test_kml_with_no_placemarks_is_valid_but_warns():
    parsed = read_kml(kml())

    assert parsed.features == []
    assert "no Placemark" in parsed.warnings[0]


def test_kml_malformed_xml_is_rejected():
    with pytest.raises(GeoFileError) as error:
        read_kml(b"<kml><Document><Placemark></Document></kml>")

    assert error.value.code == "invalid_kml"


def test_kml_that_is_xml_but_not_kml_is_rejected():
    with pytest.raises(GeoFileError) as error:
        read_kml(b"<html><body>not a map</body></html>")

    assert error.value.code == "invalid_kml"


def test_kml_entity_expansion_attack_is_blocked():
    billion_laughs = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE kml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b"<kml><Document><Placemark><name>&b;</name></Placemark></Document></kml>"
    )

    with pytest.raises(GeoFileError) as error:
        read_kml(billion_laughs)

    assert error.value.code == "unsafe_xml"


# ================================================================ Shapefile


def test_shapefile_reads_geometry_attributes_and_crs():
    data = shapefile_zip(
        [utm_box(780_000, 1_440_000, 100, 50)],
        records=[{"NAME": "P-001", "VALUE": 12.5, "SURVEYED": dt.date(2026, 9, 28)}],
        prj=UTM_43N_PRJ,
    )

    parsed = read_zip(data)

    feature = parsed.features[0]
    assert parsed.file_format == "shapefile"
    assert crs_label(parsed.crs) == "EPSG:32643"
    assert feature.geometry_type == "Polygon"
    assert feature.geometry.area == pytest.approx(5_000)
    assert feature.properties == {"NAME": "P-001", "VALUE": 12.5, "SURVEYED": "2026-09-28"}  # date made JSON-safe


def test_shapefile_reads_lines_and_points():
    lines = read_zip(shapefile_zip([LineString(LINE)], prj=WGS84_PRJ))
    points = read_zip(shapefile_zip([Point(77.59, 12.97)], prj=WGS84_PRJ))

    assert lines.features[0].geometry_type == "LineString"
    assert points.features[0].geometry_type == "Point"
    assert crs_label(lines.crs) == "EPSG:4326"


def test_shapefile_inside_a_folder_with_macos_junk_is_found():
    data = shapefile_zip(
        [utm_box(780_000, 1_440_000, 100, 50)],
        prj=UTM_43N_PRJ,
        folder="survey/export/",
        extra_files={"__MACOSX/survey/export/._layer.shp": b"junk", "survey/README.txt": b"hello"},
    )

    assert len(read_zip(data).features) == 1


def test_shapefile_with_upper_case_extensions_is_read():
    data = shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)], prj=UTM_43N_PRJ, upper_case=True)

    parsed = read_zip(data)

    assert len(parsed.features) == 1
    assert crs_label(parsed.crs) == "EPSG:32643"


def test_shapefile_without_prj_has_no_crs():
    assert read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)])).crs is None


def test_shapefile_with_unreadable_prj_warns_and_has_no_crs():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)], prj="this is not WKT"))

    assert parsed.crs is None
    assert any(".prj" in warning for warning in parsed.warnings)


@pytest.mark.parametrize("prj", ["EPSG:32643", "+proj=utm +zone=43 +datum=WGS84 +units=m +no_defs"])
def test_shapefile_prj_written_as_a_code_or_proj_string_is_understood(prj):
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)], prj=prj))

    assert crs_label(parsed.crs) == "EPSG:32643"
    assert not parsed.warnings


def test_shapefile_on_a_local_site_grid_keeps_that_grid():
    prj = 'LOCAL_CS["Site grid",LOCAL_DATUM["Arbitrary",0],UNIT["Meter",1.0],AXIS["X",EAST],AXIS["Y",NORTH]]'

    parsed = read_zip(shapefile_zip([box(0, 0, 100, 50)], prj=prj))

    assert parsed.crs.is_engineering
    assert crs_label(parsed.crs) == "Site grid"


def test_z_type_shapefile_keeps_its_heights():
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGONZ)
    writer.field("NAME", "C", 10)
    outer = [[0, 0, 1.0], [0, 10, 2.0], [10, 10, 3.0], [10, 0, 4.0], [0, 0, 1.0]]
    hole = [[2, 2, 5.0], [4, 2, 6.0], [4, 4, 7.0], [2, 4, 8.0], [2, 2, 5.0]]
    writer.polyz([outer, hole])
    writer.record("pad")
    writer.close()

    parsed = read_zip(zip_of({"pad.shp": shp.getvalue(), "pad.shx": shx.getvalue(), "pad.dbf": dbf.getvalue()}))
    polygon = parsed.features[0].geometry

    assert polygon.has_z
    assert {z for _, _, z in polygon.exterior.coords} == {1.0, 2.0, 3.0, 4.0}
    assert {z for _, _, z in polygon.interiors[0].coords} == {5.0, 6.0, 7.0, 8.0}


def test_shapefile_without_dbf_still_reads_geometry():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)], include=(".shp", ".shx")))

    assert parsed.features[0].geometry_type == "Polygon"
    assert parsed.features[0].properties == {}
    assert any(".dbf" in warning for warning in parsed.warnings)


def test_shapefile_without_shx_still_reads():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)], include=(".shp", ".dbf")))

    assert len(parsed.features) == 1


def test_shapefile_null_shape_is_a_feature_without_geometry():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50), None], prj=UTM_43N_PRJ))

    assert parsed.features[1].geometry is None
    assert parsed.features[1].properties["NAME"] == "feature-1"


def test_zip_without_a_shapefile_is_rejected():
    with pytest.raises(GeoFileError) as error:
        read_zip(zip_of({"notes.txt": b"nothing geospatial here"}))

    assert error.value.code == "missing_shp"


def test_zip_with_several_shapefiles_is_rejected():
    with pytest.raises(GeoFileError) as error:
        read_zip(zip_of({"a.shp": b"x", "b.shp": b"y"}))

    assert error.value.code == "multiple_layers"


def test_file_that_is_not_a_zip_is_rejected():
    with pytest.raises(GeoFileError) as error:
        read_zip(b"definitely not a zip archive")

    assert error.value.code == "invalid_zip"


def test_corrupt_shp_is_rejected():
    with pytest.raises(GeoFileError) as error:
        read_zip(zip_of({"layer.shp": b"\x00" * 40, "layer.dbf": b"\x00" * 10}))

    assert error.value.code == "invalid_shapefile"


def test_zip_bomb_guard_checks_uncompressed_size(monkeypatch):
    monkeypatch.setattr(settings, "max_uncompressed_bytes", 1_000)

    with pytest.raises(GeoFileError) as error:
        read_zip(zip_of({"layer.shp": b"\x00" * 50_000}))  # compresses to almost nothing

    assert error.value.code == "archive_too_large"


def test_zipped_kml_is_read_as_kml():
    parsed = read_zip(zip_of({"doc.kml": kml(placemark("Plot", polygon_xml(SQUARE)))}))

    assert parsed.file_format == "kml"
    assert parsed.features[0].geometry_type == "Polygon"


# ================================================================ dispatch and CRS resolution


def test_parse_upload_dispatches_on_extension():
    assert parse_upload("site.KML", kml(placemark("P", point_xml(77.59, 12.97)))).file_format == "kml"
    assert parse_upload("layer.zip", shapefile_zip([Point(77.59, 12.97)])).file_format == "shapefile"

    with pytest.raises(GeoFileError) as error:
        parse_upload("data.geojson", b"{}")
    assert error.value.code == "unsupported_file_type"


def test_crs_declared_in_the_file_wins_over_the_client():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)], prj=UTM_43N_PRJ))

    crs, warnings = resolve_source_crs(parsed, declared=CRS.from_epsg(4326))

    assert crs_label(crs) == "EPSG:32643"
    assert "ignored" in warnings[0]


def test_client_crs_is_used_when_the_file_has_none():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)]))

    crs, warnings = resolve_source_crs(parsed, declared=CRS.from_epsg(32643))

    assert crs_label(crs) == "EPSG:32643"
    assert "client-supplied" in warnings[0]


def test_missing_crs_with_degree_like_coordinates_is_assumed_wgs84():
    parsed = read_zip(shapefile_zip([box(77.59, 12.97, 77.60, 12.98)]))

    crs, warnings = resolve_source_crs(parsed, declared=None)

    assert crs == WGS84
    assert "assumed" in warnings[0]


def test_missing_crs_with_projected_coordinates_stays_unknown():
    parsed = read_zip(shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)]))

    crs, warnings = resolve_source_crs(parsed, declared=None)

    assert crs is None
    assert "skipped" in warnings[0]
