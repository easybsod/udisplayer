import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from udisplayer.config import Channel, Settings
from udisplayer.transcoder import StreamError, StreamManager, ffmpeg_command
from udisplayer.transcoder import (backend_candidates, HW_DECODE_HW_ENCODE,
                                  SW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE, SOFTWARE)


class FakeProcess:
    next_pid = 1000

    def __init__(self):
        self.returncode = None
        self.stderr = io.StringIO("")
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class TranscoderTests(unittest.TestCase):
    def setUp(self):
        self.channel = Channel(1, "Test", "http://127.0.0.1/live")

    def test_ffmpeg_command_preserves_display_aspect_and_has_single_output(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", encoder_threads=2)
        command = ffmpeg_command(settings, self.channel, Path("/tmp/udisplayer-test"))
        joined = " ".join(command)
        self.assertIn("bwdif=mode=send_frame:parity=auto:deint=interlaced", joined)
        self.assertIn("scale=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)'", joined)
        self.assertIn("setsar=1", joined)
        self.assertIn("scale=w='min(iw,1920)':h='min(ih,1080)'", joined)
        self.assertIn("force_original_aspect_ratio=decrease", joined)
        self.assertIn("force_divisible_by=2", joined)
        self.assertNotIn("split=", joined)
        self.assertNotIn("-var_stream_map", command)
        self.assertEqual(command.count("-map"), 2)
        self.assertEqual(command[command.index("-analyzeduration") + 1], "500000")
        self.assertEqual(command[command.index("-probesize") + 1], "500000")
        self.assertIn("2200000", command)
        self.assertIn("80000", command)
        self.assertIn("superfast", command)
        self.assertNotIn("veryfast", command)
        self.assertIn("26", command)
        self.assertIn("high", command)
        self.assertEqual(command[command.index("-bf") + 1], "2")
        self.assertEqual(command[command.index("-bufsize:v") + 1], "2200000")
        self.assertNotIn("zerolatency", command)
        self.assertTrue(command[-1].endswith("/index.m3u8"))

    def test_debug_ffmpeg_command_enables_progress_only_in_debug(self):
        normal = ffmpeg_command(Settings(ffmpeg="/usr/bin/ffmpeg"), self.channel, Path("/tmp/normal"))
        debug = ffmpeg_command(Settings(ffmpeg="/usr/bin/ffmpeg", debug=True), self.channel, Path("/tmp/debug"))
        self.assertNotIn("-progress", normal)
        self.assertIn("-progress", debug)
        self.assertEqual(debug[debug.index("-progress") + 1], "pipe:1")
        self.assertNotIn("-stats_period", debug)

    def test_hardware_pipeline_uses_same_hls_and_dimensions(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True, hw_rc_mode="VBR")
        self.assertEqual(backend_candidates(settings), (HW_DECODE_HW_ENCODE,
                         SW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE, SOFTWARE))
        for backend in backend_candidates(settings):
            with self.subTest(backend=backend):
                cmd = ffmpeg_command(settings, self.channel, Path("/tmp/hw"),
                                     backend=backend, start_number=1234, discontinuity=True)
                rendered = " ".join(cmd)
                self.assertEqual(cmd.count("-map"), 2)
                self.assertIn("-start_number 1234", rendered)
                self.assertIn("discont_start", rendered)
                self.assertIn("force_original_aspect_ratio=decrease", rendered)
                self.assertIn("bwdif=mode=send_frame", rendered)
                self.assertIn("-c:a aac", rendered)
                self.assertIn("-f hls", rendered)
                self.assertIn("-hls_time 2", rendered)
                self.assertEqual("h264_vaapi" in cmd, backend in
                                 (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE))
                self.assertEqual("-hwaccel" in cmd, backend in
                                 (HW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE))
                self.assertEqual("-rc_mode" in cmd, backend in
                                 (HW_DECODE_HW_ENCODE, SW_DECODE_HW_ENCODE))
                if backend == HW_DECODE_HW_ENCODE:
                    self.assertIn("scale_vaapi=format=nv12,hwdownload,format=nv12", rendered)
                    self.assertIn("format=nv12,hwupload", rendered)
                if backend == SOFTWARE:
                    self.assertNotIn("-init_hw_device", cmd)

    def test_decoder_encoder_fallback_are_independent(self):
        base = dict(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128")
        self.assertEqual(backend_candidates(Settings(**base, hw_encode=True)),
                         (SW_DECODE_HW_ENCODE, SOFTWARE))
        self.assertEqual(backend_candidates(Settings(**base, hw_decode=True)),
                         (HW_DECODE_SW_ENCODE, SOFTWARE))
        self.assertEqual(backend_candidates(Settings(**base)), (SOFTWARE,))
        with self.assertRaisesRegex(ValueError, "尚未驗證"):
            ffmpeg_command(Settings(**base), self.channel, Path("/tmp"),
                           backend=SW_DECODE_HW_ENCODE)

    def test_hardware_exit_restarts_with_same_token_and_software_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ffmpeg="/usr/bin/ffmpeg", cache_dir=Path(tmp),
                                hw_render="/dev/dri/renderD128", hw_decode=True, hw_encode=True,
                                startup_timeout=3)
            processes = []

            def fake_popen(cmd, **_kwargs):
                p = FakeProcess()
                processes.append(p)
                # Synthetic failures for all accelerated profiles, preserving
                # the same token and keeping existing HLS segment names unique.
                if len(processes) < 3:
                    p.returncode = 1
                else:
                    playlist = Path(cmd[-1])
                    playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nsegment_000000999.ts\n")
                    os.utime(playlist, (time.time() + 1, time.time() + 1))
                return p

            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=fake_popen):
                manager = StreamManager(settings, (self.channel,))
                try:
                    playlist = manager.playlist(1, "a" * 32)
                    stream = manager.streams[1]
                    self.assertIn(b"segment_", playlist)
                    self.assertEqual(stream.backends[stream.backend_index], SOFTWARE)
                    self.assertEqual(len(processes), 3)
                    self.assertEqual(stream.token, manager.streams[1].token)
                finally:
                    manager.close()

    def test_network_failure_does_not_downgrade_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ffmpeg="/usr/bin/ffmpeg", cache_dir=Path(tmp),
                                hw_render="/dev/dri/renderD128", hw_decode=True, hw_encode=True)
            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=lambda *_a, **_k: FakeProcess()):
                manager = StreamManager(settings, (self.channel,))
                try:
                    with manager.lock:
                        stream = manager._ensure_locked(1)
                        stream.errors.append("HTTP error 503 Service Unavailable")
                        self.assertFalse(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backend_index, 0)
                        stream.errors.clear()
                        stream.errors.append("Failed to initialise VAAPI hardware device")
                        self.assertTrue(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backend_index, 1)
                finally:
                    manager.close()

    def test_encoder_error_preserves_hardware_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ffmpeg="/usr/bin/ffmpeg", cache_dir=Path(tmp),
                                hw_render="/dev/dri/renderD128", hw_decode=True, hw_encode=True)
            with patch("udisplayer.transcoder.subprocess.Popen",
                       side_effect=lambda *_a, **_k: FakeProcess()):
                manager = StreamManager(settings, (self.channel,))
                try:
                    with manager.lock:
                        stream = manager._ensure_locked(1)
                        stream.errors.append("[h264_vaapi] Encoder failed to initialize")
                        self.assertTrue(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backends[stream.backend_index], HW_DECODE_SW_ENCODE)
                        self.assertTrue(stream.encode_failed)
                        self.assertFalse(stream.decode_failed)
                        stream.errors.append("Error during decoding")
                        self.assertTrue(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backends[stream.backend_index], SOFTWARE)
                finally:
                    manager.close()

    def test_decoder_error_preserves_hardware_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ffmpeg="/usr/bin/ffmpeg", cache_dir=Path(tmp),
                                hw_render="/dev/dri/renderD128", hw_decode=True, hw_encode=True)
            with patch("udisplayer.transcoder.subprocess.Popen",
                       side_effect=lambda *_a, **_k: FakeProcess()):
                manager = StreamManager(settings, (self.channel,))
                try:
                    with manager.lock:
                        stream = manager._ensure_locked(1)
                        stream.errors.append("Failed setup for format vaapi in decoder")
                        self.assertTrue(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backends[stream.backend_index], SW_DECODE_HW_ENCODE)
                        self.assertTrue(stream.decode_failed)
                        self.assertFalse(stream.encode_failed)
                        stream.errors.append("h264_vaapi encoding error")
                        self.assertTrue(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backends[stream.backend_index], SOFTWARE)
                finally:
                    manager.close()

    def test_filter_failure_does_not_misclassify_downstream_encoder_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ffmpeg="/usr/bin/ffmpeg", cache_dir=Path(tmp),
                                hw_render="/dev/dri/renderD128", hw_decode=True, hw_encode=True)
            with patch("udisplayer.transcoder.subprocess.Popen",
                       side_effect=lambda *_a, **_k: FakeProcess()):
                manager = StreamManager(settings, (self.channel,))
                try:
                    with manager.lock:
                        stream = manager._ensure_locked(1)
                        stream.errors.extend((
                            "Impossible to convert between the formats supported by the filter ",
                            "[vf#0:0] Error reinitializing filters!",
                            "[vost#0:0/h264_vaapi] Could not open encoder before EOF",
                            "[vost#0:0/h264_vaapi] Terminating thread with return code -22",
                            "[out#0/hls] Nothing was written into output file",
                        ))
                        self.assertTrue(manager._recover_locked(1, stream, "exit"))
                        self.assertEqual(stream.backends[stream.backend_index], SW_DECODE_HW_ENCODE)
                        self.assertTrue(stream.decode_failed)
                        self.assertFalse(stream.encode_failed)
                finally:
                    manager.close()

    def test_playlist_starts_one_worker_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                ffmpeg="/usr/bin/ffmpeg",
                cache_dir=Path(tmp),
                max_streams=2,
                startup_timeout=2,
            )

            def fake_popen(command, **_kwargs):
                process = FakeProcess()
                playlist = Path(command[-1])
                playlist.parent.mkdir(parents=True, exist_ok=True)
                playlist.write_text(
                    "#EXTM3U\n"
                    "#EXT-X-VERSION:6\n"
                    "#EXT-X-TARGETDURATION:2\n"
                    "#EXT-X-MEDIA-SEQUENCE:1\n"
                    "#EXTINF:2.0,\nsegment_000000001.ts\n",
                    encoding="utf-8",
                )
                (playlist.parent / "segment_000000001.ts").write_bytes(b"segment")
                return process

            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=fake_popen) as popen:
                manager = StreamManager(settings, (self.channel,))
                try:
                    session = "a" * 32
                    playlist = manager.playlist(1, session).decode()
                    self.assertEqual(popen.call_count, 1)
                    token = manager.streams[1].token
                    self.assertNotIn("#EXT-X-START", playlist)
                    self.assertIn(f"{token}/segment_000000001.ts?session={session}", playlist)
                    self.assertNotIn("low", playlist)
                    self.assertNotIn("medium", playlist)
                    self.assertNotIn("high", playlist)

                    manager.playlist(1, session)
                    self.assertEqual(popen.call_count, 1)

                    with manager.open_resource(1, token, "segment_000000001.ts", session) as resource:
                        self.assertEqual(resource.read(), b"segment")
                finally:
                    manager.close()

    def test_release_during_startup_stops_pending_worker_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                ffmpeg="/usr/bin/ffmpeg",
                cache_dir=Path(tmp),
                max_streams=2,
                startup_timeout=5,
            )

            with patch("udisplayer.transcoder.subprocess.Popen", return_value=FakeProcess()):
                manager = StreamManager(settings, (self.channel,))
                session = "c" * 32
                outcome = {}

                def load_playlist():
                    try:
                        manager.playlist(1, session)
                    except StreamError as exc:
                        outcome["status"] = exc.status

                worker = threading.Thread(target=load_playlist)
                worker.start()
                try:
                    deadline = time.monotonic() + 1
                    while 1 not in manager.streams and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIn(1, manager.streams)
                    process = manager.streams[1].process

                    self.assertTrue(manager.release(1, session))
                    self.assertNotIn(1, manager.streams)
                    self.assertIsNotNone(process.poll())

                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                    self.assertIn(outcome.get("status"), (410, 503))

                    with self.assertRaises(StreamError) as released:
                        manager.playlist(1, session)
                    self.assertEqual(released.exception.status, 410)
                finally:
                    manager.close()
                    worker.join(timeout=1)

    def test_release_stops_only_after_last_session_leaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                ffmpeg="/usr/bin/ffmpeg",
                cache_dir=Path(tmp),
                max_streams=2,
                startup_timeout=2,
            )

            def fake_popen(command, **_kwargs):
                process = FakeProcess()
                playlist = Path(command[-1])
                playlist.parent.mkdir(parents=True, exist_ok=True)
                playlist.write_text(
                    "#EXTM3U\n#EXT-X-VERSION:6\n#EXTINF:2.0,\nsegment_000000001.ts\n",
                    encoding="utf-8",
                )
                (playlist.parent / "segment_000000001.ts").write_bytes(b"segment")
                return process

            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=fake_popen):
                manager = StreamManager(settings, (self.channel,))
                try:
                    first = "a" * 32
                    second = "b" * 32
                    manager.playlist(1, first)
                    process = manager.streams[1].process
                    manager.playlist(1, second)
                    token = manager.streams[1].token
                    self.assertFalse(manager.release(1, first))
                    self.assertIn(1, manager.streams)
                    self.assertIsNone(process.poll())
                    with self.assertRaises(StreamError) as released:
                        manager.open_resource(1, token, "segment_000000001.ts", first)
                    self.assertEqual(released.exception.status, 410)
                    self.assertNotIn(first, manager.streams[1].sessions)
                    self.assertTrue(manager.release(1, second))
                    self.assertNotIn(1, manager.streams)
                    self.assertIsNotNone(process.poll())
                finally:
                    manager.close()

    def test_max_streams_limits_channels_not_renditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                ffmpeg="/usr/bin/ffmpeg",
                cache_dir=Path(tmp),
                max_streams=1,
                startup_timeout=2,
            )
            second = Channel(2, "Second", "http://127.0.0.1/live2")

            def fake_popen(command, **_kwargs):
                playlist = Path(command[-1])
                playlist.parent.mkdir(parents=True, exist_ok=True)
                playlist.write_text(
                    "#EXTM3U\n#EXT-X-VERSION:6\n#EXTINF:2.0,\na.ts\n",
                    encoding="utf-8",
                )
                return FakeProcess()

            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=fake_popen):
                manager = StreamManager(settings, (self.channel, second))
                try:
                    manager.playlist(1)
                    with self.assertRaises(StreamError) as raised:
                        manager.playlist(2)
                    self.assertEqual(raised.exception.status, 503)
                finally:
                    manager.close()


if __name__ == "__main__":
    unittest.main()
