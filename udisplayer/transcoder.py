from __future__ import annotations

import logging
import os
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
    # Explicit continuity and timestamp anomalies outrank generic corruption.
    # A "Packet corrupt" warning alone does not establish a TS CC error.
    ("continuity", re.compile(r"continuity check|packet loss|missing packet", re.I)),
    ("timestamp", re.compile(r"non[- ]monoton|invalid timestamps?|"
                              r"\b(?:dts|pts)\b.*(?:invalid|out of order)|"
                              r"pts\s*[<>=]+\s*dts", re.I)),
    ("pes", re.compile(r"PES packet size mismatch|PES header|PES packet", re.I)),
    ("decoder_syntax", re.compile(r"invalid sequence header|invalid pred mode|"
                                   r"AEC picture without a slice header|"
                                   r"invalid intra_chroma|invalid CU", re.I)),
    ("corrupt", re.compile(r"corrupt|invalid data|error while decoding", re.I)),
)


def classify_source_event(message: str) -> str | None:
    """Conservative FFmpeg classification: never infer TS CC from corruption."""
    return next((name for name, pattern in SOURCE_EVENT_PATTERNS
                 if pattern.search(message)), None)

SOURCE_TERMINAL_ERRORS = re.compile(
    r"tim(?:e|ed) ?out|connection (?:reset|refused|closed)|broken pipe|"
    r"http error (?:4\d\d|5\d\d)|server returned (?:4\d\d|5\d\d)|"
    r"name or service not known|temporary failure in name resolution|"
    r"network is unreachable|no route to host", re.I,
)
HARDWARE_ERRORS = re.compile(
    r"vaapi|va-api|libva|hwupload|hwdownload|hardware device|"
    r"failed to create (?:a )?device|no hw frames", re.I,
)

DECODE_ERRORS = re.compile(
    r"hwdownload|scale_vaapi|decoder|decoding|decode failed|"
    r"failed setup for format vaapi|failed to transfer.*from hardware|"
    r"failed to retrieve.*frame|impossible to convert between the formats|"
    r"error reinitializing filters|auto_scale_", re.I,
)
ENCODE_ERRORS = re.compile(
    r"encoder failed to initialize|failed to (?:initialize|initialise) encoder|"
    r"error (?:initializing|initialising) output stream|error while opening encoder|"
    r"error (?:while )?encoding|encode failed|rate control|rc_mode|failed to upload|"
    r"h264_vaapi.*(?:unsupported|failed|error|invalid configuration)", re.I,
)

# Failover is per channel.  The HLS URL and viewer sessions remain unchanged;
# a new encoder continues the same MPEG-TS/H.264/AAC playlist sequence.
HW_DECODE_HW_ENCODE = "vaapi_decode_encode"
SW_DECODE_HW_ENCODE = "vaapi_encode"
HW_DECODE_SW_ENCODE = "vaapi_decode"
SOFTWARE = "software"


def backend_candidates(settings: Settings, channel: Channel | None = None) -> tuple[str, ...]:
    # AVS+/AVS2 decoding is not supported by VA-API on this pipeline. Avoid
    # trying the hardware decoder, timing out, and reconnecting the live source
    # just to fall back to software decoding. Retain hardware H.264 encoding.
    if channel is not None and channel.codec_hint in ("cavs", "avs2"):
        return (SW_DECODE_HW_ENCODE, SOFTWARE) if settings.hw_render and settings.hw_encode else (SOFTWARE,)
    if not settings.hw_render:
        return (SOFTWARE,)
    if settings.hw_decode and settings.hw_encode:
        return (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE,
                HW_DECODE_SW_ENCODE, SOFTWARE)
    if settings.hw_encode:
        return (SW_DECODE_HW_ENCODE, SOFTWARE)
    if settings.hw_decode:
        return (HW_DECODE_SW_ENCODE, SOFTWARE)
    return (SOFTWARE,)


