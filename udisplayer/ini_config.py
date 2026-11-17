"""Small, strict, sectionless INI reader for server startup options."""
from __future__ import annotations

import re
from pathlib import Path

MAX_INI_BYTES = 64 * 1024
INI_TYPES = {
    "ffmpeg": "path",
    "hw-render": "string",
    "path": "string",
    "channel-list": "string",
    "host": "string",
    "port": "port",
    "cache-dir": "path",
    "startup-timeout": "positive",
    "idle-timeout": "positive",
    "max-streams": "positive",
    "encoder-threads": "positive",
    "decoder-threads": "positive",
    "debug": "boolean",
}


def read_ini_config(filename: Path) -> dict[str, object]:
    """Read a flat INI. Fail on unknown/duplicate keys instead of ignoring typos.

    Only ffmpeg/cache-dir relative paths are relative to the INI directory;
    channel-list is resolved by the CLI relative to server.py.
    """
    filename = filename.expanduser().resolve()
    if filename.stat().st_size > MAX_INI_BYTES:
        raise ValueError(f"{filename}: INI 檔案不可超過 64 KiB")
    try:
        content = filename.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"{filename}: INI 必須使用 UTF-8 編碼") from exc
    values: dict[str, object] = {}
    seen: dict[str, int] = {}
    for line_no, original in enumerate(content.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith(("#", ";")):
            continue
        where = f"{filename}:{line_no}"
        if line.startswith("["):
            raise ValueError(f"{where}: 不支援 INI 區段，請使用 key = value")
        match = re.fullmatch(r"([a-z][a-z0-9-]*)\s*=\s*(.*?)\s*", line)
        if match is None:
            raise ValueError(f"{where}: 無效格式，應為 key = value")
        key, raw = match.groups()
        if key not in INI_TYPES:
            raise ValueError(f"{where}: 未知的 INI 選項 {key}")
        if key in seen:
            raise ValueError(f"{where}: 重複的 INI 選項 {key}（首次出現於第 {seen[key]} 行）")
        seen[key] = line_no
        # Unquoted values, no interpolation, no escapes, no sections.
        raw = re.split(r"\s+[;#]", raw, maxsplit=1)[0].strip()
        if not raw or raw[0] in ("'", '"') or "\0" in raw:
            raise ValueError(f"{where}: {key} 不可為空，亦不支援引號或 NUL")
        kind = INI_TYPES[key]
        if kind == "boolean":
            if raw not in ("true", "false"):
                raise ValueError(f"{where}: {key} 只能是 true 或 false")
            value: object = raw == "true"
        elif kind in ("positive", "port"):
            if not re.fullmatch(r"[0-9]{1,10}", raw):
                raise ValueError(f"{where}: {key} 必須是正整數")
            value = int(raw)
            if value < 1 or (kind == "port" and value > 65535):
                raise ValueError(f"{where}: {key} 超出允許範圍")
        elif kind == "path":
            path = Path(raw).expanduser()
            resolved = ((filename.parent / path).resolve() if not path.is_absolute()
                        else path.resolve())
            value = resolved if key == "cache-dir" else str(resolved)
        else:
            value = raw
        values[key.replace("-", "_")] = value
    return values
