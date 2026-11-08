"""Run a complete request without contacting an external service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from requ import Client


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with Client() as client:
                response = client.post(f'http://127.0.0.1:{server.server_port}/', json={'name': 'Ash'})
                response.raise_for_status()
                assert response.json() == {'name': 'Ash'}
                print(response.json())
        finally:
            server.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
