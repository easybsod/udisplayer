from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


CHANNEL_ID = re.compile(r"[1-9]\d{0,8}\Z")
LOGO_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
PREFIX_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+\Z")
UNGROUPED = "未分組"


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    url: str
    group: str | None = None
    logo: str | None = None


@dataclass(frozen=True)
class ChannelConfig:
    channels: tuple[Channel, ...]
    groups: tuple[str, ...]
    grouping_enabled: bool


@dataclass(frozen=True)
class Settings:
    ffmpeg: str
    prefix: str = ""
    host: str = "0.0.0.0"
    port: int = 8081
    startup_timeout: float = 30
    idle_timeout: float = 60
    max_streams: int = 4
    encoder_threads: int = 2
    cache_dir: Path | None = None
    debug: bool = False
    hw_render: str | None = None
    hw_decode: bool = False
    hw_encode: bool = False
    hw_rc_mode: str = "auto"


def normalize_prefix(value: str) -> str:
    if not value or value == "/":
        return ""
    parts = value.strip("/").split("/")
    if any(p in ("", ".", "..") or not PREFIX_SEGMENT.fullmatch(p) for p in parts):
        raise ValueError("--path 必須是 URL 路徑；每段限英數字、-、_、.、~，不可包含 . 或 .. 路徑段")
    return "/" + "/".join(parts)


def read_config(path: Path) -> ChannelConfig:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("頻道配置不可超過 1 MiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise ValueError("配置必須包含 channels 陣列")

    declared_groups = data.get("groups")
    if declared_groups is not None:
        if not isinstance(declared_groups, list):
            raise ValueError("groups 必須是陣列")
        normalized_groups = []
        for group in declared_groups:
            if not isinstance(group, str) or not group.strip() or len(group.strip()) > 128:
                raise ValueError("groups 包含無效名稱")
            group = group.strip()
            if group in normalized_groups:
                raise ValueError(f"分組名稱重複：{group}")
            normalized_groups.append(group)
    else:
        normalized_groups = []

    result, seen = [], set()
    discovered_groups = []
    has_channel_group = False
    for index, entry in enumerate(data["channels"], 1):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {index} 個頻道必須是物件")
        cid, name, url = (entry.get(k) for k in ("id", "name", "url"))
        group, logo = entry.get("group"), entry.get("logo")
        if not isinstance(cid, int) or isinstance(cid, bool) or not CHANNEL_ID.fullmatch(str(cid)) or cid in seen:
            raise ValueError(f"第 {index} 個頻道的 id 必須是唯一正整數")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 256:
            raise ValueError(f"頻道 {cid} 缺少有效名稱")
        if group is not None:
            if not isinstance(group, str) or not group.strip() or len(group.strip()) > 128:
                raise ValueError(f"頻道 {cid} 的 group 無效")
            group = group.strip()
            has_channel_group = True
            if group not in discovered_groups:
                discovered_groups.append(group)
        if logo is not None:
            if not isinstance(logo, str) or not LOGO_FILE.fullmatch(logo):
                raise ValueError(f"頻道 {cid} 的 logo 必須是 logos 目錄中的安全檔名")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = (parsed and parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.fragment and not any(c.isspace() or ord(c) < 32 for c in url))
            if parsed:
                _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"頻道 {cid} 需要有效的 HTTP／HTTPS 串流網址")
        seen.add(cid)
        result.append(Channel(cid, name.strip(), url, group, logo))

    grouping_enabled = bool(normalized_groups or has_channel_group)
    groups = list(normalized_groups)
    if grouping_enabled:
        for group in discovered_groups:
            if group not in groups:
                groups.append(group)
        if any(channel.group is None for channel in result) and UNGROUPED not in groups:
            groups.append(UNGROUPED)
        result = [Channel(c.id, c.name, c.url, c.group or UNGROUPED, c.logo) for c in result]
    return ChannelConfig(tuple(result), tuple(groups), grouping_enabled)


def resolve_ffmpeg(explicit: str | None) -> str:
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError("--ffmpeg 指定的檔案不存在或不可執行；不會改用 PATH 中的其他版本")
        return str(candidate)
    found = shutil.which("ffmpeg")
    if not found:
        raise ValueError("找不到 FFmpeg；請安裝 FFmpeg，或使用 --ffmpeg 指定執行檔路徑")
    return str(Path(found).resolve())


def validate_ffmpeg(binary: str) -> str:
    def inspect(flag: str) -> str:
        try:
            result = subprocess.run([binary, "-hide_banner", flag], capture_output=True,
                                    text=True, timeout=10, check=True)
            return result.stdout + result.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("FFmpeg 無法執行或檢查逾時") from exc

    version = inspect("-version").splitlines()[0]
    requirements = {"-encoders": ("libx264", "aac"),
                    "-filters": ("bwdif", "fps", "scale", "setsar", "split", "aresample"),
                    "-muxers": ("hls", "mpegts"), "-protocols": ("http", "https")}
    for flag, names in requirements.items():
        output = inspect(flag)
        missing = [name for name in names if not re.search(r"\b" + re.escape(name) + r"\b", output)]
        if missing:
            raise ValueError("FFmpeg 缺少必要能力：" + ", ".join(missing))
    return version
