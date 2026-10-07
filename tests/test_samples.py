"""Every file in samples/ uploaded through the API, checked against answers known in advance.

The generated samples (scripts/make_samples.py) are drawn in exact metres, so
most expected values here come from plain geometry, not from running the code.
"""

import io
import math
import zipfile
from pathlib import Path

import pytest
import shapefile  # pyshp
from pyproj import Geod
from shapely.geometry import Polygon

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
GEOD = Geod(ellps="WGS84")


def upload(client, name: str, **params) -> dict:
    response = client.post("/api/files/", params=params, files={"file": (name, (SAMPLES / name).read_bytes())})
    assert response.status_code in (201, 202), response.text
    file_id = response.json()["id"]
    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED", info
    measurements = client.get(f"/api/files/{file_id}/measurements/", params={"limit": 1000}).json()
    report = client.get(f"/api/files/{file_id}/report/").json()
    return {"info": info, "results": measurements["results"], "summary": measurements["summary"], "report": report}


def by_name(results: list[dict], key: str) -> dict:
    return {r["properties"][key]: r for r in results}


def regular_polygon_area(radius: float, sides: int) -> float:
    return sides / 2 * radius**2 * math.sin(2 * math.pi / sides)


# ------------------------------------------------------------ original samples

def test_parcels_utm43n(client):
    areas = [r["area_m2"] for r in upload(client, "parcels_utm43n.zip")["results"]]

    assert areas == [5_000.0, 30_000.0, 1_000.0, 100_000.0]


def test_quarry_site_has_every_outcome(client):
    out = upload(client, "quarry_site.kml")

    assert [r["status"] for r in out["results"]] == [
        "MEASURED", "MEASURED", "MEASURED", "MEASURED", "MEASURED",
        "NOT_APPLICABLE", "UNSUPPORTED", "NOT_APPLICABLE", "FAILED",
    ]
    assert out["report"]["processing"]["repaired"] == 1


def test_roads_without_prj_are_assumed_wgs84(client):
    out = upload(client, "roads_no_prj.zip")

    assert out["info"]["crs"] == "EPSG:4326"
    assert any("EPSG:4326 was assumed" in w for w in out["info"]["warnings"])


def test_edge_cases(client):
    out = upload(client, "edge_cases.kml")

    assert out["report"]["processing"]["crosses_antimeridian"] == 2
    assert out["report"]["processing"]["with_elevation"] == 1
    assert out["results"][0]["area_m2"] == pytest.approx(1_181_762, rel=0.001)


# ------------------------------------------------------------ drone-survey deliverables

def test_mine_stockpiles_3d(client):
    out = upload(client, "mine_stockpiles_3d.zip")
    features = by_name(out["results"], "FEAT_ID")

    assert features["PIT-01"]["area_m2"] == pytest.approx(600 * 350 - 80 * 60, abs=0.01)
    for i, radius in enumerate([18, 25, 32, 40, 55], start=1):
        assert features[f"SP-0{i}"]["area_m2"] == pytest.approx(regular_polygon_area(radius, 24), abs=0.01)
    assert features["WD-01"]["area_m2"] == pytest.approx((300 + 180) / 2 * 140, abs=0.01)

    assert out["report"]["processing"]["with_elevation"] == 7
    assert features["PIT-01"]["elevation"]["min_m"] == 205.0  # the island inside the pit
    assert out["report"]["cross_check"]["max_abs_unexplained_pct"] < 0.001


def test_village_property_parcels(client):
    out = upload(client, "village_property_parcels.kml")
    results = out["results"]
    widths, depths = [9, 12, 15, 12, 9, 15, 12, 9], [15, 18, 24, 18, 15, 24]

    assert out["info"]["feature_count"] == 1 + 48 + 5 + 2
    assert results[0]["area_m2"] == pytest.approx(113 * 164, abs=0.05)  # settlement boundary
    plots = results[1:49]
    expected = [w * d for d in depths for w in widths]
    assert [p["area_m2"] for p in plots] == pytest.approx(expected, abs=0.01)
    assert plots[0]["properties"]["property_id"] == "KA-TMK-0417-0001"
    assert [lane["length_m"] for lane in results[49:54]] == pytest.approx([113.0] * 5, abs=0.01)


