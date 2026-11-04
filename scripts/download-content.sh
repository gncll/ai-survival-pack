#!/usr/bin/env bash
# Downloads the newest version of each ZIM in config/content.conf. Re-run to resume.
# Stops before the disk drops under MIN_FREE_GB. Point LIBRARY at an external SSD for the big set.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="${1:-$ROOT/config/content.conf}"
DEST="${LIBRARY:-$ROOT/library}"
MIN_FREE_GB="${MIN_FREE_GB:-12}"
BASE=https://download.kiwix.org/zim
mkdir -p "$DEST"
failed=0
free_gb(){ df -g "$DEST" | awk 'NR==2{print $4}'; }

while read -r folder prefix _; do
  case "$folder" in ''|\#*) continue;; esac
  latest=$(curl -fsSL "$BASE/$folder/" | grep -oE "${prefix}_[0-9]{4}-[0-9]{2}[a-z]?\.zim" | sort -u | tail -1)
  if [ -z "$latest" ]; then echo "!! $prefix not found in $BASE/$folder/"; failed=1; continue; fi
  if [ -f "$DEST/$latest" ]; then echo "== $latest already present"; continue; fi

  size=$(curl -fsSIL "$BASE/$folder/$latest" | awk 'tolower($1)=="content-length:"{v=$2} END{gsub(/\r/,"",v); print v+0}')
  need=$(( size / 1000000000 + 1 ))
  if [ $(( $(free_gb) - need )) -lt "$MIN_FREE_GB" ]; then
    echo "!! skipping $latest: needs ${need} GB, only $(free_gb) GB free (floor ${MIN_FREE_GB} GB)"; failed=1; continue
  fi

  echo ">> $latest ($(( size / 1000000 )) MB), $(free_gb) GB free"
  if curl -fL -C - --retry 5 --retry-delay 5 -sS -o "$DEST/$latest.part" "$BASE/$folder/$latest"; then
    mv "$DEST/$latest.part" "$DEST/$latest"
    echo "OK $latest"
    for old in "$DEST/${prefix}"_*.zim; do
      [ "$old" != "$DEST/$latest" ] && [ -f "$old" ] && { echo "-- removing old $old"; rm -f "$old"; }
    done
  else
    echo "!! $latest failed, re-run to resume"; failed=1
  fi
done < "$CONF"
echo ">> done. $(free_gb) GB free"
exit $failed
