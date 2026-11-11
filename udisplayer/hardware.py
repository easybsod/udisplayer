"""Optional, independently probed Linux VA-API acceleration.

No driver names or environment variables are forced: libva chooses the
appropriate backend for the selected DRM render node.  A missing accelerator
must never prevent software playback.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import stat
import subprocess
from pathlib import Path

LOG = logging.getLogger("udisplayer")

RENDER_NODE = re.compile(r"/dev/dri/renderD\d+\Z")
RENDER_BY_PATH = re.compile(r"/dev/dri/by-path/pci-[0-9a-fA-F:.]+-render\Z")


def select_render_device(requested: str | None) -> str | None:
    """Select the first render node, 'none', or an explicit DRM render path."""
    if requested == "none":
        return None
    if requested is None:
        for node in sorted(Path("/dev/dri").glob("renderD*")):
            try:
                if RENDER_NODE.fullmatch(str(node)) and _accessible(node):
                    return str(node)
            except OSError:
                continue
        return None
    if not (RENDER_NODE.fullmatch(requested) or RENDER_BY_PATH.fullmatch(requested)):
        raise ValueError("--hw-render 必須是 none、/dev/dri/renderD* 或 /dev/dri/by-path/pci-*-render")
    node = Path(requested)
    if not _accessible(node):
        raise ValueError("--hw-render 指定的 DRM Render Node 不存在或無法存取")
    return requested


def _accessible(node: Path) -> bool:
    try:
        resolved = node.resolve(strict=True)
        info = resolved.stat()
        return (str(resolved).startswith("/dev/dri/renderD")
                and stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 226
                and os.access(node, os.R_OK | os.W_OK))
    except (OSError, RuntimeError):
        return False


def probe_vaapi(ffmpeg: str, render: str | None) -> tuple[bool, bool, str]:
    """Return (decoder_eligible, h264_encoder_verified, rate_control_mode).

    Real decode is input-codec-dependent and is validated by each worker;
    decoder_eligible means VA-API device initialization succeeded. Encoding is
    exercised with a real synthetic frame, not an -encoders listing.
    """
    if render is None:
        return False, False, "auto"
    # Some statically linked FFmpeg distributions abort instead of returning
    # an error when their late-bound libva-drm dependency is absent. Verify it
    # in-process before launching a potentially crashing probe executable.
    try:
        ctypes.CDLL("libva-drm.so.2")
    except OSError:
        LOG.warning("VA-API 已略過：找不到 libva-drm.so.2，使用軟體編解碼")
        return False, False, "auto"
    init = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-init_hw_device", f"vaapi=va:{render}", "-filter_hw_device", "va"]

    def attempt(args: list[str], *, quiet: bool = False) -> bool:
        try:
            result = subprocess.run(init + args, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("VA-API 探測無法完成：%s", type(exc).__name__)
            return False
        if result.returncode:
            reason = result.stderr.decode("utf-8", errors="replace").splitlines()
            if not quiet:
                LOG.warning("VA-API 探測未通過：%s", (reason[-1] if reason else "FFmpeg exit")[:200])
            return False
        return True

    device_ok = attempt(["-f", "lavfi", "-i", "color=c=black:s=128x128:r=1",
                         "-frames:v", "1", "-f", "null", "-"])
    if not device_ok:
        return False, False, "auto"
    encoder_args = ["-f", "lavfi", "-i", "color=c=black:s=128x128:r=1",
                    "-vf", "format=nv12,hwupload", "-frames:v", "1",
                    "-c:v", "h264_vaapi", "-profile:v", "high", "-bf", "0",
                    "-b:v", "2000000", "-maxrate:v", "2200000",
                    "-bufsize:v", "2200000"]
    vbr = attempt(encoder_args + ["-rc_mode", "VBR", "-f", "null", "-"], quiet=True)
    encoder_ok = vbr or attempt(encoder_args + ["-f", "null", "-"])
    rc_mode = "VBR" if vbr else "auto"
    LOG.info("VA-API device=%s device_ok=%s h264_encode=%s rc_mode=%s",
             render, device_ok, encoder_ok, rc_mode)
    return device_ok, encoder_ok, rc_mode


