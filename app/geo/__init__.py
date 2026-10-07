"""Geospatial core: file readers, CRS handling and the measurement engine.

This package has no knowledge of HTTP or the database.
"""

import logging
from pathlib import PurePath

from app.geo.kml_reader import read_kml
from app.geo.shp_reader import read_zip
from app.geo.types import GeoFileError, ParsedFile

logger = logging.getLogger(__name__)

# Extension -> reader. Supporting a new format means adding one line here.
READERS = {
    ".kml": read_kml,
    ".zip": read_zip,  # zipped Shapefile
    ".kmz": read_zip,  # a KMZ is a zip with a KML inside
}
SUPPORTED_EXTENSIONS = tuple(READERS)


def parse_upload(filename: str, data: bytes) -> ParsedFile:
    reader = READERS.get(PurePath(filename).suffix.lower())
    if reader is None:
        raise GeoFileError("unsupported_file_type", f"Supported file types: {', '.join(SUPPORTED_EXTENSIONS)}.")
    try:
        return reader(data)
    except GeoFileError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Safety net. The readers turn the failures they know about into
        # precise GeoFileErrors, but zipfile, pyshp and the XML parser can
        # raise almost anything on corrupt bytes (mutation testing turned up
        # KeyError, ValueError and NotImplementedError). An unreadable upload
        # is the client's problem (4xx), never a server crash (500). The log
        # keeps the traceback in case the cause is a bug of ours.
        logger.exception("Reader crashed on %s", filename)
        raise GeoFileError("unreadable_file", f"The file could not be read ({type(exc).__name__}).") from exc
