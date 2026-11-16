#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
# Non-mutating source layout, script syntax and patch integrity check.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
bash -n scripts/build-ffmpeg.sh scripts/build-davs2-10bit.sh scripts/build-x264.sh
# Do not create bytecode or other generated artifacts inside the source tree.
python3 - <<'PY'
from pathlib import Path
for f in ('tests/compare-avs2.py', 'scripts/extract_awq_models.py', 'scripts/verify_ffmpeg.py',
          'scripts/analyze_mpegts.py'):
    compile(Path(f).read_bytes(), f, 'exec')
PY
# Source content should remain in upstream repos or isolated build directories.
for unexpected in ffmpeg ffprobe davs2 libavcodec libavformat source .work artifacts; do
    if [ -e "$unexpected" ]; then
        echo "Unexpected vendored source/binary directory: $unexpected" >&2
        exit 1
    fi
done
if grep -n -- '--enable-nonfree' scripts/build-ffmpeg.sh; then
    echo "Forbidden nonfree FFmpeg flag in build script" >&2
    exit 1
fi
if [ ! -f manifests/sources.env ]; then
    echo "Missing source pin manifest" >&2
    exit 1
fi
# Verify patch bytes match the pinned manifest.
sha256sum -c manifests/patches.sha256 >/dev/null
# Verify named patches and pinned source fields exist.
for f in patches/ffmpeg/avsplus-awq.patch patches/davs2/main10-udisplayer.patch; do
    test -s "$f" || { echo "Missing patch: $f" >&2; exit 1; }
done
# The third-party patch hashes are pinned separately, without storing
# their contents in this repository.
test -s manifests/davs2-upstream-patches.sha256 || {
    echo "Missing third-party patch checksum manifest" >&2
    exit 1
}
printf 'OK: AVS source layout, shell syntax and patch hashes verified.\n'
