"""Model access pool: a localhost-only, OpenAI-compatible proxy in front of every model lane.

A lane = one credential at one endpoint (a Gemini key, another provider's key, local Ollama).
On 429 the lane's quota group cools down (honouring the provider's retry delay), the next healthy
lane is tried, and the last resort is the local lane. A raw usage-limit error never reaches the
caller: if nothing can serve, the pool waits for the earliest cooldown inside max_wait, otherwise it
returns ONE setup defect with the fix.

Reference designs (maintainer READMEs, tier d): GemiNitro — quota groups, cooldown, LRU/sequential
rotation, per-model daily caps with a configured reset time; GeminiHydra — OpenAI-compatible
gateway with health-checked failover and a dashboard.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import ipaddress
import json
import re
import secrets as pysecrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from zoneinfo import ZoneInfo

# Defaults below are [INFERRED] (vendor docs were unreachable from the build environment);
# `wright doctor` probes every lane so a wrong default is detected on first run, not mid-task.
PROVIDER_DEFAULTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://127.0.0.1:11434/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
DEFAULT_COOLDOWN = 60.0
ERROR_COOLDOWN = 15.0


@dataclass
class Lane:
    id: str
    provider: str
    models: list[str]                       # patterns this lane can serve, e.g. "gemini-*"
    base_url: str = ""
    key_ref: str | None = None              # name in the KeyStore; never the key itself
    local: bool = False
    daily_caps: dict[str, int] = field(default_factory=dict)   # model -> max requests per day
    daily_cap: int | None = None            # whole-lane requests per day (e.g. a family member's share)
    owner: str = ""                         # whose key/project this is; shown in status and reports

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or PROVIDER_DEFAULTS.get(self.provider, "")).rstrip("/")

    def serves(self, model: str) -> bool:
        return any(fnmatch.fnmatch(model, p) for p in self.models)

    def upstream_model(self, model: str) -> str:
        return model.split(":", 1)[1] if model.startswith(f"{self.provider}:") else model


@dataclass
class LaneStats:
    requests: int = 0
    ok: int = 0
    limited: int = 0
    errors: int = 0
    last_used: float = 0.0
    disabled: str | None = None
    day: str = ""
    day_counts: dict[str, int] = field(default_factory=dict)


class Pool:
    def __init__(self, lanes: list[Lane], routes: dict[str, list[str]],
                 key_lookup: Callable[[str], str | None],
                 quota_groups: dict[str, str] | None = None,
                 daily_reset: dict[str, str] | None = None, max_wait: float = 120,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 600, on_event: Callable[[dict], None] | None = None,
                 local_only: bool = False):
        self.lanes = lanes
        self.routes = routes
        self.key_lookup = key_lookup
        self.quota_groups = quota_groups or {}
        self.daily_reset = daily_reset or {"time": "00:00", "tz": "America/Los_Angeles"}
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self.timeout = timeout
        self.on_event = on_event or (lambda e: None)
        self.local_only = local_only
        self.stats: dict[str, LaneStats] = {l.id: LaneStats() for l in lanes}
        self.cooldown: dict[tuple[str, str], float] = {}   # (lane, quota group) -> until
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- routing
    def group(self, model: str) -> str:
        for pat, g in self.quota_groups.items():
            if fnmatch.fnmatch(model, pat):
                return g
        return model

    def candidates(self, requested: str) -> list[str]:
        models = self.routes.get(requested) or [requested]
        if self.local_only:
            models = [m for m in models if any(l.local and l.serves(m) for l in self.lanes)]
        return models

    def _today(self) -> str:
        tz = ZoneInfo(self.daily_reset.get("tz", "UTC"))
        now = dt.datetime.fromtimestamp(self.clock(), tz)
        hh, mm = map(int, self.daily_reset.get("time", "00:00").split(":"))
        boundary = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        day = now.date() if now >= boundary else (now - dt.timedelta(days=1)).date()
        return day.isoformat()

    def next_reset(self) -> float:
        tz = ZoneInfo(self.daily_reset.get("tz", "UTC"))
        now = dt.datetime.fromtimestamp(self.clock(), tz)
        hh, mm = map(int, self.daily_reset.get("time", "00:00").split(":"))
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        return nxt.timestamp()

    def _available(self, lane: Lane, model: str, now: float) -> bool:
        st = self.stats[lane.id]
        if st.disabled:
            return False
        if self.local_only and not lane.local:
            return False
        if self.cooldown.get((lane.id, self.group(model)), 0) > now:
            return False
        if st.day != self._today():
            st.day, st.day_counts = self._today(), {}
        cap = lane.daily_caps.get(model)
        if cap is not None and st.day_counts.get(model, 0) >= cap:
            return False
        if lane.daily_cap is not None and st.day_counts.get("__total__", 0) >= lane.daily_cap:
            return False
        return True

    def plan(self, requested: str) -> list[tuple[str, Lane]]:
        """Ordered (model, lane) attempts: route order, then least-recently-used lane."""
        now = self.clock()
        out: list[tuple[str, Lane]] = []
        with self.lock:
            for m in self.candidates(requested):
                lanes = [l for l in self.lanes if l.serves(m) and self._available(l, m, now)]
                lanes.sort(key=lambda l: self.stats[l.id].last_used)
                out.extend((m, l) for l in lanes)
        return out

    # ---------------------------------------------------------------- upstream
    def _send(self, lane: Lane, model: str, body: dict) -> tuple[int, dict, dict[str, str]]:
        payload = dict(body)
        payload["model"] = lane.upstream_model(model)
        payload["stream"] = False
        headers = {"Content-Type": "application/json"}
        if lane.key_ref:
            key = self.key_lookup(lane.key_ref)
            if not key:
                return 401, {"error": {"message": f"no key stored for {lane.key_ref}"}}, {}
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(lane.base_url + "/chat/completions",
                                     data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {"error": {"message": raw.decode(errors="replace")[:500]}}
            return e.code, data, dict(e.headers or {})
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            return 599, {"error": {"message": f"unreachable: {e}"}}, {}

    def retry_after(self, status: int, data: Any, headers: dict[str, str]) -> float:
        """Cooldown seconds for a 429: Retry-After header, Gemini RetryInfo, or daily quota → next
        reset. Unknown shapes fall back to DEFAULT_COOLDOWN (cheap if wrong)."""
        for k, v in headers.items():
            if k.lower() == "retry-after":
                try:
                    return max(1.0, float(v))
                except ValueError:
                    pass
        err = data[0] if isinstance(data, list) and data else data
        err = err.get("error", err) if isinstance(err, dict) else {}
        text = json.dumps(err)
        if re.search(r"PerDay|per day|daily", text, re.I):
            return max(1.0, self.next_reset() - self.clock())
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
        if m:
            return max(1.0, float(m.group(1)))
        m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I)
        if m:
            return max(1.0, float(m.group(1)))
        return DEFAULT_COOLDOWN

    # ---------------------------------------------------------------- main entry
    def complete(self, body: dict) -> tuple[int, dict, dict[str, str]]:
        requested = body.get("model") or "auto"
        first_choice = (self.candidates(requested) or [requested])[0]
        deadline = self.clock() + self.max_wait
        last_client_error: tuple[int, dict] | None = None
        while True:
            for model, lane in self.plan(requested):
                st = self.stats[lane.id]
                with self.lock:
                    st.requests += 1
                    st.last_used = self.clock()
                status, data, headers = self._send(lane, model, body)
                if status == 200 and data.get("choices"):
                    with self.lock:
                        st.ok += 1
                        st.day_counts[model] = st.day_counts.get(model, 0) + 1
                        st.day_counts["__total__"] = st.day_counts.get("__total__", 0) + 1
                    degraded = model != first_choice
                    self.on_event({"kind": "lane", "lane": lane.id, "model": model,
                                   "degraded": degraded})
                    return 200, data, {"x-wright-lane": f"{lane.id}/{model}",
                                       "x-wright-degraded": "1" if degraded else "0"}
                if status == 429:
                    wait = self.retry_after(status, data, headers)
                    with self.lock:
                        st.limited += 1
                        self.cooldown[(lane.id, self.group(model))] = self.clock() + wait
                    self.on_event({"kind": "lane_cooldown", "lane": lane.id, "model": model,
                                   "seconds": round(wait, 1)})
                    continue
                if status in (401, 403):
                    with self.lock:
                        st.errors += 1
                        st.disabled = f"auth failed ({status}); re-add key with: wright keys add {lane.key_ref}"
                    self.on_event({"kind": "lane_disabled", "lane": lane.id, "reason": st.disabled})
                    continue
                if status == 400 or status == 404:
                    # Request-shaped error (e.g. model lacks tool support); try the next candidate.
                    last_client_error = (status, data)
                    with self.lock:
                        st.errors += 1
                    continue
                with self.lock:                       # 5xx / unreachable: brief cooldown
                    st.errors += 1
                    self.cooldown[(lane.id, self.group(model))] = self.clock() + ERROR_COOLDOWN
                self.on_event({"kind": "lane_error", "lane": lane.id, "model": model,
                               "status": status})
            # Nothing served this round.
            with self.lock:
                pending = [until for (lid, _g), until in self.cooldown.items()
                           if until > self.clock() and not self.stats[lid].disabled]
            earliest = min(pending) if pending else None
            if earliest is not None and earliest <= deadline:
                self.on_event({"kind": "pool_wait", "seconds": round(earliest - self.clock(), 1)})
                self.sleep(max(0.0, earliest - self.clock()) + 0.01)
                continue
            if last_client_error and not pending:
                return last_client_error[0], last_client_error[1], {}
            return 503, {"error": {"type": "wright_setup_defect", "message": self.setup_fix()}}, {}

    def setup_fix(self) -> str:
        local = [l for l in self.lanes if l.local]
        capped = [l.owner or l.id for l in self.lanes if l.daily_cap is not None
                  and self.stats[l.id].day_counts.get("__total__", 0) >= l.daily_cap]
        if capped and not local:
            return (f"Today's agreed shares are used up on: {', '.join(capped)}. Add a local model so work "
                    "continues (install Ollama, then `ollama pull qwen2.5:7b`), or raise a share with "
                    "`wright keys remove` + `wright keys add ... --daily-cap N`.")
        if not local:
            return ("Every cloud lane is cooling down and no local lane is configured. Install "
                    "Ollama (https://ollama.com), run `ollama pull qwen2.5vl:7b`, then `wright doctor`.")
        bad = [l.id for l in local if self.stats[l.id].errors]
        if bad:
            return (f"Every cloud lane is cooling down and the local lane ({', '.join(bad)}) is not "
                    "answering. Start Ollama (`ollama serve`) and make sure the model is pulled "
                    "(`ollama pull qwen2.5vl:7b`), then `wright doctor`.")
        return "No lane can serve this model route. Run `wright doctor` for the exact fix."

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            return {"lanes": [{
                "id": l.id, "provider": l.provider, "local": l.local, "models": l.models,
                "owner": l.owner, "daily_cap": l.daily_cap,
                "used_today": self.stats[l.id].day_counts.get("__total__", 0)
                if self.stats[l.id].day == self._today() else 0,
                "has_key": bool(l.key_ref and self.key_lookup(l.key_ref)) if l.key_ref else None,
                **{k: v for k, v in vars(self.stats[l.id]).items() if k != "day_counts"},
                "cooling": {g: round(u - now, 1) for (lid, g), u in self.cooldown.items()
                            if lid == l.id and u > now}} for l in self.lanes],
                "routes": self.routes, "local_only": self.local_only}


def lanes_from_config(cfg: list[dict]) -> list[Lane]:
    return [Lane(id=c["id"], provider=c["provider"], models=c.get("models", ["*"]),
                 base_url=c.get("base_url", ""), key_ref=c.get("key_ref"),
                 local=bool(c.get("local")), daily_caps=c.get("daily_caps", {}),
                 daily_cap=c.get("daily_cap"), owner=c.get("owner", "")) for c in cfg]


# ==================================================================== HTTP proxy
def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


class PoolServer:
    """OpenAI-compatible endpoint on 127.0.0.1. Requires the local bearer token."""

    def __init__(self, pool: Pool, host: str = "127.0.0.1", port: int = 8765,
                 token: str | None = None):
        if not is_loopback(host):
            raise ValueError("the pool proxy only binds to a loopback address")
        self.pool = pool
        self.token = token or pysecrets.token_urlsafe(24)
        pool_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _deny(self) -> bool:
                if not is_loopback(self.client_address[0]):
                    self._json(403, {"error": {"message": "localhost only"}})
                    return True
                if self.headers.get("Authorization", "") != f"Bearer {pool_ref.token}":
                    self._json(401, {"error": {"message": "missing local pool token"}})
                    return True
                return False

            def _json(self, code: int, obj: Any, extra: dict[str, str] | None = None) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self._deny():
                    return
                if self.path.startswith("/v1/models"):
                    names = sorted({m for ms in pool_ref.pool.routes.values() for m in ms}
                                   | set(pool_ref.pool.routes))
                    self._json(200, {"object": "list", "data": [{"id": n, "object": "model"}
                                                                 for n in names]})
                elif self.path.startswith("/pool/status"):
                    self._json(200, pool_ref.pool.status())
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                if self._deny():
                    return
                if not self.path.startswith("/v1/chat/completions"):
                    return self._json(404, {"error": {"message": "not found"}})
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                stream = bool(body.get("stream"))
                status, data, hdrs = pool_ref.pool.complete(body)
                if status == 200 and stream:
                    # Serve the finished answer as a one-chunk SSE stream for streaming clients.
                    msg = data["choices"][0]["message"]
                    chunk = {"id": data.get("id", "wright"), "object": "chat.completion.chunk",
                             "choices": [{"index": 0, "delta": msg,
                                          "finish_reason": data["choices"][0].get("finish_reason")}]}
                    payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    for k, v in hdrs.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._json(status, data, hdrs)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://{host}:{self.port}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "PoolServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
