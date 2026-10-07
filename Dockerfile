FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    GEO_DATABASE_URL=sqlite:////data/geomeasure.db

WORKDIR /app

# Exact, tested versions (see requirements.lock), so a rebuild months later
# installs the same libraries. This layer is cached until the lock changes.
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY app ./app

# Run as an unprivileged user; /data holds the SQLite file.
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
