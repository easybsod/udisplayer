# Third-party notices

- **hls.js 1.7.3**, Copyright (c) 2017 Dailymotion and contributors, Apache-2.0.
  Source: https://github.com/video-dev/hls.js/tree/v1.7.3
  Distribution: `vendor/hls.min.js`; archive integrity in `vendor/lock.json`.
  The complete license is in `licenses/hls.js.txt` and in the generated HTML.
- **CPython**, Python Software Foundation License Version 2 and included notices.
  Binary releases include the interpreter selected by the build environment.
  Its complete installed license is provided in `licenses/python.txt`.
- **PyInstaller 6.22.3**, GPL-2.0-or-later with the bootloader exception permitting
  distribution of frozen applications under their own license.
  Source: https://github.com/pyinstaller/pyinstaller/tree/v6.22.3
  The complete installed license is provided in `licenses/pyinstaller.txt`.
- **Pillow 12.3.0**, MIT-CMU. Used for optional PNG Logo resizing and optimization;
  official frozen builds include it so this optimization is always available.
  Source: https://github.com/python-pillow/Pillow/tree/12.3.0
  Frozen builds embed Pillow's installed license under `licenses/pillow/`.

## AVS codec sources and copyright notices

The udisplayer player invokes an independently installed FFmpeg executable.
The repository contains **udisplayer's own differential patches**, scripts
and documentation under `tools/avs/`; it does **not** bundle FFmpeg,
davs2, x264, the third-party davs2 AVX2 patches or the libcavs model table.

The patches modify upstream code and retain the corresponding upstream
copyright obligations. They are **not** relicensed by the top-level MIT
license, which applies to udisplayer's own original code.

- **FFmpeg contributors**: the AVS+ patch changes FFmpeg `libavcodec/cavs.c`,
  `cavs.h` and `cavsdec.c`, whose upstream headers state
  LGPL-2.1-or-later. Upstream: https://github.com/FFmpeg/FFmpeg ;
  legal guidance: https://ffmpeg.org/legal.html
- **libcavs contributors**: Copyright (c) 2026 libcavs contributors.
  AVS+ adaptive-weighted-quantization matrix reference, BSD-3-Clause,
  retrieved at build time from its pinned source revision;
  source: https://github.com/xeonliu/libcavs
- **VCL/NELVT, Peking University, and davs2 contributors**:
  upstream davs2 C/C++ source headers state GPL-2.0-or-later;
  sources: https://github.com/pkuvcl/davs2 and
  https://github.com/xatabhk/davs2-10bit
- **nilaoda and respective contributors**: AVS2 x86 AVX2 motion
  compensation and associated third-party fixes are **downloaded at build
  time**, rather than copied into this repository, from
  https://github.com/nilaoda/mpv-iina-avs/pull/32 and
  https://github.com/nilaoda/FFmpegSharedLibraries .
  Both reference repositories include GPLv3 license files. The
  `mpv-iina-avs` README also states research/non-commercial restrictions
  for some decoder sources: users must check whether these restrictions
  apply to the precise patches and their intended use. Fetching the
  upstream content does not grant rights that its owner has not granted.
  All applicable original copyright notices and conditions remain in effect.
- **VideoLAN x264 contributors**: the optional external build dependency
  uses GPL-2.0-or-later or other licenses granted by the holders;
  https://www.videolan.org/developers/x264.html
- **GnuTLS contributors**: an external HTTPS build dependency with its own
  license terms; https://www.gnutls.org/

Complete reference texts are in `LICENSES/`:
`LGPL-2.1-or-later.txt`, `GPL-2.0-or-later.txt`,
`GPL-3.0-only.txt`, `BSD-3-Clause.txt`, `MIT.txt`.
The source revisions are pinned in `tools/avs/manifests/sources.env`,
and original davs2 patch hashes in
`tools/avs/manifests/davs2-upstream-patches.sha256`.
The presence of a license text in this repository does not itself grant
any missing third-party redistribution permission.
