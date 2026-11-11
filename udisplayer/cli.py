from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import Settings, normalize_prefix, read_config, resolve_ffmpeg, validate_ffmpeg
from .hardware import probe_vaapi, select_render_device
from .server import AppServer


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def default_config() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "channels.json"
    return resource_root() / "channels.json"


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必須大於零")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="udisplayer — 內建播放器、HLS 轉流與弱網恢復")
    parser.add_argument("--version", action="version", version=f"udisplayer {__version__}")
    parser.add_argument("--licenses", action="store_true", help="顯示第三方授權")
    parser.add_argument("--ffmpeg", help="FFmpeg 執行檔路徑；省略時搜尋 PATH")
    parser.add_argument("--hw-render", metavar="RENDER|none",
                        help="指定 DRM Render Node；預設取第一個，none 停用硬體加速")
    parser.add_argument("--path", default="/", help="HTTP URL 前綴，預設 /")
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--init-config", action="store_true", help="在 --config 位置建立範例配置")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=positive, default=8081)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--startup-timeout", type=positive, default=30)
    parser.add_argument("--idle-timeout", type=positive, default=60,
                        help="頻道無活動後關閉其 FFmpeg 的秒數，預設 60")
    parser.add_argument("--max-streams", type=positive, default=4,
                        help="同時轉流的頻道上限，預設 4")
    parser.add_argument("--encoder-threads", type=positive, default=2,
                        help="每個視訊編碼器的執行緒數，預設 2")
    parser.add_argument("--debug", action="store_true",
                        help="輸出播放診斷埋點（segment、瀏覽器、FFmpeg、HLS 與來源健康）")
    args = parser.parse_args(argv)
    root = resource_root()
    if args.licenses:
        print((root / "THIRD_PARTY_NOTICES.md").read_text())
        for license_file in sorted(path for path in (root / "licenses").rglob("*") if path.is_file()):
            print("\n" + str(license_file.relative_to(root / "licenses")) + "\n" + license_file.read_text())
        return 0
    if args.init_config:
        try:
            with args.config.open("x", encoding="utf-8") as file:
                file.write((root / "channels.example.json").read_text())
        except OSError as exc:
            parser.error(f"無法建立配置（不會覆寫既有檔案）：{exc.strerror}")
        print(f"已建立 {args.config}")
        return 0
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        if args.port > 65535:
            raise ValueError("--port 不可超過 65535")
        config = read_config(args.config)
        ffmpeg = resolve_ffmpeg(args.ffmpeg)
        version = validate_ffmpeg(ffmpeg)
        render = select_render_device(args.hw_render)
        hw_decode, hw_encode, hw_rc_mode = probe_vaapi(ffmpeg, render)
        settings = Settings(ffmpeg=ffmpeg, prefix=normalize_prefix(args.path),
                            host=args.host, port=args.port, startup_timeout=args.startup_timeout,
                            idle_timeout=args.idle_timeout, max_streams=args.max_streams,
                            encoder_threads=args.encoder_threads, cache_dir=args.cache_dir,
                            debug=args.debug, hw_render=render, hw_decode=hw_decode,
                            hw_encode=hw_encode, hw_rc_mode=hw_rc_mode)
        server = AppServer(
            settings,
            config,
            root / "index.html",
            root / "web/not-found.html",
            args.config.parent / "logos",
        )
    except (ValueError, OSError) as exc:
        # JSON errors and file paths are local startup diagnostics, never HTTP responses.
        parser.error(str(exc))
    logging.info("%s", version)
    if settings.debug:
        logging.debug("debug telemetry enabled")
    logging.info("udisplayer %s listening on %s:%s (%s channels, prefix %s)",
                 __version__, settings.host, server.server_port, len(config.channels),
                 "enabled" if settings.prefix else "root")
    stopping = threading.Event()

    def stop(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        assert server.manager is not None
        server.manager.close()
        server.server_close()
    return 0