def test_highway_corridor_spans_two_utm_zones(client):
    out = upload(client, "highway_corridor.kml")
    features = by_name(out["results"], "name")
    package_a = [(77.700, 13.000), (77.760, 13.020), (77.830, 13.035), (77.900, 13.060), (77.975, 13.080)]

    zones = {p["projected_crs"] for p in out["report"]["projections"]}
    assert zones == {"EPSG:32643", "EPSG:32644"}
    centre_a = features["Package A centre-line"]
    assert centre_a["method"]["projected_crs"] == "EPSG:32643"
    assert features["Package B centre-line"]["method"]["projected_crs"] == "EPSG:32644"
    lons, lats = zip(*package_a, strict=True)
    assert centre_a["cross_check"]["length_m"] == pytest.approx(GEOD.line_length(lons, lats), rel=1e-6)
    # A 60 m wide corridor with flat ends and mitred bends is exactly 60 m x its centre-line.
    assert features["Package A right of way"]["area_m2"] == pytest.approx(60 * centre_a["length_m"], rel=1e-4)
    assert out["summary"]["not_applicable"] == 16  # chainage markers every 5 km


def test_solar_farm_blocks(client):
    out = upload(client, "solar_farm_blocks.zip")
    by_type: dict[str, list[float]] = {}
    for r in out["results"]:
        by_type.setdefault(r["properties"]["TYPE"], []).append(r["area_m2"])

    assert by_type["BOUNDARY"] == [700 * 520]
    assert by_type["PV_BLOCK"] == [120 * 80] * 20
    assert by_type["INVERTER"] == [10 * 6] * 12


def test_transmission_line_surface_length_includes_the_climb(client):
    results = upload(client, "transmission_line_3d.zip")["results"]
    spans = [380, 420, 350, 450, 300, 410, 390, 360, 440, 330, 400]
    heights = [1820, 1905, 2010, 2140, 2085, 2230, 2365, 2290, 2410, 2525, 2470, 2600]
    climbs = [b - a for a, b in zip(heights, heights[1:], strict=False)]

    for section, (start, end) in zip(results, [(0, 4), (4, 8), (8, 11)], strict=True):
        assert section["length_m"] == pytest.approx(sum(spans[start:end]), abs=0.01)
        expected = sum(math.hypot(s, c) for s, c in zip(spans[start:end], climbs[start:end], strict=True))
        assert section["elevation"]["surface_length_m"] == pytest.approx(expected, rel=0.001)


def test_construction_site_on_a_local_grid(client):
    out = upload(client, "construction_site_grid.zip")

    assert [r["area_m2"] for r in out["results"]] == pytest.approx(
        [180 * 120, 42 * 28, 40 * 40 - 20 * 20, 60 * 35 - 8 * 6, regular_polygon_area(30, 24)], abs=0.01
    )
    assert {r["method"]["strategy"] for r in out["results"]} == {"source_grid"}
    assert out["report"]["cross_check"]["features_checked"] == 0  # nothing to cross-check against


def test_legacy_kalianpur_survey(client):
    out = upload(client, "legacy_kalianpur_survey.zip")
    grid_areas = [100 * 80, 120 * 60, 90 * 90, 150 * 100, 75 * 50, 200 * 120]

    assert out["info"]["crs"] == "EPSG:24383"
    for result, grid_area in zip(out["results"], grid_areas, strict=True):
        # The zone IVa grid has a scale factor of 0.99879, so true areas are ~0.24% larger.
        assert 1.0015 < result["cross_check"]["area_m2"] / grid_area < 1.0035
        assert abs(result["cross_check"]["unexplained_pct"]) < 0.001


