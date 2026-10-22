#!/usr/bin/env python3
"""Fetch the exact upstream archive; only extract named files after verification."""
import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.7.3"
INTEGRITY = "MsPlx6yVW4Qv4C7mEVou4/gk/5cN2dVxOTnrNjPSmyH2f0Ln+/UkG10Ax1PE6OvrmTVPKgQYozTtBaPOuCzq7A=="
URL = f"https://registry.npmjs.org/hls.js/-/hls.js-{VERSION}.tgz"


def main():
    data = urllib.request.urlopen(URL, timeout=60).read()
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != INTEGRITY:
        raise SystemExit("hls.js archive integrity mismatch")
    files = {"package/dist/hls.min.js": ROOT / "vendor/hls.min.js",
             "package/LICENSE": ROOT / "licenses/hls.js.txt"}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name, destination in files.items():
            member = archive.extractfile(name)
            if member is None:
                raise SystemExit(f"Missing {name}")
            content = member.read()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    lock = {"name": "hls.js", "version": VERSION, "url": URL, "integrity": "sha512-" + INTEGRITY,
            "sha256": hashlib.sha256((ROOT / "vendor/hls.min.js").read_bytes()).hexdigest()}
    (ROOT / "vendor/lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Verified and vendored hls.js {VERSION}")


if __name__ == "__main__":
    main()
