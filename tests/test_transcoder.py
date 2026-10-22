import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from udisplayer.config import Channel, Settings
from udisplayer.transcoder import StreamError, StreamManager, ffmpeg_command


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
