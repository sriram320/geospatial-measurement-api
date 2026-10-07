"""End-to-end tests through the HTTP API."""

import pytest
from shapely.geometry import LineString

from app.config import settings
from tests.helpers import (
    LINE,
    SQUARE,
    UTM_43N_PRJ,
    kml,
    line_xml,
    placemark,
    point_xml,
    polygon_xml,
    shapefile_zip,
    utm_box,
    zip_of,
)

SITE_KML = kml(
    placemark("Plot", polygon_xml(SQUARE), pm_id="plot-1"),
    placemark("Road", line_xml(LINE)),
    placemark("Marker", point_xml(77.59, 12.97)),
)


def upload(client, name: str, data: bytes, **form):
    return client.post("/api/files/", files={"file": (name, data)}, data=form)


# ------------------------------------------------------------ the three required endpoints

def test_upload_kml_then_read_file_info(client):
    created = upload(client, "survey.kml", SITE_KML)

    assert created.status_code == 201
    body = created.json()
    assert body["duplicate"] is False

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200
    # The fields from the assignment's example response, with the expected values.
    assert {k: info.json()[k] for k in ("id", "filename", "feature_count", "crs", "status")} == {
        "id": body["id"],
        "filename": "survey.kml",
        "feature_count": 3,
        "crs": "EPSG:4326",
        "status": "COMPLETED",
    }


def test_measurements_for_kml(client):
    file_id = upload(client, "survey.kml", SITE_KML).json()["id"]

    body = client.get(f"/api/files/{file_id}/measurements/").json()
    plot, road, marker = body["results"]

    assert plot["status"] == "MEASURED" and plot["properties"] == {"name": "Plot"} and plot["source_id"] == "plot-1"
    assert plot["area_m2"] == pytest.approx(12_000, rel=0.02)  # ~108 m x ~111 m
    assert plot["area_hectares"] == pytest.approx(plot["area_m2"] / 10_000)
    assert plot["length_m"] is None
    assert plot["method"] == {
        "source_crs": "EPSG:4326",
        "projected_crs": "EPSG:32643",
        "strategy": "utm_zone",
        "reason": "UTM zone 43N, which covers 72E to 78E and contains the centre of this feature.",
    }
    assert abs(plot["cross_check"]["deviation_pct"]) < 0.2
    # The gap between the two methods is what the projection predicts, to 4 decimal places.
    assert plot["cross_check"]["expected_pct"] == pytest.approx(plot["cross_check"]["deviation_pct"], abs=0.001)
    assert abs(plot["cross_check"]["unexplained_pct"]) < 0.001
    assert plot["processing"] == {"repaired": False, "vertices_added": 0, "crosses_antimeridian": False}
    assert plot["elevation"] is None  # no altitudes in this file

    assert road["status"] == "MEASURED"
    assert road["length_m"] == pytest.approx(2_190, rel=0.02)  # ~1.08 km east, then ~1.11 km north
    assert road["length_km"] == pytest.approx(road["length_m"] / 1_000)
    assert road["area_m2"] is None

    assert marker["status"] == "NOT_APPLICABLE"
    assert marker["method"] is None and marker["cross_check"] is None and marker["processing"] is None

    assert body["summary"] == {
        "measured": 2,
        "not_applicable": 1,
        "unsupported": 0,
        "failed": 0,
        "total_area_m2": plot["area_m2"],
        "total_area_hectares": pytest.approx(plot["area_m2"] / 10_000),
        "total_length_m": road["length_m"],
        "total_length_km": pytest.approx(road["length_m"] / 1_000),
    }


def test_features_endpoint_returns_geometry_crs_and_properties(client):
    file_id = upload(client, "survey.kml", SITE_KML).json()["id"]

    body = client.get(f"/api/files/{file_id}/features/").json()
    plot = body["results"][0]

    assert body["total"] == 3
    assert plot["index"] == 0
    assert plot["source_id"] == "plot-1"
    assert plot["geometry_type"] == "Polygon"
    assert plot["crs"] == "EPSG:4326"
    assert plot["properties"] == {"name": "Plot"}
    assert plot["geometry"]["type"] == "Polygon"
    assert plot["geometry"]["coordinates"][0][0] == [77.59, 12.97]


