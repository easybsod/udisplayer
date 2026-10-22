#!/usr/bin/env python3
"""Convert an extended M3U playlist to udisplayer channels.json + local logos."""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
CONTENT_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_LOGO_BYTES = 2 * 1024 * 1024
USER_AGENT = "udisplayer-m3u-converter/1.0"
UNGROUPED = "未分組"
MAX_LOGO_EDGE = 160
MAX_LOGO_PIXELS = 16_000_000


def optimize_png(data: bytes) -> bytes:
    if Image is None:
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != "PNG" or image.width < 1 or image.height < 1
                    or image.width * image.height > MAX_LOGO_PIXELS
                    or getattr(image, "is_animated", False)):
                return data
            image.load()
            if max(image.size) > MAX_LOGO_EDGE:
                image.thumbnail((MAX_LOGO_EDGE, MAX_LOGO_EDGE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True, compress_level=9)
            optimized = output.getvalue()
    except (OSError, ValueError):
        return data
    return optimized if len(optimized) < len(data) else data


def parse_m3u(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    channels: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line.startswith("#EXTINF:"):
            continue
        attrs = dict(ATTR.findall(line))
        name = line.split(",", 1)[1].strip() if "," in line else ""
        while index < len(lines):
            url = lines[index].strip()
            index += 1
            if url and not url.startswith("#"):
                break
        else:
            url = ""
        if not name or not url:
            raise ValueError(f"無效的 EXTINF 項目：{line[:120]}")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"頻道 {name} 不是有效的 HTTP／HTTPS 串流")
        channels.append({
            "name": name,
            "url": url,
            "group": attrs.get("group-title", "").strip(),
            "remote_logo": attrs.get("tvg-logo", "").strip(),
        })
    if not channels:
        raise ValueError("M3U 中沒有找到任何頻道")
    return channels


def logo_extension(url: str, content_type: str) -> str | None:
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return suffix
    return CONTENT_EXTENSIONS.get(content_type.split(";", 1)[0].strip().lower())


def download_logo(item: tuple[int, str], directory: Path) -> tuple[int, str | None, str | None]:
    channel_id, url = item
    if not url:
        return channel_id, None, None
    parts = urllib.parse.urlsplit(url)
    encoded_url = urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        urllib.parse.quote(parts.path, safe="/%:@"),
        urllib.parse.quote(parts.query, safe="=&%/:;+?,"),
        "",
    ))
    request = urllib.request.Request(encoded_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            content_type = response.headers.get("Content-Type", "")
            extension = logo_extension(url, content_type)
            if extension is None:
                return channel_id, None, f"不支援的圖片格式：{content_type or url}"
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_LOGO_BYTES:
                return channel_id, None, "Logo 超過 2 MiB"
            data = response.read(MAX_LOGO_BYTES + 1)
            if len(data) > MAX_LOGO_BYTES:
                return channel_id, None, "Logo 超過 2 MiB"
            if not data:
                return channel_id, None, "Logo 為空"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return channel_id, None, str(exc)
    if extension == ".png":
        data = optimize_png(data)
    filename = f"{channel_id}{extension}"
    (directory / filename).write_bytes(data)
    return channel_id, filename, None


def convert(source: Path, output: Path, logo_dir: Path, workers: int) -> tuple[int, int, list[str]]:
    source_channels = parse_m3u(source)
    groups: list[str] = []
    has_grouping = any(channel["group"] for channel in source_channels)
    if has_grouping:
        for channel in source_channels:
            group = channel["group"] or UNGROUPED
            channel["group"] = group
            if group not in groups:
                groups.append(group)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".udisplayer-m3u-", dir=output.parent) as temp:
        temp_root = Path(temp)
        staged_logos = temp_root / "logos"
        staged_logos.mkdir()
        logo_names: dict[int, str] = {}
        errors: list[str] = []
        downloads = [
            (index, channel["remote_logo"])
            for index, channel in enumerate(source_channels, 1)
            if channel["remote_logo"]
        ]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(download_logo, item, staged_logos): item
                for item in downloads
            }
            for future in as_completed(futures):
                channel_id, filename, error = future.result()
                if filename:
                    logo_names[channel_id] = filename
                elif error:
                    errors.append(f"頻道 {channel_id}: {error}")

        channels = []
        for channel_id, source_channel in enumerate(source_channels, 1):
            entry = {
                "id": channel_id,
                "name": source_channel["name"],
                "url": source_channel["url"],
            }
            if has_grouping:
                entry["group"] = source_channel["group"]
            if channel_id in logo_names:
                entry["logo"] = logo_names[channel_id]
            channels.append(entry)

        data = {"channels": channels}
        if has_grouping:
            data = {"groups": groups, "channels": channels}
        staged_config = temp_root / "channels.json"
        staged_config.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if logo_dir.exists():
            shutil.rmtree(logo_dir)
        shutil.move(str(staged_logos), str(logo_dir))
        shutil.copy2(staged_config, output)

    return len(source_channels), len(logo_names), errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="將 Extended M3U 轉為 udisplayer channels.json，並下載本地 Logo"
    )
    parser.add_argument("source", type=Path, help="來源 .m3u 檔")
    parser.add_argument("--output", type=Path, default=Path("channels.json"))
    parser.add_argument("--logos", type=Path, default=Path("logos"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    try:
        total, logos, errors = convert(args.source, args.output, args.logos, args.workers)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"已轉換 {total} 個頻道，下載 {logos} 個 Logo")
    if errors:
        print(f"{len(errors)} 個 Logo 下載失敗；對應頻道將使用預設播放圖標")
        for error in errors[:20]:
            print(f"  {error}")
        if len(errors) > 20:
            print(f"  ... 另有 {len(errors) - 20} 項")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
