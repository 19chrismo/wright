"""Small local HTTP fixtures for tests and demos (e.g. a stand-in vendor documentation site)."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class StaticSite:
    def __init__(self, pages: dict[str, tuple[str, str]]):
        """pages: path -> (content_type, body)."""
        self.pages = pages
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path not in outer.pages:
                    self.send_response(404); self.end_headers(); return
                ctype, body = outer.pages[self.path]
                b = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


VENDOR_DOC = """<!doctype html><html><head><title>sshd_config(5) — OpenSSH 9.6 manual</title></head>
<body><h1>sshd_config</h1>
<p>PasswordAuthentication: Specifies whether password authentication is allowed. The default is yes.</p>
<p>PermitRootLogin: Specifies whether root can log in using ssh. The default is prohibit-password.</p>
<script>var tracking = 1;</script></body></html>"""
