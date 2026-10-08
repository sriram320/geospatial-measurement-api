"""Application entry point: ``uvicorn app.main:app``."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import files
from app.api.errors import ApiError, api_error_handler
from app.db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

UPLOAD_PAGE = Path(__file__).parent / "static" / "index.html"

DESCRIPTION = """
**[Open the upload page](/)** to upload a file in the browser and see the results there.

Upload a **KML** file or a **zipped Shapefile** and get back the area of every
polygon and the length of every line, in metres.

Each value is calculated in a projected CRS chosen for that feature and then
cross-checked against a geodesic calculation on the WGS84 ellipsoid. The
response states which CRS was used and why, how far the two methods disagree,
and how much of that gap the projection's own scale factor predicts.

`GET /api/files/{id}/report/` summarises a whole file: projections used, the
worst deviation, and failures grouped by reason.

Upload with `?async=true` to get `202` at once and poll the file's status.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Geospatial File Measurement API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
app.add_exception_handler(ApiError, api_error_handler)
app.include_router(files.router)


@app.get("/", include_in_schema=False)
def upload_page() -> FileResponse:
    return FileResponse(UPLOAD_PAGE, media_type="text/html")


@app.get("/health", tags=["system"], summary="Liveness check")
def health() -> dict:
    return {"status": "ok"}
