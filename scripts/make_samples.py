"""Regenerate the generated sample files in samples/.

    python scripts/make_samples.py

Every sample is synthetic. The regions are real, but the features, names and
identifiers are invented. Most shapes are drawn in exact metres on a
projected grid, so the right answer is known before the API sees the file;
tests/test_samples.py uploads every sample and checks those answers.

quarry_site.kml and edge_cases.kml are written by hand and not generated here.
"""

import datetime as dt
import io
import math
import random
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import shapefile  # pyshp
from pyproj import CRS, Geod, Transformer
from shapely.geometry import LinearRing, LineString
from shapely.ops import transform

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
WGS84 = CRS.from_epsg(4326)
SURVEY_DATE = dt.date(2026, 9, 28)


# ================================================================ shared helpers

def rectangle(x: float, y: float, width: float, height: float) -> list:
    """One clockwise ring, as a Shapefile outer ring must be."""
    return [[x, y], [x, y + height], [x + width, y + height], [x + width, y], [x, y]]


def oriented(ring: list, clockwise: bool) -> list:
    """Shapefile outer rings run clockwise and holes counter-clockwise."""
    is_ccw = LinearRing([p[:2] for p in ring]).is_ccw
    return ring[::-1] if is_ccw == clockwise else ring


def regular_polygon(cx: float, cy: float, radius: float, sides: int) -> list:
    """Area = sides/2 * r^2 * sin(2*pi/sides)."""
    ring = [
        [cx + radius * math.cos(2 * math.pi * i / sides), cy + radius * math.sin(2 * math.pi * i / sides)]
        for i in range(sides)
    ]
    return oriented(ring + [ring[0]], clockwise=True)


def new_layer(shape_type: int):
    parts = io.BytesIO(), io.BytesIO(), io.BytesIO()
    shp, shx, dbf = parts
    return shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type), parts


def write_zip(path: Path, base: str, parts, prj: str | None) -> None:
    shp, shx, dbf = parts
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{base}.shp", shp.getvalue())
        archive.writestr(f"{base}.shx", shx.getvalue())
        archive.writestr(f"{base}.dbf", dbf.getvalue())
        archive.writestr(f"{base}.cpg", "UTF-8")
        if prj:
            archive.writestr(f"{base}.prj", prj)
    report(path)


def report(path: Path) -> None:
    print(f"wrote {path.relative_to(SAMPLES.parent)} ({path.stat().st_size:,} bytes)")


def esri_prj(epsg: int) -> str:
    return CRS.from_epsg(epsg).to_wkt("WKT1_ESRI")


def grid_origin(epsg: int, lon: float, lat: float) -> tuple[float, float]:
    """The grid coordinates of a place, rounded to the nearest 1,000 m."""
    x, y = Transformer.from_crs(WGS84, CRS.from_epsg(epsg), always_xy=True).transform(lon, lat)
    return round(x, -3), round(y, -3)


def to_lonlat(epsg: int):
    return Transformer.from_crs(CRS.from_epsg(epsg), WGS84, always_xy=True).transform


# ---------------------------------------------------------------- KML

def kml_document(path: Path, name: str, body: list[str], namespace: bool = True, extra_ns: str = "") -> None:
    xmlns = ' xmlns="http://www.opengis.net/kml/2.2"' if namespace else ""
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<kml{xmlns}{extra_ns}>\n<Document>\n<name>{escape(name)}</name>\n"
        + "\n".join(body)
        + "\n</Document>\n</kml>\n"
    )
    path.write_text(text, encoding="utf-8")
    report(path)


def folder(name: str, placemarks: list[str]) -> str:
    return f"<Folder><name>{escape(name)}</name>\n" + "\n".join(placemarks) + "\n</Folder>"


def placemark(name: str, geometry: str, data: dict | None = None, pm_id: str | None = None,
              description: str | None = None) -> str:
    id_attr = f' id="{escape(pm_id)}"' if pm_id else ""
    desc = f"<description>{escape(description)}</description>" if description else ""
    extended = ""
    if data:
        rows = "".join(
            f'<Data name="{escape(str(k))}"><value>{escape(str(v))}</value></Data>' for k, v in data.items()
        )
        extended = f"<ExtendedData>{rows}</ExtendedData>"
    return f"<Placemark{id_attr}><name>{escape(name)}</name>{desc}{extended}{geometry}</Placemark>"


