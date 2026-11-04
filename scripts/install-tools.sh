#!/usr/bin/env bash
# Installs kiwix-serve and pmtiles into ./bin from their official releases (macOS arm64).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; BIN="$ROOT/bin"; mkdir -p "$BIN"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
if [ ! -x "$BIN/kiwix-serve" ]; then
  # Version-pinned. The default redirect mirror can crawl at ~20 KB/s, so try fast official mirrors first.
  v=3.8.2; f="kiwix-tools_macos-arm64-$v.tar.gz"
  for m in https://ftp.fau.de/kiwix https://mirrors.dotsrc.org/kiwix https://mirror.download.kiwix.org; do
    curl -fsSL --speed-time 20 --speed-limit 50000 -o "$T/kt.tar.gz" "$m/release/kiwix-tools/$f" && break
  done
  # Kiwix publishes only .md5. Fetched from the origin host, so it cross-checks the mirror.
  want=$(curl -fsSL -m 60 "https://download.kiwix.org/release/kiwix-tools/$f.md5" | awk '{print $1}')
  got=$(md5 -q "$T/kt.tar.gz")
  [ -n "$want" ] && [ "$want" = "$got" ] || { echo "kiwix-tools checksum mismatch: want=$want got=$got" >&2; exit 1; }
  tar -xzf "$T/kt.tar.gz" -C "$T"
  cp "$T"/kiwix-tools_*/kiwix-serve "$BIN/"
  xattr -d com.apple.quarantine "$BIN/kiwix-serve" 2>/dev/null || true
fi
if [ ! -x "$BIN/pmtiles" ]; then
  v=1.31.2
  curl -fsSL -o "$T/pm.zip" "https://github.com/protomaps/go-pmtiles/releases/download/v$v/go-pmtiles-${v}_Darwin_arm64.zip"
  unzip -q "$T/pm.zip" -d "$T/pm"
  cp "$T/pm/pmtiles" "$BIN/"
  xattr -d com.apple.quarantine "$BIN/pmtiles" 2>/dev/null || true
fi
"$BIN/kiwix-serve" --version | head -2
"$BIN/pmtiles" version 2>&1 | head -1
