"""Mock OpenAI-compatible upstream used by tests and offline demos. It emulates per-key provider
behaviour (success, Gemini-style 429 with RetryInfo, daily-quota 429, 401) in front of a
deterministic ScriptedModel. Clearly a test double; never wired into a real run."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .llm import Model


def gemini_429(retry_seconds: int = 30, daily: bool = False) -> list[dict]:
    quota = ("GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily
             else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
    return [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                       "message": "You exceeded your current quota.",
                       "details": [
                           {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaMetric": "generativelanguage.googleapis.com/"
                                            "generate_content_free_tier_requests",
                                            "quotaId": quota}]},
                           {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": f"{retry_seconds}s"}]}}]


class MockUpstream:
    """keys: {key: {"ok_for": int|None, "then": "429"|"429-daily"|"401"|"500"}}; key None = no auth."""

    def __init__(self, brain: Model, keys: dict[str | None, dict] | None = None,
                 models: list[str] | None = None, native: bool = False,
                 speed: tuple[float, float] = (15.0, 3.5)):
        self.brain = brain
        self.native = native               # also answer Ollama's /api/chat (streaming NDJSON)
        self.speed = speed                 # (prompt tokens/s, generated tokens/s) reported in eval stats
        self.native_log: list[dict] = []
        self.models = models or ["models/gemini-2.5-flash", "models/gemini-2.5-pro"]
        self.keys = keys or {None: {"ok_for": None}}
        self.served: dict[str | None, int] = {}
        self.log: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, obj: Any) -> None:
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self) -> None:
                auth = self.headers.get("Authorization", "")
                key = auth[7:] if auth.startswith("Bearer ") else None
                if not self.path.endswith("/models"):
                    return self._send(404, {"error": {"message": "not found"}})
                if key not in outer.keys:
                    return self._send(401, {"error": {"message": "API key not valid"}})
                self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in outer.models]})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.endswith("/api/chat"):
                    if not outer.native:
                        return self._send(404, {"error": "not found"})
                    outer.native_log.append(body)
                    msgs = [{"role": m["role"], "content": m.get("content", "")} for m in body.get("messages", [])]
                    reply = outer.brain.complete(msgs, body.get("tools", []))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    words = (reply.text or "").split(" ")
                    for w in words:
                        if w:
                            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": w + " "},
                                                          "done": False}) + "\n").encode())
                    calls = [{"function": {"name": c.name, "arguments": c.args}} for c in reply.tool_calls]
                    pe, ge = 1200, 60
                    self.wfile.write((json.dumps({"message": {"role": "assistant", "content": "", "tool_calls": calls},
                                                  "done": True, "prompt_eval_count": pe,
                                                  "prompt_eval_duration": int(pe / outer.speed[0] * 1e9),
                                                  "eval_count": ge, "eval_duration": int(ge / outer.speed[1] * 1e9)})
                                 + "\n").encode())
                    return
                auth = self.headers.get("Authorization", "")
                key = auth[7:] if auth.startswith("Bearer ") else None
                with outer.lock:
                    rule = outer.keys.get(key)
                    outer.log.append({"key": key, "model": body.get("model"), "t": time.time(),
                                      "auth_header": auth, "tools": body.get("tools")})
                    if rule is None:
                        return self._send(401, {"error": {"message": "API key not valid"}})
                    served = outer.served.get(key, 0)
                    limit = rule.get("ok_for")
                    if limit is not None and served >= limit:
                        then = rule.get("then", "429")
                        if then == "429":
                            return self._send(429, gemini_429(rule.get("retry", 30)))
                        if then == "429-daily":
                            return self._send(429, gemini_429(daily=True))
                        if then == "401":
                            return self._send(401, {"error": {"message": "API key not valid"}})
                        return self._send(500, {"error": {"message": "internal"}})
                    outer.served[key] = served + 1
                reply = outer.brain.complete(body.get("messages", []), body.get("tools", []))
                msg = reply.as_message()
                self._send(200, {"id": "mock-" + str(served), "object": "chat.completion",
                                 "model": body.get("model"),
                                 "choices": [{"index": 0, "message": msg,
                                              "finish_reason": "tool_calls" if reply.tool_calls else "stop"}],
                                 "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                           "total_tokens": 15}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
