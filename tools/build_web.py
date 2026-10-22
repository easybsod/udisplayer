#!/usr/bin/env python3
"""Produce the deployable HTML without Node, CDN requests or a framework."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build():
    library = (ROOT / "vendor/hls.min.js").read_bytes()
    lock = json.loads((ROOT / "vendor/lock.json").read_text())
    if hashlib.sha256(library).hexdigest() != lock["sha256"]:
        raise SystemExit("Vendored hls.js has changed; verify vendor/lock.json")
    hls = re.sub(r"(?m)^//# sourceMappingURL=.*$", "", library.decode())
    # An inline script must not contain a literal closing script tag.
    def inline(text):
        return re.sub(r"</script", r"<\\/script", text, flags=re.I)
    license_text = (ROOT / "licenses/hls.js.txt").read_text()
    template = (ROOT / "web/template.html").read_text()
    replacements = {
        "/* INLINE_STYLE */": (ROOT / "web/style.css").read_text(),
        "/* INLINE_HLS */": inline(hls),
        "/* INLINE_APP */": inline((ROOT / "web/app.js").read_text()),
    }
    for marker, content in replacements.items():
        if template.count(marker) != 1:
            raise SystemExit(f"Missing or repeated template marker: {marker}")
        template = template.replace(marker, content)
    notice = ("<!--\nudisplayer: MIT. Embedded hls.js " + lock["version"] +
              ", Copyright (c) 2017 Dailymotion and contributors.\n" +
              license_text.replace("--", "—") + "\n-->\n")
    (ROOT / "index.html").write_text(template.replace("<!doctype html>\n", "<!doctype html>\n" + notice), encoding="utf-8")
    print(f"Built index.html ({(ROOT / 'index.html').stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
