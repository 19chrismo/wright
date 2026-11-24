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

    def __init__(self, brain: Model, keys: dict[str | None, dict] | None = None):
        self.brain = brain
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

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                auth = self.headers.get("Authorization", "")
                key = auth[7:] if auth.startswith("Bearer ") else None
                with outer.lock:
                    rule = outer.keys.get(key)
                    outer.log.append({"key": key, "model": body.get("model"), "t": time.time(),
                                      "auth_header": auth})
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
