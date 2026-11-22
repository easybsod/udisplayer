import subprocess
import unittest
from unittest.mock import patch

from udisplayer.hardware import probe_vaapi, select_render_device


class HardwareTests(unittest.TestCase):
    def test_explicit_none_and_default_without_devices(self):
        self.assertIsNone(select_render_device("none"))
        with patch("udisplayer.hardware.Path.glob", return_value=[]):
            self.assertIsNone(select_render_device(None))

    def test_render_device_rejects_unsafe_and_falls_back_on_missing_paths(self):
        for value in ("auto", "../../dev/dri/renderD128", "/tmp/renderD128", "", "renderD128"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "--hw-render"):
                select_render_device(value)
        with self.assertLogs("udisplayer", level="WARNING") as recorded:
            self.assertIsNone(select_render_device("/dev/dri/renderD999999"))
        self.assertIn("回退軟體轉碼", recorded.output[0])

    def test_probe_requires_actual_encoder_success(self):
        self.assertEqual(probe_vaapi("/ffmpeg", None), (False, False, "auto"))
        with patch("udisplayer.hardware.ctypes.CDLL"), patch("udisplayer.hardware.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 1, b"", b"encoder unsupported"),
            subprocess.CompletedProcess([], 1, b"", b"encoder unsupported"),
        ]) as run:
            self.assertEqual(probe_vaapi("/ffmpeg", "/dev/dri/renderD128"), (True, False, "auto"))
            self.assertIn("h264_vaapi", run.call_args.args[0])

    def test_vbr_is_used_only_after_actual_probe(self):
        with patch("udisplayer.hardware.ctypes.CDLL"), patch("udisplayer.hardware.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
        ]) as run:
            self.assertEqual(probe_vaapi("/ffmpeg", "/dev/dri/renderD128"), (True, True, "VBR"))
            self.assertIn("VBR", run.call_args.args[0])

    def test_missing_libva_drm_prevents_crashing_ffmpeg_probe(self):
        with patch("udisplayer.hardware.ctypes.CDLL", side_effect=OSError("missing")), \
                patch("udisplayer.hardware.subprocess.run") as run:
            self.assertEqual(probe_vaapi("/ffmpeg", "/dev/dri/renderD128"), (False, False, "auto"))
            run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
