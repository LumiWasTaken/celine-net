# celine-net, the app: the server and its web UI. See docs/DEPLOY.md.
#
# Four stages: the web bundle (node), the Python environment (uv), the vault's bw, and
# the runtime, which carries neither node nor uv. Each stage's input is only what it
# builds from (web/, the lock), and the runtime takes the code with COPY --link and has
# nothing after it but settings (no RUN, no WORKDIR): a code change rebuilds no stage, and
# a builder with only a registry cache (CI) needs none of the earlier layers' content for
# it. The commit is the last layer. The connectors have images of their own
# (connectors/discord/Dockerfile, wa-link/Dockerfile). Model servers are added in the
# app's Settings, so nothing here names a machine.

# ---- web bundle ----------------------------------------------------------------------
FROM node:22.23.3-trixie-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web/ ./
# the whole build: the useReveal check, eslint and tsc first, then vite
RUN npm run build -- --outDir /out/static --emptyOutDir

# ---- python environment --------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM python:3.14.7-slim-trixie AS build
COPY --from=uv /uv /bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# the dependencies only: the code is on PYTHONPATH (runtime), so a code change installs nothing
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

# ---- the vault's bw (server/celine/vault): the Bitwarden CLI, pinned by its sha256 ---
FROM python:3.14.7-slim-trixie AS bw
ARG BW_VERSION=2026.9.1
ARG BW_SHA256=84289eaf59d691f04242605112f21b00e8dba65acdedbc60dde001a2860f2097
RUN python -c "import hashlib, io, os, urllib.request, zipfile; \
v = os.environ['BW_VERSION']; \
data = urllib.request.urlopen(f'https://github.com/bitwarden/clients/releases/download/cli-v{v}/bw-linux-{v}.zip').read(); \
assert hashlib.sha256(data).hexdigest() == os.environ['BW_SHA256'], 'bw: the sha256 does not match'; \
zipfile.ZipFile(io.BytesIO(data)).extract('bw', '/out')" && chmod 755 /out/bw

# ---- runtime -------------------------------------------------------------------------
FROM python:3.14.7-slim-trixie
RUN useradd --uid 1000 --create-home celine && mkdir /data && chown celine:celine /data
COPY --link --from=bw /out/bw /usr/local/bin/bw
WORKDIR /app
COPY --link --from=build /app/.venv ./.venv
COPY --link --from=web /out/static/ ./server/celine/static/
# `celine` (pyproject's script) without installing the code into the environment
COPY --link --chmod=755 <<'SH' /usr/local/bin/celine
#!/bin/sh
exec python -m celine "$@"
SH

# Started as root on the whole volume: the folders the other services mount (a service
# mounts only a folder that exists), its home given to its uid, then the app as celine,
# which keeps no capability. docker-compose.yml waits on the health check, which answers
# only after the app made its keys.
COPY --link --chmod=755 <<'SH' /usr/local/bin/docker-entrypoint
#!/bin/sh
set -eu
as_celine="setpriv --reuid=celine --regid=celine --init-groups env HOME=/home/celine"
$as_celine mkdir -p /data/app /data/shellbox /data/discord-link /data/wa-link /data/reranker
uid=${HER_UID:-1000}   # docker-compose.yml names it (x-her-uid)
chown "$uid:$uid" /data/shellbox
exec $as_celine "$@"
SH
ENTRYPOINT ["docker-entrypoint"]
# the code last: a --link layer after a changed one makes a builder fetch the layers below
COPY --link config/ ./config/
COPY --link server/ ./server/

# Data lives on a volume. The server binds all interfaces, so the passkey login is on
# (with it off, startup refuses any address but loopback). The health check asks the
# health port, which listens on the container's loopback only, as does the metrics
# port (server/celine/metrics.py). The connectors run in
# their own containers, so this app never starts a copy of them (empty *_COMMAND).
ENV CELINE_DATA_DIR=/data/app \
    CELINE_HOST=0.0.0.0 \
    CELINE_PORT=8766 \
    CELINE_LOGIN=on \
    CELINE_HEALTH_PORT=8765 \
    CELINE_METRICS_PORT=8772 \
    DISCORD_LINK_COMMAND="" \
    WA_LINK_COMMAND="" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/server \
    PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4).status == 200 else 1)"]

CMD ["celine"]

# the commit and the time of this build, for the rail's brand mark (GET /api/status
# `build`); CI passes both, a local build has neither
ARG CELINE_COMMIT=dev
ARG CELINE_BUILT=
ENV CELINE_COMMIT=$CELINE_COMMIT CELINE_BUILT=$CELINE_BUILT
