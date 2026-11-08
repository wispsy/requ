# requ

Explicit, bounded HTTP(S) GET retrieval over Python's standard library.

`requ` provides a lightweight, dependency-free HTTP client built on `urllib.request` and `ssl`. Python 3.14+ is required.

## Features

- **Owned and Buffered**: Returns an immutable `Response(url, status, headers, body)` and closes all sockets before returning.
- **Strict Bounding**: Defends against runaway resource usage via `timeout`, `max_bytes`, and `max_redirects`.
- **Zero Global State**: Never installs global openers or modifies standard library globals.
- **Safe by Default**: Verified TLS, cross-origin redirect isolation, and environment proxy opt-in.
- **No Surprises**: No connection pooling, no cookies, no retries, no automatic decompression.

## Installation

```sh
pip install requ
```

For development:

```sh
git clone https://github.com/wispsy/requ.git
cd requ
uv sync
```

## Quick Start

```python
from requ import Client, HTTPStatusError

client = Client(timeout=10.0, max_bytes=2_000_000)

response = client.get('https://httpbin.org/get')
response.raise_for_status()

print(response.status)
print(response.header('content-type'))
print(response.text)
```

## Documentation

See [docs/api.md](docs/api.md) for full API details, configuration options, and error handling.

## Running Tests

```sh
python -m unittest discover -s tests -v
```

## License

MIT
