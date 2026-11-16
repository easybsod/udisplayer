#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
"""Verify an external AVS-capable FFmpeg against udisplayer's real requirements.

This tool does not install binaries, modify services, or read IPTV URLs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from udisplayer.config import resolve_ffmpeg, validate_ffmpeg  # noqa: E402


def run(binary: str, *args: str, timeout: int = 15) -> str:
    result = subprocess.run([binary, *args], capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        tail = (result.stderr or result.stdout).strip()[-1000:]
        raise ValueError(f"FFmpeg 執行失敗（exit={result.returncode}）：{tail}")
    return result.stdout + result.stderr


def ensure_listing(binary: str, flag: str, name: str) -> None:
    output = run(binary, "-hide_banner", flag)
    # FFmpeg -decoders/-parsers/-encoders each start a row with flags,
    # followed by the codec name. Avoid false matches in descriptions.
    if not re.search(r"(?m)^\s*[A-Z.]{0,6}\s+" + re.escape(name) + r"\s", output):
        raise ValueError(f"FFmpeg {flag} 缺少 {name}")


def check(ffmpeg: str, *, require_avs2: bool = False,
          require_vaapi: bool = False, vaapi_device: str | None = None,
          sample_ts: Path | None = None) -> str:
    binary = resolve_ffmpeg(ffmpeg)
    version = validate_ffmpeg(binary)
    buildconf = run(binary, "-hide_banner", "-buildconf")
    if "--enable-nonfree" in buildconf:
        raise ValueError("檢測到 --enable-nonfree；請先人工確認授權與散布限制")
    if "--enable-gpl" not in buildconf or "--enable-libx264" not in buildconf:
        raise ValueError("預期啟用 --enable-gpl 和 --enable-libx264")
    if "--enable-version3" not in buildconf:
        raise ValueError("AVS 研究建置須啟用 --enable-version3；GPLv3 衍生修改不可使用 GPLv2-only 組合")
    if "--enable-gnutls" not in buildconf:
        raise ValueError("預期啟用 GnuTLS；請檢查 HTTPS 建置策略")
    # FFmpeg CLI has no -parsers listing option; inspect the pinned build flag.
    if "--enable-parser=cavsvideo" not in buildconf:
        raise ValueError("預期 --enable-parser=cavsvideo；FFmpeg 未按 AVS+ 建置配方編譯")

    ensure_listing(binary, "-decoders", "cavs")
    if require_avs2:
        if "--enable-libdavs2" not in buildconf:
            raise ValueError("AVS2 10-bit 建置缺少 --enable-libdavs2")
        ensure_listing(binary, "-decoders", "libdavs2")

    if require_vaapi or vaapi_device is not None:
        ensure_listing(binary, "-encoders", "h264_vaapi")
        ensure_listing(binary, "-filters", "scale_vaapi")
        if "vaapi" not in run(binary, "-hide_banner", "-hwaccels").split():
            raise ValueError("FFmpeg 缺少 VA-API 硬體裝置支援")
    if vaapi_device is not None:
        if not Path(vaapi_device).exists():
            raise ValueError(f"VA-API 裝置不存在：{vaapi_device}")
        run(binary, "-nostdin", "-hide_banner", "-v", "error",
            "-init_hw_device", f"vaapi=va:{vaapi_device}",
            "-filter_hw_device", "va", "-f", "lavfi", "-i",
            "testsrc2=size=640x360:rate=10", "-vf", "format=nv12,hwupload",
            "-frames:v", "10", "-c:v", "h264_vaapi", "-f", "null", "-",
            timeout=30)

    if sample_ts is not None:
        if not sample_ts.is_file():
            raise ValueError(f"測試檔不存在：{sample_ts}")
        output = run(binary, "-nostdin", "-hide_banner", "-v", "error",
            "-progress", "pipe:1", "-nostats",
            "-probesize", "4000000", "-analyzeduration", "4000000",
            "-threads", "4", "-i", str(sample_ts), "-map", "0:v:0",
            "-an", "-frames:v", "50", "-f", "null", "-", timeout=120)
        counts = [int(frame) for frame in re.findall(r"(?m)^frame=(\d+)\s*$", output)]
        if not counts or max(counts) < 50:
            raise ValueError("樣本沒有成功解出 50 幀；請確認使用完整有效的 AVS 來源")

    return version


def main() -> int:
    parser = argparse.ArgumentParser(description="驗證外部 FFmpeg 的 udisplayer／AVS 相容性")
    parser.add_argument("--ffmpeg", required=True, help="已編譯 FFmpeg 可執行檔")
    parser.add_argument("--require-avs2", action="store_true", help="額外要求 libdavs2 解碼器")
    parser.add_argument("--require-vaapi", action="store_true", help="檢查 VA-API 編碼器／濾鏡（不存取 GPU）")
    parser.add_argument("--vaapi-device", help="選擇性執行真實 VA-API H.264 硬編測試")
    parser.add_argument("--sample-ts", type=Path, help="選擇性用本地合法樣本解碼 50 幀")
    args = parser.parse_args()
    try:
        version = check(args.ffmpeg, require_avs2=args.require_avs2,
                        require_vaapi=args.require_vaapi,
                        vaapi_device=args.vaapi_device, sample_ts=args.sample_ts)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"驗證失敗：{exc}", file=sys.stderr)
        return 1
    print(f"驗證通過：{version}")
    print("AVS+ AWQ 與 AVS2 Main10 的實際碼流正確性仍需由來源樣本另外驗證。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
