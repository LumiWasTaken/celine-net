#!/bin/sh
# A demo instance for screenshots: invented data in a fresh folder, every feature on and
# answering (CELINE_DEMO=1: no connector, box or ComfyUI is asked), the stub model server
# for live replies, dictation and read aloud. Nothing real is set up or reached.
#   tools/testdata/demo.sh [DIR] [PORT]     (default: ./demo-data, 8801)
# Ctrl+C stops both. Run it again for a fresh folder (DIR is replaced).
set -e
cd "$(dirname "$0")/../.."
dir=$(realpath -m "${1:-demo-data}")
port=${2:-8801}
stub=8900
rm -rf "$dir"
uv run python tools/testdata/model_stub.py --port $stub &
trap 'kill $! 2>/dev/null' EXIT INT TERM
uv run python tools/testdata/make.py "$dir/data" --models "http://127.0.0.1:$stub" --done
CELINE_DEMO=1 CELINE_DATA_DIR="$dir/data" ./start.sh --port "$port"
