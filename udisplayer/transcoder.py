from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import Channel, Settings

LOG = logging.getLogger("udisplayer")

SOURCE_EVENT_PATTERNS = (
    ("timeout", re.compile(r"tim(?:e|ed) ?out|i/o timeout|rw_timeout", re.I)),
    ("connection", re.compile(r"connection (?:reset|refused|closed)|broken pipe|end of file", re.I)),
    ("corrupt", re.compile(r"corrupt|invalid data|error while decoding", re.I)),
    ("continuity", re.compile(r"continuity check|packet loss|missing packet", re.I)),
    ("timestamp", re.compile(r"non[- ]monoton|timestamp|\b(?:dts|pts)\b.*(?:invalid|out of order)", re.I)),
)


class StreamError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def ffmpeg_command(settings: Settings, channel: Channel, directory: Path) -> list[str]:
    # Preserve source resolution up to 1080p. Larger sources are downscaled
    # inside 1920x1080, then encoded as progressive H.264/AAC for broad browser
    # compatibility and smoother Internet playback.
    start_number = str(int(time.time() // 2))
    command = [settings.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    if settings.debug:
        # FFmpeg's machine-readable progress stream is emitted on stdout only in
        # debug mode; media still goes to HLS files.
        command += ["-progress", "pipe:1"]
    command += [
        "-y", "-rw_timeout", "10000000", "-fflags", "+discardcorrupt",
        "-analyzeduration", "500000", "-probesize", "500000",
        "-threads", str(settings.encoder_threads), "-i", channel.url,
        "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn",
        # Some broadcast sources use non-square pixels (for example 720x576
        # with SAR 16:15 for a 4:3 picture). First bake SAR into the coded width
        # so browsers receive square pixels with the correct DAR. Then cap the
        # square-pixel result inside 1920x1080; <=1080p sources remain unchanged.
        "-vf", ("bwdif=mode=send_frame:parity=auto:deint=interlaced,"
                "scale=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)':"
                "h='max(2,trunc(ih/2)*2)':flags=fast_bilinear,setsar=1,"
                "scale=w='min(iw,1920)':h='min(ih,1080)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2:"
                "flags=fast_bilinear"),
        # superfast leaves enough realtime headroom for 1080i + bwdif on the
        # production 2-vCPU host. A fixed CCTV-5 sample benchmark improved from
        # about 1.09x (veryfast) to 1.58x while staying near the same 2.2 Mbps VBV.
        "-c:v", "libx264", "-preset", "superfast",
        "-crf", "26", "-pix_fmt", "yuv420p", "-profile:v", "high", "-bf", "2", "-refs", "3",
        "-threads:v", str(settings.encoder_threads), "-g", "60", "-keyint_min", "1",
        "-sc_threshold", "0", "-flags", "+cgop", "-forced-idr", "1",
        "-force_key_frames:v", "expr:gte(t,n_forced*2)",
        "-maxrate:v", "2200000", "-bufsize:v", "2200000",
        "-c:a", "aac", "-profile:a", "aac_low", "-ac", "2", "-ar", "48000",
        "-af", "aresample=async=1", "-b:a", "80000",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "24",
        "-hls_delete_threshold", "6", "-start_number", start_number,
        "-hls_flags", "delete_segments+independent_segments+temp_file+program_date_time",
        "-hls_segment_filename", str(directory / "segment_%09d.ts"),
        str(directory / "index.m3u8"),
    ]
    return command


@dataclass
class Stream:
    channel: Channel
    token: str
    directory: Path
    process: subprocess.Popen
    started: float
    last_request: float
    sessions: dict[str, float] = field(default_factory=dict)
    anonymous_last_request: float | None = None
    errors: deque = field(default_factory=lambda: deque(maxlen=12))
    source_events: Counter = field(default_factory=Counter)
    debug_last_summary: float = 0


class StreamManager:
    def __init__(self, settings: Settings, channels: tuple[Channel, ...]):
        self.settings = settings
        self.channels = {c.id: c for c in channels}
        if settings.cache_dir:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="udisplayer-", dir=settings.cache_dir))
        self.lock = threading.RLock()
        self.streams: dict[int, Stream] = {}
        self.cooldowns: dict[int, tuple[float, int]] = {}
        self.released_sessions: dict[tuple[int, str], float] = {}
        self.closed = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True, name="stream-reaper")
        self.reaper.start()

    def _read_errors(self, stream: Stream):
        assert stream.process.stderr is not None
        with stream.process.stderr:
            for line in stream.process.stderr:
                # Source URLs may contain credentials. Keep them out of logs.
                cleaned = line.strip().replace(stream.channel.url, "[source]")
                cleaned = re.sub(r"https?://\S+", "[source]", cleaned)[:500]
                category = next(
                    (name for name, pattern in SOURCE_EVENT_PATTERNS if pattern.search(cleaned)),
                    None,
                )
                with self.lock:
                    stream.errors.append(cleaned)
                    if self.settings.debug and category:
                        stream.source_events[category] += 1
                if self.settings.debug and category:
                    LOG.debug(
                        "source_event channel=%s category=%s message=%s",
                        stream.channel.id, category, cleaned,
                    )

    def _read_progress(self, stream: Stream):
        assert stream.process.stdout is not None
        values = {}
        last_emit = 0.0
        with stream.process.stdout:
            for raw in stream.process.stdout:
                line = raw.strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key] = value
                if key == "progress":
                    now = time.monotonic()
                    if value == "end" or now - last_emit >= 5:
                        LOG.debug(
                            "ffmpeg_progress channel=%s frame=%s fps=%s bitrate=%s "
                            "total_size=%s out_time=%s dup_frames=%s drop_frames=%s speed=%s progress=%s",
                            stream.channel.id,
                            values.get("frame", "-"),
                            values.get("fps", "-"),
                            values.get("bitrate", "-"),
                            values.get("total_size", "-"),
                            values.get("out_time", "-"),
                            values.get("dup_frames", "-"),
                            values.get("drop_frames", "-"),
                            values.get("speed", "-"),
                            value,
                        )
                        last_emit = now
                    values = {}

    def _debug_hls_locked(self, cid: int, stream: Stream, now: float):
        if not self.settings.debug or now - stream.debug_last_summary < 10:
            return
        stream.debug_last_summary = now
        playlist = stream.directory / "index.m3u8"
        try:
            text = playlist.read_text()
            stat = playlist.stat()
        except (FileNotFoundError, OSError, UnicodeError):
            return
        segments = [line for line in text.splitlines() if line.startswith("segment_") and line.endswith(".ts")]
        durations = [float(value) for value in re.findall(r"#EXTINF:([0-9.]+)", text)]
        sizes, mtimes = [], []
        for filename in segments:
            try:
                segment_stat = (stream.directory / filename).stat()
                sizes.append(segment_stat.st_size)
                mtimes.append(segment_stat.st_mtime)
            except OSError:
                pass
        gaps = [max(0, (right - left) * 1000) for left, right in zip(mtimes, mtimes[1:])]
        LOG.debug(
            "hls_production channel=%s playlist_age_ms=%d segment_count=%d "
            "avg_duration_s=%.3f avg_size_kb=%.1f latest_segment=%s "
            "latest_generation_gap_ms=%s avg_generation_gap_ms=%s sessions=%d source_events=%s",
            cid,
            max(0, int((time.time() - stat.st_mtime) * 1000)),
            len(segments),
            sum(durations) / len(durations) if durations else 0,
            (sum(sizes) / len(sizes) / 1024) if sizes else 0,
            segments[-1] if segments else "-",
            int(gaps[-1]) if gaps else "-",
            int(sum(gaps) / len(gaps)) if gaps else "-",
            len(stream.sessions),
            dict(stream.source_events),
        )

    def _stop_locked(self, cid: int, reason: str):
        stream = self.streams.pop(cid, None)
        if not stream:
            return
        if stream.process.poll() is None:
            stream.process.terminate()
            try:
                stream.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stream.process.kill()
                stream.process.wait(timeout=3)
        shutil.rmtree(stream.directory, ignore_errors=True)
        if reason in ("failed", "stalled", "startup_timeout"):
            LOG.warning("channel=%s stopped reason=%s", cid, reason)
        elif self.settings.debug:
            LOG.debug("channel=%s stopped reason=%s", cid, reason)
        if self.settings.debug:
            LOG.debug(
                "stream_summary channel=%s reason=%s uptime_s=%.1f sessions=%d source_events=%s",
                cid, reason, max(0, time.monotonic() - stream.started),
                len(stream.sessions), dict(stream.source_events),
            )
        if reason in ("failed", "stalled", "startup_timeout"):
            failures = min(5, self.cooldowns.get(cid, (0, 0))[1] + 1)
            self.cooldowns[cid] = (
                time.monotonic() + min(15, 2 ** (failures - 1)),
                failures,
            )
            if stream.errors:
                LOG.warning("channel=%s ffmpeg: %s", cid, stream.errors[-1])

    @staticmethod
    def _touch_locked(stream: Stream, session: str | None, now: float | None = None):
        now = time.monotonic() if now is None else now
        stream.last_request = now
        if session is None:
            stream.anonymous_last_request = now
        else:
            stream.sessions[session] = now

    def _ensure_locked(self, cid: int, session: str | None = None) -> Stream:
        if self.closed.is_set():
            raise StreamError(503, "服務正在停止")
        channel = self.channels.get(cid)
        if not channel:
            raise StreamError(404, "頻道不存在")
        now = time.monotonic()
        if session is not None and self.released_sessions.get((cid, session), 0) > now:
            raise StreamError(410, "串流已釋放")
        stream = self.streams.get(cid)
        if stream and stream.process.poll() is not None:
            self._stop_locked(cid, "failed")
            stream = None
        if stream:
            self._touch_locked(stream, session, now)
            return stream
        if self.cooldowns.get(cid, (0, 0))[0] > now:
            raise StreamError(503, "串流正在恢復，請稍候")
        if len(self.streams) >= self.settings.max_streams:
            raise StreamError(503, "目前觀看中的頻道已達上限，請稍候")
        token = uuid.uuid4().hex
        directory = self.root / token
        directory.mkdir(parents=True, exist_ok=False)
        try:
            process = subprocess.Popen(
                ffmpeg_command(self.settings, channel, directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if self.settings.debug else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise StreamError(502, "無法啟動 FFmpeg") from exc
        stream = Stream(channel, token, directory, process, now, now)
        self._touch_locked(stream, session, now)
        self.streams[cid] = stream
        threading.Thread(target=self._read_errors, args=(stream,), daemon=True).start()
        if self.settings.debug:
            threading.Thread(target=self._read_progress, args=(stream,), daemon=True).start()
        if self.settings.debug:
            LOG.debug("channel=%s started pid=%s", cid, process.pid)
        return stream

    @staticmethod
    def _ready(stream: Stream) -> bool:
        try:
            # Start as soon as the first complete segment exists. Playback is
            # allowed to drift behind the source later; startup itself should not
            # deliberately wait for a large buffer.
            return (stream.directory / "index.m3u8").read_text().count("#EXTINF:") >= 1
        except (FileNotFoundError, OSError, UnicodeError):
            return False

    @staticmethod
    def _rewrite_playlist(stream: Stream, session: str | None) -> bytes:
        text = (stream.directory / "index.m3u8").read_text()
        rows = []
        suffix = f"?session={session}" if session is not None else ""
        for line in text.splitlines():
            rows.append(f"{stream.token}/{line}{suffix}" if line and not line.startswith("#") else line)
        return ("\n".join(rows) + "\n").encode()

    def playlist(self, cid: int, session: str | None = None) -> bytes:
        with self.lock:
            stream = self._ensure_locked(cid, session)
        deadline = time.monotonic() + self.settings.startup_timeout
        while not self.closed.is_set():
            with self.lock:
                if self.streams.get(cid) is not stream:
                    raise StreamError(503, "串流已重新啟動，請稍候")
                now = time.monotonic()
                if session is not None and self.released_sessions.get((cid, session), 0) > now:
                    raise StreamError(410, "串流已釋放")
                self._touch_locked(stream, session, now)
                if stream.process.poll() is not None:
                    self._stop_locked(cid, "failed")
                    raise StreamError(502, "來源無法連線或轉碼失敗")
                if self._ready(stream):
                    try:
                        return self._rewrite_playlist(stream, session)
                    except (FileNotFoundError, OSError, UnicodeError):
                        pass
                if now >= deadline:
                    self._stop_locked(cid, "startup_timeout")
                    raise StreamError(504, "串流啟動逾時，請確認來源是否可用")
            self.closed.wait(0.15)
        raise StreamError(503, "服務正在停止")

    def open_resource(self, cid: int, token: str, filename: str, session: str | None = None):
        with self.lock:
            if (session is not None
                    and self.released_sessions.get((cid, session), 0) > time.monotonic()):
                raise StreamError(410, "串流已釋放")
            stream = self.streams.get(cid)
            if not stream or stream.token != token:
                raise StreamError(410, "串流已過期，請重新取得播放清單")
            if stream.process.poll() is not None:
                self._stop_locked(cid, "failed")
                raise StreamError(503, "串流正在恢復")
            try:
                file = (stream.directory / filename).open("rb")
            except FileNotFoundError as exc:
                raise StreamError(404, "分段已過期或尚未完成") from exc
            self._touch_locked(stream, session)
            return file

    def release(self, cid: int, session: str) -> bool:
        with self.lock:
            if cid not in self.channels:
                return False
            now = time.monotonic()
            self.released_sessions[(cid, session)] = now + self.settings.idle_timeout
            while len(self.released_sessions) > 4096:
                self.released_sessions.pop(next(iter(self.released_sessions)))
            stream = self.streams.get(cid)
            if not stream:
                return False
            stream.sessions.pop(session, None)
            for sid, last_seen in list(stream.sessions.items()):
                if now - last_seen > self.settings.idle_timeout:
                    stream.sessions.pop(sid, None)
            anonymous_active = (stream.anonymous_last_request is not None
                                and now - stream.anonymous_last_request <= self.settings.idle_timeout)
            if not stream.sessions and not anonymous_active:
                self._stop_locked(cid, "released")
                return True
            return False

    def _reap(self):
        while not self.closed.wait(1):
            with self.lock:
                now = time.monotonic()
                for key, expiry in list(self.released_sessions.items()):
                    if expiry <= now:
                        self.released_sessions.pop(key, None)
                for cid, stream in list(self.streams.items()):
                    self._debug_hls_locked(cid, stream, now)
                    if now - stream.last_request > self.settings.idle_timeout:
                        self._stop_locked(cid, "idle")
                    elif stream.process.poll() is not None:
                        self._stop_locked(cid, "failed")
                    elif now - stream.started > self.settings.startup_timeout:
                        playlist = stream.directory / "index.m3u8"
                        try:
                            latest = playlist.stat().st_mtime
                        except FileNotFoundError:
                            latest = 0
                        if not latest:
                            self._stop_locked(cid, "startup_timeout")
                        elif time.time() - latest > 20:
                            self._stop_locked(cid, "stalled")
                        elif now - stream.started > 60:
                            self.cooldowns.pop(cid, None)

    def close(self):
        self.closed.set()
        self.reaper.join(timeout=2)
        with self.lock:
            for cid in list(self.streams):
                self._stop_locked(cid, "shutdown")
        shutil.rmtree(self.root, ignore_errors=True)
