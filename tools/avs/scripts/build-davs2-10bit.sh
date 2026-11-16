#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
# Build an AVS2 10-bit decoder from pinned upstream code and patches.
# This script never installs to /usr or deploys to a live service.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../manifests/sources.env
source "$ROOT/manifests/sources.env"
# Reject accidental patch modifications without a corresponding manifest update.
(cd "$ROOT" && sha256sum --check manifests/patches.sha256 --status) || {
    echo "Patch checksum mismatch: verify changes and update manifests/patches.sha256" >&2
    exit 1
}
WORK_DIR="${1:-${HOME}/.cache/udisplayer-davs2-10bit}"
JOBS="${JOBS:-4}"

for tool in git make g++ pkg-config; do
    command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done
case "$JOBS" in ''|*[!0-9]*) echo "JOBS must be an integer" >&2; exit 1;; esac
if [ "$JOBS" -lt 1 ]; then echo "JOBS must be >= 1" >&2; exit 1; fi
if [ -e "$WORK_DIR/davs2-src" ]; then
    echo "Source tree already exists; refusing to overwrite: $WORK_DIR/davs2-src" >&2
    exit 1
fi
mkdir -p "$WORK_DIR"
git init -q "$WORK_DIR/davs2-src"
git -C "$WORK_DIR/davs2-src" remote add origin "$DAVS2_URL"
git -C "$WORK_DIR/davs2-src" fetch -q --depth=1 origin "$DAVS2_COMMIT"
git -C "$WORK_DIR/davs2-src" checkout -q --detach FETCH_HEAD

# Keep third-party patches outside this repository. Source them directly
# from their maintainer, pin the exact revision, verify every patch byte.
git init -q "$WORK_DIR/third-party-patches"
git -C "$WORK_DIR/third-party-patches" remote add origin "$NILAODA_URL"
git -C "$WORK_DIR/third-party-patches" fetch -q --depth=1 origin "$NILAODA_COMMIT"
git -C "$WORK_DIR/third-party-patches" checkout -q --detach FETCH_HEAD
(
    cd "$WORK_DIR/third-party-patches"
    sha256sum --check "$ROOT/manifests/davs2-upstream-patches.sha256"
)
cd "$WORK_DIR/davs2-src"
for patch_name in \
    0001-enable-10bit-build-and-propagate-frame-packet-position.patch \
    0012-x86-build-avx-codepaths-as-dispatch-only.patch \
    0013-x86-10bit-motion-compensation.patch
do
    patch="$WORK_DIR/third-party-patches/tools/patches/davs2-10bit/$patch_name"
    git apply --check "$patch"
    git apply "$patch"
done
# The following file contains only udisplayer's downstream changes.
# A zero-context delta prevents copying third-party patch context lines.
git apply --check --unidiff-zero "$ROOT/patches/davs2/main10-udisplayer.patch"
git apply --unidiff-zero "$ROOT/patches/davs2/main10-udisplayer.patch"
cd build/linux
./configure --prefix="$WORK_DIR/install" --enable-pic --bit-depth=10
if [ "${DAVS2_X86_V3:-0}" = 1 ]; then
    if [ "$(uname -m)" != x86_64 ]; then
        echo "DAVS2_X86_V3=1 requires x86_64" >&2
        exit 1
    fi
    # Explicitly opt in only on CPUs supporting the entire x86-64-v3 ISA.
    # This enables wider compiler vectorization in addition to the library's
    # existing runtime-dispatched hand-written AVX2 kernels.
    if ! grep -q -- '-fno-tree-vectorize' config.mak; then
        echo "Unexpected davs2 toolchain flags; refusing to change CFLAGS" >&2
        exit 1
    fi
    sed -i 's/-fno-tree-vectorize/-ftree-vectorize -march=x86-64-v3 -mtune=generic/' config.mak
    echo "Enabled optional x86-64-v3 compiler vectorization; incompatible CPUs may SIGILL"
fi
make -j"$JOBS"
make install-lib-static

g++ -std=c++11 -DHIGH_BIT_DEPTH=1 -DBIT_DEPTH=10 \
    -I. -I../../source -I../../source/common \
    "$ROOT/tests/skip-registry.cc" libdavs2.a -lpthread -lm \
    -o "$WORK_DIR/skip-registry-test"
"$WORK_DIR/skip-registry-test"

# Both the corrected SSE fallback and optional AVX2 SAO kernels must match
# a scalar reference, including 10-bit clipping and horizontal boundaries.
# On older CPUs the AVX2 cases are skipped at runtime by each test binary.
if [ "$(uname -m)" = x86_64 ]; then
    for suite in sao-bo-10bit sao-eo0-10bit; do
        g++ -std=c++11 -O2 -DHIGH_BIT_DEPTH=1 -DBIT_DEPTH=10 \
            -I. -I../../source -I../../source/common \
            "$ROOT/tests/$suite.cpp" libdavs2.a -lpthread -lm \
            -o "$WORK_DIR/$suite-test"
        "$WORK_DIR/$suite-test"
    done
fi

printf '\nAVS2 10-bit static lib: %s\n' "$WORK_DIR/install/lib/libdavs2.a"
printf 'Test decoder: %s\n' "$WORK_DIR/davs2-src/build/linux/davs2"
printf 'For FFmpeg configure: export PKG_CONFIG_PATH=%s/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}\n' "$WORK_DIR/install"
printf 'For opt-in software-only 50/60->~25/30 decode: UDISPLAYER_AVS2_SKIP_NONREF=1\n'
printf 'For experimental AVX2 SAO (not faster on sampled 4K50 channels): UDISPLAYER_AVS2_AVX2_SAO=1\n'
printf 'Nothing was deployed or installed to the system.\n'
