#!/usr/bin/env bash
# Start Celine.
#
#   ./start.sh              build the UI if it is stale, then serve everything
#                           from one process on http://127.0.0.1:8766
#   ./start.sh dev          two processes with hot reload, UI on :5174
#   ./start.sh --port 9000  override the port for this run (CELINE_PORT=9000 is the same;
#                           docker-compose.dev.yml reads that variable)
#
# First run installs dependencies. Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

MODE=serve
ARGS=()
for arg in "$@"; do
  case "$arg" in
    dev)  MODE=dev ;;
    *)    ARGS+=("$arg") ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
have uv  || { echo "celine: uv is not installed - https://docs.astral.sh/uv/"; exit 1; }
have npm || { echo "celine: npm is not installed"; exit 1; }

[ -d .venv ]          || { echo "installing python deps..."; uv sync --group dev; }
[ -d web/node_modules ] || { echo "installing web deps..."; (cd web && npm install); }

# Rebuild only when something under web/src has changed since the last bundle.
# Checking is cheap; a 1.2s build on every start is not.
stale() {
  [ -f server/celine/static/index.html ] || return 0
  [ -n "$(find web/src web/index.html web/vite.config.ts \
            -newer server/celine/static/index.html -print -quit 2>/dev/null)" ]
}

if [ "$MODE" = dev ]; then
  # --dev lets the API accept the Vite origin. Without it every request from
  # :5174 is correctly refused by the Origin guard and the app looks broken.
  echo "celine: api on :8766, ui on http://127.0.0.1:5174"
  trap 'kill 0' INT TERM EXIT
  uv run celine --dev "${ARGS[@]}" &
  (cd web && npm run dev) &
  wait
else
  if stale; then
    echo "building the ui..."
    (cd web && npm run build >/dev/null)
  fi
  # exec so Ctrl-C reaches the server rather than this script.
  exec uv run celine "${ARGS[@]}"
fi
