# syntax=docker/dockerfile:1

# Image for server (FastAPI).
#
# The build context is the repository root, not the server/ directory this file lives in (see build.context in docker-compose.yml): server and bot share common/, and narrowing the context to server/ would put common/ out of reach of COPY. Every COPY path is therefore written relative to the repository root.
#
# Stages: base holds the dependencies and the application; test adds pytest and tests/ (docker-compose.test.yml builds it); runtime is the last stage, so a build without a target produces the production image, which carries no test code.

FROM python:3.12-slim AS base

# PYTHONDONTWRITEBYTECODE: keep code that runs read-only from writing .pyc.
# PYTHONUNBUFFERED: stream stdout instead of buffering it, since buffered output loses the last log lines when the container dies.
# PYTHONPATH: resolve common and server as top-level packages.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install the dependencies first: as long as requirements.txt is unchanged, this layer is reused across source changes.
COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r server/requirements.txt

# Every ingested image is decoded and re-encoded to WebP, so a Pillow that cannot handle WebP leaves this service unable to accept a single image. Pillow's wheel bundles libwebp, so the slim image needs no extra OS packages; but where no wheel is available Pillow is built from source and can land without libwebp — importable, yet WebP alone unusable. Fail at build time instead of the moment a user posts an image.
RUN python -c "from PIL import features; assert features.check('webp'), 'Pillow が WebP を扱えません(libwebp を欠いた状態でビルドされています)'"

COPY common/ ./common/
COPY server/ ./server/

# Directory holding the image blobs, kept in step with the default of media.storage_dir in config.yaml. At runtime a named volume is mounted over it (docker-compose.yml). Shipping the directory with its owner already set makes Docker inherit that owner when it first creates the volume, so the non-root user can write to the volume.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /var/lib/instant-news/media \
 && chown -R app:app /var/lib/instant-news

# The integration tests, run against a throwaway MySQL by docker-compose.test.yml.
FROM base AS test

COPY tests/requirements.txt ./tests/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r tests/requirements.txt
COPY tests/ ./tests/

USER app

# The cache provider is off because /app is root-owned and not writable by the app user.
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "tests"]

# The production image.
FROM base AS runtime

# No reason to run as root. The sources stay root-owned, so the application cannot rewrite its own code.
USER app

# No EXPOSE: the listen port is decided by server.port in config.yaml, so a number written here becomes a lie the moment that config changes. Publishing the port is docker-compose.yml's ports.
# The listen address and port likewise have config.yaml as their single source of truth, so do not pin them in the start command. server/main.py reads the config and starts uvicorn.
CMD ["python", "-m", "server.main"]
