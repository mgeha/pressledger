"""Mock of the PRISMAsync accounting endpoint, for testing without the printer.

Serves /accounting/ as a directory listing with absolute hrefs, like the machine.

    python tools/fakeprinter.py <directory> <port>

The directory holds the .CSV/.ACL files to serve. Point a [[machine]] url at
http://127.0.0.1:<port> and run `pressledger sync`.
"""

import contextlib
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

if len(sys.argv) != 3:
    sys.exit(f"usage: {Path(sys.argv[0]).name} <directory> <port>")

SRC = Path(sys.argv[1])
if not SRC.is_dir():
    sys.exit(f"Not a directory: {SRC}")
try:
    PORT = int(sys.argv[2])
except ValueError:
    sys.exit(f"Port must be a number, got {sys.argv[2]!r}")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/accounting":
            files = sorted(p.name for p in SRC.iterdir() if p.suffix.upper() in (".CSV", ".ACL"))
            links = "\n".join(f'<a href="/accounting/{n}">{n}</a><br>' for n in files)
            body = f"<html><body><h1>accounting</h1>{links}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/accounting/"):
            name = self.path.split("/")[-1]
            target = SRC / name
            if target.is_file() and target.suffix.upper() in (".CSV", ".ACL"):
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        self.send_error(404)

    def log_message(self, fmt, *args):
        sys.stderr.write("fake  " + fmt % args + "\n")


print(f"Serving {SRC} on http://127.0.0.1:{PORT}/accounting/ — Ctrl-C to stop")
with contextlib.suppress(KeyboardInterrupt):
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
