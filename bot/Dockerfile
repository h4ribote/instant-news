# syntax=docker/dockerfile:1

# Image for the bot (the Discord bot).
#
# The build context is the repository root, not the bot/ directory this file lives in (see build.context in docker-compose.yml): server and bot share common/, and narrowing the context to bot/ would put common/ out of reach of COPY. Every COPY path is therefore written relative to the repository root.
#
# The bot has no volume for image blobs. server writes and serves the images and the bot only sends them to Discord, so it never needs to know where they are stored.

FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: keep code that runs read-only from writing .pyc.
# PYTHONUNBUFFERED: stream stdout instead of buffering it, since buffered output loses the last log lines when the container dies.
# PYTHONPATH: resolve common and bot as top-level packages.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install the dependencies first: as long as requirements.txt is unchanged, this layer is reused across source changes.
COPY bot/requirements.txt ./bot/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r bot/requirements.txt

COPY common/ ./common/
COPY bot/ ./bot/

# No reason to run as root. The sources stay root-owned, so the application cannot rewrite its own code.
RUN useradd --create-home --uid 10001 app
USER app

CMD ["python", "-m", "bot.main"]