def test_farm_fields_with_an_epsg_code_as_prj(client):
    out = upload(client, "farm_fields_epsg_prj.zip")
    fields = by_name(out["results"], "FIELD_ID")
    true_areas = {"F01": 40_000, "F02": 30_000, "F03": 25_000, "F04": 60_000,
                  "F05": 18_000, "F06": 19_500, "F07": 12_000, "F08": 16_200}

    assert out["info"]["crs"] == "EPSG:32643" and not out["info"]["warnings"]
    for field_id, area in true_areas.items():
        assert fields[field_id]["area_m2"] == pytest.approx(area, abs=0.01)

    # What a crop-insurance check would do with the result: compare with the declared area.
    mismatched = {
        f for f, r in fields.items() if abs(r["area_hectares"] - r["properties"]["DECL_HA"]) / r["area_hectares"] > 0.05
    }
    assert mismatched == {"F02", "F04", "F06"}


def test_pacific_antimeridian(client):
    out = upload(client, "pacific_antimeridian.kml")
    features = by_name(out["results"], "name")

    assert out["report"]["processing"]["crosses_antimeridian"] == 4
    assert out["report"]["processing"]["repaired"] == 0  # halves split at 180 are rejoined, not "repaired"
    reef_area, _ = GEOD.polygon_area_perimeter([179.98, 180.02, 180.02, 179.98], [-16.95, -16.95, -16.92, -16.92])
    assert features["Reef survey zone (stored split at 180)"]["cross_check"]["area_m2"] == pytest.approx(
        abs(reef_area), rel=1e-6
    )
    _, _, cable = GEOD.inv(178.00, 52.40, -176.65, 51.88)
    _, _, first_leg = GEOD.inv(173.20, 52.90, 178.00, 52.40)
    assert features["Subsea cable route"]["cross_check"]["length_m"] == pytest.approx(first_leg + cable, rel=0.001)


def test_regional_features_get_the_right_projection(client):
    results = upload(client, "regional_large_features.kml")["results"]

    assert [r["method"]["strategy"] for r in results] == [
        "local_equal_area", "local_equidistant", "local_equal_area", "utm_zone",
    ]
    for r in (results[0], results[2]):  # equal-area: projected and geodesic areas agree
        assert abs(r["cross_check"]["deviation_pct"]) < 0.001


def test_messy_field_export(client):
    out = upload(client, "messy_field_export.kml")
    results = out["results"]

    assert [r["status"] for r in results] == [
        "MEASURED", "FAILED", "MEASURED", "FAILED", "MEASURED", "FAILED",
        "FAILED", "NOT_APPLICABLE", "UNSUPPORTED", "UNSUPPORTED", "UNSUPPORTED", "NOT_APPLICABLE",
    ]
    assert results[0]["properties"]["name"].startswith("ಗೋಮಾಳ")  # Kannada text survives
    assert "swapped" in results[1]["message"]
    assert results[2]["processing"]["repaired"] is True
    assert "no spaces around the commas" in results[6]["message"]
    assert results[4]["properties"]["depth_m"] == "2.5"


def test_city_buildings_20k_in_the_background(client):
    out = upload(client, "city_buildings_20k.zip", **{"async": "true"})

    with zipfile.ZipFile(SAMPLES / "city_buildings_20k.zip") as archive:
        reader = shapefile.Reader(shp=io.BytesIO(archive.read("buildings.shp")))
        grid_total = sum(Polygon(shape.points).area for shape in reader.iterShapes())

    assert out["info"]["feature_count"] == 20_000
    assert out["summary"]["measured"] == 20_000
    assert out["summary"]["total_area_m2"] == pytest.approx(grid_total, abs=0.5)
    assert out["report"]["cross_check"]["max_abs_unexplained_pct"] < 0.002
