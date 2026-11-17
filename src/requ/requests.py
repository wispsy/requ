"""Bounded Requests-style conveniences over Requ's native owned transport."""

import base64
from collections.abc import Mapping, MutableMapping
from email.message import Message
from http.cookiejar import CookieJar
import json as _json
import math
import os
import re
import secrets
import threading
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request as CookieRequest

from . import Client, ClientClosedError, ContentEncodingError, PoolTimeoutError, RedirectError
from . import RequestLimitError, Response as NativeResponse, ResponseLimitError, TransportError, TransportTimeoutError
from .exceptions import ConnectionError, HTTPError, InvalidHeader, InvalidJSONError, InvalidURL, JSONDecodeError, RequestException, Timeout, TooManyRedirects

_MISSING = object()
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class CaseInsensitiveDict(MutableMapping):
    def __init__(self, values=(), **kwargs):
        self._values = {}
        self.update(values, **kwargs)

    def __getitem__(self, key):
        return self._values[key.lower()][1]

    def __setitem__(self, key, value):
        if not isinstance(key, str):
            raise TypeError('Header names must be strings.')
        self._values[key.lower()] = (key, value)

    def __delitem__(self, key):
        del self._values[key.lower()]

    def __iter__(self):
        return (key for key, value in self._values.values())

    def __len__(self):
        return len(self._values)

    def copy(self):
        return type(self)(self.items())