def coords(points) -> str:
    return " ".join(",".join(f"{v:.9f}" if i < 2 else f"{v:.2f}" for i, v in enumerate(p)) for p in points)


def kml_point(lon: float, lat: float) -> str:
    return f"<Point><coordinates>{lon:.9f},{lat:.9f}</coordinates></Point>"


def kml_line(points, absolute: bool = False) -> str:
    mode = "<altitudeMode>absolute</altitudeMode>" if absolute else ""
    return f"<LineString>{mode}<coordinates>{coords(points)}</coordinates></LineString>"


def kml_polygon(shell, holes=()) -> str:
    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{coords(h)}</coordinates></LinearRing></innerBoundaryIs>"
        for h in holes
    )
    return (
        f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords(shell)}</coordinates></LinearRing>"
        f"</outerBoundaryIs>{inner}</Polygon>"
    )


def lonlat_ring(ring, epsg: int) -> list:
    convert = to_lonlat(epsg)
    return [convert(x, y) for x, y in ring]


# ================================================================ original samples

def make_parcels() -> None:
    """parcels_utm43n.zip: four rectangles on the UTM 43N grid near Bengaluru (5,000 / 30,000 / 1,000 / 100,000 m2)."""
    writer, parts = new_layer(shapefile.POLYGON)
    writer.field("PARCEL_ID", "C", 12)
    writer.field("LAND_USE", "C", 24)
    writer.field("SURVEYED", "D")

    parcels = [
        ("P-001", "Residential", 0, 0, 100, 50),
        ("P-002", "Agricultural", 120, 0, 200, 150),
        ("P-003", "Commercial", 0, 70, 40, 25),
        ("P-004", "Industrial", 340, 0, 500, 200),
    ]
    origin_x, origin_y = 780_000, 1_440_000
    for parcel_id, land_use, dx, dy, width, height in parcels:
        writer.poly([rectangle(origin_x + dx, origin_y + dy, width, height)])
        writer.record(parcel_id, land_use, SURVEY_DATE)
    writer.close()
    write_zip(SAMPLES / "parcels_utm43n.zip", "parcels", parts, esri_prj(32643))


def make_roads() -> None:
    """roads_no_prj.zip: two road centre-lines in lon/lat with the .prj deliberately left out."""
    writer, parts = new_layer(shapefile.POLYLINE)
    writer.field("ROAD_NAME", "C", 32)
    writer.field("LANES", "N", 2, 0)
    writer.line([[[77.4040, 13.3502], [77.4048, 13.3499], [77.4056, 13.3501], [77.4060, 13.3505]]])
    writer.record("Haul road", 2)
    writer.line([[[77.3990, 13.3490], [77.4010, 13.3492], [77.4030, 13.3490]]])
    writer.record("Access road", 1)
    writer.close()
    write_zip(SAMPLES / "roads_no_prj.zip", "roads", parts, prj=None)


# ================================================================ drone-survey deliverables

