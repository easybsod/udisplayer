"""The AVS tool must verify the FFmpeg that the player will actually run."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools/avs/scripts/verify_ffmpeg.py"
spec = importlib.util.spec_from_file_location("verify_avs_ffmpeg", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerifyFFmpegTests(unittest.TestCase):
    def test_avs2_checks_existing_player_contract_and_vaapi(self):
        flags = "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo --enable-libdavs2"
        with patch.object(verify, "resolve_ffmpeg", return_value="/tmp/custom/ffmpeg"), \
                patch.object(verify, "validate_ffmpeg", return_value="FFmpeg custom"), \
                patch.object(verify, "ensure_listing") as names, \
                patch.object(verify, "run", return_value=flags) as run:
            self.assertEqual(verify.check("/tmp/custom/ffmpeg", require_avs2=True),
                             "FFmpeg custom")
            self.assertEqual(names.call_count, 2)
            self.assertEqual([c.args[2] for c in names.call_args_list],
                             ["cavs", "libdavs2"])
            self.assertEqual(run.call_args.args[0], "/tmp/custom/ffmpeg")

    def test_reject_missing_features_and_nonfree_build(self):
        with patch.object(verify, "resolve_ffmpeg", return_value="/ffmpeg"), \
                patch.object(verify, "validate_ffmpeg", return_value="FFmpeg test"), \
                patch.object(verify, "ensure_listing"):
            for flags in ("--enable-gpl --enable-version3 --enable-libx264",
                          "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo --enable-nonfree",
                          "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo"):
                with self.subTest(flags=flags), \
                        patch.object(verify, "run", return_value=flags):
                    with self.assertRaises(ValueError):
                        verify.check("/ffmpeg", require_avs2=True)

    def test_missing_vaapi_device_fails_before_hardware_encoding(self):
        with patch.object(verify, "resolve_ffmpeg", return_value="/ffmpeg"), \
                patch.object(verify, "validate_ffmpeg", return_value="FFmpeg test"), \
                patch.object(verify, "ensure_listing"):
            with patch.object(verify, "run", side_effect=[
                "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo", "Hardware acceleration methods: vaapi"]):
                with self.assertRaisesRegex(ValueError, "裝置不存在"):
                    verify.check("/ffmpeg", vaapi_device="/nonexistent-render-node")

    def test_vaapi_static_validation_without_gpu_device(self):
        with patch.object(verify, "resolve_ffmpeg", return_value="/ffmpeg"), \
                patch.object(verify, "validate_ffmpeg", return_value="FFmpeg test"), \
                patch.object(verify, "ensure_listing") as listings, \
                patch.object(verify, "run", side_effect=[
                    "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo",
                    "Hardware acceleration methods:\nvaapi\n"]):
            self.assertEqual(verify.check("/ffmpeg", require_vaapi=True), "FFmpeg test")
            self.assertEqual([c.args[2] for c in listings.call_args_list],
                             ["cavs", "h264_vaapi", "scale_vaapi"])

    def test_sample_ts_is_optional_but_must_exist_when_requested(self):
        with patch.object(verify, "resolve_ffmpeg", return_value="/ffmpeg"), \
                patch.object(verify, "validate_ffmpeg", return_value="FFmpeg test"), \
                patch.object(verify, "ensure_listing"), \
                patch.object(verify, "run", return_value="--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo"):
            with self.assertRaisesRegex(ValueError, "測試檔不存在"):
                verify.check("/ffmpeg", sample_ts=Path("/nonexistent-sample.ts"))

    def test_sample_ts_requires_fifty_decoded_frames(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(verify, "resolve_ffmpeg", return_value="/ffmpeg"), \
                patch.object(verify, "validate_ffmpeg", return_value="FFmpeg test"), \
                patch.object(verify, "ensure_listing"):
            sample = Path(directory) / "avs2.ts"
            sample.write_bytes(b"local test sample")
            with patch.object(verify, "run", side_effect=[
                    "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo", "frame=49\n"]):
                with self.assertRaisesRegex(ValueError, "50 幀"):
                    verify.check("/ffmpeg", sample_ts=sample)
            with patch.object(verify, "run", side_effect=[
                    "--enable-gpl --enable-version3 --enable-libx264 --enable-gnutls --enable-parser=cavsvideo", "frame=50\n"]):
                self.assertEqual(verify.check("/ffmpeg", sample_ts=sample), "FFmpeg test")


if __name__ == "__main__":
    unittest.main()
