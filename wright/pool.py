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
import os
import re
import secrets as pysecrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


def _mins(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600, 1)} h"


def describe_error(lane: "Lane", model: str, status: int, data: Any) -> str:
    """A provider refusal in plain words."""
    err = data[0] if isinstance(data, list) and data else data
    err = err.get("error", err) if isinstance(err, dict) else {}
    msg = str(err.get("message") or err.get("status") or "")[:160]
    m = model.split(":", 1)[1] if model.startswith("ollama:") else model
    if status == 429:
        return "hit its usage limit" + (f" ({msg})" if msg else "")
    if status in (401, 403):
        return "the service rejected the key"
    if status == 404:
        return (f"the model {m} isn't downloaded yet" if lane.local else f"the model {m} isn't available to this key")
    if status == 400:
        return f"the service refused the request ({msg or 'bad request'})"
    if status == 599:
        return "not reachable" + (" — Ollama isn't running" if lane.local else "")
    return f"the AI service had a problem ({status}{': ' + msg if msg else ''})"


GEMINI_KEYS = {"type", "description", "enum", "properties", "required", "items", "nullable", "format",
               "minItems", "maxItems", "minimum", "maximum"}


def gemini_schema(schema: Any) -> Any:
    """Reduce a JSON Schema to the subset Gemini's function declarations accept: one type per field
    (unions → first non-null type + nullable), no $schema/$defs/additionalProperties/title/default,
    `required` limited to declared properties. [INFERRED from Gemini's documented OpenAPI subset;
    a stricter schema only narrows what the model sends, and every tool still validates its args.]"""
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema and "type" not in schema:
        first = next((x for x in schema["anyOf"] if isinstance(x, dict) and x.get("type") != "null"), {})
        schema = {**first, **{k: v for k, v in schema.items() if k != "anyOf"}}
    out: dict[str, Any] = {}
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        out["type"] = non_null[0] if non_null else "string"
        if "null" in t:
            out["nullable"] = True
    elif t:
        out["type"] = t
    for k in GEMINI_KEYS - {"type", "properties", "items", "required"}:
        if k in schema:
            out[k] = schema[k]
    if out.get("format") not in (None, "enum", "date-time"):
        out.pop("format")
    if "properties" in schema and isinstance(schema["properties"], dict):
        out["properties"] = {k: gemini_schema(v) for k, v in schema["properties"].items()}
        if not out.get("type"):
            out["type"] = "object"
        req = [r for r in schema.get("required", []) if r in out["properties"]]
        if req:
            out["required"] = req
    if "items" in schema:
        out["items"] = gemini_schema(schema["items"])
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {"type": "string"}
    return out


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
    last_error: str = ""        # the latest refusal, in plain words (shown by "check setup")
    total: int = 0              # all-time successful requests (persisted)
    last_model: str = ""
    last_task: str = ""         # the request it last served (shown in "show keys")