def make_mine_stockpiles() -> None:
    """mine_stockpiles_3d.zip: an opencast coal mine in Jharkhand, PolygonZ in UTM 45N.

    Pit with an island (600 x 350 m minus 80 x 60 m = 205,200 m2), five
    stockpile toes drawn as 24-sided polygons (radius 18 to 55 m) and a
    trapezoidal waste dump (33,600 m2). Every boundary carries surveyed heights.
    """
    writer, parts = new_layer(shapefile.POLYGONZ)
    writer.field("FEAT_ID", "C", 10)
    writer.field("TYPE", "C", 12)
    writer.field("MATERIAL", "C", 20)
    writer.field("SURVEY_DT", "D")

    x0, y0 = grid_origin(32645, 86.40, 23.80)

    def with_z(ring, height_at):
        return [[x, y, round(height_at(x, y), 2)] for x, y in ring]

    pit = with_z(rectangle(x0, y0, 600, 350), lambda x, y: 236.0 + (x - x0) / 600 * 4)
    island = with_z(oriented(rectangle(x0 + 260, y0 + 140, 80, 60), clockwise=False), lambda x, y: 205.0)
    writer.polyz([pit, island])
    writer.record("PIT-01", "PIT", "Coal (opencast)", SURVEY_DATE)

    for i, (radius, material) in enumerate(zip(
        [18, 25, 32, 40, 55], ["Coal ROM", "Coal ROM", "Coal washed", "Overburden", "Topsoil"], strict=True
    )):
        cx, cy = x0 + 80 + i * 130, y0 - 150
        toe = with_z(regular_polygon(cx, cy, radius, 24), lambda x, y, cx=cx: 214.0 + 0.004 * (x - cx))
        writer.polyz([toe])
        writer.record(f"SP-{i + 1:02d}", "STOCKPILE", material, SURVEY_DATE)

    dump = [[x0, y0 + 450], [x0 + 60, y0 + 590], [x0 + 240, y0 + 590], [x0 + 300, y0 + 450], [x0, y0 + 450]]
    writer.polyz([with_z(oriented(dump, clockwise=True), lambda x, y: 241.0 + (y - y0 - 450) / 10)])
    writer.record("WD-01", "WASTE_DUMP", "Overburden", SURVEY_DATE)
    writer.close()
    write_zip(SAMPLES / "mine_stockpiles_3d.zip", "mine_stockpiles", parts, esri_prj(32645))


def make_village_parcels() -> None:
    """village_property_parcels.kml: rural property mapping of a village settlement near Tumakuru.

    48 house plots in six rows, sharing boundaries, each w x d metres exactly
    (widths 9/12/15 m, depths 15 to 24 m), separated by 6 m lanes; the
    settlement boundary (113 x 164 m = 18,532 m2), lane centre-lines and two landmarks.
    """
    epsg = 32643
    x0, y0 = grid_origin(epsg, 77.05, 13.30)
    widths = [9, 12, 15, 12, 9, 15, 12, 9]
    depths = [15, 18, 24, 18, 15, 24]
    uses = ["Residential", "Residential", "Commercial", "Residential", "Vacant", "Residential", "Community"]

    plots, y, number = [], y0 + 10, 1
    for depth in depths:
        x = x0 + 10
        for width in widths:
            ring = lonlat_ring(rectangle(x, y, width, depth), epsg)
            data = {
                "property_id": f"KA-TMK-0417-{number:04d}",
                "owner_ref": f"OWN-{(number * 7919) % 10000:04d}",
                "land_use": uses[number % len(uses)],
                "plot_size": f"{width} x {depth} m",
                "declared_sqft": round(width * depth * 10.7639),
            }
            plots.append(placemark(f"Property {number:04d}", kml_polygon(ring), data, pm_id=f"plot-{number}"))
            x += width
            number += 1
        y += depth + 6

    total_width, total_depth = sum(widths) + 20, sum(depths) + 6 * (len(depths) - 1) + 20
    boundary = lonlat_ring(rectangle(x0, y0, total_width, total_depth), epsg)
    lanes, lane_y = [], y0 + 10
    for i, depth in enumerate(depths[:-1]):
        lane_y += depth + 3
        line = lonlat_ring([(x0, lane_y), (x0 + total_width, lane_y)], epsg)
        lanes.append(placemark(f"Lane {i + 1}", kml_line(line), {"width_m": 6, "surface": "Concrete"}))
        lane_y += 3
    well = to_lonlat(epsg)(x0 + 5, y0 + 5)
    office = to_lonlat(epsg)(x0 + total_width - 5, y0 + total_depth - 5)

    kml_document(SAMPLES / "village_property_parcels.kml", "Village settlement survey - Kunduru (synthetic)", [
        folder("Settlement boundary", [placemark(
            "Settlement boundary", kml_polygon(boundary), {"village_code": "0417", "taluk": "Tumakuru"}
        )]),
        folder("Property parcels", plots),
        folder("Lanes", lanes),
        folder("Landmarks", [
            placemark("Community well", kml_point(*well)),
            placemark("Panchayat office", kml_point(*office)),
        ]),
    ])


