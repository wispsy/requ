"""Bounded, client-owned HTTP/1.1 connection leases."""

from collections.abc import Callable
from http.client import HTTPConnection
import threading
import time


class ClientClosedError(RuntimeError):
    """The client no longer accepts requests."""


class PoolTimeoutError(TimeoutError):
    """No connection lease became available in time."""


class Pool:
    def __init__(self, capacity: int, timeout: float, expiry: float) -> None:
        self.capacity = capacity
        self.timeout = timeout
        self.expiry = expiry
        self._condition = threading.Condition()
        self._idle: list[tuple[tuple, HTTPConnection, float]] = []
        self._total = 0
        self._closed = False

    def check_open(self) -> None:
        with self._condition:
            if self._closed:
                raise ClientClosedError('client is closed')

    def acquire(self, key: tuple, factory: Callable[[], HTTPConnection]) -> HTTPConnection:
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while True:
                if self._closed:
                    raise ClientClosedError('client is closed')
                now = time.monotonic()
                keep = []
                for entry in self._idle:
                    if now - entry[2] >= self.expiry:
                        entry[1].close()
                        self._total -= 1
                    else:
                        keep.append(entry)
                self._idle = keep
                for index, entry in enumerate(self._idle):
                    if entry[0] == key:
                        return self._idle.pop(index)[1]
                if self._total < self.capacity:
                    self._total += 1
                    break
                if self._idle:
                    self._idle.pop(0)[1].close()
                    break
                remaining = deadline - now
                if remaining <= 0:
                    raise PoolTimeoutError('connection lease timed out')
                self._condition.wait(remaining)
        try:
            return factory()
        except BaseException:
            with self._condition:
                self._total -= 1
                self._condition.notify()
            raise

    def release(self, key: tuple, connection: HTTPConnection, reusable: bool) -> None:
        with self._condition:
            if reusable and not self._closed:
                self._idle.append((key, connection, time.monotonic()))
            else:
                connection.close()
                self._total -= 1
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for _, connection, _ in self._idle:
                connection.close()
            self._total -= len(self._idle)
            self._idle.clear()
            self._condition.notify_all()

    def __del__(self) -> None:
        if hasattr(self, '_condition'):
            self.close()