class Pool:
    def __init__(self, lanes: list[Lane], routes: dict[str, list[str]],
                 key_lookup: Callable[[str], str | None],
                 quota_groups: dict[str, str] | None = None,
                 daily_reset: dict[str, str] | None = None, max_wait: float = 120,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 600, on_event: Callable[[dict], None] | None = None,
                 local_only: bool = False, usage_file: Path | None = None):
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
        self.usage_file = usage_file
        self._saved: dict[str, dict] = {}
        self._load_usage()

    # ---------------------------------------------------------------- usage survives restarts
    # (so "show keys" can say where each key was used, and daily shares hold across restarts)
    PERSIST = ("day", "day_counts", "total", "last_used", "last_model", "last_task")

    def _load_usage(self) -> None:
        if not self.usage_file or not self.usage_file.exists():
            return
        try:
            self._saved = json.loads(self.usage_file.read_text()).get("lanes", {})
        except (json.JSONDecodeError, OSError):
            self._saved = {}
        for lane_id, st in self.stats.items():
            self._restore(lane_id, st)

    def _restore(self, lane_id: str, st: LaneStats) -> None:
        saved = self._saved.get(lane_id)
        if not saved:
            return
        for k in self.PERSIST:
            if k in saved:
                setattr(st, k, saved[k])
        if st.day != self._today():
            st.day, st.day_counts = self._today(), {}

    def _save_usage(self) -> None:
        if not self.usage_file:
            return
        for lane_id, st in self.stats.items():
            self._saved[lane_id] = {k: getattr(st, k) for k in self.PERSIST}
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.usage_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"lanes": self._saved}, indent=1))
        os.replace(tmp, self.usage_file)

    def annotate(self, lane_id: str, task: str) -> None:
        with self.lock:
            st = self.stats.get(lane_id)
            if st is not None:
                st.last_task = task[:120]
                self._save_usage()

    # ---------------------------------------------------------------- live changes (from the window)
    def add_lane(self, lane: Lane) -> None:
        with self.lock:
            self.lanes = [l for l in self.lanes if l.id != lane.id]
            local = [l for l in self.lanes if l.local]
            self.lanes = [l for l in self.lanes if not l.local] + [lane] + local
            self.stats[lane.id] = LaneStats()
            self._restore(lane.id, self.stats[lane.id])

    def remove_lane(self, lane_id: str) -> None:
        with self.lock:
            self.lanes = [l for l in self.lanes if l.id != lane_id]
            self.stats.pop(lane_id, None)
            for k in [k for k in self.cooldown if k[0] == lane_id]:
                del self.cooldown[k]

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
        if lane.provider == "gemini" and payload.get("tools"):
            payload["tools"] = [{**t, "function": {**t["function"],
                                                   "parameters": gemini_schema(t["function"].get("parameters") or {})}}
                                for t in payload["tools"]]
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
                        st.total += 1
                        st.last_model = model
                        st.day_counts[model] = st.day_counts.get(model, 0) + 1
                        st.day_counts["__total__"] = st.day_counts.get("__total__", 0) + 1
                        self._save_usage()
                    degraded = model != first_choice
                    self.on_event({"kind": "lane", "lane": lane.id, "model": model,
                                   "degraded": degraded})
                    return 200, data, {"x-wright-lane": f"{lane.id}/{model}",
                                       "x-wright-degraded": "1" if degraded else "0"}
                if status != 200:
                    st.last_error = describe_error(lane, model, status, data)
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
        now = self.clock()
        local_models = sorted({m.split(":", 1)[1] for ms in self.routes.values() for m in ms if m.startswith("ollama:")})
        lines = []
        for l in self.lanes:
            st = self.stats[l.id]
            name = l.id + (f" ({l.owner})" if l.owner else "")
            cool = [u - now for (lid, _g), u in self.cooldown.items() if lid == l.id and u > now]
            if st.disabled:
                lines.append(f"{name}: the key was rejected — remove it and add it again.")
            elif l.daily_cap is not None and st.day_counts.get("__total__", 0) >= l.daily_cap:
                lines.append(f"{name}: today's agreed share ({l.daily_cap}) is used up.")
            elif cool and not st.last_error.startswith(("not reachable", "the AI service had a problem")):
                lines.append(f"{name}: {st.last_error or 'resting after a usage limit'} — usable again in "
                             f"{_mins(max(cool))}.")
            elif st.last_error:
                lines.append(f"{name}: {st.last_error}")
        has_local = any(l.local for l in self.lanes)
        head = "No AI model could answer right now."
        tail = []
        if not has_local:
            tail.append("Add the free local model so work never stops: type “install the free local model”.")
        elif any(self.stats[l.id].last_error for l in self.lanes if l.local):
            tail.append("For the local model: type “install the free local model” (it uses a model already on this "
                        f"Mac if there is one{', otherwise downloads ' + local_models[0] if local_models else ''}). "
                        "If a download is still running, wait for it to finish.")
        if any(self.stats[l.id].disabled for l in self.lanes):
            tail.append("For a rejected key: type “remove <name>'s key”, then “add <name>'s key” with a fresh one.")
        tail.append("Type “check setup” to test every key again.")
        return head + ("\n• " + "\n• ".join(lines) if lines else "") + "\n" + " ".join(tail)

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            return {"lanes": [{
                "id": l.id, "provider": l.provider, "local": l.local, "models": l.models,
                "owner": l.owner, "daily_cap": l.daily_cap, "last_error": self.stats[l.id].last_error,
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
