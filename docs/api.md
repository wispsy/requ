# HTTP client contract

`Client` and `AsyncClient` accept these keyword-only configuration fields (the
synchronous dataclass also accepts positional configuration for compatibility):

| Option | Default | Meaning |
| --- | --- | --- |
| timeout | 10 seconds | Each blocking socket operation; not DNS or total time |
| max_bytes | 5,000,000 | Maximum buffered response bytes |
| max_request_bytes | 5,000,000 | Maximum transmitted body bytes |
| max_connections | 10 | Total concurrent leases/retained direct connections across origins |
| pool_timeout | 10 seconds | Maximum wait for an available lease |
| keepalive_expiry | 30 seconds | Idle direct connections expire on the next acquisition |
| max_redirects | 5 | Maximum GET/HEAD redirects |
| allow_cross_origin_redirects | False | Opt in, stripping all caller headers |
| trust_env | False | Opt in to urllib environment/system proxy discovery |

Numeric time bounds must be finite and positive; count/byte bounds must be positive
integers, except `max_redirects` permits zero. Booleans are not accepted as numbers.
Clients can be shared between threads. Async clients use the same core via
`asyncio.to_thread`, not native event-loop sockets. Cancellation stops waiting and
does not roll back mutations or cancel the active worker. Applications needing a
hard process-level deadline must arrange it outside this client.

## Requests

`request(method, url, *, headers=None, body=None, json=..., params=None)` accepts
absolute ASCII HTTP(S) URLs without credentials, whitespace or control characters.
Fragments are removed. Unicode paths/hosts must already be URL/IDNA encoded.
`params` uses standard `urlencode(..., doseq=True)` and may be a mapping or ordered
pairs, preserving repeated keys. CONNECT, TRACE and GET/HEAD bodies are unsupported.
Conveniences: `get`, `head`, `post`, `put`, `patch`, `delete`.

Bodies are immutable bytes or strict finite JSON (`json=None` sends JSON null).
`body` and `json` are exclusive. JSON uses UTF-8, string object keys, JSON primitive
values, lists and dictionaries; tuple/custom-value coercion is rejected. Encoding
occurs before the byte limit is checked, so the caller must also bound input memory.
`encode_json` exposes the same encoder and `JSONValue` its recursive type.

Headers are copied, case-insensitively deduplicated and validated as token names
and Latin-1 values without controls. The transport owns Host, Connection,
Content-Length, Transfer-Encoding, Proxy-Authorization, Proxy-Connection,
Accept-Encoding, Keep-Alive, TE, Trailer, Upgrade and Expect. JSON defaults to
Content-Type application/json; callers may select a specific JSON media type.
No cookie store, ambient API credentials or automatic retries are installed.

## Redirects, proxies and pooling

Only GET/HEAD redirects are followed. Other methods return their 3xx response;
`raise_for_status()` follows the usual 4xx/5xx convention, so inspect 3xx explicitly
if your application requires 2xx. HTTPS downgrade, cycles, invalid targets and
unselected cross-origin redirects fail. Cross-origin opt-in drops **all** caller
headers permanently for the remaining redirect chain. Automatic decompression is
absent and non-identity Content-Encoding fails for representation bodies.

TLS uses the platform default verified context with HTTP/1.1 ALPN. There is no
certificate-verification bypass. By default proxies and global urllib openers are
ignored. `trust_env=True` selects urllib's proxy discovery, CONNECT and proxy auth;
these requests hold a bounded lease but always close their actual sockets and do
not pool proxy connections. HTTP proxies can observe unencrypted HTTP traffic.
URLs are not an SSRF policy; callers select permitted destinations themselves.

Direct connections are pooled by scheme, host and port. Only completely consumed,
validly framed keep-alive responses return connections to the pool. Errors evict
connections. Idle connections may be evicted for another origin; stale connections
can fail and are never automatically retried, including safe methods. `close()` is
idempotent, closes idle sockets, wakes lease waiters and prevents new requests.
Active leases finish and close on return. Closing does not wait for or cancel them.
Use `with Client(...)` / `async with AsyncClient(...)`; async close is `aclose()`.

## Responses and errors

Frozen `Response(url, status, headers, body)` owns immutable bytes and ordered
header pairs, retaining duplicate response headers. `header(name)` selects the
first; `headers_all(name)` returns all. `text` / `decode(encoding=None,
errors='strict')` uses a declared charset or UTF-8, without sniffing. `json()`
requires UTF-8 finite JSON and rejects duplicate object members. JSON/status errors
retain no connection lease. Response content type is data; JSON parsing is explicit.

Response framing accepts a valid Content-Length or exactly chunked transfer
encoding, rejects conflicting/invalid lengths, TE+CL ambiguity and unsupported TE,
and detects truncated bodies. Header line/count bounds come from `http.client`.
HEAD/204/304 ignore representation length when checking body limits.

| Error | Meaning |
| --- | --- |
| TransportError (OSError) | Connection, TLS, socket or HTTP protocol failure; chained original error |
| TransportTimeoutError (also TimeoutError) | Socket operation timed out |
| PoolTimeoutError (TimeoutError) | Lease wait expired; no request was sent |
| ClientClosedError (RuntimeError) | No new request/lease is accepted |
| RequestLimitError / ResponseLimitError (ValueError) | Selected byte bound exceeded |
| RedirectError / ContentEncodingError (ValueError) | Selected redirect/encoding policy failed |
| HTTPStatusError (RuntimeError) | Explicit raise_for_status rejected 4xx/5xx; `.response` preserved |
| TypeError / ValueError | Invalid input, framing or JSON; no implicit correction |

Transport failure or await cancellation can occur after a mutation reached the
server. Applications must reconcile state before any explicit retry.