def make_highway_corridor() -> None:
    """highway_corridor.kml: a 4-lane highway survey that crosses the UTM 43N/44N boundary at 78E.

    Two construction packages (one per side of 78E), the full centre-line,
    60 m right-of-way polygons, and chainage markers every 5 km.
    """
    geod = Geod(ellps="WGS84")
    package_a = [(77.700, 13.000), (77.760, 13.020), (77.830, 13.035), (77.900, 13.060), (77.975, 13.080)]
    package_b = [(78.025, 13.090), (78.100, 13.100), (78.180, 13.120), (78.260, 13.150), (78.340, 13.170),
                 (78.400, 13.190)]

    def right_of_way(points, epsg):
        to_grid = Transformer.from_crs(WGS84, CRS.from_epsg(epsg), always_xy=True).transform
        corridor = transform(to_grid, LineString(points)).buffer(30, cap_style="flat", join_style="mitre")
        return lonlat_ring(list(corridor.exterior.coords), epsg)

    full = package_a + package_b
    chainage, markers = 0.0, []
    for (lon1, lat1), (lon2, lat2) in zip(full, full[1:], strict=False):
        azimuth, _, segment = geod.inv(lon1, lat1, lon2, lat2)
        while chainage <= segment:
            lon, lat, _ = geod.fwd(lon1, lat1, azimuth, chainage)
            km = len(markers) * 5
            markers.append(placemark(f"Km {km}+000", kml_point(lon, lat), {"chainage_km": km}))
            chainage += 5_000
        chainage -= segment

    kml_document(SAMPLES / "highway_corridor.kml", "NH corridor survey, Kolar section (synthetic)", [
        folder("Centre-lines", [
            placemark("Package A centre-line", kml_line(package_a), {"package": "A", "lanes": 4}),
            placemark("Package B centre-line", kml_line(package_b), {"package": "B", "lanes": 4}),
            placemark("Full route centre-line", kml_line(full), {"package": "A+B", "lanes": 4}),
        ]),
        folder("Right of way", [
            placemark("Package A right of way", kml_polygon(right_of_way(package_a, 32643)), {"width_m": 60}),
            placemark("Package B right of way", kml_polygon(right_of_way(package_b, 32644)), {"width_m": 60}),
        ]),
        folder("Chainage", markers),
    ])


def make_solar_farm() -> None:
    """solar_farm_blocks.zip: a solar park layout in Rajasthan, UTM 42N.

    Site boundary 700 x 520 m (364,000 m2), 20 PV blocks of 120 x 80 m
    (9,600 m2 each, 2.5 MWp) and 12 inverter stations of 10 x 6 m (60 m2).
    """
    writer, parts = new_layer(shapefile.POLYGON)
    writer.field("ASSET_ID", "C", 12)
    writer.field("TYPE", "C", 12)
    writer.field("CAP_MWP", "N", 6, 2)
    writer.field("PHASE", "C", 4)

    x0, y0 = grid_origin(32642, 71.60, 27.53)
    writer.poly([rectangle(x0, y0, 700, 520)])
    writer.record("SITE-01", "BOUNDARY", 50.0, "-")

    for row in range(4):
        for col in range(5):
            bx, by = x0 + 20 + col * 132, y0 + 20 + row * 120
            writer.poly([rectangle(bx, by, 120, 80)])
            writer.record(f"PV-{row + 1}{col + 1}", "PV_BLOCK", 2.5, "I" if row < 2 else "II")
            if col % 2 == 0:
                writer.poly([rectangle(bx + 55, by + 88, 10, 6)])
                writer.record(f"INV-{row + 1}{col + 1}", "INVERTER", 0.0, "I" if row < 2 else "II")
    writer.close()
    write_zip(SAMPLES / "solar_farm_blocks.zip", "solar_farm", parts, esri_prj(32642))


