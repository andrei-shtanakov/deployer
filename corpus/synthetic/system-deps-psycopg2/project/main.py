"""HTTP service proving a source-built system dependency really imported."""

from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(psycopg2.__version__.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