class Response:
    """Mutable application/test facade containing only owned response bytes."""

    def __init__(self):
        self.status_code, self.url, self.encoding = 0, '', None
        self.headers, self.raw_headers, self._content = CaseInsensitiveDict(), (), b''

    @classmethod
    def from_native(cls, response):
        result = cls()
        result.status_code, result.url, result.raw_headers, result._content = response.status, response.url, response.headers, response.body
        for key, value in response.headers:
            result.headers[key] = result.headers[key] + ', ' + value if key in result.headers else value
        return result

    @property
    def status(self):
        return self.status_code

    @property
    def content(self):
        return self._content

    @property
    def body(self):
        return self.content

    @property
    def text(self):
        return NativeResponse(self.url, self.status_code, tuple(self.headers.items()), self.content).decode(self.encoding)

    @property
    def ok(self):
        return self.status_code < 400

    def __bool__(self):
        return self.ok

    def json(self):
        try:
            return NativeResponse(self.url, self.status_code, (), self.content).json()
        except (ValueError, UnicodeError):
            raise JSONDecodeError('Response is not valid finite JSON.', response=self) from None

    def raise_for_status(self):
        if 400 <= self.status_code <= 599:
            raise HTTPError(f'HTTP {self.status_code}', response=self)

    def close(self):
        """Responses have no live socket or file to release."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return f'<Response [{self.status_code}]>'


def _url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise InvalidURL('An absolute HTTP(S) URL without controls is required.')
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        host = parts.hostname.encode('idna').decode('ascii')
        host = '[' + host + ']' if ':' in host else host
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if port is not None:
            host += ':' + str(port)
        return urlunsplit((parts.scheme.lower(), host, quote(parts.path or '/', safe='/%:@!$&\'()*+,;=-._~'),
                           quote(parts.query, safe='/%?:@!$&\'()*+,;=-._~'), ''))
    except (ValueError, UnicodeError):
        raise InvalidURL('An absolute HTTP(S) URL without credentials is required.') from None


def _timeouts(value):
    values = value if isinstance(value, tuple) else (value, value)
    if len(values) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('Timeout must be finite positive seconds or a (connect, read) tuple.')
    return tuple(float(value) for value in values)


def _pairs(data):
    if data is None:
        return []
    result = []
    for name, value in data.items() if isinstance(data, Mapping) else data:
        for item in value if isinstance(value, (list, tuple)) else (value,):
            if item is not None:
                result.append((name, item))
                if len(result) > 1024:
                    raise RequestLimitError('Form exceeds the field count limit.')
    return result


def _parameter(value):
    if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Multipart names must be bounded strings without controls.')
    return value.replace('\\', '\\\\').replace('"', '\\"')


def _multipart(data, files, limit):
    boundary = 'requ-' + secrets.token_hex(16)
    body = bytearray()
    def append(value):
        if len(body) + len(value) > limit:
            raise RequestLimitError('Multipart request exceeds max_request_bytes.')
        body.extend(value)
    def part(name, value, filename=None, content_type=None, headers=()):
        disposition = 'Content-Disposition: form-data; name="' + _parameter(name) + '"'
        if filename is not None:
            disposition += '; filename="' + _parameter(filename) + '"'
        block = [disposition]
        if content_type is not None:
            if not isinstance(content_type, str) or not content_type.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in content_type):
                raise InvalidHeader('Invalid multipart content type.')
            block.append('Content-Type: ' + content_type)
        for key, header in headers.items() if isinstance(headers, Mapping) else headers:
            if (not isinstance(key, str) or not _TOKEN.fullmatch(key) or key.lower() in ('content-disposition', 'content-type', 'content-length', 'transfer-encoding')
                    or not isinstance(header, str) or not header.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in header)):
                raise InvalidHeader('Invalid multipart part header.')
            block.append(key + ': ' + header)
        encoded = ('\r\n'.join(block) + '\r\n\r\n').encode('utf-8')
        if len(encoded) > 8192:
            raise RequestLimitError('Multipart part headers exceed the limit.')
        append(b'--' + boundary.encode() + b'\r\n' + encoded)
        if hasattr(value, 'read'):
            value = value.read(max(0, limit - len(body)) + 1)
        if isinstance(value, str):
            value = value.encode('utf-8')
        if not isinstance(value, bytes):
            raise TypeError('Multipart content must be bytes, text or a readable file.')
        append(value)
        append(b'\r\n')
    fields = _pairs(data)
    for name, value in fields:
        part(str(name), value if isinstance(value, bytes) else str(value))
    entries = files.items() if isinstance(files, Mapping) else files
    count = len(fields)
    for name, value in entries:
        count += 1
        if count > 1024:
            raise RequestLimitError('Multipart request exceeds the part count limit.')
        filename, content_type, extra = None, None, ()
        if isinstance(value, tuple):
            if not 2 <= len(value) <= 4:
                raise ValueError('File tuples require filename, content and optional content type/headers.')
            filename, value, *options = value
            content_type = options[0] if options else None
            extra = options[1] if len(options) > 1 else ()
        else:
            filename = os.path.basename(getattr(value, 'name', str(name)))
        part(str(name), value, filename, content_type, extra)
    append(b'--' + boundary.encode() + b'--\r\n')
    return bytes(body), 'multipart/form-data; boundary=' + boundary


class Session:
    """A serialized, explicitly closed session with bounded transport and cookies."""

    def __init__(self, *, timeout=10, max_bytes=5_000_000, max_request_bytes=5_000_000, trust_env=False):
        _timeouts(timeout)
        if (any(type(value) is not int or value < 1 for value in (max_bytes, max_request_bytes))
                or type(trust_env) is not bool):
            raise ValueError('Select positive integer byte limits and a boolean proxy selection.')
        self.timeout, self.max_bytes, self.max_request_bytes, self.trust_env = timeout, max_bytes, max_request_bytes, trust_env
        self.headers, self.cookies, self.auth = CaseInsensitiveDict(), CookieJar(), None
        self.max_redirects, self.allow_cross_origin_redirects = 5, False
        self._client, self._configuration, self._closed, self._lock = None, None, False, threading.RLock()

    def close(self):
        with self._lock:
            self._closed = True
            if self._client is not None:
                self._client.close()

    def __enter__(self):
        if self._closed:
            raise RequestException('Session is closed.')
        return self

    def __exit__(self, *args):
        self.close()

    def request(self, method, url, *, params=None, data=None, json=None, files=None, headers=None,
                timeout=None, allow_redirects=True, auth=None):
        with self._lock:
            if self._closed:
                raise RequestException('Session is closed.')
            url = _url(url)
            if type(allow_redirects) is not bool:
                raise TypeError('allow_redirects must be a boolean.')
            connect, read = _timeouts(self.timeout if timeout is None else timeout)
            selected = self.headers.copy()
            for name, value in (headers or {}).items():
                if value is None:
                    selected.pop(name, None)
                else:
                    selected[name] = value
            chosen_auth = self.auth if auth is None else auth
            if chosen_auth is not None:
                if not isinstance(chosen_auth, tuple) or len(chosen_auth) != 2 or any(not isinstance(value, str) for value in chosen_auth):
                    raise TypeError('auth must be a (username, password) string tuple.')
                token = base64.b64encode(':'.join(chosen_auth).encode('latin-1')).decode('ascii')
                selected['Authorization'] = 'Basic ' + token
            cookie_request = CookieRequest(url)
            self.cookies.add_cookie_header(cookie_request)
            if 'cookie' not in selected and cookie_request.has_header('Cookie'):
                selected['Cookie'] = cookie_request.get_header('Cookie')
            if json is not None and (data is not None or files is not None):
                raise ValueError('Select JSON or form/file data.')
            body = None
            if files is not None:
                try:
                    body, content_type = _multipart(data, files, self.max_request_bytes)
                except RequestLimitError as error:
                    raise RequestException('Multipart request exceeds the selected policy.') from error
                if 'content-type' in selected and selected['content-type'] != content_type:
                    raise InvalidHeader('Multipart Content-Type is owned by its encoder.')
                selected['Content-Type'] = content_type
            elif json is not None:
                try:
                    body = _json.dumps(json, allow_nan=False, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                except (TypeError, ValueError, UnicodeError):
                    raise InvalidJSONError('Request is not finite JSON.') from None
                selected.setdefault('Content-Type', 'application/json')
            elif data is not None:
                if isinstance(data, (str, bytes)):
                    body = data.encode('utf-8') if isinstance(data, str) else data
                else:
                    try:
                        body = urlencode(_pairs(data)).encode('ascii')
                    except RequestLimitError as error:
                        raise RequestException('Form request exceeds the selected policy.') from error
                    selected.setdefault('Content-Type', 'application/x-www-form-urlencoded')
            configuration = (connect, read, self.max_bytes, self.max_request_bytes, self.trust_env,
                             self.max_redirects, self.allow_cross_origin_redirects)
            if configuration != self._configuration:
                client = Client(timeout=read, connect_timeout=connect, max_bytes=self.max_bytes,
                                max_request_bytes=self.max_request_bytes, trust_env=self.trust_env,
                                max_redirects=self.max_redirects, allow_cross_origin_redirects=self.allow_cross_origin_redirects)
                if self._client is not None:
                    self._client.close()
                self._client, self._configuration = client, configuration
            try:
                from . import _headers
                _headers(dict(selected.items()))
            except (ValueError, TypeError):
                raise InvalidHeader('Request headers are invalid or owned by the transport.') from None
            try:
                native = self._client.request(method, url, headers=dict(selected.items()), body=body,
                                              params=params, follow_redirects=allow_redirects)
            except (TransportTimeoutError, PoolTimeoutError) as error:
                raise Timeout('HTTP operation timed out.') from error
            except (TransportError, ClientClosedError) as error:
                raise ConnectionError('HTTP transport failed.') from error
            except RedirectError as error:
                raise TooManyRedirects('Redirect policy rejected the response.') from error
            except (RequestLimitError, ResponseLimitError, ContentEncodingError) as error:
                raise RequestException('HTTP response or request exceeds the selected policy.') from error
            except ValueError as error:
                raise RequestException('HTTP input or response violates the transport contract.') from error
            response = Response.from_native(native)
            cookie_headers = Message()
            for name, value in native.headers:
                cookie_headers.add_header(name, value)
            class CookieResponse:
                def info(self):
                    return cookie_headers
            self.cookies.extract_cookies(CookieResponse(), CookieRequest(native.url))
            return response

    def get(self, url, params=None, **kwargs):
        return self.request('GET', url, params=params, **kwargs)

    def head(self, url, **kwargs):
        kwargs.setdefault('allow_redirects', False)
        return self.request('HEAD', url, **kwargs)

    def options(self, url, **kwargs):
        return self.request('OPTIONS', url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        return self.request('POST', url, data=data, json=json, **kwargs)

    def put(self, url, data=None, **kwargs):
        return self.request('PUT', url, data=data, **kwargs)

    def patch(self, url, data=None, **kwargs):
        return self.request('PATCH', url, data=data, **kwargs)

    def delete(self, url, **kwargs):
        return self.request('DELETE', url, **kwargs)


def request(method, url, **kwargs):
    """Send one bounded request and close its client before returning bytes."""
    with Session() as session:
        return session.request(method, url, **kwargs)


def get(url, params=None, **kwargs):
    return request('GET', url, params=params, **kwargs)


def head(url, **kwargs):
    kwargs.setdefault('allow_redirects', False)
    return request('HEAD', url, **kwargs)


def options(url, **kwargs):
    return request('OPTIONS', url, **kwargs)


def post(url, data=None, json=None, **kwargs):
    return request('POST', url, data=data, json=json, **kwargs)


def put(url, data=None, **kwargs):
    return request('PUT', url, data=data, **kwargs)


def patch(url, data=None, **kwargs):
    return request('PATCH', url, data=data, **kwargs)


def delete(url, **kwargs):
    return request('DELETE', url, **kwargs)
