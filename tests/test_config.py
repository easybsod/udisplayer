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

    def test_explicit_missing_ffmpeg_does_not_fall_back_to_path(self):
        with self.assertRaisesRegex(ValueError, '不會改用 PATH'):
            resolve_ffmpeg('/missing/udisplayer/ffmpeg')

    def test_prefix_and_request_canonicalization(self):
        self.assertEqual(normalize_prefix('/tv/secret/'), '/tv/secret')
        self.assertEqual(normalize_prefix('/'), '')
        self.assertEqual(parse_request_path('/tv/secret/?channel=one'), ('/tv/secret/', 'channel=one'))
        for bad in ('/a/../secret', '/a/%2e%2e/secret', '/a/%2fsecret',
                    '/a//secret', '/a/%5csecret', '/a/%GG', '//tv/secret'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_request_path(bad)
        for bad in ('/tv/../secret', '/tv//secret', '/tv/secret?x=1', '/tv/secret#fragment'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_prefix(bad)


if __name__ == '__main__':
    unittest.main()
