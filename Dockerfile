# syntax=docker/dockerfile:1
# S-CCD monitor image. Build:  docker build -t forestmon .
# Run:    docker run --rm --env-file .env -v "$PWD/data:/data" forestmon run <aoi>
# See docs/deployment.md. CDSE keys are passed at run time, never baked in.
#
# `ogr2ogr` (GDAL >= 3.8 with the PMTiles driver) is NOT installed: it could not
# be verified from apt in this base, so `publish site` needs a derived image or
# FORESTMON_GDAL_BIN pointing at a mounted GDAL. Everything else works.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
# git: aef-loader-plus is a pinned git dependency.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# Dependency layer: cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Project layer. README.md is the declared package readme.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 forestmon \
    && mkdir -p /data/aoi /data/logs \
    && chown -R forestmon:forestmon /data
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FORESTMON_DATA_DIR=/data \
    FORESTMON_AOI_DIR=/data/aoi \
    FORESTMON_LOG_DIR=/data/logs
USER forestmon
WORKDIR /data
VOLUME ["/data"]
ENTRYPOINT ["forestmon"]
CMD ["--help"]
