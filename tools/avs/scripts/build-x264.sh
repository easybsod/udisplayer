#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
# Optional pinned x264 build for Linux distributions without libx264 headers.
# No system installation, FFmpeg deployment, or service restart is performed.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../manifests/sources.env
source "$ROOT/manifests/sources.env"
WORK_DIR="${1:-${HOME}/.cache/udisplayer-x264}"
JOBS="${JOBS:-4}"

for tool in git make gcc pkg-config; do
    command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done
if ! command -v nasm >/dev/null && ! command -v yasm >/dev/null; then
    echo "Missing assembler: nasm or yasm" >&2
    exit 1
fi
case "$JOBS" in ''|*[!0-9]*) echo "JOBS must be a positive integer" >&2; exit 1;; esac
if [ "$JOBS" -lt 1 ]; then echo "JOBS must be >= 1" >&2; exit 1; fi
if [ -e "$WORK_DIR/x264-src" ]; then
    echo "Source tree already exists: $WORK_DIR/x264-src; use a fresh work directory" >&2
    exit 1
fi
mkdir -p "$WORK_DIR"
git init -q "$WORK_DIR/x264-src"
git -C "$WORK_DIR/x264-src" remote add origin "$X264_URL"
git -C "$WORK_DIR/x264-src" fetch -q --depth=1 origin "$X264_COMMIT"
git -C "$WORK_DIR/x264-src" checkout -q --detach FETCH_HEAD
cd "$WORK_DIR/x264-src"
./configure --prefix="$WORK_DIR/install" --enable-static --enable-pic --disable-cli
make -j"$JOBS"
make install
PKG_CONFIG_PATH="$WORK_DIR/install/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
    pkg-config --modversion x264
printf '\nx264 installed into isolated prefix: %s\n' "$WORK_DIR/install"
printf 'No system binaries or services were changed.\n'
