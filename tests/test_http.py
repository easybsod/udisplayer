import io
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from udisplayer.config import Channel, ChannelConfig, Settings
from udisplayer.server import AppServer


class FakeManager:
    token = 'a' * 32

    def __init__(self):
        self.playlist_calls = 0
        self.playlist_sessions = []
        self.resources = []
        self.releases = []

    def playlist(self, cid, session=None):
        self.playlist_calls += 1
        self.playlist_sessions.append((cid, session))
        return (f'#EXTM3U\n{self.token}/segment_000000001.ts\n').encode()

    def open_resource(self, cid, token, filename, session=None):
        self.resources.append((cid, token, filename, session))
        return io.BytesIO(b'abcdefghijklmnop')

    def release(self, cid, session):
        self.releases.append((cid, session))
        return True


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = FakeManager()
        settings = Settings(ffmpeg='/ignored', host='127.0.0.1', port=0, prefix='/tv/secret')
        cls.web_tmp = tempfile.TemporaryDirectory()
        web = Path(cls.web_tmp.name) / 'index.html'
        web.write_text('<!doctype html><title>udisplayer</title>', encoding='utf-8')
        not_found = Path(cls.web_tmp.name) / 'not-found.html'
        not_found.write_text('404 Not Found\n', encoding='utf-8')
        logos = Path(cls.web_tmp.name) / 'logos'
        logos.mkdir()
        (logos / '1.png').write_bytes(b'fake-png')
        config = ChannelConfig((Channel(1, 'Test', 'http://example.com/live', '測試', '1.png'),), ('測試',), True)
        cls.server = AppServer(settings, config, web, not_found, logos, manager=cls.manager)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.web_tmp.cleanup()

    @staticmethod
    def request_to(base, path, method='GET', headers=None, data=None):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, hdrs, newurl):
                return None
        try:
            with build_opener(NoRedirect).open(
                Request(base + path, data=data, method=method, headers=headers or {}),
                timeout=5,
            ) as response:
                return response.status, response.headers, response.read()
        except HTTPError as response:
            return response.status, response.headers, response.read()

    def request(self, path, method='GET', headers=None, data=None):
        return self.request_to(self.base, path, method=method, headers=headers, data=data)

    def test_absolute_form_same_origin_requests_and_cross_origin_rejection(self):
        # urllib converts absolute URLs back into origin-form; send the actual
        # HTTP/1.1 absolute-form bytes that were observed on the production VM.
        authority = f'127.0.0.1:{self.server.server_port}'
        def raw(method, target):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            try:
                conn.putrequest(method, target, skip_host=True)
                conn.putheader('Host', authority)
                conn.endheaders()
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()
        prefix = f'http://{authority}/tv/secret'
        status, body = raw('GET', prefix + '/?channel=1')
        self.assertEqual(status, 200)
        self.assertIn(b'udisplayer', body)
        status, body = raw('GET', prefix + '/hls/1/index.m3u8?session=abc')
        self.assertEqual(status, 200)
        self.assertIn(b'#EXTM3U', body)
        status, _ = raw('GET', 'http://evil.example/tv/secret/')
        self.assertEqual(status, 404)

    def test_secret_prefix_guards_all_routes_and_errors(self):
        for path in ('/', '/index.html', '/channels.json', '/hls/1/index.m3u8',
                     '/tv/secretextra/', '/tv/%2e%2e/tv/secret/', '/tv/secret/../secret/'):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertNotIn('secret', headers.get('Location', ''))
        status, headers, _ = self.request('/tv/secret')
        self.assertEqual(status, 308)
        self.assertEqual(headers['Location'], '/tv/secret/')

    def test_unknown_channel_query_returns_plain_404(self):
        expected = b'404 Not Found\n'
        status, headers, data = self.request('/tv/secret/?channel=999')
        self.assertEqual(status, 404)
        self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertEqual(data, expected)

        status, _, data = self.request('/tv/secret/?channel=1')
        self.assertEqual(status, 200)
        self.assertIn(b'udisplayer', data)

        status, headers, data = self.request('/tv/secret/?channel=1&channel=999')
        self.assertEqual(status, 404)
        self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertEqual(data, expected)

    def test_all_not_found_routes_share_plain_text(self):
        expected = b'404 Not Found\n'
        paths = (
            '/outside-prefix',
            '/tv/secret/does-not-exist',
            '/tv/secret/logos/not-used.png',
            '/tv/secret/hls/not-an-id/index.m3u8',
            '/tv/secret/hls/1/index.m3u8?session=bad!',
        )
        for path in paths:
            with self.subTest(path=path):
                status, headers, data = self.request(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
                self.assertEqual(data, expected)

    def test_normal_mode_suppresses_per_request_access_log(self):
        with self.assertNoLogs('udisplayer', level='INFO'):
            status, _, _ = self.request(
                '/tv/secret/?channel=1',
                headers={
                    'CF-Connecting-IP': '203.0.113.42',
                    'X-Forwarded-For': '203.0.113.42, 198.51.100.10',
                    'CF-Ray': 'abc123-TPE',
                },
            )
        self.assertEqual(status, 200)

    def test_avs_client_playback_hint_does_not_expose_private_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, payload in (("index.html", "<html></html>"),
                                  ("not-found.html", "404 Not Found\n")):
                (root / name).write_text(payload)
            (root / "logos").mkdir()
            config = ChannelConfig((
                Channel(1, "Generic", "http://example.com/1"),
                Channel(2, "AVS+", "http://private.example/2", codec_hint="cavs"),
                Channel(3, "AVS2", "http://private.example/3", codec_hint="avs2",
                        decoder_threads=6, avs2_skip_nonref=True),
            ), (), False)
            server = AppServer(Settings(ffmpeg="/ignored", host="127.0.0.1", port=0),
                               config, root / "index.html", root / "not-found.html",
                               root / "logos", manager=FakeManager())
            try:
                channels = json.loads(server.config_json)["channels"]
                self.assertNotIn("codec_hint", channels[0])
                self.assertEqual(channels[1]["codec_hint"], "cavs")
                self.assertEqual(channels[2]["codec_hint"], "avs2")
                self.assertNotIn("url", server.config_json.decode())
                self.assertNotIn("decoder_threads", server.config_json.decode())
                self.assertNotIn("avs2_skip_nonref", server.config_json.decode())
            finally:
                server.server_close()

    def test_assets_playlist_segments_and_range_share_prefix(self):
        status, headers, html = self.request('/tv/secret/')
        self.assertEqual(status, 200)
        self.assertIn(b'udisplayer', html)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')

        status, _, data = self.request('/tv/secret/channels.json')
        public = json.loads(data)
        channel = public['channels'][0]
        self.assertEqual(public['groups'], ['測試'])
        self.assertRegex(public['logo_version'], r'^[a-f0-9]{16}$')
        self.assertEqual(channel, {'id': 1, 'name': 'Test', 'group': '測試', 'logo': '1.png'})
        self.assertNotIn(b'example.com', data)

        status, headers, logo = self.request(
            '/tv/secret/logos/1.png?v=' + public['logo_version']
        )
        self.assertEqual((status, headers['Content-Type'], logo), (200, 'image/png', b'fake-png'))
        self.assertEqual(headers['Cache-Control'], 'private, max-age=31536000, immutable')
        self.assertIsNone(headers.get('Pragma'))
        self.assertIsNone(headers.get('Expires'))
        self.assertEqual(self.request('/tv/secret/logos/not-used.png')[0], 404)

        prewarm_session = 'c' * 32
        status, _, body = self.request(
            '/tv/secret/hls/1/index.m3u8?session=' + prewarm_session,
            method='HEAD',
        )
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(self.manager.playlist_sessions[-1], (1, prewarm_session))

        session = 'b' * 32
        status, _, playlist = self.request('/tv/secret/hls/1/index.m3u8?session=' + session)
        self.assertEqual(status, 200)
        self.assertIn(b'/segment_000000001.ts', playlist)
        self.assertNotIn(b'/low/', playlist)
        self.assertEqual(self.manager.playlist_sessions[-1], (1, session))

        base = '/tv/secret/hls/1/' + self.manager.token + '/'
        status, headers, data = self.request(
            base + 'segment_000000001.ts?session=' + session,
            headers={'Range': 'bytes=4-7'},
        )
        self.assertEqual((status, data, headers['Content-Range']), (206, b'efgh', 'bytes 4-7/16'))
        self.assertEqual(self.manager.resources[-1], (1, self.manager.token, 'segment_000000001.ts', session))

        status, headers, data = self.request(base + 'segment_000000001.ts', method='HEAD')
        self.assertEqual((status, len(data), headers['Content-Length']), (200, 0, '16'))

        status, _, _ = self.request(
            base + 'segment_000000001.ts',
            headers={'Range': 'bytes=999-'},
        )
        self.assertEqual(status, 416)

        status, _, data = self.request(
            '/tv/secret/hls/1/release?session=' + session,
            method='POST',
        )
        self.assertEqual((status, data), (204, b''))
        self.assertEqual(self.manager.releases[-1], (1, session))

        status, _, _ = self.request('/tv/secret/hls/1/release?session=bad!', method='POST')
        self.assertEqual(status, 404)
        self.assertEqual(self.request('/tv/secret/telemetry', method='POST', data=b'{}')[0], 400)

    def test_normal_playback_lifecycle_logging(self):
        session = 'd' * 32
        headers = {
            'Content-Type': 'application/json',
            'CF-Connecting-IP': '203.0.113.42',
            'CF-Ray': 'ray-TPE',
            'User-Agent': 'Test Browser',
        }
        def event(name, **extra):
            payload = {'event': name, 'channel': 1, 'session': session, **extra}
            return self.request(
                '/tv/secret/telemetry',
                method='POST',
                headers=headers,
                data=json.dumps(payload).encode(),
            )[0]

        with self.assertLogs('udisplayer', level='INFO') as captured:
            self.assertEqual(event('session_start', engine='hls.js'), 204)
            status, _, _ = self.request('/tv/secret/hls/1/index.m3u8?session=' + session)
            self.assertEqual(status, 200)
            self.assertEqual(event('playing', engine='hls.js'), 204)
            self.assertEqual(event('recovery', recoveries=1, reason='播放暫時停頓'), 204)
            self.assertEqual(event(
                'session_end',
                recoveries=1,
                stalls=1,
                live_jumps=0,
                reason='pagehide',
            ), 204)
        output = '\n'.join(captured.output)
        self.assertIn('playback_start channel=1', output)
        self.assertIn('peer_ip=127.0.0.1', output)
        self.assertIn('"CF-Connecting-IP":"203.0.113.42"', output)
        self.assertIn('playback_ok channel=1', output)
        self.assertIn('engine="hls.js"', output)
        self.assertIn('playback_issue channel=1', output)
        self.assertIn('playback_end channel=1', output)
        self.assertIn('ok=true', output)
        self.assertIn('recoveries=1 stalls=1 live_jumps=0 reason="pagehide"', output)
        self.assertNotIn('access peer_ip=', output)

    def test_startup_stall_end_marks_playback_ok_once(self):
        session = 'a' * 32
        headers = {'Content-Type': 'application/json'}

        def post(event, **extra):
            payload = {'event': event, 'channel': 1, 'session': session, **extra}
            return self.request(
                '/tv/secret/telemetry',
                method='POST',
                headers=headers,
                data=json.dumps(payload).encode(),
            )[0]

        with self.assertLogs('udisplayer', level='INFO') as captured:
            self.assertEqual(post('session_start', engine='hls.js'), 204)
            self.assertEqual(post(
                'stall_end',
                engine='hls.js',
                phase='playing',
                stalls=1,
                stall_ms=4500,
            ), 204)
            # The browser also emits the canonical playing event after stall_end.
            # It must not create a second playback_ok record.
            self.assertEqual(post('playing', engine='hls.js', stalls=1), 204)
            self.assertEqual(post('session_end', reason='pagehide', stalls=1), 204)

        output = '\n'.join(captured.output)
        self.assertEqual(output.count('playback_ok channel=1'), 1)
        self.assertIn('engine="hls.js"', output)
        self.assertIn('playback_end channel=1', output)
        self.assertIn('ok=true', output)
        self.assertIn('stalls=1', output)

    def test_normal_terminal_failure_is_logged(self):
        session = 'f' * 32
        headers = {'Content-Type': 'application/json'}
        def post(event, **extra):
            payload = {'event': event, 'channel': 1, 'session': session, **extra}
            return self.request(
                '/tv/secret/telemetry',
                method='POST',
                headers=headers,
                data=json.dumps(payload).encode(),
            )[0]

        with self.assertLogs('udisplayer', level='INFO') as captured:
            self.assertEqual(post('session_start', engine='none'), 204)
            self.assertEqual(post('terminal', reason='unsupported', hls_error='codec'), 204)
        output = '\n'.join(captured.output)
        self.assertIn('playback_start channel=1', output)
        self.assertIn('playback_failed channel=1', output)
        self.assertIn('reason="unsupported"', output)
        self.assertIn('hls_error="codec"', output)

    def test_idle_playback_session_is_logged_as_timeout(self):
        session = 'e' * 32
        self.server.playback_start(1, session, '127.0.0.1', {})
        with self.server.playback_lock:
            last_seen = self.server.playback_sessions[(1, session)].last_seen
        with self.assertLogs('udisplayer', level='WARNING') as captured:
            self.server._expire_playback_sessions(last_seen + self.server.settings.idle_timeout + 1)
        output = '\n'.join(captured.output)
        self.assertIn('playback_end channel=1', output)
        self.assertIn('ok=false', output)
        self.assertIn('reason="idle_timeout"', output)

    def test_debug_telemetry_and_segment_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / 'index.html'
            web.write_text('<!doctype html><title>udisplayer</title>', encoding='utf-8')
            not_found = root / 'not-found.html'
            not_found.write_text('404 Not Found\n', encoding='utf-8')
            logos = root / 'logos'
            logos.mkdir()
            manager = FakeManager()
            config = ChannelConfig((Channel(1, 'Test', 'http://example.com/live'),), (), False)
            server = AppServer(
                Settings(ffmpeg='/ignored', host='127.0.0.1', port=0, prefix='/tv/secret', debug=True),
                config,
                web,
                not_found,
                logos,
                manager=manager,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                public = json.loads(server.config_json)
                self.assertIs(public['debug'], True)
                session = 'c' * 32
                payload = json.dumps({
                    'event': 'heartbeat',
                    'channel': 1,
                    'session': session,
                    'buffer_s': 8.5,
                    'live_lag_s': 6.2,
                    'engine': 'hls.js',
                }).encode()
                with self.assertLogs('udisplayer', level='DEBUG') as captured:
                    status, _, _ = self.request_to(
                        base,
                        '/tv/secret/telemetry',
                        method='POST',
                        headers={'Content-Type': 'application/json'},
                        data=payload,
                    )
                    self.assertEqual(status, 204)
                    status, _, data = self.request_to(
                        base,
                        '/tv/secret/hls/1/' + manager.token + '/segment_000000001.ts?session=' + session,
                    )
                    self.assertEqual((status, data), (200, b'abcdefghijklmnop'))
                output = '\n'.join(captured.output)
                self.assertIn('client_telemetry', output)
                self.assertIn('"buffer_s":8.5', output)
                self.assertIn('access peer_ip=127.0.0.1', output)
                self.assertIn('hls_segment channel=1', output)
                self.assertIn('completed=true', output)
                self.assertIn('bytes_sent=16', output)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
