#!/usr/bin/env bash
# One command setup. Re-run it if a download stops, it continues where it left off.
# Downloads about 16 GB. Change what you get in config/content.conf.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"; cd "$ROOT"
MODEL="${SURVIVAL_MODEL:-gemma4:e2b-it-qat}"
[ "$(uname -sm)" = "Darwin arm64" ] || echo "Warning: tested on Apple Silicon Macs only. scripts/install-tools.sh downloads Mac binaries."
command -v ollama >/dev/null || { echo "Install Ollama first: https://ollama.com" >&2; exit 1; }
curl -fs -m 2 http://127.0.0.1:11434/api/version >/dev/null || { nohup ollama serve >/dev/null 2>&1 & sleep 3; }

echo "== 1/5 Local AI model"
ollama pull "$MODEL"
echo "== 2/5 Tools"
./scripts/install-tools.sh
echo "== 3/5 Library"
./scripts/download-content.sh "${CONTENT_CONF:-config/content.conf}" || echo "Some books were skipped. See the lines above."
echo "== 4/5 Maps"
[ "${SKIP_MAPS:-0}" = "1" ] || ./scripts/download-maps.sh
echo "== 5/5 PDF search index"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip -q install libzim pymupdf
.venv/bin/python scripts/build-pdf-index.py "${LIBRARY:-library}" 2>/dev/null | tail -1 || true
echo "Done. Start it with ./start.sh and open http://localhost:8080"
