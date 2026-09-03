# syntax=docker/dockerfile:1
#
# Task Hub — single-container image.
# Runs the FastAPI web application with the Radicale CalDAV server mounted
# inside it at /radicale, so there is only one process and one port to manage.
#
# Builds and runs identically on macOS, Windows (Docker Desktop) and Linux.

# The tunnel binary is lifted from Cloudflare's own image rather than
# downloaded, so the architecture always matches the image being built and
# there is no network fetch to fail. It is only used if remote access is
# switched on in Settings.
FROM cloudflare/cloudflared:latest AS cloudflared

# Obsidian's own headless Sync client, taken from the official npm package. It
# is a client rather than a server: it signs in to Obsidian Sync as another
# device and writes the vault out as plain markdown, which is all the Obsidian
# connector needs. Node is confined to this stage -- the runtime image gets the
# built CLI and a Node binary, not a toolchain.
FROM node:22-slim AS obsidian
RUN npm install -g obsidian-headless@0.0.14

FROM python:3.12-slim

# Keep Python predictable inside a container: no .pyc files, unbuffered logs
# so `docker compose logs` shows output immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TASKHUB_DATA_DIR=/data \
    # The Obsidian client keeps its login and sync database under here. Pointed
    # at the data volume so a rebuild does not sign the user out and re-download
    # the whole vault.
    XDG_CONFIG_HOME=/data/obsidian/config

WORKDIR /app

# Install dependencies first, in their own layer, so that editing application
# code does not trigger a full dependency reinstall on every rebuild.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

# The Node runtime and the Obsidian CLI, without the rest of the Node image.
COPY --from=obsidian /usr/local/bin/node /usr/local/bin/node
COPY --from=obsidian /usr/local/lib/node_modules/obsidian-headless \
     /usr/local/lib/node_modules/obsidian-headless
RUN ln -s /usr/local/lib/node_modules/obsidian-headless/cli.js /usr/local/bin/ob \
    && chmod +x /usr/local/lib/node_modules/obsidian-headless/cli.js

# Application code.
COPY app /app/app
COPY docs /app/docs

# The data directory holds the SQLite database, the encryption key and all
# Radicale collections. docker-compose mounts a named volume here so nothing
# is lost when the container is rebuilt.
RUN mkdir -p /data && chmod 700 /data
VOLUME ["/data"]

EXPOSE 8080

# A failing healthcheck makes `docker compose ps` show the problem instead of
# leaving you guessing why the page will not load.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# Single worker on purpose: the app calls its own embedded Radicale over HTTP,
# and blocking calls are dispatched to a threadpool, so one worker serves both
# sides without deadlocking. Scaling happens via the event loop, not processes.
# Forwarded headers are handled by the application itself rather than by
# uvicorn, because the application also decides whether to believe them: it can
# still see the true peer address, which uvicorn's own handling would have
# already overwritten. See app/web/forwarded.py.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
