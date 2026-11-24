import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from udisplayer.config import Channel, Settings
from udisplayer.transcoder import StreamError, StreamManager, ffmpeg_command, classify_source_event
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

    def test_source_events_separate_ts_cc_pts_dts_and_decoder_failures(self):
        self.assertEqual(classify_source_event("[mpegts] continuity check failed for pid 256 expected 4 got 9"), "continuity")
        self.assertEqual(classify_source_event("[mpegts] Invalid timestamps stream=0, pts=99, dts=101"), "timestamp")
        self.assertEqual(classify_source_event("Non-monotonous DTS in output stream"), "timestamp")
        self.assertEqual(classify_source_event("[mpegts] PES packet size mismatch"), "pes")
        self.assertEqual(classify_source_event("[davs2] invalid pred mode 33"), "decoder_syntax")
        self.assertEqual(classify_source_event("[mpegts] Packet corrupt (stream = 0, dts=300)"), "corrupt")
        self.assertIsNone(classify_source_event("Frame decoded OK"))

    def test_ffmpeg_command_preserves_display_aspect_and_has_single_output(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", encoder_threads=2,
                            decoder_threads=4)
        command = ffmpeg_command(settings, self.channel, Path("/tmp/udisplayer-test"))
        joined = " ".join(command)
        self.assertIn("bwdif=mode=send_frame:parity=auto:deint=interlaced", joined)
        self.assertIn("fps=fps='min(30,if(gte(source_fps,50),source_fps/2,source_fps))'", joined)
        self.assertLess(joined.index("bwdif="), joined.index("fps=fps="))
        self.assertLess(joined.index("fps=fps="), joined.index("scale=w="))
        self.assertIn("scale=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)'", joined)
        self.assertIn("setsar=1", joined)
        self.assertIn("scale=w='min(iw,1920)':h='min(ih,1080)'", joined)
        self.assertIn("force_original_aspect_ratio=decrease", joined)
        self.assertIn("force_divisible_by=2", joined)
        self.assertNotIn("split=", joined)
        self.assertNotIn("-var_stream_map", command)
        self.assertEqual(command.count("-map"), 2)
        self.assertEqual(command[command.index("-analyzeduration") + 1], "4000000")
        self.assertEqual(command[command.index("-probesize") + 1], "4000000")
        self.assertEqual(command[command.index("-threads") + 1], "4")
        self.assertEqual(command[command.index("-threads:v") + 1], "2")
        override = ffmpeg_command(settings,
                                  Channel(2, "AVS2", "http://127.0.0.1/avs2",
                                          decoder_threads=6), Path("/tmp/avstwo"))
        self.assertEqual(override[override.index("-threads") + 1], "6")
        self.assertEqual(override[override.index("-threads:v") + 1], "2")
        self.assertEqual(command[command.index("-fps_mode:v") + 1], "passthrough")
        self.assertLess(command.index("-vf"), command.index("-fps_mode:v"))
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
                if backend != HW_DECODE_HW_ENCODE:
                    self.assertIn("bwdif=mode=send_frame", rendered)
                self.assertIn("fps=fps='min(30,if(gte(source_fps,50),source_fps/2,source_fps))'", rendered)
                self.assertEqual(cmd[cmd.index("-fps_mode:v") + 1], "passthrough")
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
                    self.assertIn("deinterlace_vaapi=mode=motion_adaptive:rate=frame:auto=1", rendered)
                    self.assertIn("scale_vaapi=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)'", rendered)
                    self.assertIn("scale_vaapi=w='min(iw,1920)'", rendered)
                    self.assertIn("setsar=1", rendered)
                    self.assertNotIn("hwdownload", rendered)
                    self.assertNotIn("hwupload", rendered)
                    self.assertLess(rendered.index("deinterlace_vaapi="), rendered.index("fps=fps="))
                if backend == SOFTWARE:
                    self.assertNotIn("-init_hw_device", cmd)

    def test_gpu_vpp_preserves_progressive_frames_and_cpu_filter_fallback(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True)
        progressive = Channel(2, "1080p", "http://127.0.0.1/progressive",
                              video_scan="progressive")
        gpu = ffmpeg_command(settings, progressive, Path("/tmp/gpu"),
                             backend=HW_DECODE_HW_ENCODE)
        gpu_filter = gpu[gpu.index("-vf") + 1]
        self.assertNotIn("deinterlace_vaapi", gpu_filter)
        self.assertIn("fps=fps=", gpu_filter)
        self.assertIn("scale_vaapi", gpu_filter)
        self.assertNotIn("hwdownload", gpu_filter)
        self.assertNotIn("hwupload", gpu_filter)
        cpu = ffmpeg_command(settings, self.channel, Path("/tmp/cpu"),
                             backend=HW_DECODE_HW_ENCODE, enable_gpu_vpp=False)
        cpu_filter = cpu[cpu.index("-vf") + 1]
        self.assertIn("scale_vaapi=format=nv12,hwdownload,format=nv12", cpu_filter)
        self.assertIn("bwdif=", cpu_filter)
        self.assertIn("format=nv12,hwupload", cpu_filter)
        self.assertEqual(cpu[cpu.index("-c:v") + 1], "h264_vaapi")
        # The GPU VPP path is not attempted when either codec engine is software.
        for backend in (SW_DECODE_HW_ENCODE, HW_DECODE_SW_ENCODE, SOFTWARE):
            with self.subTest(backend=backend):
                cmd = ffmpeg_command(settings, self.channel, Path("/tmp/cpu"), backend=backend)
                self.assertNotIn("deinterlace_vaapi", cmd[cmd.index("-vf") + 1])

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

    def test_avs_software_decode_preserves_vaapi_encode_without_failed_probe(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True)
        for codec in ('cavs', 'avs2'):
            channel = Channel(408, codec, "http://127.0.0.1/live", codec_hint=codec)
            self.assertEqual(backend_candidates(settings, channel),
                             (SW_DECODE_HW_ENCODE, SOFTWARE))
            command = ffmpeg_command(settings, channel, Path('/tmp/avs'),
                                     backend=SW_DECODE_HW_ENCODE)
            self.assertIn('h264_vaapi', command)
            self.assertNotIn('-hwaccel', command)
            with self.assertRaisesRegex(ValueError, 'AVS 軟體解碼'):
                ffmpeg_command(settings, channel, Path('/tmp/avs'), backend=HW_DECODE_HW_ENCODE)
        self.assertEqual(backend_candidates(Settings(ffmpeg='/usr/bin/ffmpeg'), channel),
                         (SOFTWARE,))

    def test_progressive_avs2_can_skip_deinterlace_without_changing_other_channels(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True)
        progress = Channel(2, "progressive AVS2", "https://example.org/live",
                           codec_hint="avs2", video_scan="progressive")
        interlace = Channel(3, "AVS+ interlaced", "https://example.org/live",
                            codec_hint="cavs")
        for ch in (progress, interlace):
            command = ffmpeg_command(settings, ch, Path("/tmp/avstest"),
                                     backend=SW_DECODE_HW_ENCODE)
            filters = command[command.index("-vf") + 1]
            self.assertEqual("bwdif=" in filters, ch is interlace)
            self.assertIn("fps=fps=", filters)
            self.assertIn("scale=w='max(2,trunc(iw*", filters)
            self.assertIn("format=nv12,hwupload", filters)

    def test_gpu_scale_is_opt_in_and_falls_back_to_cpu_scale(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True)
        ch = Channel(2, "progressive AVS2", "https://example.org/live",
                     codec_hint="avs2", video_scan="progressive", avs2_gpu_scale=True)
        gpu = ffmpeg_command(settings, ch, Path("/tmp/gpuscale"),
                             backend=SW_DECODE_HW_ENCODE)
        filters = gpu[gpu.index("-vf") + 1]
        self.assertIn("hwupload,scale_vaapi=", filters)
        self.assertNotIn("bwdif", filters)
        self.assertNotIn("hwdownload", filters)
        self.assertNotIn("scale=w=", filters)
        self.assertIn("format=nv12", filters)
        self.assertIn("h264_vaapi", gpu)
        for backend in (SOFTWARE, SW_DECODE_HW_ENCODE):
            cpu = ffmpeg_command(settings, ch, Path("/tmp/cpuscale"), backend=backend,
                                 enable_gpu_scale=False)
            self.assertIn("scale=w=", cpu[cpu.index("-vf") + 1])
            self.assertNotIn("scale_vaapi", cpu[cpu.index("-vf") + 1])
        self.assertNotIn("scale_vaapi", ffmpeg_command(
            settings, ch, Path("/tmp/sw"), backend=SOFTWARE)[
                ffmpeg_command(settings, ch, Path("/tmp/sw"),
                               backend=SOFTWARE).index("-vf") + 1])

    def test_gpu_scale_failure_preserves_hardware_encoder_and_sessions(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True)
        ch = Channel(2, "progressive AVS2", "https://example.org/live",
                     codec_hint="avs2", video_scan="progressive", avs2_gpu_scale=True)
        with tempfile.TemporaryDirectory() as tmp:
            commands = []
            def spawn(cmd, **kwargs):
                commands.append(cmd)
                return FakeProcess()
            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=spawn):
                manager = StreamManager(Settings(**{**settings.__dict__,
                                                    'cache_dir': Path(tmp)}), (ch,))
                try:
                    with manager.lock:
                        stream = manager._ensure_locked(2, session='abcdef')
                        self.assertIn("scale_vaapi=", ' '.join(commands[0]))
                        token = stream.token
                        stream.errors.append("Failed to configure output pad on Parsed_scale_vaapi_1")
                        self.assertTrue(manager._recover_locked(2, stream, "exit"))
                        self.assertEqual(stream.token, token)
                        self.assertIn('abcdef', stream.sessions)
                        self.assertEqual(stream.backend_index, 0)
                        self.assertTrue(stream.gpu_scale_disabled)
                        self.assertNotIn("scale_vaapi=", ' '.join(commands[1]))
                        self.assertIn("h264_vaapi", commands[1])
                        stream.errors.append("h264_vaapi encoding error")
                        self.assertTrue(manager._recover_locked(2, stream, "exit"))
                        self.assertEqual(stream.backends[stream.backend_index], SOFTWARE)
                finally:
                    manager.close()

    def test_gpu_vpp_failure_retries_cpu_filters_without_disabling_codecs(self):
        settings = Settings(ffmpeg="/usr/bin/ffmpeg", hw_render="/dev/dri/renderD128",
                            hw_decode=True, hw_encode=True)
        with tempfile.TemporaryDirectory() as tmp:
            commands = []
            def spawn(cmd, **_kwargs):
                commands.append(cmd)
                return FakeProcess()
            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=spawn):
                manager = StreamManager(Settings(**{**settings.__dict__, 'cache_dir': Path(tmp)}),
                                        (self.channel,))
                try:
                    with manager.lock:
                        stream = manager._ensure_locked(1, session='abcdef')
                        token = stream.token
                        self.assertIn('deinterlace_vaapi=', ' '.join(commands[0]))
                        stream.errors.extend((
                            'Failed to configure output pad on Parsed_deinterlace_vaapi_0',
                            '[vf#0:0] Error reinitializing filters!',
                            '[vost#0:0/h264_vaapi] Could not open encoder before EOF',
                        ))
                        self.assertTrue(manager._recover_locked(1, stream, 'exit'))
                        self.assertTrue(stream.gpu_vpp_disabled)
                        self.assertFalse(stream.decode_failed)
                        self.assertFalse(stream.encode_failed)
                        self.assertEqual(stream.backend_index, 0)
                        self.assertEqual(stream.token, token)
                        self.assertIn('abcdef', stream.sessions)
                        self.assertIn('bwdif=', ' '.join(commands[1]))
                        self.assertIn('hwdownload', ' '.join(commands[1]))
                        self.assertIn('h264_vaapi', commands[1])
                        self.assertIn('discont_start', ' '.join(commands[1]))
                        # Subsequent genuine decoder failure follows existing
                        # codec fallback; never retry the unsupported VPP stage.
                        stream.errors.append('Failed setup for format vaapi in decoder')
                        self.assertTrue(manager._recover_locked(1, stream, 'exit'))
                        self.assertTrue(stream.decode_failed)
                        self.assertEqual(stream.backends[stream.backend_index], SW_DECODE_HW_ENCODE)
                        self.assertNotIn('deinterlace_vaapi', ' '.join(commands[2]))
                finally:
                    manager.close()

    def test_avs2_skip_nonref_environment_is_set_only_for_opted_channel(self):
        settings = Settings(ffmpeg='/usr/bin/ffmpeg', hw_render='/dev/dri/renderD128',
                            hw_decode=True, hw_encode=True)
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            def spawn(command, **kwargs):
                captured.append(kwargs['env'])
                return FakeProcess()
            avs2 = Channel(408, 'AVS2', 'http://127.0.0.1/live', codec_hint='avs2',
                           avs2_skip_nonref=True)
            avsplus = Channel(400, 'AVS+', 'http://127.0.0.1/live', codec_hint='cavs')
            with patch.dict(os.environ, {}, clear=True), patch('udisplayer.transcoder.subprocess.Popen', side_effect=spawn):
                manager = StreamManager(Settings(**{**settings.__dict__, 'cache_dir': Path(tmp)}),
                                        (avs2, avsplus))
                try:
                    with manager.lock:
                        manager._ensure_locked(408)
                        manager._ensure_locked(400)
                finally:
                    manager.close()
            self.assertEqual(captured[0]['UDISPLAYER_AVS2_SKIP_NONREF'], '1')
            self.assertNotIn('UDISPLAYER_AVS2_SKIP_NONREF', captured[1])

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
                        self.assertEqual(stream.backends[stream.backend_index], HW_DECODE_HW_ENCODE)
                        self.assertTrue(stream.gpu_vpp_disabled)
                        self.assertFalse(stream.decode_failed)
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

    def test_web_heartbeat_lease_and_mixed_viewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(ffmpeg="/usr/bin/ffmpeg", cache_dir=Path(tmp))
            self.assertEqual(settings.idle_timeout, 20)

            def fake_popen(command, **_kwargs):
                playlist = Path(command[-1])
                playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nsegment_000000001.ts\n")
                (playlist.parent / "segment_000000001.ts").write_bytes(b"segment")
                return FakeProcess()

            with patch("udisplayer.transcoder.subprocess.Popen", side_effect=fake_popen) as popen:
                manager = StreamManager(settings, (self.channel,))
                first, second = "web-" + "a" * 32, "web-" + "b" * 32
                try:
                    with self.assertRaises(StreamError) as missing:
                        manager.playlist(1, first)
                    self.assertEqual(missing.exception.status, 410)
                    manager.heartbeat(1, first)
                    self.assertEqual(popen.call_count, 0)
                    manager.playlist(1, first)
                    stream = manager.streams[1]
                    manager.heartbeat(1, second)
                    manager.playlist(1, second)
                    manager.playlist(1)
                    self.assertEqual(popen.call_count, 1)
                    # HLS reads must not renew a Web lease.
                    with manager.lock:
                        seen = time.monotonic() - 19
                        manager.web_heartbeats[(1, first)] = seen
                        stream.sessions[first] = seen
                        manager.playlist(1, first)
                        with manager.open_resource(1, stream.token, "segment_000000001.ts", first):
                            pass
                        self.assertEqual(stream.sessions[first], seen)
                        manager.heartbeat(1, first)
                        self.assertGreater(stream.sessions[first], seen)
                        expired = time.monotonic() - 21
                        manager.web_heartbeats[(1, first)] = expired
                        stream.sessions[first] = expired
                        with self.assertRaises(StreamError) as stale:
                            manager.playlist(1, first)
                        self.assertEqual(stale.exception.status, 410)
                        self.assertTrue(manager._has_viewers_locked(stream, time.monotonic()))
                        self.assertNotIn(first, stream.sessions)
                        # Expire second Web viewer; anonymous direct viewer remains.
                        manager.web_heartbeats[(1, second)] = expired
                        stream.sessions[second] = expired
                        self.assertTrue(manager._has_viewers_locked(stream, time.monotonic()))
                        self.assertFalse(stream.sessions)
                        # No remaining active viewer: reaper stops the pipeline.
                        stream.anonymous_last_request = expired
                    deadline = time.monotonic() + 3
                    while 1 in manager.streams and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertNotIn(1, manager.streams)
                    self.assertIsNotNone(stream.process.poll())
                    self.assertEqual(popen.call_count, 1)
                    # A fresh heartbeat can recover a suspended player, but
                    # cannot create a stream on its own.
                    manager.heartbeat(1, first)
                    self.assertNotIn(1, manager.streams)
                    manager.playlist(1, first)
                    self.assertEqual(popen.call_count, 2)
                    self.assertTrue(manager.release(1, first))
                    with self.assertRaises(StreamError) as released:
                        manager.heartbeat(1, first)
                    self.assertEqual(released.exception.status, 410)
                finally:
                    manager.close()


if __name__ == "__main__":
    unittest.main()
