from __future__ import annotations

import json
import logging
import math
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .config import CHANNEL_ID, LOGO_FILE, ChannelConfig, Settings
from .logo import load_logo_assets
from .transcoder import StreamError, StreamManager

LOG = logging.getLogger("udisplayer")
PROXY_LOG_HEADERS = (
    "CF-Connecting-IP",
    "CF-Connecting-IPv6",
    "CF-Pseudo-IPv4",
    "True-Client-IP",
    "X-Forwarded-For",
    "X-Real-IP",
    "Forwarded",
    "Via",
    "CF-Ray",
    "CF-IPCountry",
    "CDN-Loop",
)
TOKEN = re.compile(r"[a-f0-9]{32}\Z")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SEGMENT = re.compile(r"segment_\d{9,}\.ts\Z")
TELEMETRY_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}\Z")


@dataclass
class PlaybackSession:
    channel: int
    session: str
    started: float
    last_seen: float
    peer_ip: str
    proxy_headers: dict[str, str]
    ok: bool = False
    recoveries: int = 0
    stalls: int = 0
    live_jumps: int = 0
    last_issue: float = 0


def parse_request_path(target: str, host: str | None = None) -> tuple[str, str]:
    # HTTP/1.1 allows the absolute-form request target used by some clients
    # and intermediary/proxy configurations. In particular, some mobile
    # clients switch to this form after their initial HLS segment request.
    # Accept it only for the same authority as the Host header; never act as
    # a forward proxy for another server.
    if target.startswith(("http://", "https://")):
        absolute = urlsplit(target)
        if (host is None or not absolute.netloc or absolute.netloc.lower() != host.strip().lower()
                or absolute.username is not None or absolute.password is not None
                or absolute.fragment):
            raise ValueError("Invalid absolute request target")
        target = (absolute.path or "/") + ("?" + absolute.query if absolute.query else "")
    if not target.startswith("/") or target.startswith("//") or "#" in target:
        raise ValueError("Invalid request target")
    if re.search(r"%(?![0-9a-fA-F]{2})|%(?:2f|5c|00)", target.split("?", 1)[0], re.I):
        raise ValueError("Invalid path encoding")
    parts = urlsplit(target)
    path = unquote(parts.path, errors="strict")
    if ("\\" in path or "%" in path or "//" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(p in (".", "..") for p in path.split("/"))):
        raise ValueError("Invalid path")
    return path, parts.query


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, settings: Settings, config: ChannelConfig, web: Path, not_found: Path,
                 logo_dir: Path, manager: StreamManager | None = None):
        self.settings = settings
        self.html = web.read_bytes()
        self.not_found_body = not_found.read_bytes()
        self.logo_version = secrets.token_hex(8)
        public = {"channels": [], "logo_version": self.logo_version}
        if settings.debug:
            public["debug"] = True
        if config.grouping_enabled:
            public["groups"] = list(config.groups)
        for channel in config.channels:
            entry = {"id": channel.id, "name": channel.name}
            if config.grouping_enabled:
                entry["group"] = channel.group
            if channel.logo:
                entry["logo"] = channel.logo
            # Only disclose a harmless playback profile hint, not the IPTV
            # source URL or any server-side encoder/decoder settings.
            if channel.codec_hint in ("cavs", "avs2"):
                entry["codec_hint"] = channel.codec_hint
            public["channels"].append(entry)
        self.config_json = json.dumps(public, ensure_ascii=False).encode()
        self.channel_ids = frozenset(c.id for c in config.channels)
        self.channel_names = {c.id: c.name for c in config.channels}
        self.playback_lock = threading.RLock()
        self.playback_sessions: dict[tuple[int, str], PlaybackSession] = {}
        self.playback_closed = threading.Event()
        self.playback_reaper: threading.Thread | None = None
        self.logo_dir = logo_dir
        self.logo_files = frozenset(c.logo for c in config.channels if c.logo)
        self.logo_assets = load_logo_assets(logo_dir, self.logo_files)
        self.slots = threading.BoundedSemaphore(64)
        self.manager = manager
        try:
            super().__init__((settings.host, settings.port), Handler)
            if self.manager is None:
                self.manager = StreamManager(settings, config.channels)
            self.playback_reaper = threading.Thread(
                target=self._reap_playback_sessions,
                daemon=True,
                name="playback-reaper",
            )
            self.playback_reaper.start()
        except Exception:
            self.server_close()
            raise

    def _playback_label(self, cid: int) -> str:
        return json.dumps(self.channel_names.get(cid, ""), ensure_ascii=False)

    def playback_start(self, cid: int, session: str, peer_ip: str,
                       proxy_headers: dict[str, str]):
        now = time.monotonic()
        key = (cid, session)
        with self.playback_lock:
            current = self.playback_sessions.get(key)
            if current:
                current.last_seen = now
                return
            self.playback_sessions[key] = PlaybackSession(
                cid, session, now, now, peer_ip, dict(proxy_headers),
            )
        LOG.info(
            "playback_start channel=%s channel_name=%s session=%s peer_ip=%s "
            "proxy_headers=%s",
            cid,
            self._playback_label(cid),
            session,
            peer_ip,
            json.dumps(proxy_headers, ensure_ascii=False, separators=(",", ":")),
        )

    def playback_touch(self, cid: int, session: str | None):
        if session is None:
            return
        with self.playback_lock:
            current = self.playback_sessions.get((cid, session))
            if current:
                current.last_seen = time.monotonic()

    @staticmethod
    def _update_playback_counters(current: PlaybackSession, payload: dict):
        for attr, key in (("recoveries", "recoveries"), ("stalls", "stalls"),
                          ("live_jumps", "live_jumps")):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(current, attr, max(getattr(current, attr), value))

    def _log_playback_end(self, current: PlaybackSession, reason: str, *, warning: bool = False):
        logger = LOG.warning if warning else LOG.info
        logger(
            "playback_end channel=%s channel_name=%s session=%s peer_ip=%s "
            "duration_s=%.1f ok=%s recoveries=%s stalls=%s live_jumps=%s reason=%s",
            current.channel,
            self._playback_label(current.channel),
            current.session,
            current.peer_ip,
            max(0, time.monotonic() - current.started),
            str(current.ok).lower(),
            current.recoveries,
            current.stalls,
            current.live_jumps,
            json.dumps(str(reason)[:300], ensure_ascii=False),
        )

    def playback_finish(self, cid: int, session: str, reason: str, payload: dict | None = None,
                        *, warning: bool = False) -> bool:
        with self.playback_lock:
            current = self.playback_sessions.pop((cid, session), None)
            if current is None:
                return False
            if payload:
                self._update_playback_counters(current, payload)
        self._log_playback_end(current, reason, warning=warning)
        return True

    def playback_failed(self, cid: int, session: str, reason: str, payload: dict | None = None):
        with self.playback_lock:
            current = self.playback_sessions.pop((cid, session), None)
            if current is None:
                return
            if payload:
                self._update_playback_counters(current, payload)
        LOG.warning(
            "playback_failed channel=%s channel_name=%s session=%s peer_ip=%s "
            "duration_s=%.1f recoveries=%s stalls=%s live_jumps=%s reason=%s hls_error=%s",
            current.channel,
            self._playback_label(current.channel),
            current.session,
            current.peer_ip,
            max(0, time.monotonic() - current.started),
            current.recoveries,
            current.stalls,
            current.live_jumps,
            json.dumps(str(reason)[:300], ensure_ascii=False),
            json.dumps((payload or {}).get("hls_error") or "-", ensure_ascii=False),
        )

    def playback_event(self, cid: int, session: str, event: str, payload: dict):
        now = time.monotonic()
        with self.playback_lock:
            current = self.playback_sessions.get((cid, session))
            if current is None:
                return
            current.last_seen = now
            self._update_playback_counters(current, payload)
            reached_playing = event == "playing" or (
                event == "stall_end" and payload.get("phase") == "playing"
            )
            if reached_playing and not current.ok:
                current.ok = True
                startup_ms = int(max(0, now - current.started) * 1000)
                peer_ip = current.peer_ip
                engine = payload.get("engine") or "-"
            else:
                startup_ms = None
                peer_ip = current.peer_ip
                engine = "-"
            if event == "recovery" and (not current.last_issue or now - current.last_issue >= 30):
                current.last_issue = now
                issue = True
                recoveries = current.recoveries
                reason = payload.get("reason") or "recovery"
            else:
                issue = False
                recoveries = current.recoveries
                reason = ""
        if startup_ms is not None:
            LOG.info(
                "playback_ok channel=%s channel_name=%s session=%s peer_ip=%s "
                "startup_ms=%s engine=%s",
                cid, self._playback_label(cid), session, peer_ip, startup_ms,
                json.dumps(str(engine)[:80], ensure_ascii=False),
            )
        if issue:
            LOG.warning(
                "playback_issue channel=%s channel_name=%s session=%s peer_ip=%s "
                "recoveries=%s reason=%s",
                cid, self._playback_label(cid), session, peer_ip, recoveries,
                json.dumps(str(reason)[:300], ensure_ascii=False),
            )

    def _expire_playback_sessions(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        expired = []
        with self.playback_lock:
            for key, current in list(self.playback_sessions.items()):
                timeout = self.settings.idle_timeout if current.ok else max(
                    self.settings.idle_timeout, self.settings.startup_timeout + 5,
                )
                if now - current.last_seen > timeout:
                    expired.append(self.playback_sessions.pop(key))
        for current in expired:
            self._log_playback_end(current, "idle_timeout", warning=True)

    def _reap_playback_sessions(self):
        interval = min(1.0, max(0.1, self.settings.idle_timeout / 2))
        while not self.playback_closed.wait(interval):
            self._expire_playback_sessions()

    def server_close(self):
        self.playback_closed.set()
        reaper = self.playback_reaper
        if reaper and reaper is not threading.current_thread():
            reaper.join(timeout=2)
        with self.playback_lock:
            remaining = list(self.playback_sessions.values())
            self.playback_sessions.clear()
        for current in remaining:
            self._log_playback_end(current, "server_shutdown")
        super().server_close()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                                b"Cache-Control: no-store\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # BaseServer prints request/exception details that can include private paths.
        LOG.error("HTTP request failed")


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    server_version = "udisplayer"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(12)
        super().setup()

    def _proxy_headers(self) -> dict[str, str]:
        # Proxy/CDN headers are recorded only as untrusted observations; they never
        # affect routing or the peer address used by the application.
        proxy_headers = {}
        for name in PROXY_LOG_HEADERS:
            value = self.headers.get(name)
            if value:
                proxy_headers[name] = value[:512]
        return proxy_headers

    def log_request(self, code="-", size="-"):
        if not self.server.settings.debug:
            return
        proxy_headers = self._proxy_headers()
        LOG.debug(
            "access peer_ip=%s method=%s status=%s target=%s proxy_headers=%s",
            self.client_address[0],
            self.command,
            code,
            self.path,
            json.dumps(proxy_headers, ensure_ascii=False, separators=(",", ":")),
        )

    def log_message(self, format, *args):
        # Suppress BaseHTTPRequestHandler's duplicate stderr access log.
        pass

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            self.not_found()
            return
        self.respond(code, b"Request rejected\n", "text/plain; charset=utf-8")

    def send_common_headers(self, status: int, length: int, mime: str, extra: dict | None = None,
                            cache_control: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        if cache_control == "no-store":
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        if status == 503:
            self.send_header("Retry-After", "2")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def respond(self, status: int, content: bytes, mime="text/plain; charset=utf-8", extra=None,
                cache_control: str = "no-store"):
        self.send_common_headers(status, len(content), mime, extra, cache_control)
        if self.command != "HEAD":
            self.wfile.write(content)

    def not_found(self):
        self.respond(404, self.server.not_found_body, "text/plain; charset=utf-8")

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except StreamError as exc:
            if exc.status == 404:
                self.not_found()
            else:
                self.respond(exc.status, (str(exc) + "\n").encode())
        except (ValueError, UnicodeError):
            self.not_found()

    def route(self):
        path, query = parse_request_path(self.path, self.headers.get("Host"))
        prefix = self.server.settings.prefix
        if prefix and path == prefix:
            self.respond(308, b"", extra={"Location": prefix + "/" + ("?" + query if query else "")})
            return
        if prefix and not path.startswith(prefix + "/"):
            self.not_found()
            return
        relative = path[len(prefix):]
        if self.command == "POST" and not (
                relative.startswith("/hls/") or relative == "/telemetry"):
            self.not_found()
            return
        if relative in ("/", "/index.html"):
            requested = parse_qs(query, keep_blank_values=True, max_num_fields=16).get("channel")
            if requested is not None:
                if (len(requested) != 1 or not CHANNEL_ID.fullmatch(requested[0])
                        or int(requested[0]) not in self.server.channel_ids):
                    self.not_found()
                    return
            self.respond(200, self.server.html, "text/html; charset=utf-8")
        elif relative == "/channels.json":
            self.respond(200, self.server.config_json, "application/json; charset=utf-8")
        elif relative.startswith("/logos/"):
            self.logo(relative)
        elif relative == "/telemetry":
            self.telemetry()
        elif relative.startswith("/hls/"):
            self.hls(relative, query)
        else:
            self.not_found()

    def logo(self, relative: str):
        if self.command not in ("GET", "HEAD"):
            raise StreamError(404, "Not found")
        filename = relative.removeprefix("/logos/")
        if (not LOGO_FILE.fullmatch(filename) or filename not in self.server.logo_files
                or "/" in filename):
            raise StreamError(404, "Not found")
        asset = self.server.logo_assets.get(filename)
        if asset is None:
            raise StreamError(404, "Not found")
        content, mime = asset
        self.respond(200, content, mime, cache_control="private, max-age=31536000, immutable")

    @staticmethod
    def session_from_query(query: str) -> str | None:
        values = parse_qs(query, keep_blank_values=True, max_num_fields=16).get("session")
        if values is None:
            return None
        if len(values) != 1 or not SESSION.fullmatch(values[0]):
            raise StreamError(404, "Not found")
        return values[0]

    def telemetry(self):
        if self.command != "POST":
            raise StreamError(404, "Not found")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as exc:
            raise StreamError(400, "Invalid telemetry") from exc
        if length < 2 or length > 16 * 1024:
            raise StreamError(413, "Telemetry too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StreamError(400, "Invalid telemetry") from exc
        if not isinstance(payload, dict):
            raise StreamError(400, "Invalid telemetry")

        clean = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not TELEMETRY_KEY.fullmatch(key):
                continue
            if isinstance(value, str):
                clean[key] = value[:300]
            elif isinstance(value, bool) or value is None:
                clean[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                clean[key] = value

        event = clean.get("event")
        channel = clean.get("channel")
        session = clean.get("session")
        if (not isinstance(event, str) or not event
                or not isinstance(channel, int) or isinstance(channel, bool)
                or channel not in self.server.channel_ids
                or not isinstance(session, str) or not SESSION.fullmatch(session)):
            raise StreamError(400, "Invalid telemetry")
        if self.server.settings.debug:
            LOG.debug(
                "client_telemetry peer_ip=%s user_agent=%s proxy_headers=%s data=%s",
                self.client_address[0],
                (self.headers.get("User-Agent") or "-")[:300],
                json.dumps(self._proxy_headers(), ensure_ascii=False, separators=(",", ":")),
                json.dumps(clean, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            )
        if event in ("session_start", "playing", "recovery", "terminal"):
            self.server.playback_start(
                channel,
                session,
                self.client_address[0],
                self._proxy_headers(),
            )
        if event == "session_start":
            pass
        elif event == "terminal":
            self.server.playback_failed(channel, session, str(clean.get("reason") or "terminal"), clean)
        elif event == "session_end":
            self.server.playback_finish(
                channel, session, str(clean.get("reason") or "client_end"), clean,
            )
        else:
            self.server.playback_event(channel, session, event, clean)
        self.respond(204, b"")

    def hls(self, relative: str, query: str):
        parts = relative.strip("/").split("/")
        manager = self.server.manager
        assert manager is not None
        if len(parts) < 3 or not CHANNEL_ID.fullmatch(parts[1]):
            raise StreamError(404, "Not found")
        cid = int(parts[1])
        session = self.session_from_query(query)
        if len(parts) == 3 and parts[2] == "index.m3u8":
            if self.command not in ("GET", "HEAD"):
                raise StreamError(404, "Not found")
            if session is not None:
                self.server.playback_start(
                    cid,
                    session,
                    self.client_address[0],
                    self._proxy_headers(),
                )
            try:
                playlist = manager.playlist(cid, session)
            except StreamError as exc:
                if session is not None:
                    self.server.playback_failed(
                        cid, session, f"playlist_http_{exc.status}:{exc}",
                    )
                raise
            self.respond(200, playlist, "application/vnd.apple.mpegurl")
            return
        if len(parts) == 3 and parts[2] == "release":
            if self.command != "POST" or session is None:
                raise StreamError(404, "Not found")
            self.server.playback_finish(cid, session, "release")
            manager.release(cid, session)
            self.respond(204, b"")
            return
        if (self.command not in ("GET", "HEAD") or len(parts) != 4
                or not TOKEN.fullmatch(parts[2]) or not SEGMENT.fullmatch(parts[3])):
            raise StreamError(404, "Not found")
        with manager.open_resource(cid, parts[2], parts[3], session) as resource:
            self.server.playback_touch(cid, session)
            size = resource.seek(0, 2)
            resource.seek(0)
            start, end, status = 0, size - 1, 200
            extra = {"Accept-Ranges": "bytes"}
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match or not any(match.groups()):
                    self.respond(416, b"", extra={"Content-Range": f"bytes */{size}"})
                    return
                first, last = match.groups()
                if first:
                    start, end = int(first), min(size - 1, int(last)) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    self.respond(416, b"", extra={"Content-Range": f"bytes */{size}"})
                    return
                status = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            remaining = end - start + 1
            self.send_common_headers(status, remaining, "video/mp2t", extra)
            if self.command == "HEAD":
                return
            resource.seek(start)
            deadline = time.monotonic() + 20
            started = time.monotonic()
            sent = 0
            reason = "complete"
            completed = False
            try:
                while remaining > 0:
                    if time.monotonic() > deadline:
                        reason = "send_deadline"
                        self.close_connection = True
                        break
                    block = resource.read(min(64 * 1024, remaining))
                    if not block:
                        reason = "segment_eof"
                        self.close_connection = True
                        break
                    self.wfile.write(block)
                    sent += len(block)
                    remaining -= len(block)
                completed = remaining == 0
            except BrokenPipeError:
                reason = "broken_pipe"
                raise
            except ConnectionResetError:
                reason = "connection_reset"
                raise
            except socket.timeout:
                reason = "socket_timeout"
                raise
            finally:
                if self.server.settings.debug:
                    elapsed = max(0.001, time.monotonic() - started)
                    LOG.debug(
                        "hls_segment channel=%s session=%s segment=%s status=%s "
                        "size=%s bytes_sent=%s duration_ms=%d throughput_kbps=%.1f "
                        "completed=%s reason=%s peer_ip=%s proxy_headers=%s",
                        cid,
                        session or "-",
                        parts[3],
                        status,
                        end - start + 1,
                        sent,
                        int(elapsed * 1000),
                        sent * 8 / elapsed / 1000,
                        str(completed).lower(),
                        reason,
                        self.client_address[0],
                        json.dumps(self._proxy_headers(), ensure_ascii=False, separators=(",", ":")),
                    )
