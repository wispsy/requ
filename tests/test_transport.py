from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading
import time
import unittest
from unittest.mock import Mock, patch

from requ import (
    Client, ClientClosedError, PoolTimeoutError, RequestLimitError, Response,
    ResponseLimitError, TransportError, TransportTimeoutError,
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_HEAD(self):
        self.respond()

    def do_GET(self):
        self.respond()

    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        path = self.path
        if path == '/hold':
            self.server.entered.set()
            self.server.release.wait(3)
        if path == '/disconnect':
            self.close_connection = True
            return
        payload = json.dumps({
            'method': self.command, 'path': path, 'body': body.decode(),
            'port': self.client_address[1], 'headers': dict(self.headers),
        }).encode()
        self.send_response(307 if path == '/redirect' else 200)
        self.send_header('Content-Type', 'application/json')
        if path == '/redirect':
            self.send_header('Location', '/echo')
        if path in {'/chunked', '/ambiguous', '/bad-chunk'}:
            self.send_header('Transfer-Encoding', 'chunked')
        if path != '/chunked' and path != '/bad-chunk':
            self.send_header('Content-Length', str(len(payload)))
        if path == '/unsupported-te':
            self.send_header('Transfer-Encoding', 'gzip')
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            if path == '/chunked':
                self.wfile.write(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
            elif path == '/bad-chunk':
                self.wfile.write(b'4\r\nx')
                self.close_connection = True
            else:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.entered = threading.Event()
        cls.server.release = threading.Event()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def test_methods_json_query_and_owned_headers(self):
        with Client() as client:
            for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                options = {} if method == 'GET' else {'json': {'name': 'café'}}
                source = {'X-Private': 'value'}
                response = client.request(method, self.base + '/echo?first=1', headers=source,
                                          params=[('q', 'a/b &é'), ('q', 'two')], **options)
                data = response.json()
                self.assertEqual(data['method'], method)
                self.assertEqual(data['path'], '/echo?first=1&q=a%2Fb+%26%C3%A9&q=two')
                self.assertEqual(source, {'X-Private': 'value'})
                self.assertEqual(data['headers']['accept-encoding'], 'identity')
                if method != 'GET':
                    self.assertEqual(json.loads(data['body']), {'name': 'café'})
                    self.assertEqual(data['headers']['content-type'], 'application/json')
            self.assertEqual(client.head(self.base).body, b'')
            self.assertEqual(client.post(self.base, json=None).json()['body'], 'null')
            self.assertEqual(client.put(self.base, body=b'raw').json()['body'], 'raw')
            self.assertEqual(client.patch(self.base).status, 200)
            self.assertEqual(client.delete(self.base).status, 200)
        self.assertEqual(response.json()['method'], 'OPTIONS')

    def test_pool_reuses_connections_and_expires_idle(self):
        with Client(keepalive_expiry=0.02) as client:
            first = client.get(self.base).json()['port']
            self.assertEqual(first, client.get(self.base).json()['port'])
            time.sleep(0.04)
            self.assertNotEqual(first, client.get(self.base).json()['port'])
        with self.assertRaises(ClientClosedError):
            client.get(self.base)
        client.close()

    def test_pool_capacity_wait_timeout_and_close_wakes_waiter(self):
        self.server.entered.clear()
        self.server.release.clear()
        client = Client(max_connections=1, pool_timeout=0.05)
        try:
            with ThreadPoolExecutor(2) as executor:
                first = executor.submit(client.get, self.base + '/hold')
                self.assertTrue(self.server.entered.wait(2))
                with self.assertRaises(PoolTimeoutError):
                    client.get(self.base)
                pending = executor.submit(client.get, self.base)
                client.close()
                with self.assertRaises(ClientClosedError):
                    pending.result(2)
                self.server.release.set()
                self.assertEqual(first.result(2).status, 200)
        finally:
            self.server.release.set()
            client.close()

    def test_error_eviction_and_no_mutation_replay(self):
        with Client(max_connections=1) as client:
            first = client.get(self.base).json()['port']
            for path in ('/ambiguous', '/unsupported-te'):
                with self.assertRaises(ValueError):
                    client.get(self.base + path)
            self.assertNotEqual(first, client.get(self.base).json()['port'])
            with self.assertRaises(TransportError) as raised:
                client.get(self.base + '/bad-chunk')
            self.assertIsInstance(raised.exception.__cause__, IncompleteRead)
            self.assertEqual(client.get(self.base + '/chunked').status, 200)
            self.assertEqual(client.post(self.base + '/redirect', json={'x': 1}).status, 307)
            with self.assertRaises(TransportError):
                client.post(self.base + '/disconnect', json={'x': 1})
            self.assertEqual(client.get(self.base).status, 200)

    def test_limits_and_invalid_inputs_precede_connection(self):
        with Client(max_request_bytes=3) as client, patch('requ.HTTPConnection') as connection:
            for kwargs in ({'body': 'text'}, {'body': b'x', 'json': {}}, {'json': float('nan')},
                           {'headers': {'TE': 'trailers'}}, {'headers': {'Upgrade': 'websocket'}}):
                with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                    client.post(self.base, **kwargs)
            with self.assertRaises(RequestLimitError):
                client.post(self.base, body=b'1234')
            for method in ('bad method', 'CONNECT', 'TRACE'):
                with self.assertRaises(ValueError):
                    client.request(method, self.base)
            with self.assertRaises(ValueError):
                client.get(self.base, body=b'x')
            connection.assert_not_called()
        for option in ('max_connections', 'max_request_bytes', 'pool_timeout', 'keepalive_expiry'):
            with self.assertRaises(ValueError):
                Client(**{option: 0})
        with Client(max_bytes=1) as client:
            with self.assertRaises(ResponseLimitError):
                client.get(self.base + '/chunked')
            self.assertEqual(client.head(self.base).body, b'')

    def test_finite_json_decoding(self):
        for body in (b'NaN', b'Infinity', b'1e999', b'{"x":1,"x":2}', b'\xff'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                Response('', 200, (), body).json()
        self.assertEqual(Response('', 200, (), b'{"x":[null,true,1.5]}').json(), {'x': [None, True, 1.5]})

    def test_transport_errors_close_connections_without_retry(self):
        for error in (ssl.SSLCertVerificationError('untrusted'), TimeoutError('socket timeout')):
            connection = Mock()
            connection.request.side_effect = error
            with Client() as client, patch('requ.HTTPSConnection', return_value=connection) as factory:
                expected = TransportTimeoutError if isinstance(error, TimeoutError) else TransportError
                with self.assertRaises(expected):
                    client.post('https://example.test', json={'value': 1})
                factory.assert_called_once()
                connection.close.assert_called_once()

    def test_explicit_proxy_routing(self):
        with patch.dict('os.environ', {'http_proxy': self.base, 'no_proxy': '', 'NO_PROXY': ''}):
            with Client(trust_env=True) as client:
                data = client.get('http://unresolvable.invalid/echo').json()
                self.assertEqual(data['path'], 'http://unresolvable.invalid/echo')


if __name__ == '__main__':
    unittest.main()
