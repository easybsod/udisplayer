from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import (Settings, available_cpu_threads, normalize_prefix, read_config,
                     resolve_ffmpeg, validate_ffmpeg)
from .hardware import probe_vaapi, select_render_device
from .ini_config import read_ini_config
from .server import AppServer


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def server_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_channel_list() -> Path:
    return server_directory() / "channels.json"


def resolve_channel_list(value: str | Path) -> Path:
    """Relative channel lists always belong to server.py, not cwd or the INI."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else server_directory() / path).resolve()


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
    parser.add_argument("--path", help="HTTP URL 前綴，預設 /")
    parser.add_argument("--config", type=Path, help="伺服器 INI 配置檔（選填）")
    parser.add_argument("--channel-list", type=Path,
                        help="頻道 JSON 路徑；相對路徑以 server.py 所在目錄為基準")
    init_group = parser.add_mutually_exclusive_group()
    init_group.add_argument("--init-config", action="store_true", help="建立範例 udisplayer.ini（不覆寫）")
    init_group.add_argument("--init-channel-list", action="store_true", help="建立範例 channels.json（不覆寫）")
    parser.add_argument("--host")
    parser.add_argument("--port", type=positive)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--startup-timeout", type=positive)
    parser.add_argument("--idle-timeout", type=positive,
                        help="頻道無活動後關閉其 FFmpeg 的秒數，預設 60")
    parser.add_argument("--max-streams", type=positive,
                        help="同時轉流的頻道上限，預設 4")
    parser.add_argument("--encoder-threads", type=positive,
                        help="每個軟體視訊編碼器的執行緒數；省略時自動使用可用邏輯 CPU 數")
    parser.add_argument("--decoder-threads", type=positive,
                        help="每路來源視訊解碼器的執行緒數；省略時自動使用可用邏輯 CPU 數")
    parser.add_argument("--debug", action="store_true", default=None,
                        help="輸出播放診斷埋點（segment、瀏覽器、FFmpeg、HLS 與來源健康）")
    args = parser.parse_args(argv)
    root = resource_root()
    if args.licenses:
        print((root / "THIRD_PARTY_NOTICES.md").read_text())
        for license_file in sorted(path for path in (root / "licenses").rglob("*") if path.is_file()):
            print("\n" + str(license_file.relative_to(root / "licenses")) + "\n" + license_file.read_text())
        return 0
    if args.init_config:
        filename = (args.config if args.config is not None else
                    server_directory() / "udisplayer.ini").expanduser().resolve()
        try:
            with filename.open("x", encoding="utf-8") as file:
                file.write((root / "udisplayer.example.ini").read_text(encoding="utf-8"))
        except OSError as exc:
            parser.error(f"無法建立 INI（不會覆寫既有檔案）：{exc}")
        print(f"已建立 {filename}")
        return 0
    try:
        ini = read_ini_config(args.config) if args.config is not None else {}
        options = {**ini, **{key: value for key, value in vars(args).items()
                            if value is not None and key not in
                            ("config", "init_config", "init_channel_list", "licenses")}}
        channel_list = resolve_channel_list(options.get("channel_list", "channels.json"))
        if args.init_channel_list:
            try:
                with channel_list.open("x", encoding="utf-8") as file:
                    file.write((root / "channels.example.json").read_text(encoding="utf-8"))
            except OSError as exc:
                parser.error(f"無法建立頻道列表（不會覆寫既有檔案）：{exc}")
            print(f"已建立 {channel_list}")
            return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    def option(name: str, default=None):
        return options.get(name, default)

    logging.basicConfig(
        level=logging.DEBUG if option("debug", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        if option("port", 8081) > 65535:
            raise ValueError("--port 不可超過 65535")
        config = read_config(channel_list)
        ffmpeg = resolve_ffmpeg(option("ffmpeg"))
        version = validate_ffmpeg(ffmpeg)
        render = select_render_device(option("hw_render"))
        hw_decode, hw_encode, hw_rc_mode = probe_vaapi(ffmpeg, render)
        auto_threads = (available_cpu_threads() if option("encoder_threads") is None
                        or option("decoder_threads") is None else None)
        settings = Settings(ffmpeg=ffmpeg, prefix=normalize_prefix(option("path", "/")),
                            host=option("host", "0.0.0.0"), port=option("port", 8081),
                            startup_timeout=option("startup_timeout", 30),
                            idle_timeout=option("idle_timeout", 60), max_streams=option("max_streams", 4),
                            encoder_threads=option("encoder_threads", auto_threads),
                            decoder_threads=option("decoder_threads", auto_threads),
                            cache_dir=option("cache_dir"),
                            debug=option("debug", False), hw_render=render, hw_decode=hw_decode,
                            hw_encode=hw_encode, hw_rc_mode=hw_rc_mode)
        server = AppServer(
            settings,
            config,
            root / "index.html",
            root / "web/not-found.html",
            channel_list.parent / "logos",
        )
    except (ValueError, OSError) as exc:
        # JSON errors and file paths are local startup diagnostics, never HTTP responses.
        parser.error(str(exc))
    logging.info("%s", version)
    logging.info("FFmpeg threads: decoder=%s encoder=%s", settings.decoder_threads,
                 settings.encoder_threads)
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
