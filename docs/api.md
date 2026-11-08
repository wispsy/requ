# Requ API Reference

`requ` provides explicit, bounded HTTP(S) GET retrieval built strictly on Python's standard library (`urllib.request` and `ssl`). It has zero third-party dependencies.

## Key Design Principles

1. **Explicit Bounds**: Enforces limits on response body size (`max_bytes`), redirects (`max_redirects`), and blocking socket operations (`timeout`).
2. **Immutable Responses**: `Response` objects own and buffer their payload, closing underlying sockets before returning to the caller.
3. **No Hidden State**: No connection pooling, no cookies, no implicit decompression, no ambient global opener modification.
4. **Strict URL Validation**: Requires absolute ASCII HTTP(S) URLs without credentials, control characters, or whitespace.

---

## Classes & Exceptions

### `Client`

```python
@dataclass(frozen=True, slots=True)
class Client:
    timeout: float = 10.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    allow_cross_origin_redirects: bool = False
    trust_env: bool = False
```

- `timeout`: Maximum seconds allowed for blocking socket operations (does not bound DNS resolution or total wall-clock time).
- `max_bytes`: Maximum allowed payload byte length. Responses exceeding this threshold raise `ResponseLimitError`.
- `max_redirects`: Maximum number of sequential redirects followed before raising `RedirectError`.
- `allow_cross_origin_redirects`: When `False` (default), redirects to a different origin raise `RedirectError`. When `True`, redirects across origins proceed, but all caller-supplied headers are stripped for safety.
- `trust_env`: Whether to read proxy configuration from environment variables (defaults to `False`).

#### Methods

- `get(url: str, *, headers: Mapping[str, str] | None = None) -> Response`
  Executes an HTTP GET request. `headers` must be Latin-1 encodable string pairs with no control characters. Reserved headers (`Host`, `Connection`, `Content-Length`, `Transfer-Encoding`, `Proxy-Authorization`, `Accept-Encoding`) cannot be overridden.

---

### `Response`

```python
@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
```

#### Properties & Methods

- `status: int`: HTTP status code (e.g. 200, 404).
- `body: bytes`: Immutable response body bytes.
- `text: str`: Decodes `body` using the declared `Content-Type` charset, falling back to UTF-8.
- `decode(encoding: str | None = None, *, errors: str = 'strict') -> str`: Decodes body bytes with optional encoding override.
- `header(name: str) -> str | None`: Returns the first matching header value (case-insensitive), or `None`.
- `headers_all(name: str) -> tuple[str, ...]`: Returns all matching header values in appearance order.
- `raise_for_status() -> None`: Raises `HTTPStatusError` if `status` is in the 4xx or 5xx range.

---

### Exceptions

- `ResponseLimitError(ValueError)`: Raised when the received bytes exceed `max_bytes`, or when `Content-Length` exceeds `max_bytes`.
- `RedirectError(ValueError)`: Raised on redirect limit exhaustion, cycle detection, unauthorized cross-origin navigation, or HTTPS-to-HTTP downgrade.
- `ContentEncodingError(ValueError)`: Raised when a server responds with non-identity `Content-Encoding`.
- `HTTPStatusError(RuntimeError)`: Raised by `response.raise_for_status()` for 4xx/5xx responses; references `.response`.