def make_transmission_line() -> None:
    """transmission_line_3d.zip: a 400 kV line through the hills near Shimla, PolyLineZ in UTM 43N.

    Three sections between angle towers. Plan spans are exact (300 to 450 m)
    and each tower carries its ground elevation (1,820 to 2,600 m), so the
    surface length is sqrt(span^2 + climb^2) summed over the spans.
    """
    writer, parts = new_layer(shapefile.POLYLINEZ)
    writer.field("SECTION", "C", 8)
    writer.field("VOLTAGE_KV", "N", 4, 0)
    writer.field("CIRCUIT", "C", 4)
    writer.field("TOWERS", "C", 12)

    spans = [380, 420, 350, 450, 300, 410, 390, 360, 440, 330, 400]
    heights = [1820, 1905, 2010, 2140, 2085, 2230, 2365, 2290, 2410, 2525, 2470, 2600]
    bearings = [60] * 4 + [35] * 4 + [75] * 3  # degrees from grid north, turning at angle towers

    x, y = grid_origin(32643, 77.25, 31.05)
    towers = [(x, y, heights[0])]
    for span, bearing, height in zip(spans, bearings, heights[1:], strict=True):
        x += span * math.sin(math.radians(bearing))
        y += span * math.cos(math.radians(bearing))
        towers.append((x, y, height))

    for name, start, end in [("SEC-1", 0, 4), ("SEC-2", 4, 8), ("SEC-3", 8, 11)]:
        writer.linez([[list(t) for t in towers[start:end + 1]]])
        writer.record(name, 400, "D/C", f"T{start + 1:02d}-T{end + 1:02d}")
    writer.close()
    write_zip(SAMPLES / "transmission_line_3d.zip", "transmission_line", parts, esri_prj(32643))


def make_construction_site() -> None:
    """construction_site_grid.zip: a building site drawn on a local site grid with no lat/lon link.

    Boundary 180 x 120 m (21,600 m2), Tower A 42 x 28 m (1,176 m2), an
    L-shaped Tower B (1,200 m2), basement parking 60 x 35 m with an 8 x 6 m
    ramp core (2,052 m2) and a 24-sided crane exclusion zone of radius 30 m.
    """
    writer, parts = new_layer(shapefile.POLYGON)
    writer.field("ELEMENT", "C", 24)
    writer.field("LEVEL", "C", 8)
    writer.field("STATUS", "C", 16)

    x0, y0 = 1_000.0, 5_000.0
    tower_b = [[x0 + 120, y0 + 20], [x0 + 120, y0 + 60], [x0 + 140, y0 + 60], [x0 + 140, y0 + 40],
               [x0 + 160, y0 + 40], [x0 + 160, y0 + 20], [x0 + 120, y0 + 20]]
    elements = [
        ("Site boundary", "GL", "Approved", [rectangle(x0, y0, 180, 120)]),
        ("Tower A footprint", "GL", "Under construction", [rectangle(x0 + 20, y0 + 20, 42, 28)]),
        ("Tower B footprint", "GL", "Planned", [oriented(tower_b, clockwise=True)]),
        ("Basement parking", "B1", "Under construction", [
            rectangle(x0 + 20, y0 + 70, 60, 35), oriented(rectangle(x0 + 46, y0 + 84, 8, 6), clockwise=False),
        ]),
        ("Tower crane exclusion zone", "GL", "Active", [regular_polygon(x0 + 100, y0 + 90, 30, 24)]),
    ]
    for element, level, status, rings in elements:
        writer.poly(rings)
        writer.record(element, level, status)
    writer.close()

    prj = ('LOCAL_CS["Site grid - Whitefield tower project",LOCAL_DATUM["Site datum",0],'
           'UNIT["Meter",1.0],AXIS["Easting",EAST],AXIS["Northing",NORTH]]')
    write_zip(SAMPLES / "construction_site_grid.zip", "site_layout", parts, prj)