def test_upload_shapefile_in_projected_crs(client):
    parcels = shapefile_zip(
        [utm_box(780_000, 1_440_000, 100, 50), utm_box(780_200, 1_440_000, 200, 150)],
        records=[{"NAME": "P-001"}, {"NAME": "P-002"}],
        prj=UTM_43N_PRJ,
    )

    created = upload(client, "parcels.zip", parcels)
    assert created.status_code == 201
    assert created.json()["crs"] == "EPSG:32643"
    assert created.json()["file_format"] == "shapefile"

    results = client.get(f"/api/files/{created.json()['id']}/measurements/").json()["results"]
    assert [r["properties"]["NAME"] for r in results] == ["P-001", "P-002"]
    assert results[0]["area_m2"] == pytest.approx(5_000, abs=0.05)
    assert results[1]["area_m2"] == pytest.approx(30_000, abs=0.05)
    assert results[0]["perimeter_m"] == pytest.approx(300, abs=0.01)

    # Geometry is returned in the file's own CRS, not converted.
    feature = client.get(f"/api/files/{created.json()['id']}/features/").json()["results"][0]
    assert feature["crs"] == "EPSG:32643"
    assert feature["geometry"]["coordinates"][0][0][0] >= 780_000


def test_kmz_upload_is_accepted(client):
    created = upload(client, "site.kmz", zip_of({"doc.kml": SITE_KML, "files/icon.png": b"not really a png"}))

    assert created.status_code == 201
    assert created.json()["file_format"] == "kml"
    assert created.json()["feature_count"] == 3


# ------------------------------------------------------------ CRS edge cases

def test_shapefile_without_prj_in_degrees_is_assumed_wgs84(client):
    body = upload(client, "roads.zip", shapefile_zip([LineString(LINE)])).json()

    assert body["crs"] == "EPSG:4326"
    assert "assumed" in body["warnings"][0]
    results = client.get(f"/api/files/{body['id']}/measurements/").json()["results"]
    assert results[0]["status"] == "MEASURED"


def test_shapefile_without_prj_in_metres_is_stored_but_not_measured(client):
    body = upload(client, "parcels.zip", shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)])).json()

    assert body["status"] == "COMPLETED"  # the file was read fine; only measuring is impossible
    assert body["crs"] is None
    result = client.get(f"/api/files/{body['id']}/measurements/").json()["results"][0]
    assert result["status"] == "FAILED"
    assert "CRS is unknown" in result["message"]


def test_client_can_supply_the_missing_crs(client):
    data = shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)])

    body = upload(client, "parcels.zip", data, crs="EPSG:32643").json()

    assert body["crs"] == "EPSG:32643"
    result = client.get(f"/api/files/{body['id']}/measurements/").json()["results"][0]
    assert result["area_m2"] == pytest.approx(5_000, abs=0.05)


def test_invalid_crs_value_is_rejected_before_anything_is_stored(client):
    response = upload(client, "parcels.zip", shapefile_zip([utm_box(780_000, 1_440_000, 100, 50)]), crs="EPSG:banana")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_crs"
    assert client.get("/api/files/").json()["total"] == 0


# ------------------------------------------------------------ robustness

def test_one_file_can_hold_every_outcome_without_failing(client):
    mixed = f"<MultiGeometry>{point_xml(77.59, 12.97)}{polygon_xml(SQUARE)}</MultiGeometry>"
    bow_tie = [(77.0, 13.0), (77.01, 13.01), (77.01, 13.0), (77.0, 13.01), (77.0, 13.0)]
    data = kml(
        placemark("Good", polygon_xml(SQUARE)),
        placemark("Mixed", mixed),
        placemark("Corrupt", "<LineString><coordinates>77.59,12.97 oops</coordinates></LineString>"),
        placemark("Bow-tie", polygon_xml(bow_tie)),
        placemark("Note"),
    )

    created = upload(client, "messy.kml", data)
    assert created.status_code == 201
    assert created.json()["status"] == "COMPLETED"

    body = client.get(f"/api/files/{created.json()['id']}/measurements/").json()
    assert [r["status"] for r in body["results"]] == [
        "MEASURED", "UNSUPPORTED", "FAILED", "MEASURED", "NOT_APPLICABLE",
    ]
    assert "repaired" in body["results"][3]["warnings"][0]
    assert body["results"][3]["processing"]["repaired"] is True
    assert body["summary"]["measured"] == 2 and body["summary"]["failed"] == 1


# ------------------------------------------------------------ the file report

