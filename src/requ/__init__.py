"""Explicit, bounded HTTP(S) GET retrieval over urllib.request."""

from collections.abc import Mapping
from dataclasses import dataclass
from email.message import Message
from http.client import IncompleteRead
import math
import re
import ssl
from urllib.error import HTTPError as _HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

__all__ = [
    'Client',
    'Response',
    'ResponseLimitError',
    'RedirectError',
    'ContentEncodingError',
    'HTTPStatusError',
]

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED = {
    'host',
    'connection',
    'content-length',
    'transfer-encoding',
    'proxy-authorization',
    'accept-encoding',
}
_REDIRECTS = {301, 302, 303, 307, 308}


class ResponseLimitError(ValueError):
    """The response exceeds the configured byte limit."""


class RedirectError(ValueError):
    """A redirect exceeds the selected count, origin, scheme or cycle policy."""


class ContentEncodingError(ValueError):
    """A server sent content encoding other than identity."""


class HTTPStatusError(RuntimeError):
    """An explicit raise_for_status call rejected a 4xx/5xx response."""

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
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class Response:
    """An owned, buffered response; no socket remains open after get returns."""

    url: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def headers_all(self, name: str) -> tuple[str, ...]:
        return tuple(value for key, value in self.headers if key.lower() == name.lower())

    def header(self, name: str) -> str | None:
        values = self.headers_all(name)
        return values[0] if values else None

    def decode(self, encoding: str | None = None, *, errors: str = 'strict') -> str:
        """Decode with an override, declared charset, or UTF-8; never sniff HTML."""
        if encoding is None:
            message = Message()
            message['Content-Type'] = self.header('content-type') or ''
            encoding = message.get_content_charset() or 'utf-8'
        return self.body.decode(encoding, errors)

    @property
    def text(self) -> str:
        return self.decode()

    def raise_for_status(self) -> None:
        if 400 <= self.status <= 599:
            raise HTTPStatusError(self)


@dataclass(frozen=True, slots=True)
class Client:
    """A reusable configuration for independent, fully owned HTTP(S) GETs.

    timeout applies to blocking socket operations, not a total wall-clock/DNS
    deadline. There are no retries, cookies, decompression or connection pooling.
    No global opener or proxy configuration is modified. URLs are not SSRF-filtered.
    """

    timeout: float = 10.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    allow_cross_origin_redirects: bool = False
    trust_env: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError('timeout must be a finite positive number')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('timeout must be a finite positive number')
        for name, minimum in (('max_bytes', 1), ('max_redirects', 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f'{name} must be an integer')
            if value < minimum:
                raise ValueError(f'{name} must be at least {minimum}')
        if not isinstance(self.allow_cross_origin_redirects, bool) or not isinstance(self.trust_env, bool):
            raise TypeError('redirect and proxy flags must be bools')

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> Response:
        current = _url(url)
        if headers is not None and not isinstance(headers, Mapping):
            raise TypeError('headers must be a mapping or None')
        supplied: dict[str, str] = {}
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
        opener = build_opener(
            _NoRedirect(),
            ProxyHandler(None if self.trust_env else {}),
            HTTPSHandler(context=ssl.create_default_context()),
        )
        visited = {current}
        redirects = 0
        while True:
            request_headers = {
                'user-agent': 'requ/0.1.0',
                **supplied,
                'accept-encoding': 'identity',
                'connection': 'close',
            }
            request = Request(current, headers=request_headers, method='GET')
            try:
                raw = opener.open(request, timeout=self.timeout)
            except _HTTPError as error:
                raw = error
            with raw:
                location = raw.headers.get('Location')
                if raw.status in _REDIRECTS and location is not None:
                    if redirects >= self.max_redirects:
                        raise RedirectError('max_redirects exceeded')
                    try:
                        target = _url(urljoin(current, location))
                    except (TypeError, ValueError) as error:
                        raise RedirectError('invalid redirect target') from error
                    if urlsplit(current).scheme == 'https' and urlsplit(target).scheme == 'http':
                        raise RedirectError('HTTPS downgrade is forbidden')
                    if _origin(current) != _origin(target):
                        if not self.allow_cross_origin_redirects:
                            raise RedirectError('cross-origin redirect requires explicit opt-in')
                        supplied = {}  # Never forward caller headers to a new origin.
                    if target in visited:
                        raise RedirectError('redirect cycle')
                    visited.add(target)
                    redirects += 1
                    current = target
                    continue
                encodings = raw.headers.get_all('Content-Encoding', [])
                if any(value.strip().lower() not in {'', 'identity'} for value in encodings):
                    raise ContentEncodingError('only identity Content-Encoding is supported')
                lengths = raw.headers.get_all('Content-Length', [])
                length = None
                if lengths:
                    if (
                        any(not value.strip().isascii() or not value.strip().isdigit() for value in lengths)
                        or len({int(value.strip()) for value in lengths}) != 1
                    ):
                        raise ValueError('invalid or conflicting Content-Length')
                    length = int(lengths[0].strip())
                    if raw.status not in {204, 304} and length > self.max_bytes:
                        raise ResponseLimitError('declared response exceeds max_bytes')
                pieces: list[bytes] = []
                size = 0
                while True:
                    piece = raw.read1(min(65_536, self.max_bytes - size + 1))
                    if not piece:
                        break
                    size += len(piece)
                    if size > self.max_bytes:
                        raise ResponseLimitError('response exceeds max_bytes')
                    pieces.append(piece)
                body = b''.join(pieces)
                # HEAD is not supported; 204/304 have no representation body.
                if raw.status not in {204, 304} and length is not None and size < length:
                    raise IncompleteRead(body, length - size)
                return Response(current, raw.status, tuple(raw.headers.items()), body)