def make_legacy_survey() -> None:
    """legacy_kalianpur_survey.zip: old cadastral parcels near Mysuru on India's pre-GPS datum.

    Coordinates are in Kalianpur 1975 / India zone IVa (EPSG:24383), a
    Lambert conic grid on the Everest ellipsoid. Parcels are exact rectangles
    on that grid; its scale factor (0.99879) makes ground areas about 0.24% larger.
    """
    writer, parts = new_layer(shapefile.POLYGON)
    writer.field("SURVEY_NO", "C", 12)
    writer.field("VILLAGE", "C", 20)
    writer.field("LAND_CLASS", "C", 16)
    writer.field("ACRES_REC", "N", 8, 2)

    x0, y0 = grid_origin(24383, 76.64, 12.30)
    parcels = [("101", 100, 80, "Dry"), ("102", 120, 60, "Wet"), ("103/1", 90, 90, "Dry"),
               ("103/2", 150, 100, "Garden"), ("104", 75, 50, "Dry"), ("105", 200, 120, "Wet")]
    x = x0
    for number, width, height, land_class in parcels:
        writer.poly([rectangle(x, y0, width, height)])
        writer.record(f"Sy. {number}", "Hosahalli", land_class, round(width * height / 4046.8564, 2))
        x += width
    writer.close()
    write_zip(SAMPLES / "legacy_kalianpur_survey.zip", "cadastral_parcels", parts, esri_prj(24383))


def make_farm_fields() -> None:
    """farm_fields_epsg_prj.zip: wheat and paddy fields in Punjab, with a .prj that says only "EPSG:32643".

    Rectangles, parallelograms, trapezoids and a triangle with simple areas
    (12,000 to 60,000 m2). DECL_HA is the farmer-declared area; three of the
    eight are off by 5 to 8%, the kind of gap a crop-insurance check looks for.
    """
    writer, parts = new_layer(shapefile.POLYGON)
    writer.field("FIELD_ID", "C", 8)
    writer.field("CROP", "C", 12)
    writer.field("SEASON", "C", 12)
    writer.field("DECL_HA", "N", 6, 2)

    x0, y0 = grid_origin(32643, 75.85, 30.90)

    def parallelogram(x, base, height, skew):
        return [[x, y0], [x + skew, y0 + height], [x + skew + base, y0 + height], [x + base, y0], [x, y0]]

    def trapezoid(x, bottom, top, height):
        inset = (bottom - top) / 2
        return [[x, y0], [x + inset, y0 + height], [x + inset + top, y0 + height], [x + bottom, y0], [x, y0]]

    fields = [  # (id, crop, ring, declared ha); true areas in the comments
        ("F01", "Wheat", rectangle(x0, y0, 250, 160), 4.00),               # 40,000 m2
        ("F02", "Wheat", parallelogram(x0 + 270, 200, 150, 40), 3.20),     # 30,000 m2, declared 6.7% high
        ("F03", "Mustard", trapezoid(x0 + 530, 220, 180, 125), 2.50),      # 25,000 m2
        ("F04", "Paddy", rectangle(x0 + 770, y0, 300, 200), 5.50),         # 60,000 m2, declared 8.3% low
        ("F05", "Paddy", [[x0 + 1090, y0], [x0 + 1210, y0 + 150], [x0 + 1330, y0], [x0 + 1090, y0]], 1.80),
        ("F06", "Wheat", trapezoid(x0 + 1350, 160, 100, 150), 2.10),       # 19,500 m2, declared 7.7% high
        ("F07", "Fodder", rectangle(x0 + 1530, y0, 120, 100), 1.20),       # 12,000 m2
        ("F08", "Paddy", parallelogram(x0 + 1700, 180, 90, -25), 1.62),    # 16,200 m2
    ]                                                                       # F05 triangle: 18,000 m2
    for field_id, crop, ring, declared in fields:
        writer.poly([oriented(ring, clockwise=True)])
        writer.record(field_id, crop, "Rabi 2025-26", declared)
    writer.close()
    write_zip(SAMPLES / "farm_fields_epsg_prj.zip", "farm_fields", parts, prj="EPSG:32643")


