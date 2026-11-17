# Changelog

## 0.2.0 (unreleased)

- Add bounded Requests-style helpers and sessions, forms/multipart, response facades
  and request errors while preserving the native client/response contracts.
- Add separate connect/socket timeouts and explicit redirect suppression.

## 0.1.0

- Bounded HTTP(S) methods, query pairs, bytes and finite JSON over a client-owned
  HTTP/1.1 pool with explicit lifecycle and asynchronous worker-thread facade.
- Verified TLS, explicit proxies, isolated redirects, strict framing, transport
  errors and owned buffered responses without automatic retries or decompression.
