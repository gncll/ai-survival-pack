#!/usr/bin/env bash
# Starts the AI Survival Pack: local model, Kiwix library (localhost only) and the web server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"; cd "$ROOT"
LIBRARY="${LIBRARY:-$ROOT/library}"; export LIBRARY
KIWIX_PORT="${KIWIX_PORT:-8081}"; export KIWIX_PORT
mkdir -p logs

if ! curl -fs -m 2 http://127.0.0.1:11434/api/version >/dev/null; then
  echo "Starting Ollama"
  nohup ollama serve > logs/ollama.log 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fs -m 2 http://127.0.0.1:11434/api/version >/dev/null && break
    sleep 1
  done
fi

shopt -s nullglob
zims=("$LIBRARY"/*.zim)
[ ${#zims[@]} -gt 0 ] || { echo "No ZIM files in $LIBRARY. Run scripts/download-content.sh first." >&2; exit 1; }

pkill -f "kiwix-serve .*-p $KIWIX_PORT" 2>/dev/null || true
sleep 0.5
# kiwix-serve can crash under load, so a small loop restarts it. -a and the kill -0 check stop it with the server.
# exec below keeps this PID, so $$ stays valid.
(
  while kill -0 $$ 2>/dev/null; do
    ./bin/kiwix-serve -i 127.0.0.1 -p "$KIWIX_PORT" -r /kiwix -b -a $$ "${zims[@]}" >> logs/kiwix.log 2>&1 || true
    sleep 1
  done
) &
echo "Serving ${#zims[@]} books"
exec python3 server/server.py
