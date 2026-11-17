import base64
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

import requ
from requ import requests


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self):
        body = self.rfile.read(int(self.headers.get('content-length', '0')))
        self.server.calls.append((self.command, self.path, body))
        if self.path == '/slow':
            time.sleep(0.15)
        data = json.dumps({'method': self.command, 'path': self.path,
                           'body': base64.b64encode(body).decode(), 'type': self.headers.get('content-type'),
                           'auth': self.headers.get('authorization'), 'cookie': self.headers.get('cookie'),
                           'custom': self.headers.get('x-custom')}).encode()
        self.send_response(404 if self.path == '/missing' else 302 if self.path == '/redirect' else 200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(data)))
        self.send_header('X-Multi', 'one')
        self.send_header('X-Multi', 'two')
        if self.path == '/redirect':
            self.send_header('location', '/echo')
        if self.path == '/set':
            self.send_header('set-cookie', 'session=one; Path=/')
            self.send_header('set-cookie', 'other=two; Path=/')
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(data)
            except OSError:
                pass

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = respond


class RequestsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.calls = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def test_top_level_methods_and_response_surface(self):
        for method in ('get', 'post', 'put', 'patch', 'delete', 'options'):
            result = getattr(requ, method)(self.url + '/echo')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.json()['method'], method.upper())
            self.assertIsInstance(result.content, bytes)
            self.assertEqual(result.content, result.body)
            self.assertEqual(result.headers['x-MULTI'], 'one, two')
            self.assertEqual(len([key for key, value in result.raw_headers if key.lower() == 'x-multi']), 2)
            self.assertTrue(result.ok)
            result.close()
        self.assertEqual(requ.head(self.url + '/echo').content, b'')

    def test_native_response_aliases_keep_the_immutable_contract(self):
        result = requ.Response('https://example.test', 200, (('X-A', 'one'),), b'body')
        self.assertEqual((result.status_code, result.content), (200, b'body'))
        self.assertEqual(result.headers, (('X-A', 'one'),))
        with self.assertRaises(AttributeError):
            result.body = b'changed'

    def test_json_and_repeated_form_fields(self):
        result = requ.post(self.url + '/echo', json={'aliases': ('one', 'two')})
        self.assertEqual(json.loads(base64.b64decode(result.json()['body'])), {'aliases': ['one', 'two']})
        self.assertEqual(result.json()['type'], 'application/json')
        result = requ.post(self.url + '/echo', data=[('x', 'one'), ('x', 'two'), ('omit', None), ('list', ['a', 'b'])])
        self.assertEqual(base64.b64decode(result.json()['body']), b'x=one&x=two&list=a&list=b')
        self.assertEqual(result.json()['type'], 'application/x-www-form-urlencoded')
        self.assertEqual(requ.get(self.url + '/echo', params=[('x', 'a b'), ('x', 'c')]).json()['path'], '/echo?x=a+b&x=c')

    def test_multipart_fields_files_and_caller_ownership(self):
        source = io.BytesIO(b'\x00image\xff')
        result = requ.put(self.url + '/echo', data={'caption': 'café'},
                          files={'file': ('a"b.png', source, 'image/png', {'X-Part': 'yes'})})
        payload = result.json()
        raw = ('Content-Type: ' + payload['type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + base64.b64decode(payload['body'])
        form = BytesParser(policy=default).parsebytes(raw)
        field, upload = list(form.iter_parts())
        self.assertEqual(field.get_payload(decode=True).decode(), 'café')
        self.assertEqual(upload.get_filename(), 'a"b.png')
        self.assertEqual(upload.get_payload(decode=True), b'\x00image\xff')
        self.assertEqual(upload['X-Part'], 'yes')
        self.assertFalse(source.closed)

    def test_request_limits_apply_before_any_send(self):
        before = len(self.server.calls)
        with requests.Session(max_request_bytes=128) as session:
            with self.assertRaises((requ.RequestException, requ.RequestLimitError)):
                session.post(self.url, files={'file': ('x', io.BytesIO(b'x' * 1000))})
            with self.assertRaises(requ.RequestException):
                session.post(self.url, data=b'x' * 129)
        self.assertEqual(len(self.server.calls), before)

    def test_http_transport_timeout_and_json_exception_contract(self):
        response = requ.get(self.url + '/missing')
        self.assertFalse(response)
        with self.assertRaises(requ.HTTPError) as raised:
            response.raise_for_status()
        self.assertIs(raised.exception.response, response)
        self.assertIsInstance(raised.exception, requ.RequestException)
        self.assertEqual(raised.exception.response.status_code, 404)
        with self.assertRaises(requ.Timeout):
            requ.get(self.url + '/slow', timeout=(1, 0.02))
        with patch('requ.Client.request', side_effect=requ.TransportError('private value')):
            with self.assertRaises(requ.ConnectionError) as raised:
                requ.get(self.url)
        self.assertNotIn('private', str(raised.exception))
        value = requests.Response()
        value.status_code, value._content = 200, b'not json'
        with self.assertRaises(requests.JSONDecodeError) as raised:
            value.json()
        self.assertIsInstance(raised.exception, ValueError)
        self.assertIsInstance(raised.exception, requ.RequestException)

    def test_session_headers_basic_auth_cookie_persistence_and_close(self):
        session = requ.Session()
        session.headers['X-Custom'] = 'default'
        session.auth = ('name', 'password')
        response = session.get(self.url + '/set')
        self.assertEqual(response.json()['auth'], 'Basic bmFtZTpwYXNzd29yZA==')
        result = session.get(self.url + '/echo', headers={'x-custom': None})
        self.assertIsNone(result.json()['custom'])
        self.assertIn('session=one', result.json()['cookie'])
        self.assertIn('other=two', result.json()['cookie'])
        self.assertEqual(session.headers['x-custom'], 'default')
        session.close()
        session.close()
        with self.assertRaises(requ.RequestException):
            session.get(self.url)

    def test_redirect_selection_and_no_mutation_replay(self):
        self.assertEqual(requ.get(self.url + '/redirect').url, self.url + '/echo')
        self.assertEqual(requ.get(self.url + '/redirect', allow_redirects=False).status_code, 302)
        self.assertEqual(requ.head(self.url + '/redirect').status_code, 302)
        before = len(self.server.calls)
        self.assertEqual(requ.post(self.url + '/redirect', json={'x': 1}).status_code, 302)
        self.assertEqual(len(self.server.calls), before + 1)

    def test_timeouts_are_applied_to_connect_and_socket_io(self):
        with requ.Session() as session:
            response = session.get(self.url + '/echo', timeout=(0.2, 0.5))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(session._client.connect_timeout, 0.2)
            self.assertEqual(session._client.timeout, 0.5)
            for timeout in (True, 0, float('nan'), (1,), (1, None), (1, -1)):
                with self.assertRaises(ValueError):
                    session.get(self.url, timeout=timeout)

    def test_unicode_urls_headers_and_unsupported_options_fail_explicitly(self):
        self.assertIn('/caf%C3%A9', requ.get(self.url + '/café').url)
        for url in ('/relative', 'file:///tmp/file', 'https://name:secret@example.test', 'http://example.test/\n'):
            with self.assertRaises(requests.InvalidURL):
                requ.get(url)
        for kwargs in ({'headers': {'X': 'injected\r\nvalue'}}, {'headers': {'Host': 'other'}}):
            with self.assertRaises(requests.InvalidHeader):
                requ.get(self.url, **kwargs)
        with self.assertRaises(TypeError):
            requ.get(self.url, verify=False)
        with self.assertRaises(requests.InvalidJSONError):
            requ.post(self.url, json={'number': float('nan')})
        with self.assertRaises(ValueError):
            requ.post(self.url, files={'file': ('bad\r\nname', b'body')})


if __name__ == '__main__':
    unittest.main()