def test_report_summarises_the_whole_file(client):
    bow_tie = [(77.0, 13.0), (77.01, 13.01), (77.01, 13.0), (77.0, 13.01), (77.0, 13.0)]
    mixed = f"<MultiGeometry>{point_xml(77.59, 12.97)}{polygon_xml(SQUARE)}</MultiGeometry>"
    broken = "<LineString><coordinates>77.59,12.97 oops</coordinates></LineString>"
    data = kml(
        placemark("Plot", polygon_xml(SQUARE)),
        placemark("Road", line_xml(LINE)),
        placemark("Bow-tie", polygon_xml(bow_tie)),
        placemark("Marker", point_xml(77.59, 12.97)),
        placemark("Mixed", mixed),
        placemark("Broken 1", broken),
        placemark("Broken 2", broken),
    )
    file_id = upload(client, "site.kml", data).json()["id"]

    report = client.get(f"/api/files/{file_id}/report/").json()

    assert report["status"] == "COMPLETED"
    assert report["summary"]["measured"] == 3 and report["summary"]["failed"] == 2
    assert report["projections"] == [{"projected_crs": "EPSG:32643", "strategy": "utm_zone", "features": 3}]
    assert report["processing"] == {"repaired": 1, "densified": 0, "crosses_antimeridian": 0, "with_elevation": 0}

    # All three measured features sit in one UTM zone, so every gap is explained by its scale factor.
    check = report["cross_check"]
    assert check["features_checked"] == 3
    assert 0 < check["max_abs_deviation_pct"] < 0.2
    assert check["max_abs_unexplained_pct"] < 0.001
    assert check["largest_deviation_index"] in (0, 1, 2)

    # The two identical failures are one line, not two, and the most common reason comes first.
    failed, unsupported = report["problems"]
    assert (failed["status"], failed["features"], failed["first_index"]) == ("FAILED", 2, 5)
    assert (unsupported["status"], unsupported["features"], unsupported["first_index"]) == ("UNSUPPORTED", 1, 4)


def test_report_for_an_unreadable_file_shows_the_error_and_no_figures(client):
    file_id = upload(client, "broken.zip", b"this is not a zip archive").json()["error"]["file_id"]

    report = client.get(f"/api/files/{file_id}/report/").json()

    assert report["status"] == "FAILED"
    assert "not a valid zip" in report["error"]
    assert report["projections"] == [] and report["problems"] == []
    assert report["cross_check"] == {
        "features_checked": 0,
        "max_abs_deviation_pct": None,
        "max_abs_unexplained_pct": None,
        "largest_deviation_index": None,
    }


def test_report_counts_features_in_different_utm_zones_separately(client):
    """One file, two countries: each feature is measured in its own zone."""
    sydney = [(151.20, -33.87), (151.21, -33.87), (151.21, -33.86), (151.20, -33.86), (151.20, -33.87)]
    data = kml(
        placemark("Bengaluru plot", polygon_xml(SQUARE)),
        placemark("Bengaluru road", line_xml(LINE)),
        placemark("Sydney plot", polygon_xml(sydney)),
    )
    file_id = upload(client, "two-sites.kml", data).json()["id"]

    report = client.get(f"/api/files/{file_id}/report/").json()

    assert report["projections"] == [
        {"projected_crs": "EPSG:32643", "strategy": "utm_zone", "features": 2},
        {"projected_crs": "EPSG:32756", "strategy": "utm_zone", "features": 1},
    ]


def test_unreadable_file_is_recorded_as_failed(client):
    response = upload(client, "broken.zip", b"this is not a zip archive")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_zip"

    info = client.get(f"/api/files/{error['file_id']}/").json()
    assert info["status"] == "FAILED"
    assert "not a valid zip" in info["error"]
    assert info["feature_count"] == 0


def test_identical_upload_returns_the_existing_result(client):
    first = upload(client, "survey.kml", SITE_KML)
    second = upload(client, "renamed-copy.kml", SITE_KML)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert client.get("/api/files/").json()["total"] == 1


def test_failed_upload_is_not_treated_as_a_duplicate(client):
    first = upload(client, "broken.zip", b"garbage")
    second = upload(client, "broken.zip", b"garbage")

    assert first.status_code == second.status_code == 422
    assert first.json()["error"]["file_id"] != second.json()["error"]["file_id"]


# ------------------------------------------------------------ request validation

def test_unsupported_extension_is_rejected(client):
    response = upload(client, "data.geojson", b"{}")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"


def test_empty_file_is_rejected(client):
    response = upload(client, "empty.kml", b"")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_oversized_file_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 100)

    response = upload(client, "survey.kml", SITE_KML)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_request_without_a_file_is_rejected(client):
    assert client.post("/api/files/").status_code == 422


def test_path_components_are_stripped_from_the_filename(client):
    body = upload(client, "../../etc/survey.kml", SITE_KML).json()

    assert body["filename"] == "survey.kml"


# ------------------------------------------------------------ lookups, paging, delete

