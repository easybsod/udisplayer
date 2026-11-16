#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
# Build an isolated FFmpeg with AVS1-P16 AWQ support; never deploy automatically.
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../manifests/sources.env
source "$PROJECT_ROOT/manifests/sources.env"
# Reject accidental patch modifications without a corresponding manifest update.
(cd "$PROJECT_ROOT" && sha256sum --check manifests/patches.sha256 --status) || {
    echo "Patch checksum mismatch: verify changes and update manifests/patches.sha256" >&2
    exit 1
}
WORK_DIR="${1:-${HOME}/.cache/udisplayer-ffmpeg-avs}"
JOBS="${JOBS:-4}"

for tool in git make pkg-config python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "Missing tool: $tool" >&2; exit 1; }
done
if ! command -v nasm >/dev/null 2>&1 && ! command -v yasm >/dev/null 2>&1; then
    echo "An x86 assembler (nasm or yasm) is required for an optimized FFmpeg build." >&2
    exit 1
fi
if ! pkg-config --exists x264; then
    echo "libx264 development files are required (set PKG_CONFIG_PATH if custom built)." >&2
    exit 1
fi
# FFmpeg without a TLS provider silently drops the https protocol. The
# udisplayer startup capability check requires https even for HTTP-only TV
# playlists. GnuTLS is compatible with our GPL-enabled libx264 build.
if ! pkg-config --exists gnutls; then
    echo "GnuTLS development files are required for HTTPS (Rocky: gnutls-devel)." >&2
    echo "Set PKG_CONFIG_PATH for an isolated SDK or install the devel package." >&2
    exit 1
fi
case "$JOBS" in ''|*[!0-9]*) echo "JOBS must be an integer" >&2; exit 1;; esac
if [ "$JOBS" -lt 1 ]; then echo "JOBS must be >= 1" >&2; exit 1; fi

mkdir -p "$WORK_DIR"
if [ -e "$WORK_DIR/ffmpeg-src" ]; then
    echo "Refusing to reuse or overwrite existing source tree: $WORK_DIR/ffmpeg-src" >&2
    echo "Use a fresh work directory for a reproducible build." >&2
    exit 1
fi

git init -q "$WORK_DIR/ffmpeg-src"
git -C "$WORK_DIR/ffmpeg-src" remote add origin "$FFMPEG_URL"
git -C "$WORK_DIR/ffmpeg-src" fetch -q --depth=1 origin "$FFMPEG_COMMIT"
git -C "$WORK_DIR/ffmpeg-src" checkout -q --detach FETCH_HEAD
git init -q "$WORK_DIR/libcavs-src"
git -C "$WORK_DIR/libcavs-src" remote add origin "$LIBCAVS_URL"
git -C "$WORK_DIR/libcavs-src" fetch -q --depth=1 origin "$LIBCAVS_COMMIT"
git -C "$WORK_DIR/libcavs-src" checkout -q --detach FETCH_HEAD
cd "$WORK_DIR/ffmpeg-src"
git apply --check "$PROJECT_ROOT/patches/ffmpeg/avsplus-awq.patch"
git apply "$PROJECT_ROOT/patches/ffmpeg/avsplus-awq.patch"
# Generate the AVS+ AWQ model selectors from the upstream BSD-3-Clause
# reference at build time; do not vendor their code/tables in this repo.
python3 "$PROJECT_ROOT/scripts/extract_awq_models.py" \
    "$WORK_DIR/libcavs-src/src/codec/broadcast_quant.c" \
    "$WORK_DIR/ffmpeg-src/libavcodec/cavs_awq_models.inc"

extra=()
if [ "${WITH_LIBDAVS2:-0}" = 1 ]; then
    if ! pkg-config --exists davs2; then
        echo "WITH_LIBDAVS2=1 requires libdavs2 development files" >&2
        exit 1
    fi
    echo "WARNING: libdavs2 10-bit capability must be independently verified before use." >&2
    # davs2 is a C++ static archive. Carry its standard C++ runtime and
    # threading dependencies through FFmpeg's final executable link.
    extra+=(--enable-libdavs2 '--extra-libs=-lstdc++ -lpthread -lm')
fi
# Optional: fail rather than quietly omitting VA-API when a Linux deployment
# requires the GPU H.264 encoding path. Without this option FFmpeg can still
# run the AVS CPU decode + x264 CPU encode pipeline.
if [ "${WITH_VAAPI:-0}" = 1 ]; then
    if ! pkg-config --exists libva libva-drm libdrm; then
        echo "WITH_VAAPI=1 requires libva, libva-drm and libdrm development packages." >&2
        exit 1
    fi
    extra+=(--enable-vaapi --enable-libdrm)
fi
# GPLv3-compatible build for potential GPLv3 third-party AVS2 additions.
# This flag does not itself clear disputed copyright provenance.
./configure --prefix="$WORK_DIR/install" --enable-gpl --enable-version3 --enable-libx264 \
    --enable-gnutls --enable-parser=cavsvideo --disable-doc --disable-ffplay --disable-debug \
    "${extra[@]}"
make -j"$JOBS" ffmpeg ffprobe
verification=(--ffmpeg "$PWD/ffmpeg")
if [ "${WITH_LIBDAVS2:-0}" = 1 ]; then
    verification+=(--require-avs2)
fi
if [ "${WITH_VAAPI:-0}" = 1 ]; then
    verification+=(--require-vaapi)
fi
python3 "$PROJECT_ROOT/scripts/verify_ffmpeg.py" "${verification[@]}"
printf '\nFFmpeg built at: %s\n' "$WORK_DIR/ffmpeg-src/ffmpeg"
./ffmpeg -hide_banner -decoders | grep -E '(^ V.*(cavs|libdavs2))' || true
if ! ./ffmpeg -hide_banner -protocols | grep -Eq '^[[:space:]]*https[[:space:]]*$'; then
    echo "ERROR: built FFmpeg lacks HTTPS; refusing to report a deployable build" >&2
    exit 1
fi
sha256sum ffmpeg
printf '\nNo system binaries or services were changed.\n'