def make_pacific() -> None:
    """pacific_antimeridian.kml: survey features in Fiji and the Aleutians that cross the 180th meridian."""
    kml_document(SAMPLES / "pacific_antimeridian.kml", "Pacific surveys across the 180th meridian (synthetic)", [
        placemark(
            "Taveuni survey block", kml_polygon(
                [(179.97, -16.82), (-179.97, -16.82), (-179.97, -16.78), (179.97, -16.78), (179.97, -16.82)],
                holes=[[(179.99, -16.81), (-179.99, -16.81), (-179.99, -16.79), (179.99, -16.79),
                        (179.99, -16.81)]],
            ), {"country": "Fiji", "note": "Protected forest excluded as a hole"},
        ),
        placemark(
            "Reef survey zone (stored split at 180)",
            "<MultiGeometry>"
            + kml_polygon([(179.98, -16.95), (180.0, -16.95), (180.0, -16.92), (179.98, -16.92), (179.98, -16.95)])
            + kml_polygon([(-180.0, -16.95), (-179.98, -16.95), (-179.98, -16.92), (-180.0, -16.92),
                           (-180.0, -16.95)])
            + "</MultiGeometry>",
            {"country": "Fiji", "note": "Written as two halves, as GeoJSON recommends"},
        ),
        placemark("Inter-island ferry route", kml_line([(179.34, -16.78), (179.80, -16.80), (-179.77, -16.76)]),
                  {"from": "Savusavu", "to": "Qamea"}),
        placemark("Subsea cable route", kml_line([(173.20, 52.90), (178.00, 52.40), (-176.65, 51.88)]),
                  {"region": "Aleutian Islands", "note": "Long enough to need densifying"}),
        placemark("Survey control point", kml_point(180.0, -16.80), {"type": "GNSS base"}),
    ])


def make_regional() -> None:
    """regional_large_features.kml: features too big or too far south for a single UTM zone."""
    deccan = [(73.5, 15.0), (77.0, 14.2), (80.5, 15.2), (80.2, 18.8), (77.0, 19.6), (73.8, 18.9), (73.5, 15.0)]
    rail = [(72.84, 18.94), (75.00, 20.00), (79.08, 21.15), (81.63, 21.25), (85.30, 22.80), (88.36, 22.57)]
    antarctic = [(70.0, -82.0), (80.0, -82.0), (80.0, -85.0), (70.0, -85.0), (70.0, -82.0)]
    ladakh = [(77.0, 33.8), (78.5, 33.8), (78.5, 34.8), (77.0, 34.8), (77.0, 33.8)]
    kml_document(SAMPLES / "regional_large_features.kml", "Regional-scale features (synthetic)", [
        placemark("Deccan plateau study area", kml_polygon(deccan), {"note": "7 degrees wide: wider than a UTM zone"}),
        placemark("Mumbai-Kolkata rail corridor", kml_line(rail), {"note": "15.5 degrees of longitude"}),
        placemark("Antarctic survey area", kml_polygon(antarctic), {"note": "South of UTM's 80S limit"}),
        placemark("Ladakh survey block", kml_polygon(ladakh),
                  {"note": "Fits one UTM zone's width but reaches past its edge at 78E"}),
    ])


def make_city_buildings() -> None:
    """city_buildings_20k.zip: 20,000 building footprints in a Bengaluru grid, UTM 43N.

    A load test. Footprints are whole-metre rectangles (6 to 18 m by 8 to 20 m)
    from a fixed random seed, so the total area is an exact sum of integers.
    """
    writer, parts = new_layer(shapefile.POLYGON)
    writer.field("BLDG_ID", "C", 10)
    writer.field("FLOORS", "N", 3, 0)
    writer.field("USE", "C", 12)

    rng = random.Random(20260928)
    x0, y0 = grid_origin(32643, 77.60, 12.95)
    uses = ["Residential", "Residential", "Residential", "Commercial", "Mixed", "Institutional"]
    for i in range(20_000):
        row, col = divmod(i, 200)
        width, depth = rng.randint(6, 18), rng.randint(8, 20)
        x = x0 + col * 25 + rng.randint(0, 25 - width - 1)
        y = y0 + row * 25 + rng.randint(0, 25 - depth - 1)
        writer.poly([rectangle(x, y, width, depth)])
        writer.record(f"B{i + 1:05d}", rng.randint(1, 12), rng.choice(uses))
    writer.close()
    write_zip(SAMPLES / "city_buildings_20k.zip", "buildings", parts, esri_prj(32643))


