#!/usr/bin/env bash
# Extracts offline vector maps from the latest Protomaps planet build (HTTP range requests, no full download).
# Defaults: world to zoom 8 (cities, 560 MB) + one region to zoom 14 (streets, Turkey = 860 MB).
# Change REGION_NAME and REGION_BBOX (west,south,east,north) for your own country.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
REGION_NAME="${REGION_NAME:-turkey}"
REGION_BBOX="${REGION_BBOX:-25.6,35.7,44.9,42.2}"
KEY=$(curl -fsSL https://build-metadata.protomaps.dev/builds.json | python3 -c "import json,sys;print(json.load(sys.stdin)[-1]['key'])")
SRC="https://build.protomaps.com/$KEY"
mkdir -p maps
[ -f maps/world.pmtiles ] || ./bin/pmtiles extract "$SRC" maps/world.pmtiles --maxzoom=8
[ -f "maps/$REGION_NAME.pmtiles" ] || ./bin/pmtiles extract "$SRC" "maps/$REGION_NAME.pmtiles" --bbox="$REGION_BBOX" --maxzoom=14
ls -la maps
