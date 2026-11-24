"""Model layer. The harness only ever speaks OpenAI-compatible chat completions to ONE endpoint
(the local pool proxy). ScriptedModel is a deterministic stand-in used by tests and demos; it is
never presented as a real model."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    lane: str | None = None          # which pool lane served this turn (shown in UI)
    usage: dict[str, int] = field(default_factory=dict)

    def as_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                for c in self.tool_calls]
        return msg


class Model:
    name = "model"

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:  # pragma: no cover
        raise NotImplementedError


class ModelUnavailable(RuntimeError):
    """Raised only for a setup defect the pool could not route around (never a raw 429)."""


class OpenAICompatModel(Model):
    """Client for any OpenAI-compatible /chat/completions endpoint (normally the local pool)."""

    def __init__(self, base_url: str, model: str = "auto", api_key: str = "wright-local",
                 timeout: float = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.name = f"openai-compat:{model}"

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
                lane = r.headers.get("x-wright-lane")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ModelUnavailable(f"model endpoint returned {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise ModelUnavailable(f"model endpoint unreachable at {self.base_url}: {e.reason}") from None
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": fn.get("arguments")}
            calls.append(ToolCall(name=fn["name"], args=args, id=tc.get("id") or ToolCall("x", {}).id))
        return Reply(text=msg.get("content") or "", tool_calls=calls, lane=lane,
                     usage=data.get("usage") or {})


Step = Callable[[list[dict]], Reply]


class ScriptedModel(Model):
    """Deterministic model for tests and offline demos. Each step is a function of the transcript
    so far, which lets a script react to what it observed (e.g. a failing hook)."""

    name = "scripted"

    def __init__(self, steps: list[Step | Reply], loop_last: bool = False):
        self.steps = list(steps)
        self.i = 0
        self.loop_last = loop_last
        self.seen: list[list[dict]] = []
        self.seen_tools: list[list[str]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        self.seen.append(json.loads(json.dumps(messages)))
        self.seen_tools.append([t["function"]["name"] for t in tools])
        if self.i >= len(self.steps):
            if self.loop_last and self.steps:
                self.i = len(self.steps) - 1
            else:
                return Reply(text="(script exhausted)")
        step = self.steps[self.i]
        self.i += 1
        r = step(messages) if callable(step) else step
        return Reply(text=r.text, tool_calls=[ToolCall(c.name, dict(c.args)) for c in r.tool_calls],
                     lane="scripted")


def call(name: str, **args: Any) -> Reply:
    """Shorthand for a scripted turn that makes one tool call."""
    return Reply(tool_calls=[ToolCall(name, args)])


def say(text: str) -> Reply:
    return Reply(text=text)


def last_tool_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "tool":
            return m.get("content") or ""
    return ""