def make_messy_export() -> None:
    """messy_field_export.kml: what a real export from a field app can look like.

    No default namespace, nested folders, non-ASCII names, CDATA descriptions,
    stray whitespace, an unclosed ring, latitude/longitude written the wrong way
    round, a bow-tie, a zero-area sliver, empty and malformed coordinates, a GPS
    track, a 3D model and a mixed MultiGeometry.
    """
    swapped = [(26.1400, 91.7400), (26.1410, 91.7400), (26.1410, 91.7412), (26.1400, 91.7412), (26.1400, 91.7400)]
    body = [
        "<Folder><name>Survey 2026-09 / ಸರ್ವೆ</name>",
        "<Folder><name>Parcels</name>",
        "<Placemark><name>ಗೋಮಾಳ ಜಮೀನು (common grazing land)</name><Polygon><outerBoundaryIs><LinearRing>"
        "<coordinates>\n\t77.4000,13.3500,0\n\t77.4012,13.3500,0  77.4012,13.3500,0\n\t77.4012,13.3511,0"
        "\n\t77.4000,13.3511,0\n</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>",
        placemark("Plot written as lat,lon (Guwahati)", kml_polygon(swapped), {"surveyor": "Team NE-2"}),
        placemark("Boundary that crosses itself", kml_polygon(
            [(77.4020, 13.3500), (77.4030, 13.3510), (77.4030, 13.3500), (77.4020, 13.3510), (77.4020, 13.3500)]
        )),
        placemark("Zero-area sliver", kml_polygon(
            [(77.4040, 13.3500), (77.4045, 13.3500), (77.4050, 13.3500), (77.4040, 13.3500)]
        )),
        "<Placemark><name>Pond</name><description><![CDATA[<b>Seasonal</b> pond, dry Jan-May]]></description>"
        "<ExtendedData><SchemaData schemaUrl=\"#survey\"><SimpleData name=\"depth_m\">2.5</SimpleData>"
        "</SchemaData></ExtendedData>"
        + kml_polygon([(77.4060, 13.3500), (77.4066, 13.3500), (77.4066, 13.3505), (77.4060, 13.3505),
                       (77.4060, 13.3500)])
        + "</Placemark>",
        "</Folder>",
        "<Folder><name>Lines and points</name>",
        "<Placemark><name>Footpath (empty coordinates)</name><LineString><coordinates>  </coordinates>"
        "</LineString></Placemark>",
        "<Placemark><name>Drain (comma-space typed by hand)</name><LineString>"
        "<coordinates>77.4000, 13.3520 77.4010, 13.3521</coordinates></LineString></Placemark>",
        placemark("Boundary stone BS-7", kml_point(77.4000, 13.3500)),
        "<Placemark><name>Walked boundary (GPS track)</name><gx:Track><when>2026-09-28T09:00:00Z</when>"
        "<gx:coord>77.4 13.35 0</gx:coord></gx:Track></Placemark>",
        "<Placemark><name>Water tank (3D model)</name><Model><Location><longitude>77.401</longitude>"
        "<latitude>13.351</latitude></Location></Model></Placemark>",
        "<Placemark><name>Gate and fence</name><MultiGeometry>" + kml_point(77.4070, 13.3500)
        + kml_line([(77.4070, 13.3500), (77.4080, 13.3500)]) + "</MultiGeometry></Placemark>",
        "<Placemark><name>Note to office</name><description>Re-survey plot 12 next visit</description></Placemark>",
        "</Folder>",
        "</Folder>",
    ]
    kml_document(SAMPLES / "messy_field_export.kml", "Field app export", body, namespace=False,
                 extra_ns=' xmlns:gx="http://www.google.com/kml/ext/2.2"')


if __name__ == "__main__":
    SAMPLES.mkdir(exist_ok=True)
    for make in (make_parcels, make_roads, make_mine_stockpiles, make_village_parcels, make_highway_corridor,
                 make_solar_farm, make_transmission_line, make_construction_site, make_legacy_survey,
                 make_farm_fields, make_pacific, make_regional, make_city_buildings, make_messy_export):
        make()
