# requ

Explicit, bounded HTTP(S) requests with Python's standard library. Python 3.14+;
no runtime dependencies.

```sh
pip install requ
```

```python
from requ import Client

with Client(timeout=10, max_bytes=2_000_000, max_connections=4) as client:
    response = client.post('https://api.example.com/users', json={'name': 'Ash'})
    response.raise_for_status()
    user = response.json()
```

Use `AsyncClient` with `async with` and `await client.request(...)` for asynchronous
applications. Its I/O runs in worker threads. Canceling an await does not undo or
stop a dispatched request; the connection remains leased until the worker returns.

- General methods, query pairs, bounded bytes/finite JSON and owned responses.
- Client-owned HTTP/1.1 pool with finite capacity, lease wait and idle expiry.
- Verified TLS, validated headers, strict framing and bounded redirects.
- No retries, cookies, automatic decompression or global opener changes.
- Environment proxies require `trust_env=True`; proxy requests use non-pooled
  urllib transport while retaining the client's concurrency/body/socket bounds.

`timeout` bounds individual blocking socket operations. It is **not** a DNS or total
wall-clock deadline. Direct sockets remain in the pool until expiry or `close()`;
responses themselves contain only owned bytes. Always use a context or close the
client when finished. GET/HEAD may follow bounded redirects; mutations return their
redirect response without replay. Cross-origin redirect opt-in strips caller headers.

See [the API reference](docs/api.md) for the precise limits and error contract.
API resources, credentials and GraphQL belong in a higher-level library such as
[apiconn](https://github.com/wispsy/apiconn).

Development: `uv sync`, then
`uv run python -W error::ResourceWarning -m unittest discover -s tests -v`.
Build and independently test both distributions with
`uv run python scripts/check_distribution.py`.

MIT.

## Requests-style calls

Version 0.2.0 adds a bounded frontend for ordinary Requests-style usage:

```python
import requ

response = requ.post('https://api.example.com/users', json={'name': 'Ash'}, timeout=(5, 30))
response.raise_for_status()
assert response.status_code == 200
user = response.json()

with requ.Session(max_request_bytes=8_500_000) as session:
    session.headers['Authorization'] = 'Bearer caller-supplied-token'
    with open('image.png', 'rb') as source:
        response = session.put('https://api.example.com/image',
                               files={'file': ('image.png', source, 'image/png')})
```

The frontend supports forms, bounded multipart, case-insensitive response headers,
owned content, request exceptions and explicit session cookies. Native Client and
AsyncClient retain their original API and safety policies. See the
[supported frontend contract](docs/api.md#requests-style-frontend-020)
for differences from Requests and unsupported options.
