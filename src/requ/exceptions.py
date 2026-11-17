"""Errors for the explicitly selected Requests-style frontend."""


class RequestException(Exception):
    def __init__(self, *args, response=None, request=None):
        self.response, self.request = response, request
        super().__init__(*args)


class HTTPError(RequestException):
    pass


class ConnectionError(RequestException, OSError):
    pass


class Timeout(RequestException, TimeoutError):
    pass


class TooManyRedirects(RequestException):
    pass


class InvalidURL(RequestException, ValueError):
    pass


class InvalidHeader(RequestException, ValueError):
    pass


class InvalidJSONError(RequestException, ValueError):
    pass


class JSONDecodeError(InvalidJSONError):
    pass
