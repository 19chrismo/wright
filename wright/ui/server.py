"""Local UI server: one window, one chat. Binds to 127.0.0.1 only, rejects foreign Host/Origin
headers (DNS-rebinding), and requires a per-launch token (set as a cookie from the launch URL).
Events stream to the page over SSE; confirmations (the four unsnapshottable categories only) are
answered by clicking in the page — never by text."""
from __future__ import annotations

import base64
import ipaddress
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
MAX_BODY = 40 * 1024 * 1024


class Confirmations:
    """Bridges the agent's confirm callback to a click in the UI."""

    def __init__(self, events: Any, timeout: float = 600):
        self.events = events
        self.timeout = timeout
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    def __call__(self, category: str, description: str, details: dict) -> bool:
        cid = uuid.uuid4().hex[:10]
        ev = threading.Event()
        with self.lock:
            self.pending[cid] = {"event": ev, "answer": False}
        self.events.emit("confirm", id=cid, category=category, description=description)
        ev.wait(self.timeout)
        with self.lock:
            ans = self.pending.pop(cid, {}).get("answer", False)
        self.events.emit("confirm_answer", id=cid, approved=ans)
        return ans

    def answer(self, cid: str, approved: bool) -> bool:
        with self.lock:
            p = self.pending.get(cid)
            if not p:
                return False
            p["answer"] = bool(approved)
            p["event"].set()
            return True


class UIServer:
    def __init__(self, agent_holder: dict, events: Any, confirmations: Confirmations,
                 host: str = "127.0.0.1", port: int = 7770, info: Callable[[], dict] | None = None,
                 control: Any = None, force_ready: bool = False):
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("the UI only binds to a loopback address")
        self.holder = agent_holder
        self.events = events
        self.confirmations = confirmations
        self.info = info or (lambda: {})
        self.control = control
        self.force_ready = force_ready
        self.pending: dict | None = None          # a goal sent before any model was set up
        self.token = secrets.token_urlsafe(24)
        self.busy = threading.Lock()
        self.worker: threading.Thread | None = None
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost"):
                    return False
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                    return False
                return ipaddress.ip_address(self.client_address[0]).is_loopback

            def _authed(self) -> bool:
                c = self.headers.get("Cookie") or ""
                return f"wright={outer.token}" in [x.strip() for x in c.split(";")]

            def _send(self, code: int, body: bytes, ctype: str = "application/json",
                      extra: dict[str, str] | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, default=str).encode())

            def _guard(self) -> bool:
                if not self._host_ok():
                    self._json(403, {"error": "localhost only"})
                    return False
                if not self._authed():
                    self._json(401, {"error": "open Wright with the launch link"})
                    return False
                return True

            def do_GET(self) -> None:
                u = urlparse(self.path)
                if not self._host_ok():
                    return self._json(403, {"error": "localhost only"})
                if u.path == "/" and parse_qs(u.query).get("t", [""])[0] == outer.token:
                    return self._send(302, b"", "text/plain", {
                        "Set-Cookie": f"wright={outer.token}; HttpOnly; SameSite=Strict; Path=/",
                        "Location": "/"})
                if not self._authed():
                    return self._send(401, b"<p>Open Wright with the link it printed at launch.</p>", "text/html")
                if u.path == "/":
                    return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/info":
                    return self._json(200, outer.info())
                if u.path == "/api/events":
                    return self._stream(int(parse_qs(u.query).get("since", ["0"])[0]))
                return self._json(404, {"error": "not found"})

            def _stream(self, since: int) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                i = since
                try:
                    while True:
                        hist = outer.events.history
                        while i < len(hist):
                            data = json.dumps({**hist[i], "seq": i}, default=str)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            i += 1
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(0.25)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def do_POST(self) -> None:
                if not self._guard():
                    return
                n = int(self.headers.get("Content-Length", "0"))
                if n > MAX_BODY:
                    return self._json(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                path = urlparse(self.path).path
                if path == "/api/message":
                    return self._json(*outer.start_task(body))
                if path == "/api/confirm":
                    ok = outer.confirmations.answer(body.get("id", ""), bool(body.get("approve")))
                    return self._json(200 if ok else 404, {"ok": ok})
                if path == "/api/resume":
                    return self._json(*outer.start_task({"resume": body.get("task_id")}))
                if outer.control is not None:
                    if path == "/api/keys":            # the key goes straight to the Keychain; never logged
                        r = outer.control.add_key(body.get("provider", "gemini"), body.get("key", ""),
                                                  (body.get("owner") or "").strip(), body.get("daily_cap") or None)
                        if r["ok"]:
                            outer.control.show_setup()
                            outer.resume_pending()
                        return self._json(200, r)
                    if path == "/api/action":
                        r = outer.control.handle({"action": body.get("action", ""), "args": body.get("args", {})})
                        return self._json(200, r)
                    if path == "/api/connect":
                        r = outer.control.connect(body.get("name", "service"), body.get("url", ""),
                                                  (body.get("token") or "").strip() or None)
                        return self._json(200, r)
                return self._json(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def resume_pending(self) -> None:
        if self.pending and self.control is not None and self.control.setup_status()["ready"]:
            body, self.pending = self.pending, None
            self.events.emit("notice", text="Setup is ready — starting your request now.")
            self.start_task({**body, "force_task": True})

    def start_task(self, body: dict) -> tuple[int, dict]:
        text0 = (body.get("text") or "").strip()
        if self.control is not None and not body.get("resume"):
            if not body.get("force_task") and not body.get("images"):
                routed = __import__("wright.control", fromlist=["route"]).route(text0)
                if routed:
                    self.events.emit("user", text=text0, images=[], target=None)
                    if routed["action"] == "key_card":
                        routed.setdefault("args", {})["text"] = text0
                    return 200, {"routed": routed["action"], **self.control.handle(routed)}
            if not self.force_ready and not self.control.setup_status()["ready"]:
                self.pending = {**body, "_echoed": True}
                self.events.emit("user", text=text0, images=[{"mime": i.get("mime"), "data": i.get("data")}
                                                            for i in body.get("images") or []], target=None)
                self.events.emit("notice", text="Wright needs an AI model before it can start. Add a key or "
                                                "install the free local model below — your request will start "
                                                "by itself as soon as one is ready.")
                self.control.show_setup()
                return 202, {"pending": True}
        if not self.busy.acquire(blocking=False):
            return 409, {"error": "a task is already running; it will finish or stop at its budget"}
        agent = self.holder["agent"]
        text = (body.get("text") or "").strip()
        images = []
        for im in body.get("images") or []:
            mime = im.get("mime", "")
            if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                continue
            base64.b64decode(im["data"][:64] + "=" * (-len(im["data"][:64]) % 4))
            images.append({"mime": mime, "data": im["data"]})
        if not text and not images and not body.get("resume"):
            self.busy.release()
            return 400, {"error": "empty message"}
        if not text and images:
            text = "Act on what this screenshot shows."
        target = (body.get("target") or "").strip() or None

        def work() -> None:
            try:
                if body.get("resume"):
                    agent.resume(body["resume"])
                else:
                    if not body.get("_echoed"):
                        self.events.emit("user", text=text, images=[{"mime": i["mime"], "data": i["data"]}
                                                                    for i in images], target=target)
                    agent.run(text, target=target, images=images)
            except Exception as e:
                self.events.emit("error", detail=f"{type(e).__name__}: {e}")
            finally:
                self.busy.release()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        return 202, {"accepted": True}

    def start(self) -> "UIServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