@pytest.mark.parametrize(
    "path",
    ["/api/files/{id}/", "/api/files/{id}/features/", "/api/files/{id}/measurements/", "/api/files/{id}/report/"],
)
def test_unknown_file_id_returns_404(client, path):
    response = client.get(path.format(id="does-not-exist"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "file_not_found"


def test_measurements_can_be_paged_and_filtered(client):
    file_id = upload(client, "survey.kml", SITE_KML).json()["id"]
    url = f"/api/files/{file_id}/measurements/"

    page = client.get(url, params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3
    assert [r["index"] for r in page["results"]] == [1]
    assert page["summary"]["measured"] == 2  # the summary covers the whole file, not the page

    only_points = client.get(url, params={"status": "NOT_APPLICABLE"}).json()
    assert only_points["total"] == 1
    assert only_points["results"][0]["geometry_type"] == "Point"

    assert client.get(url, params={"status": "NONSENSE"}).status_code == 422
    assert client.get(url, params={"limit": 0}).status_code == 422


def test_files_are_listed_newest_first(client):
    upload(client, "first.kml", kml(placemark("A", point_xml(77.1, 12.1))))
    upload(client, "second.kml", kml(placemark("B", point_xml(77.2, 12.2))))

    body = client.get("/api/files/").json()

    assert body["total"] == 2
    assert [f["filename"] for f in body["results"]] == ["second.kml", "first.kml"]


def test_delete_removes_the_file_and_its_features(client):
    file_id = upload(client, "survey.kml", SITE_KML).json()["id"]

    assert client.delete(f"/api/files/{file_id}/").status_code == 204
    assert client.get(f"/api/files/{file_id}/").status_code == 404
    assert client.delete(f"/api/files/{file_id}/").status_code == 404

    # With the original gone, the same bytes are processed afresh.
    assert upload(client, "survey.kml", SITE_KML).status_code == 201


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ------------------------------------------------------------ background processing

def upload_async(client, name: str, data: bytes, **form):
    return client.post("/api/files/", params={"async": "true"}, files={"file": (name, data)}, data=form)


def test_async_upload_returns_at_once_and_finishes_in_the_background(client):
    accepted = upload_async(client, "survey.kml", SITE_KML)

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["status"] == "PROCESSING" and body["feature_count"] == 0
    assert accepted.headers["location"] == f"/api/files/{body['id']}/"

    # The test client runs background tasks before returning, so the work is done by now.
    info = client.get(accepted.headers["location"]).json()
    assert info["status"] == "COMPLETED" and info["feature_count"] == 3
    measurements = client.get(f"/api/files/{body['id']}/measurements/").json()
    assert measurements["summary"]["measured"] == 2


def test_async_upload_of_an_unreadable_file_ends_as_failed(client):
    accepted = upload_async(client, "broken.zip", b"this is not a zip archive")

    assert accepted.status_code == 202  # the content is only read after the response
    info = client.get(f"/api/files/{accepted.json()['id']}/").json()
    assert info["status"] == "FAILED"
    assert "not a valid zip" in info["error"]


def test_async_upload_of_a_known_file_returns_the_earlier_result(client):
    first = upload(client, "survey.kml", SITE_KML).json()

    again = upload_async(client, "survey.kml", SITE_KML)

    assert again.status_code == 200
    assert again.json()["id"] == first["id"] and again.json()["duplicate"] is True


def test_async_upload_still_rejects_a_bad_crs_up_front(client):
    response = upload_async(client, "survey.kml", SITE_KML, crs="EPSG:999999")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_crs"


# ------------------------------------------------------------ heights and the antimeridian

def test_heights_and_antimeridian_appear_in_measurements_and_report(client):
    climbing_road = (
        "<LineString><altitudeMode>absolute</altitudeMode>"
        "<coordinates>77.59,12.97,900 77.60,12.97,960</coordinates></LineString>"
    )
    fiji_plot = polygon_xml([(179.995, -17.0), (-179.995, -17.0), (-179.995, -16.99), (179.995, -16.99)])
    file_id = upload(client, "edge-cases.kml", kml(
        placemark("Haul road", climbing_road),
        placemark("Fiji plot", fiji_plot),
    )).json()["id"]

    road, plot = client.get(f"/api/files/{file_id}/measurements/").json()["results"]

    assert road["elevation"]["min_m"] == 900.0 and road["elevation"]["max_m"] == 960.0
    assert road["elevation"]["surface_length_m"] > road["cross_check"]["length_m"]
    assert road["elevation"]["surface_perimeter_m"] is None
    assert plot["processing"]["crosses_antimeridian"] is True
    assert plot["area_m2"] == pytest.approx(1_180_000, rel=0.01)  # not 42,000 km2
    assert plot["elevation"] is None

    report = client.get(f"/api/files/{file_id}/report/").json()
    assert report["processing"]["crosses_antimeridian"] == 1
    assert report["processing"]["with_elevation"] == 1