class StreamError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def ffmpeg_command(settings: Settings, channel: Channel, directory: Path,
                   *, backend: str = SOFTWARE, start_number: int | None = None,
                   discontinuity: bool = False,
                   enable_gpu_scale: bool = True) -> list[str]:
    # Preserve source resolution up to 1080p. Larger sources are downscaled
    # inside 1920x1080, cap output at 30 fps, then encode as progressive
    # H.264/AAC for broad browser compatibility and smoother Internet playback.
    start_number = str(int(time.time() // 2) if start_number is None else start_number)
    hardware_decode = backend in (HW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE)
    hardware_encode = backend in (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE)
    if backend not in (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE, SOFTWARE):
        raise ValueError("不支援的轉碼管線")
    if (hardware_decode and not settings.hw_decode) or (hardware_encode and not settings.hw_encode):
        raise ValueError("尚未驗證要求的硬體轉碼能力")
    if hardware_decode and channel.codec_hint in ("cavs", "avs2"):
        raise ValueError("此頻道標記為 AVS 軟體解碼，不能使用 VA-API 解碼")
    if (hardware_decode or hardware_encode) and not settings.hw_render:
        raise ValueError("硬體轉碼缺少 DRM Render Node")
    # GPU scaling is an experimental *opt-in* for other GPUs. On the measured
    # four-vCPU AVS2 host the extra 4K upload was 6–11% slower than CPU scale;
    # keep CPU scaling the default even when hardware H.264 encoding is used.
    gpu_scale = (enable_gpu_scale and channel.avs2_gpu_scale
                 and channel.video_scan == "progressive"
                 and channel.codec_hint == "avs2"
                 and backend == SW_DECODE_HW_ENCODE)
    deinterlace = ("" if channel.video_scan == "progressive" else
                   "bwdif=mode=send_frame:parity=auto:deint=interlaced,")
    frame_rate = "fps=fps='min(30,if(gte(source_fps,50),source_fps/2,source_fps))'"
    # Only when the source is confirmed progressive and GPU scale is explicitly
    # requested: 4K -> GPU upload -> VA-API scaling and 10-bit/8-bit conversion
    # -> h264_vaapi. Do not download frames or upload them a second time.
    if gpu_scale:
        video_filters = (
            frame_rate + ",format=nv12,hwupload,"
            "scale_vaapi=w=1920:h=1080:format=nv12:mode=fast:"
            "force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1"
        )
    else:
        video_filters = (
            ("scale_vaapi=format=nv12,hwdownload,format=nv12," if hardware_decode else "")
            + deinterlace + frame_rate + ","
            "scale=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)':"
            "h='max(2,trunc(ih/2)*2)':flags=fast_bilinear,setsar=1,"
            "scale=w='min(iw,1920)':h='min(ih,1080)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2:"
            "flags=fast_bilinear"
            + (",format=nv12,hwupload" if hardware_encode else ",format=yuv420p")
        )
    command = [settings.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    if settings.debug:
        # FFmpeg's machine-readable progress stream is emitted on stdout only in
        # debug mode; media still goes to HLS files.
        command += ["-progress", "pipe:1"]
    if hardware_decode or hardware_encode:
        command += ["-init_hw_device", f"vaapi=va:{settings.hw_render}",
                    "-filter_hw_device", "va"]
    if hardware_decode:
        command += ["-hwaccel", "vaapi", "-hwaccel_device", "va",
                    "-hwaccel_output_format", "vaapi"]
    command += [
        "-y", "-rw_timeout", "10000000", "-fflags", "+discardcorrupt",
        # 500 kB / 0.5 s often misses the AVS+/AVS2 sequence header after
        # joining live MPEG-TS mid-GOP. These are upper bounds; FFmpeg can
        # finish probing sooner when it identifies streams successfully.
        "-analyzeduration", "4000000", "-probesize", "4000000",
        # Decoder thread count is independent of encoder workers. AVS2 4K
        # often benefits from more decoder threads without increasing x264's
        # own thread count. Preserve the historical default of two.
        "-threads", str(channel.decoder_threads or settings.decoder_threads), "-i", channel.url,
        "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn",
        # Some broadcast sources use non-square pixels (for example 720x576
        # with SAR 16:15 for a 4:3 picture). First bake SAR into the coded width
        # so browsers receive square pixels with the correct DAR. Then cap the
        # square-pixel result inside 1920x1080; <=1080p sources remain unchanged.
        # Drop frames after deinterlacing but before scaling / encoding. Preserve
        # rates <=30 fps, halve 50/59.94/60 fps for even frame cadence, and cap
        # any higher rates at 30 fps. The decoder still handles reference frames.
        "-vf", video_filters,
        # fps already owns the output cadence. Do not let a second FFmpeg
        # sync stage duplicate frames to fill start-time gaps in live TV.
        "-fps_mode:v", "passthrough",
        # superfast leaves enough realtime headroom for 1080i + bwdif on the
        # production 2-vCPU host. A fixed CCTV-5 sample benchmark improved from
        # about 1.09x (veryfast) to 1.58x while staying near the same 2.2 Mbps VBV.
        "-c:v", "h264_vaapi" if hardware_encode else "libx264",
    ]
    if hardware_encode:
        # VA-API rate-control is driver-specific. No CPU preset/CRF or x264-only
        # switches are passed to the hardware encoder.
        command += ["-profile:v", "high", "-bf", "0", "-b:v", "2000000"]
        if settings.hw_rc_mode == "VBR":
            command += ["-rc_mode", "VBR"]
    else:
        command += ["-preset", "superfast", "-crf", "26", "-pix_fmt", "yuv420p",
                    "-profile:v", "high", "-bf", "2", "-refs", "3",
                    "-threads:v", str(settings.encoder_threads), "-forced-idr", "1"]
    command += ["-g", "60", "-keyint_min", "1"]
    if not hardware_encode:
        command += ["-sc_threshold", "0"]
    command += [
        "-flags", "+cgop",
        "-force_key_frames:v", "expr:gte(t,n_forced*2)",
        "-maxrate:v", "2200000", "-bufsize:v", "2200000",
        "-c:a", "aac", "-profile:a", "aac_low", "-ac", "2", "-ar", "48000",
        "-af", "aresample=async=1", "-b:a", "80000",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "24",
        "-hls_delete_threshold", "6", "-start_number", start_number,
        "-hls_flags", "delete_segments+independent_segments+temp_file+program_date_time"
        + ("+discont_start" if discontinuity else ""),
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
    backends: tuple[str, ...] = (SOFTWARE,)
    backend_index: int = 0
    start_number: int = 0
    ready_after: float = 0.0
    decode_failed: bool = False
    encode_failed: bool = False
    error_lock: threading.Lock = field(default_factory=threading.Lock)
    error_reader: threading.Thread | None = None
    gpu_scale_disabled: bool = False


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

    def _read_errors(self, stream: Stream, process: subprocess.Popen):
        assert process.stderr is not None
        with process.stderr:
            for line in process.stderr:
                # Source URLs may contain credentials. Keep them out of logs.
                cleaned = line.strip().replace(stream.channel.url, "[source]")
                cleaned = re.sub(r"https?://\S+", "[source]", cleaned)[:500]
                category = classify_source_event(cleaned)
                with stream.error_lock:
                    if stream.process is process:
                        stream.errors.append(cleaned)
                        if self.settings.debug and category:
                            stream.source_events[category] += 1
                if self.settings.debug and category:
                    LOG.debug(
                        "source_event channel=%s category=%s message=%s",
                        stream.channel.id, category, cleaned,
                    )

    def _read_progress(self, stream: Stream, process: subprocess.Popen):
        assert process.stdout is not None
        values = {}
        last_emit = 0.0
        with process.stdout:
            for raw in process.stdout:
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
            "latest_generation_gap_ms=%s avg_generation_gap_ms=%s sessions=%d "
            "source_events=%s transcoder=%s",
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
            stream.backends[stream.backend_index],
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

    def _launch(self, stream: Stream, *, discontinuity: bool):
        backend = stream.backends[stream.backend_index]
        # Do not reuse names produced by a previous process, even if two
        # fallbacks happen within the same two-second clock interval.
        existing = [int(m.group(1)) for path in stream.directory.glob("segment_*.ts")
                    if (m := re.fullmatch(r"segment_(\d+)\.ts", path.name))]
        stream.start_number = max(int(time.time() // 2), stream.start_number + 1,
                                  max(existing, default=0) + 1)
        environment = os.environ.copy()
        if stream.channel.avs2_skip_nonref:
            # Opt in for this AVS2 channel's decoder process only. The
            # patched libdavs2 still verifies bit depth, fps and RPS safety.
            environment["UDISPLAYER_AVS2_SKIP_NONREF"] = "1"
        process = subprocess.Popen(
            ffmpeg_command(self.settings, stream.channel, stream.directory,
                           backend=backend, start_number=stream.start_number,
                           discontinuity=discontinuity,
                           enable_gpu_scale=not stream.gpu_scale_disabled),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if self.settings.debug else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
            text=True, errors="replace",
        )
        stream.process = process
        stream.started = time.monotonic()
        stream.ready_after = time.time() if discontinuity else 0.0
        with stream.error_lock:
            stream.errors.clear()
        reader = threading.Thread(target=self._read_errors, args=(stream, process), daemon=True)
        stream.error_reader = reader
        reader.start()
        if self.settings.debug:
            threading.Thread(target=self._read_progress, args=(stream, process), daemon=True).start()
        LOG.info("channel=%s transcoder=%s gpu_scale=%s progressive=%s pid=%s",
                 stream.channel.id, backend,
                 bool(stream.channel.avs2_gpu_scale and not stream.gpu_scale_disabled
                      and backend == SW_DECODE_HW_ENCODE),
                 stream.channel.video_scan == "progressive", process.pid)

    def _recover_locked(self, cid: int, stream: Stream, reason: str) -> bool:
        """Disable only the failing acceleration stage of this channel."""
        # Drain diagnostics from the finished FFmpeg before deciding which
        # side failed. The stderr reader uses its own lock, so joining it does
        # not deadlock this manager's lock during recovery.
        if stream.process.poll() is not None and stream.error_reader:
            stream.error_reader.join(timeout=0.5)
        with stream.error_lock:
            messages = tuple(stream.errors)
        # A dead source is not a reason to permanently downgrade the encoder.
        # Let the existing bounded cooldown retry the preferred profile once
        # the source has recovered.
        if reason == "exit" and any(SOURCE_TERMINAL_ERRORS.search(line) for line in messages) \
                and not any(HARDWARE_ERRORS.search(line) for line in messages):
            return False
        old = stream.backends[stream.backend_index]
        decoding = old in (HW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE)
        encoding = old in (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE)
        if not (decoding or encoding):
            return False
        errors = "\n".join(messages)
        decoder_issue = bool(DECODE_ERRORS.search(errors))
        encoder_issue = bool(ENCODE_ERRORS.search(errors))
        # If a newer GPU rejects GPU scaling or frame upload, retry within
        # the *same* VA-API encoder backend using the proven CPU scale path.
        # Do not downgrade working H.264 hardware encoding merely because a
        # scale_vaapi filter was unavailable or failed to negotiate a format.
        if (old == SW_DECODE_HW_ENCODE and stream.channel.avs2_gpu_scale
                and not stream.gpu_scale_disabled
                and decoder_issue and not encoder_issue):
            if stream.process.poll() is None:
                stream.process.terminate()
                try:
                    stream.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    stream.process.kill()
                    stream.process.wait(timeout=3)
            stream.gpu_scale_disabled = True
            try:
                self._launch(stream, discontinuity=True)
            except OSError as exc:
                LOG.warning("channel=%s gpu_scale_fallback spawn_error=%s", cid,
                            type(exc).__name__)
                return False
            LOG.warning("channel=%s gpu_scale_fallback to=cpu_scale+h264_vaapi reason=%s",
                        cid, reason)
            return True
        # When FFmpeg identifies a specific failing stage, keep the other
        # stage accelerated. For generic failures, try software decoding first
        # while preserving the verified H.264 encoder.
        if decoding and decoder_issue and not encoder_issue:
            stream.decode_failed = True
        elif encoding and encoder_issue and not decoder_issue:
            stream.encode_failed = True
        elif decoding:
            stream.decode_failed = True
        elif encoding:
            stream.encode_failed = True

        next_index = next((index for index, candidate in enumerate(stream.backends)
                           if candidate != old
                           and (not stream.decode_failed or candidate not in
                                (HW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE))
                           and (not stream.encode_failed or candidate not in
                                (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE))), None)
        if next_index is None:
            return False
        if stream.process.poll() is None:
            stream.process.terminate()
            try:
                stream.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stream.process.kill()
                stream.process.wait(timeout=3)
        prior_error = messages[-1] if messages else "none"
        stream.backend_index = next_index
        try:
            self._launch(stream, discontinuity=True)
        except OSError as exc:
            LOG.warning("channel=%s transcoder_fallback spawn_error=%s", cid, type(exc).__name__)
            return False
        LOG.warning("channel=%s transcoder_fallback from=%s to=%s reason=%s last_error=%s",
                    cid, old, stream.backends[stream.backend_index], reason, prior_error)
        return True

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
            if not self._recover_locked(cid, stream, "exit"):
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
        stream = Stream(channel, token, directory, None, now, now,
                        backends=backend_candidates(self.settings, channel))
        try:
            self._launch(stream, discontinuity=False)
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise StreamError(502, "無法啟動 FFmpeg") from exc
        self._touch_locked(stream, session, now)
        self.streams[cid] = stream
        return stream

    @staticmethod
    def _ready(stream: Stream) -> bool:
        try:
            # Start as soon as the first complete segment exists. Playback is
            # allowed to drift behind the source later; startup itself should not
            # deliberately wait for a large buffer.
            playlist = stream.directory / "index.m3u8"
            return (playlist.stat().st_mtime >= stream.ready_after
                    and playlist.read_text().count("#EXTINF:") >= 1)
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
                    if self._recover_locked(cid, stream, "exit"):
                        continue
                    self._stop_locked(cid, "failed")
                    raise StreamError(502, "來源無法連線或轉碼失敗")
                if self._ready(stream):
                    try:
                        return self._rewrite_playlist(stream, session)
                    except (FileNotFoundError, OSError, UnicodeError):
                        pass
                if now >= deadline:
                    if self._recover_locked(cid, stream, "startup_timeout"):
                        deadline = time.monotonic() + self.settings.startup_timeout
                        continue
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
                        if not self._recover_locked(cid, stream, "exit"):
                            self._stop_locked(cid, "failed")
                    elif now - stream.started > self.settings.startup_timeout:
                        playlist = stream.directory / "index.m3u8"
                        try:
                            latest = playlist.stat().st_mtime
                        except FileNotFoundError:
                            latest = 0
                        if not latest:
                            if not self._recover_locked(cid, stream, "startup_timeout"):
                                self._stop_locked(cid, "startup_timeout")
                        elif time.time() - latest > 20:
                            if not self._recover_locked(cid, stream, "stalled"):
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
