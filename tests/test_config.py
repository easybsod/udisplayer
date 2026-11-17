import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from udisplayer.cli import main, resolve_channel_list
from udisplayer.config import (Settings, UNGROUPED, available_cpu_threads,
                              normalize_prefix, read_config, resolve_ffmpeg)
from udisplayer.ini_config import read_ini_config
from udisplayer.server import parse_request_path


class ConfigTests(unittest.TestCase):
    def test_automatic_cpu_thread_count_respects_affinity_and_fallback(self):
        with patch('udisplayer.config.os.sched_getaffinity', return_value={0, 2, 4}, create=True), \
                patch('udisplayer.config.os.cpu_count', return_value=8):
            self.assertEqual(available_cpu_threads(), 3)
            settings = Settings(ffmpeg='/usr/bin/ffmpeg')
            self.assertEqual((settings.decoder_threads, settings.encoder_threads), (3, 3))
            explicit = Settings(ffmpeg='/usr/bin/ffmpeg', decoder_threads=1, encoder_threads=5)
            self.assertEqual((explicit.decoder_threads, explicit.encoder_threads), (1, 5))
        with patch('udisplayer.config.os.sched_getaffinity', side_effect=OSError, create=True), \
                patch('udisplayer.config.os.cpu_count', return_value=6):
            self.assertEqual(available_cpu_threads(), 6)
        with patch('udisplayer.config.os.sched_getaffinity', return_value=set(), create=True), \
                patch('udisplayer.config.os.cpu_count', return_value=None):
            self.assertEqual(available_cpu_threads(), 1)

    def test_cli_uses_auto_threads_only_for_omitted_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'channels.json'
            path.write_text('{"channels": []}')
            for extra, expected in (([], (6, 6)),
                                    (['--decoder-threads', '3'], (3, 6)),
                                    (['--encoder-threads', '1'], (6, 1)),
                                    (['--decoder-threads', '4', '--encoder-threads', '2'], (4, 2))):
                with self.subTest(extra=extra):
                    server = MagicMock(server_port=0)
                    with patch('udisplayer.cli.available_cpu_threads', return_value=6), \
                            patch('udisplayer.cli.resolve_ffmpeg', return_value='/usr/bin/ffmpeg'), \
                            patch('udisplayer.cli.validate_ffmpeg', return_value='FFmpeg test'), \
                            patch('udisplayer.cli.select_render_device', return_value=None), \
                            patch('udisplayer.cli.probe_vaapi', return_value=(False, False, 'auto')), \
                            patch('udisplayer.cli.AppServer', return_value=server) as app, \
                            patch('udisplayer.cli.signal.signal'):
                        self.assertEqual(main(['--channel-list', str(path), *extra]), 0)
                    settings = app.call_args.args[0]
                    self.assertEqual((settings.decoder_threads, settings.encoder_threads), expected)

    def test_channel_list_option_and_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'custom-channels.json'
            self.assertEqual(main(['--channel-list', str(path), '--init-channel-list']), 0)
            self.assertTrue(path.is_file())
            self.assertIn('channels', path.read_text())
            with self.assertRaises(SystemExit):
                main(['--channel-list', str(path), '--init-channel-list'])
            ini_path = Path(tmp) / 'udisplayer.ini'
            self.assertEqual(main(['--config', str(ini_path), '--init-config']), 0)
            self.assertIn('channel-list = channels.json', ini_path.read_text())
            with self.assertRaises(SystemExit):
                main(['--config', str(ini_path), '--init-config'])
            with self.assertRaises(SystemExit):
                main(['--init-config', '--init-channel-list'])

    def test_ini_config_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini_path = Path(tmp) / 'udisplayer.ini'
            ini_path.write_text('''host = 127.0.0.1
port = 9555
path = /watch
channel-list = channels.json
ffmpeg = ./tools/ffmpeg
cache-dir = ./cache
hw-render = none
max-streams = 3
encoder-threads = 2
startup-timeout = 15
idle-timeout = 70
debug = true
''')
            ini = read_ini_config(ini_path)
            self.assertEqual(ini['ffmpeg'], str(Path(tmp, 'tools/ffmpeg')))
            self.assertEqual(ini['cache_dir'], Path(tmp, 'cache'))
            self.assertTrue(ini['debug'])
            server = MagicMock(server_port=0)
            with patch('udisplayer.cli.available_cpu_threads', return_value=8), \
                    patch('udisplayer.cli.resolve_ffmpeg', return_value='/usr/bin/ffmpeg') as ff, \
                    patch('udisplayer.cli.validate_ffmpeg', return_value='FFmpeg test'), \
                    patch('udisplayer.cli.select_render_device', return_value=None) as render, \
                    patch('udisplayer.cli.probe_vaapi', return_value=(False, False, 'auto')), \
                    patch('udisplayer.cli.read_config', return_value=MagicMock(channels=())), \
                    patch('udisplayer.cli.AppServer', return_value=server) as app, \
                    patch('udisplayer.cli.signal.signal'):
                self.assertEqual(main(['--config', str(ini_path), '--port', '8088',
                                       '--max-streams', '6', '--decoder-threads', '4']), 0)
            settings = app.call_args.args[0]
            self.assertEqual((settings.host, settings.port, settings.prefix),
                             ('127.0.0.1', 8088, '/watch'))
            self.assertEqual((settings.max_streams, settings.decoder_threads,
                              settings.encoder_threads), (6, 4, 2))
            self.assertTrue(settings.debug)
            self.assertEqual(settings.cache_dir, Path(tmp, 'cache'))
            ff.assert_called_once_with(str(Path(tmp, 'tools/ffmpeg')))
            render.assert_called_once_with('none')

    def test_channel_list_relative_paths_use_server_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'server'
            root.mkdir()
            other = Path(tmp) / 'elsewhere'
            other.mkdir()
            (root / 'channels.json').write_text('{"channels": []}')
            (root / 'sub').mkdir()
            (root / 'sub' / 'tv.json').write_text('{"channels": []}')
            with patch('udisplayer.cli.server_directory', return_value=root):
                self.assertEqual(resolve_channel_list('sub/tv.json'), root / 'sub/tv.json')
                self.assertEqual(resolve_channel_list(root / 'channels.json'), root / 'channels.json')
                ini_path = other / 'test.ini'
                ini_path.write_text('channel-list = sub/tv.json\n')
                server = MagicMock(server_port=0)
                with patch('udisplayer.cli.resolve_ffmpeg', return_value='/usr/bin/ffmpeg'), \
                        patch('udisplayer.cli.validate_ffmpeg', return_value='FFmpeg test'), \
                        patch('udisplayer.cli.select_render_device', return_value=None), \
                        patch('udisplayer.cli.probe_vaapi', return_value=(False, False, 'auto')), \
                        patch('udisplayer.cli.AppServer', return_value=server) as app, \
                        patch('udisplayer.cli.signal.signal'):
                    self.assertEqual(main(['--config', str(ini_path)]), 0)
                    self.assertEqual(app.call_args.args[4], root / 'sub' / 'logos')
                    self.assertEqual(main(['--channel-list', 'sub/tv.json']), 0)
                    self.assertEqual(app.call_args.args[4], root / 'sub' / 'logos')
                    ini_path.write_text('channel-list = missing.json\n')
                    # CLI must override INI even if the INI channel list is missing.
                    self.assertEqual(main(['--config', str(ini_path),
                                           '--channel-list', str(root / 'channels.json')]), 0)
                    self.assertEqual(app.call_args.args[4], root / 'logos')
                self.assertEqual(main(['--channel-list', 'sub/fresh.json', '--init-channel-list']), 0)
                self.assertTrue((root / 'sub' / 'fresh.json').is_file())

    def test_ini_strict_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.ini'
            cases = {
                'unrecognized = 2\n': '未知',
                'port = 1\nport = 2\n': '重複',
                '[server]\nport = 2\n': '區段',
                'port = 65536\n': '範圍',
                'port = 0\n': '範圍',
                'max-streams = 0\n': '範圍',
                'decoder-threads = -1\n': '正整數',
                'encoder-threads = abc\n': '正整數',
                'debug = yes\n': 'true 或 false',
                'host = "127.0.0.1"\n': '引號',
                'port : 1\n': '格式',
            }
            for content, expected in cases.items():
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaisesRegex(ValueError, expected):
                        read_ini_config(path)
            path.write_bytes(b'port = 22\n\xff')
            with self.assertRaisesRegex(ValueError, 'UTF-8'):
                read_ini_config(path)
            path.write_text('debug = false\n' * 6500)
            with self.assertRaisesRegex(ValueError, '64 KiB'):
                read_ini_config(path)
            path.write_text('debug = false  # test\nport = 2222\n')
            self.assertEqual(read_ini_config(path), {'debug': False, 'port': 2222})

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
