from __future__ import annotations

import io
from pathlib import Path

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # Source deployments may run without the optional optimizer.
    Image = None
    UnidentifiedImageError = OSError


MAX_LOGO_EDGE = 160
MAX_LOGO_PIXELS = 16_000_000


def optimize_png(data: bytes, max_edge: int = MAX_LOGO_EDGE) -> bytes:
    """Return a smaller PNG when Pillow can safely optimize it."""
    if Image is None:
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != "PNG" or image.width < 1 or image.height < 1
                    or image.width * image.height > MAX_LOGO_PIXELS
                    or getattr(image, "is_animated", False)):
                return data
            image.load()
            original_size = image.size
            if max(original_size) > max_edge:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True, compress_level=9)
            optimized = output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError):
        return data

    # For already-small images, never replace the source with a larger encoding.
    # A resized image should also only be used when it reduces transfer size.
    return optimized if len(optimized) < len(data) else data


def logo_mime(filename: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


def load_logo_assets(directory: Path, filenames: frozenset[str]) -> dict[str, tuple[bytes, str]]:
    assets: dict[str, tuple[bytes, str]] = {}
    for filename in filenames:
        try:
            data = (directory / filename).read_bytes()
        except OSError:
            continue
        if Path(filename).suffix.lower() == ".png":
            data = optimize_png(data)
        assets[filename] = (data, logo_mime(filename))
    return assets
