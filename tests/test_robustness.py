"""Mutation tests: damage valid files at random and check nothing crashes.

The readers sit on top of three parsers we did not write (zipfile, pyshp and
an XML parser), and each raises its own assortment of exceptions on corrupt
input. Hand-written tests only cover the failures someone thought of, so this
test generates a few hundred damaged files instead. The rule it enforces:

    whatever the bytes, the result is either a parsed file or a GeoFileError.

The seed is fixed, so a failure is reproducible.
"""

import io
import random
import zipfile

import pytest
from shapely.geometry import LineString

from app.geo import parse_upload
from app.geo.crs import resolve_source_crs
from app.geo.measure import measure_feature
from app.geo.types import GeoFileError, MeasurementStatus
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
)

ROUNDS = 300

VALID_FILES = {
    "site.kml": kml(
        placemark("Plot", polygon_xml(SQUARE)),
        placemark("Road", line_xml(LINE)),
        placemark("Marker", point_xml(77.59, 12.97)),
    ),
    "parcels.zip": shapefile_zip(
        [utm_box(780_000, 1_440_000, 100, 50), utm_box(780_200, 1_440_000, 200, 150)], prj=UTM_43N_PRJ
    ),
    "roads.zip": shapefile_zip([LineString(LINE)]),
}


def damage(data: bytes, rng: random.Random) -> bytes:
    """Flip, drop, repeat or blank out part of a byte string."""
    if len(data) < 2:
        return data
    buffer = bytearray(data)
    at = rng.randrange(len(buffer))
    mode = rng.choice(["flip", "truncate", "repeat", "blank"])
    if mode == "flip":
        for _ in range(rng.randint(1, 12)):
            buffer[rng.randrange(len(buffer))] = rng.randrange(256)
    elif mode == "truncate":
        buffer = buffer[: max(at, 1)]
    elif mode == "repeat":
        buffer = buffer[:at] + buffer[at : at + 50] * 3 + buffer[at:]
    else:
        buffer[at : at + rng.randint(1, 40)] = b"\x00" * rng.randint(1, 40)
    return bytes(buffer)


def damage_one_member(archive_bytes: bytes, rng: random.Random) -> bytes:
    """Damage a single file inside a zip while keeping the zip itself valid,
    so the corruption reaches the Shapefile and .prj parsers."""
    source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    victim = rng.choice(source.namelist())
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            target.writestr(name, damage(content, rng) if name == victim else content)
    return out.getvalue()


@pytest.mark.parametrize("filename", sorted(VALID_FILES))
def test_damaged_files_never_crash_the_pipeline(filename):
    rng = random.Random(20261007)
    original = VALID_FILES[filename]
    rejected = 0

    for _ in range(ROUNDS):
        if filename.endswith(".zip") and rng.random() < 0.7:
            data = damage_one_member(original, rng)
        else:
            data = damage(original, rng)

        try:
            parsed = parse_upload(filename, data)
        except GeoFileError:
            rejected += 1  # the only acceptable way to refuse a file
            continue

        # Anything that parsed must also resolve a CRS and measure without raising.
        crs, _warnings = resolve_source_crs(parsed, declared=None)
        for feature in parsed.features:
            assert isinstance(measure_feature(feature, crs).status, MeasurementStatus)

    assert rejected > 0, "the mutations were too gentle to test anything"
