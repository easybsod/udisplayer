import json
import tempfile
import unittest
from pathlib import Path

from udisplayer.config import UNGROUPED, normalize_prefix, read_config, resolve_ffmpeg
from udisplayer.server import parse_request_path


class ConfigTests(unittest.TestCase):
    def test_external_channel_file_rejects_duplicate_and_non_http_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'channels.json'
            data = {'channels': [{'id': 1, 'name': '頻道一', 'url': 'http://127.0.0.1/live'}]}
            path.write_text(json.dumps(data))
            config = read_config(path)
            self.assertEqual(config.channels[0].name, '頻道一')
            self.assertFalse(config.grouping_enabled)
            data['channels'].append(data['channels'][0])
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, '唯一正整數'):
                read_config(path)
            data['channels'] = [{'id': 1, 'name': '頻道', 'url': 'file:///etc/passwd'}]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'HTTP'):
                read_config(path)

    def test_groups_and_default_ungrouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'channels.json'
            data = {
                'groups': ['央視'],
                'channels': [
                    {'id': 1, 'name': '一', 'group': '央視', 'logo': '1.png', 'url': 'http://127.0.0.1/1'},
                    {'id': 2, 'name': '二', 'url': 'http://127.0.0.1/2'},
                ],
            }
            path.write_text(json.dumps(data))
            config = read_config(path)
            self.assertTrue(config.grouping_enabled)
            self.assertEqual(config.groups, ('央視', UNGROUPED))
            self.assertEqual(config.channels[1].group, UNGROUPED)

    def test_per_channel_decoder_threads_override_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'channels.json'
            channel = {'id': 1, 'name': '4K AVS2', 'url': 'http://127.0.0.1/avs2',
                       'decoder_threads': 4}
            data = {'groups': ['4K'], 'channels': [channel]}
            path.write_text(json.dumps(data))
            self.assertEqual(read_config(path).channels[0].decoder_threads, 4)
            for invalid in (0, -1, 17, 4.5, '4', True):
                with self.subTest(invalid=invalid):
                    channel['decoder_threads'] = invalid
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(ValueError, 'decoder_threads'):
                        read_config(path)
            channel.pop('decoder_threads')
            path.write_text(json.dumps(data))
            self.assertIsNone(read_config(path).channels[0].decoder_threads)

    def test_avs_software_decoder_hint_and_opt_in_skip_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'channels.json'
            channel = {'id': 408, 'name': '4K AVS2', 'url': 'http://127.0.0.1/live',
                       'codec_hint': 'avs2', 'avs2_skip_nonref': True}
            data = {'groups': ['AVS測試'], 'channels': [channel]}
            path.write_text(json.dumps(data))
            config = read_config(path)
            self.assertEqual(config.channels[0].codec_hint, 'avs2')
            self.assertTrue(config.channels[0].avs2_skip_nonref)
            self.assertTrue(config.grouping_enabled)
            for bad in ('h264', 'AVS2', True, 1):
                with self.subTest(codec_hint=bad):
                    channel['codec_hint'] = bad
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(ValueError, 'codec_hint'):
                        read_config(path)
            channel['codec_hint'] = 'cavs'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'avs2_skip_nonref'):
                read_config(path)
            channel['codec_hint'] = 'avs2'
            for bad in (1, 'true', None):
                with self.subTest(avs2_skip_nonref=bad):
                    channel['avs2_skip_nonref'] = bad
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(ValueError, 'avs2_skip_nonref'):
                        read_config(path)

    def test_scan_mode_and_gpu_scale_are_explicit_avs2_opt_ins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'channels.json'
            channel = {'id': 2, 'name': 'Generic source', 'url': 'https://example.org/live',
                       'codec_hint': 'avs2', 'video_scan': 'progressive',
                       'avs2_gpu_scale': True}
            data = {'groups': ['Testing'], 'channels': [channel]}
            def load():
                path.write_text(json.dumps(data))
                return read_config(path).channels[0]
            config = load()
            self.assertEqual(config.video_scan, 'progressive')
            self.assertTrue(config.avs2_gpu_scale)
            for bad in ('interlaced', 'AUTO', None, False, 1):
                with self.subTest(video_scan=bad):
                    channel['video_scan'] = bad
                    with self.assertRaisesRegex(ValueError, 'video_scan'):
                        load()
            channel['video_scan'] = 'auto'
            with self.assertRaisesRegex(ValueError, 'avs2_gpu_scale'):
                load()
            channel['video_scan'] = 'progressive'
            channel['codec_hint'] = 'cavs'
            with self.assertRaisesRegex(ValueError, 'avs2_gpu_scale'):
                load()
            channel['codec_hint'] = 'avs2'
            for bad in (1, None, 'true'):
                with self.subTest(avs2_gpu_scale=bad):
                    channel['avs2_gpu_scale'] = bad
                    with self.assertRaisesRegex(ValueError, 'avs2_gpu_scale'):
                        load()
            channel.pop('avs2_gpu_scale')
            channel.pop('video_scan')
            self.assertEqual(load().video_scan, 'auto')
            self.assertFalse(load().avs2_gpu_scale)

    def test_explicit_missing_ffmpeg_does_not_fall_back_to_path(self):
        with self.assertRaisesRegex(ValueError, '不會改用 PATH'):
            resolve_ffmpeg('/missing/udisplayer/ffmpeg')

    def test_prefix_and_request_canonicalization(self):
        self.assertEqual(normalize_prefix('/tv/secret/'), '/tv/secret')
        self.assertEqual(normalize_prefix('/'), '')
        self.assertEqual(parse_request_path('/tv/secret/?channel=one'), ('/tv/secret/', 'channel=one'))
        self.assertEqual(parse_request_path('http://127.0.0.1:8081/tv/secret/?channel=one',
                                            '127.0.0.1:8081'), ('/tv/secret/', 'channel=one'))
        self.assertEqual(parse_request_path('https://example.org/tv/secret/hls/1/index.m3u8?session=abc',
                                            'example.org'),
                         ('/tv/secret/hls/1/index.m3u8', 'session=abc'))
        for bad in ('http://evil.example/tv/secret/',
                    'http://127.0.0.1:8081/a/../secret',
                    'http://127.0.0.1:8081/a/%2fsecret',
                    'http://127.0.0.1:8081/tv/secret/#fragment',
                    'http://user@127.0.0.1:8081/tv/secret/'):
            with self.subTest(absolute=bad), self.assertRaises(ValueError):
                parse_request_path(bad, '127.0.0.1:8081')
        with self.assertRaises(ValueError):
            parse_request_path('http://127.0.0.1:8081/tv/secret/')
        for bad in ('/a/../secret', '/a/%2e%2e/secret', '/a/%2fsecret',
                    '/a//secret', '/a/%5csecret', '/a/%GG', '//tv/secret'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_request_path(bad)
        for bad in ('/tv/../secret', '/tv//secret', '/tv/secret?x=1', '/tv/secret#fragment'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_prefix(bad)


if __name__ == '__main__':
    unittest.main()
