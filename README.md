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
