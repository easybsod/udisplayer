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

FFmpeg is executed as a separate, user-installed program. Neither FFmpeg nor
ffprobe is included in the HTML or executable distribution.
