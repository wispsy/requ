"""Explicit, bounded HTTP(S) requests using Python's standard library."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from http.client import HTTPConnection, HTTPSConnection, HTTPException, IncompleteRead
import json as _json
import math
import re
import ssl
from urllib.error import HTTPError as _HTTPError, URLError as _URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from ._pool import ClientClosedError, Pool, PoolTimeoutError

__all__ = [
    'Client', 'Response', 'ResponseLimitError', 'RequestLimitError',
    'RedirectError', 'ContentEncodingError', 'HTTPStatusError',
    'TransportError', 'TransportTimeoutError', 'ClientClosedError', 'PoolTimeoutError',
]

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED = {
    'host', 'connection', 'content-length', 'transfer-encoding',
    'proxy-authorization', 'proxy-connection', 'accept-encoding',
    'keep-alive', 'te', 'trailer', 'upgrade', 'expect',
}
_REDIRECTS = {301, 302, 303, 307, 308}
_MISSING = object()


class ResponseLimitError(ValueError):
    """The response exceeds the configured byte limit."""


class RequestLimitError(ValueError):
    """The request body exceeds the configured byte limit."""


class RedirectError(ValueError):
    """A redirect violates the selected count, origin, scheme or cycle policy."""


class ContentEncodingError(ValueError):
    """A server sent content encoding other than identity."""


class TransportError(OSError):
    """A connection, TLS, socket or HTTP protocol failure; never auto-retried."""


class TransportTimeoutError(TransportError, TimeoutError):
    """A blocking socket operation timed out (not a total/DNS deadline)."""


class HTTPStatusError(RuntimeError):
    """An explicit raise_for_status rejected a 4xx/5xx response."""

    def __init__(self, response: 'Response') -> None:
        self.response = response
        super().__init__(f'HTTP status {response.status}')


def _url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError('URL must be a string')
    if not value or not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('URL must be ASCII with no whitespace or control characters')
    parts = urlsplit(value)
    if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname:
        raise ValueError('an absolute HTTP(S) URL is required')
    if parts.username is not None or parts.password is not None:
        raise ValueError('URL credentials are not supported')
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError('invalid URL port')
    if '\\' in parts.netloc or '%' in parts.netloc:
        raise ValueError('invalid URL authority')
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


def _headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is not None and not isinstance(headers, Mapping):
        raise TypeError('headers must be a mapping or None')
    supplied = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError('header names and values must be strings')
        if not _TOKEN.fullmatch(name) or name.lower() in _RESERVED:
            raise ValueError('invalid or reserved request header')
        if any(ord(char) < 32 or ord(char) == 127 or ord(char) > 255 for char in value):
            raise ValueError('header values must be Latin-1 without control characters')
        key = name.lower()
        if key in supplied:
            raise ValueError('duplicate case-insensitive request header')
        supplied[key] = value
    return supplied


def _constant(value: str):
    raise ValueError('non-finite JSON number')


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate JSON object member')
        result[name] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class Response:
    """Owned bytes and ordered duplicate headers; no live response stream."""

    url: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes = field(repr=False)

    def headers_all(self, name: str) -> tuple[str, ...]:
        return tuple(value for key, value in self.headers if key.lower() == name.lower())

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

    def decode(self, encoding: str | None = None, *, errors: str = 'strict') -> str:
        if encoding is None:
            message = Message()
            message['Content-Type'] = self.header('content-type') or ''
            encoding = message.get_content_charset() or 'utf-8'
        return self.body.decode(encoding, errors)

    @property
    def text(self) -> str:
        return self.decode()

    def json(self):
        """Decode UTF-8 JSON; reject duplicate members and non-finite numbers."""
        return _json.loads(self.body.decode('utf-8'), parse_constant=_constant,
                           parse_float=_finite_float, object_pairs_hook=_object)

    def raise_for_status(self) -> None:
        if 400 <= self.status <= 599:
            raise HTTPStatusError(self)


@dataclass(frozen=True, slots=True)
class Client:
    """Client-owned bounded HTTP/1.1 pool; requests are never auto-retried.

    timeout bounds blocking socket operations, not total time or DNS. Environment
    proxy opt-in uses urllib's non-pooled transport. URLs are not SSRF-filtered.
    """

    timeout: float = 10.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    allow_cross_origin_redirects: bool = False
    trust_env: bool = False
    max_request_bytes: int = 5_000_000
    max_connections: int = 10
    pool_timeout: float = 10.0
    keepalive_expiry: float = 30.0
    _pool: Pool = field(init=False, repr=False, compare=False)
    _context: ssl.SSLContext = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ('timeout', 'pool_timeout', 'keepalive_expiry'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f'{name} must be a finite positive number')
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be a finite positive number')
        for name, minimum in (('max_bytes', 1), ('max_request_bytes', 1), ('max_redirects', 0), ('max_connections', 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f'{name} must be an integer')
            if value < minimum:
                raise ValueError(f'{name} must be at least {minimum}')
        if not isinstance(self.allow_cross_origin_redirects, bool) or not isinstance(self.trust_env, bool):
            raise TypeError('redirect and proxy flags must be bools')
        context = ssl.create_default_context()
        context.set_alpn_protocols(['http/1.1'])
        object.__setattr__(self, '_context', context)
        object.__setattr__(self, '_pool', Pool(self.max_connections, self.pool_timeout, self.keepalive_expiry))

    def close(self) -> None:
        self._pool.close()

    def __enter__(self):
        self._pool.check_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, url: str, **kwargs) -> Response:
        return self.request('GET', url, **kwargs)

    def head(self, url: str, **kwargs) -> Response:
        return self.request('HEAD', url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        return self.request('POST', url, **kwargs)

    def put(self, url: str, **kwargs) -> Response:
        return self.request('PUT', url, **kwargs)

    def patch(self, url: str, **kwargs) -> Response:
        return self.request('PATCH', url, **kwargs)

    def delete(self, url: str, **kwargs) -> Response:
        return self.request('DELETE', url, **kwargs)

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, json=_MISSING,
                params: Mapping | Sequence[tuple] | None = None) -> Response:
        self._pool.check_open()
        if not isinstance(method, str) or not _TOKEN.fullmatch(method):
            raise ValueError('method must be an HTTP token')
        method = method.upper()
        if method in {'CONNECT', 'TRACE'}:
            raise ValueError('CONNECT and TRACE are not supported')
        current = _url(url)
        if params is not None:
            query = urlencode(params, doseq=True)
            if query:
                current += ('&' if urlsplit(current).query else '?') + query
        supplied = _headers(headers)
        if json is not _MISSING:
            if body is not None:
                raise ValueError('body and json are mutually exclusive')
            body = _json.dumps(json, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            supplied.setdefault('content-type', 'application/json')
        if body is not None and not isinstance(body, bytes):
            raise TypeError('body must be bytes or None')
        if body is not None and len(body) > self.max_request_bytes:
            raise RequestLimitError('request exceeds max_request_bytes')
        if body is not None and method in {'GET', 'HEAD'}:
            raise ValueError('GET and HEAD request bodies are not supported')
        visited = {current}
        for redirects in range(self.max_redirects + 1):
            response = self._send(method, current, supplied, body)
            location = response.header('location')
            if method not in {'GET', 'HEAD'} or response.status not in _REDIRECTS or location is None:
                return response
            if redirects >= self.max_redirects:
                raise RedirectError('max_redirects exceeded')
            try:
                if not location.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in location):
                    raise ValueError('invalid redirect characters')
                target = _url(urljoin(current, location))
            except (TypeError, ValueError) as error:
                raise RedirectError('invalid redirect target') from error
            if urlsplit(current).scheme == 'https' and urlsplit(target).scheme == 'http':
                raise RedirectError('HTTPS downgrade is forbidden')
            if _origin(current) != _origin(target):
                if not self.allow_cross_origin_redirects:
                    raise RedirectError('cross-origin redirect requires explicit opt-in')
                supplied = {}
            if target in visited:
                raise RedirectError('redirect cycle')
            visited.add(target)
            current = target
        raise AssertionError('unreachable')

    def _send(self, method: str, url: str, supplied: dict, body: bytes | None) -> Response:
        headers = {'user-agent': 'requ/0.1.0', **supplied, 'accept-encoding': 'identity'}
        connection = None
        raw = None
        reusable = False
        key = _origin(url)
        try:
            if self.trust_env:
                # A lease still bounds concurrent proxy requests; urllib owns
                # actual proxy routing/CONNECT and closes its transport.
                connection = self._pool.acquire(key, lambda: HTTPConnection(key[1], key[2]))
                opener = build_opener(_NoRedirect(), ProxyHandler(), HTTPSHandler(context=self._context))
                request = Request(url, data=body, headers=headers, method=method)
                try:
                    raw = opener.open(request, timeout=self.timeout)
                except _HTTPError as error:
                    raw = error
            else:
                kind = HTTPSConnection if key[0] == 'https' else HTTPConnection
                options = {'timeout': self.timeout}
                if key[0] == 'https':
                    options['context'] = self._context
                connection = self._pool.acquire(key, lambda: kind(key[1], key[2], **options))
                parts = urlsplit(url)
                target = urlunsplit(('', '', parts.path, parts.query, ''))
                connection.request(method, target, body=body, headers=headers)
                raw = connection.getresponse()
            response = self._read(raw, method, url)
            reusable = not self.trust_env and not getattr(raw, 'will_close', True)
            return response
        except (ClientClosedError, PoolTimeoutError):
            raise
        except (OSError, HTTPException, _URLError) as error:
            reason = error.reason if isinstance(error, _URLError) else error
            kind = TransportTimeoutError if isinstance(reason, TimeoutError) else TransportError
            raise kind('HTTP transport failed') from error
        finally:
            if raw is not None:
                raw.close()
            if connection is not None:
                self._pool.release(key, connection, reusable)

    def _read(self, raw, method: str, url: str) -> Response:
        if raw.status < 200:
            raise ValueError('protocol switching is not supported')
        no_body = method == 'HEAD' or raw.status in {204, 304}
        encodings = raw.headers.get_all('Content-Encoding', [])
        if not no_body and any(v.strip().lower() not in {'', 'identity'} for v in encodings):
            raise ContentEncodingError('only identity Content-Encoding is supported')
        lengths = raw.headers.get_all('Content-Length', [])
        transfers = raw.headers.get_all('Transfer-Encoding', [])
        if transfers and (lengths or [v.strip().lower() for v in transfers] != ['chunked']):
            raise ValueError('ambiguous or unsupported response framing')
        length = None
        if lengths:
            if any(not v.strip().isascii() or not v.strip().isdigit() or len(v.strip()) > 20 for v in lengths):
                raise ValueError('invalid Content-Length')
            if len({int(v.strip()) for v in lengths}) != 1:
                raise ValueError('conflicting Content-Length')
            length = int(lengths[0].strip())
            if not no_body and length > self.max_bytes:
                raise ResponseLimitError('declared response exceeds max_bytes')
        pieces = []
        size = 0
        if not no_body:
            while True:
                piece = raw.read1(min(65_536, self.max_bytes - size + 1))
                if not piece:
                    break
                size += len(piece)
                if size > self.max_bytes:
                    raise ResponseLimitError('response exceeds max_bytes')
                pieces.append(piece)
        result = b''.join(pieces)
        if not no_body and length is not None and size < length:
            raise IncompleteRead(result, length - size)
        return Response(url, raw.status, tuple(raw.headers.items()), result)
