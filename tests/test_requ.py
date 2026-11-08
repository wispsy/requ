from email.message import Message
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ssl
import threading
import unittest
from unittest.mock import Mock, patch

from requ import Client, ContentEncodingError, HTTPStatusError, RedirectError, Response, ResponseLimitError


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        if path in {'/redirect', '/cycle', '/file', '/cross', '/redirect-limit'}:
            targets = {
                '/redirect': '/ok',
                '/cycle': '/cycle',
                '/file': 'file:///etc/passwd',
                '/cross': f'http://localhost:{self.server.server_port}/headers',
                '/redirect-limit': '/redirect',
            }
            self.send_response(302)
            self.send_header('Location', targets[path])
            self.end_headers()
            return
        if path == '/headers':
            body = '|'.join(self.headers.get(key, '') for key in ('Authorization', 'Cookie', 'X-Private')).encode()
        elif path == '/latin':
            body = 'café'.encode('latin-1')
        elif path == '/bad-utf8':
            body = b'\xff'
        else:
            body = b'hello'
        self.send_response(404 if path == '/missing' else 304 if path == '/not-modified' else 200)
        self.send_header('Content-Type', 'text/plain; charset=iso-8859-1' if path == '/latin' else 'text/plain')
        self.send_header('X-Multi', 'one')
        self.send_header('X-Multi', 'two')
        if path == '/gzip':
            self.send_header('Content-Encoding', 'gzip')
        if path not in {'/unbounded', '/stall'}:
            self.send_header(
                'Content-Length',
                '999' if path in {'/oversize', '/not-modified'} else '12' if path == '/short' else str(len(body)),
            )
        if path == '/conflicting':
            self.send_header('Content-Length', '8')
        if path == '/invalid-length':
            self.send_header('Content-Length', 'invalid')
        self.end_headers()
        if path == '/stall':
            self.server.release.wait(5)
            return
        if path != '/not-modified':
            self.wfile.write(body)


class RequHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.release = threading.Event()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_owned_bytes_duplicate_headers_and_decoding(self):
        response = Client().get(self.base + '/ok#fragment')
        self.assertEqual(response.url, self.base + '/ok')
        self.assertEqual(response.body, b'hello')
        self.assertEqual(response.headers_all('x-MULTI'), ('one', 'two'))
        self.assertEqual(response.header('X-Multi'), 'one')
        self.assertIsNone(response.header('missing'))
        self.assertEqual(response.text, 'hello')
        self.assertEqual(Client().get(self.base + '/latin').text, 'café')
        with self.assertRaises(AttributeError):
            response.body = b'changed'
        with self.assertRaises(UnicodeDecodeError):
            Client().get(self.base + '/bad-utf8').text
        self.assertEqual(Client().get(self.base + '/bad-utf8').decode('latin-1'), 'ÿ')

    def test_status_is_data_until_explicitly_raised(self):
        response = Client().get(self.base + '/missing')
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body, b'hello')
        with self.assertRaises(HTTPStatusError) as raised:
            response.raise_for_status()
        self.assertIs(raised.exception.response, response)
        Client().get(self.base + '/ok').raise_for_status()

    def test_same_origin_redirect_and_limit(self):
        self.assertEqual(Client().get(self.base + '/redirect').url, self.base + '/ok')
        for path, client in [
            ('/redirect', Client(max_redirects=0)),
            ('/cycle', Client()),
            ('/redirect-limit', Client(max_redirects=1)),
            ('/file', Client()),
        ]:
            with self.subTest(path=path), self.assertRaises(RedirectError):
                client.get(self.base + path)

    def test_cross_origin_opt_in_drops_all_caller_headers(self):
        headers = {'Authorization': 'secret', 'Cookie': 'session=value', 'X-Private': 'private'}
        self.assertEqual(Client().get(self.base + '/headers', headers=headers).text, 'secret|session=value|private')
        with self.assertRaises(RedirectError):
            Client().get(self.base + '/cross', headers=headers)
        result = Client(allow_cross_origin_redirects=True).get(self.base + '/cross', headers=headers)
        self.assertEqual(result.text, '||')

    def test_byte_limits_and_truncated_framing(self):
        self.assertEqual(Client(max_bytes=5).get(self.base + '/ok').body, b'hello')
        for path in ('/ok', '/oversize', '/unbounded'):
            with self.subTest(path=path), self.assertRaises(ResponseLimitError):
                Client(max_bytes=4).get(self.base + path)
        with self.assertRaises(IncompleteRead):
            Client().get(self.base + '/short')
        for path in ('/conflicting', '/invalid-length'):
            with self.assertRaises(ValueError):
                Client().get(self.base + path)

    def test_encoded_content_is_rejected(self):
        with self.assertRaises(ContentEncodingError):
            Client().get(self.base + '/gzip')

    def test_no_body_status_ignores_representation_length(self):
        self.assertEqual(Client(max_bytes=4).get(self.base + '/not-modified').body, b'')

    def test_socket_read_timeout(self):
        try:
            with self.assertRaises(TimeoutError):
                Client(timeout=0.05).get(self.base + '/stall')
        finally:
            self.server.release.set()

    def test_urls_and_headers_fail_before_network(self):
        with patch('requ.build_opener') as opener:
            for value in (
                'file:///tmp/x',
                'ftp://example.test/',
                'data:text/plain,x',
                '/relative',
                'http://name:password@example.test',
                'http://example.test:0',
                'http://example.test:abc',
                'http://example.test/with space',
                'http://example.test/\n',
                'http://example.test/é',
                '',
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Client().get(value)
            with self.assertRaises(TypeError):
                Client().get(None)
            for headers in (
                {'Host': 'evil'},
                {'Accept-Encoding': 'gzip'},
                {'X': 'bad\r\nvalue'},
                {'bad header': 'value'},
                {'X': 'a', 'x': 'b'},
                {'X': 1},
            ):
                with self.subTest(headers=headers), self.assertRaises((TypeError, ValueError)):
                    Client().get(self.base, headers=headers)
            opener.assert_not_called()

    def test_tls_verification_proxy_defaults_and_downgrade(self):
        class Raw(io.BytesIO):
            status = 302
            headers = Message()

        raw = Raw()
        raw.headers['Location'] = 'http://example.test/'
        opener = Mock()
        opener.open.return_value = raw
        from urllib.request import HTTPSHandler, ProxyHandler

        with (
            patch('requ.build_opener', return_value=opener),
            patch('requ.HTTPSHandler', wraps=HTTPSHandler) as https,
            patch('requ.ProxyHandler', wraps=ProxyHandler) as proxy,
        ):
            with self.assertRaises(RedirectError):
                Client().get('https://example.test/')
            context = https.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            proxy.assert_called_once_with({})
        self.assertTrue(raw.closed)

    def test_proxy_environment_requires_opt_in(self):
        with patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:1'}):
            self.assertEqual(Client().get(self.base + '/ok').text, 'hello')

    def test_configuration_validation(self):
        for kwargs in (
            {'timeout': True},
            {'timeout': 0},
            {'timeout': float('inf')},
            {'timeout': float('nan')},
            {'max_bytes': False},
            {'max_bytes': 0},
            {'max_redirects': -1},
            {'max_redirects': 1.5},
            {'trust_env': 1},
            {'allow_cross_origin_redirects': None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                Client(**kwargs)


if __name__ == '__main__':
    unittest.main()
