# discord-link: the Discord connector. Build from the repository root:
#   docker build -f connectors/discord/Dockerfile .
#
# Its state (the login, the journal) lives on the volume at /data,
# never in the image. Clients come from the clients file at run time
# (/data/connector-clients, the app's keys/connector-clients mounted by compose).

FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM python:3.14.7-slim-trixie AS build
COPY --from=uv /uv /bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY connectors/discord/pyproject.toml connectors/discord/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY connectors/discord/discord_link/ ./discord_link/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.14.7-slim-trixie
RUN useradd --uid 1000 --create-home celine && mkdir /data && chown celine:celine /data
COPY --link --from=build /app/.venv /app/.venv
USER celine
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
EXPOSE 8768

# healthy = the doors answer a known key (the first client in the clients file)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request as u; k = open('/data/connector-clients').read().replace('\\n', ',').split(',')[0].split(':')[1]; r = u.urlopen(u.Request('http://127.0.0.1:8768/admin/status', headers={'Authorization': 'Bearer ' + k}), timeout=4); sys.exit(0 if r.status == 200 else 1)"]

CMD ["discord-link", "--state", "/data", "--listen", "0.0.0.0:8768"]
