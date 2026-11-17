#!/usr/bin/env python3
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("This release target is Linux x86_64")
    subprocess.run([sys.executable, str(ROOT / "tools/build_web.py")], check=True, cwd=ROOT)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "udisplayer.spec"], check=True, cwd=ROOT)
    source = ROOT / "dist/udisplayer"
    output = ROOT / "dist/udisplayer-linux-x86_64"
    staged = output.with_name(output.name + ".tmp")
    try:
        shutil.copy2(source, staged)
        staged.replace(output)
    finally:
        staged.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (ROOT / "dist/SHA256SUMS").write_text(f"{digest}  {output.name}\n")
    metadata = {"platform": platform.platform(), "libc": platform.libc_ver(),
                "python": platform.python_version(), "ffmpeg_bundled": False,
                "sha256": digest}
    (ROOT / "dist/build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
    shutil.copy2(ROOT / "channels.example.json", ROOT / "dist/channels.example.json")
    shutil.copy2(ROOT / "udisplayer.example.ini", ROOT / "dist/udisplayer.example.ini")
    print(f"Built {output}")


if __name__ == "__main__":
    main()
