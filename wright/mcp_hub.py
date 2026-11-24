"""MCP hub: attaches MCP servers (stdio or streamable HTTP) and registers their tools in the
ToolRegistry as `<server>__<tool>`. Built-in servers are ordinary MCP servers launched as
subprocesses; user servers (Home Assistant, router management, FreeCAD, ...) attach by adding one
entry to config.json → "mcp_servers". The core loop never changes when a capability is added.

The official MCP Python SDK (mcp>=2.3) does the protocol; this module only bridges its async client
into the synchronous harness via one event-loop thread and one task per server (anyio cancel scopes
must be exited by the task that entered them).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .tools import Tool, ToolRegistry, ToolResult

BUILTIN_SERVERS: dict[str, dict] = {
    "shell": {"module": "wright.servers.shell"},
    "fs": {"module": "wright.servers.fs"},
    "web": {"module": "wright.servers.web"},
    "code": {"module": "wright.servers.code"},
    "image": {"module": "wright.servers.image"},
    "browser": {"module": "wright.servers.browser", "optional": "playwright"},
}
# Built-in servers resolve relative paths against the current task's sandbox.
PATH_ARGS = ("path", "cwd")


@dataclass
class ServerHandle:
    name: str
    spec: dict
    queue: asyncio.Queue
    task: asyncio.Task
    ready: concurrent.futures.Future
    tools: list[str]


class McpHub:
    def __init__(self, registry: ToolRegistry, get_workdir: Callable[[], str] | None = None,
                 env: dict[str, str] | None = None, on_event: Callable[[dict], None] | None = None,
                 call_timeout: float = 600, key_lookup: Callable[[str], str | None] | None = None):
        self.registry = registry
        self.get_workdir = get_workdir or os.getcwd
        self.env = env or {}
        self.on_event = on_event or (lambda e: None)
        self.call_timeout = call_timeout
        self.key_lookup = key_lookup or (lambda ref: None)
        self.servers: dict[str, ServerHandle] = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="wright-mcp")
        self.thread.start()

    # ---------------------------------------------------------------- lifecycle
    def _params(self, name: str, spec: dict) -> Any:
        from mcp import StdioServerParameters
        if "url" in spec:
            if not spec.get("headers"):
                return spec["url"]
            # Header values may reference stored secrets ("Bearer keyref:ha_token") so that tokens
            # never sit in config.json.
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
            headers = {}
            for k, v in spec["headers"].items():
                if "keyref:" in v:
                    pre, ref = v.split("keyref:", 1)
                    v = pre + (self.key_lookup(ref.strip()) or "")
                headers[k.strip()] = v.strip()
            return streamable_http_client(spec["url"], http_client=httpx2.AsyncClient(headers=headers, timeout=60))
        if "module" in spec:
            cmd, args = sys.executable, ["-m", spec["module"]]
        else:
            cmd, args = spec["command"], list(spec.get("args", []))
        env = {**self.env, **spec.get("env", {})}
        return StdioServerParameters(command=cmd, args=args, env=env, cwd=spec.get("cwd"))

    async def _serve(self, name: str, spec: dict, queue: asyncio.Queue,
                     ready: concurrent.futures.Future) -> None:
        from mcp import Client
        try:
            async with Client(self._params(name, spec), read_timeout_seconds=self.call_timeout) as client:
                listed = await client.list_tools()
                if not ready.done():
                    ready.set_result(listed.tools)
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    tool, args, fut = item
                    try:
                        res = await client.call_tool(tool, args)
                        if not fut.done():
                            fut.set_result(res)
                    except Exception as e:  # report the failure to the caller; keep serving
                        if not fut.done():
                            fut.set_exception(e)
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
            self.on_event({"kind": "mcp_server_down", "server": name, "error": str(e)[:300]})

    def attach(self, name: str, spec: dict, timeout: float = 60) -> list[str]:
        if name in self.servers:
            self.detach(name)
        opt = spec.get("optional")
        if opt:
            import importlib.util
            if importlib.util.find_spec(opt) is None:
                self.on_event({"kind": "mcp_server_skipped", "server": name, "reason": f"{opt} not installed"})
                return []
        ready: concurrent.futures.Future = concurrent.futures.Future()

        async def start() -> ServerHandle:
            q: asyncio.Queue = asyncio.Queue()
            t = asyncio.ensure_future(self._serve(name, spec, q, ready))
            return ServerHandle(name, spec, q, t, ready, [])

        handle = asyncio.run_coroutine_threadsafe(start(), self.loop).result(10)
        tools = ready.result(timeout)
        builtin = "module" in spec and spec["module"].startswith("wright.servers.")
        for t in tools:
            full = f"{name}__{t.name}"
            ann = t.annotations
            self.registry.register(Tool(
                name=full, description=(t.description or "")[:1000],
                schema=t.input_schema or {"type": "object", "properties": {}},
                fn=self._caller(handle, t.name, builtin), source=f"mcp:{name}",
                read_only=bool(ann and ann.read_only_hint),
                destructive=bool(ann and ann.destructive_hint),
                open_world=bool(ann and ann.open_world_hint),
                mutating=(False if (ann and ann.read_only_hint) else None)))
            handle.tools.append(full)
        self.servers[name] = handle
        self.on_event({"kind": "mcp_server_attached", "server": name, "tools": handle.tools})
        return handle.tools

    def detach(self, name: str) -> None:
        h = self.servers.pop(name, None)
        if not h:
            return
        self.registry.unregister_source(f"mcp:{name}")
        self.loop.call_soon_threadsafe(h.queue.put_nowait, None)

    def attach_builtins(self, names: list[str] | None = None) -> dict[str, list[str]]:
        out = {}
        for n, spec in BUILTIN_SERVERS.items():
            if names is None or n in names:
                out[n] = self.attach(n, spec)
        return out

    def attach_config(self, servers: dict[str, dict]) -> dict[str, list[str] | str]:
        out: dict[str, list[str] | str] = {}
        for n, spec in servers.items():
            if spec.get("disabled"):
                continue
            try:
                out[n] = self.attach(n, spec)
            except Exception as e:
                out[n] = f"failed: {e}"
                self.on_event({"kind": "mcp_server_down", "server": n, "error": str(e)[:300]})
        return out

    def close(self) -> None:
        for n in list(self.servers):
            self.detach(n)
        self.loop.call_soon_threadsafe(self.loop.stop)

    # ---------------------------------------------------------------- calls
    def _caller(self, handle: ServerHandle, tool: str, builtin: bool) -> Callable[[dict], ToolResult]:
        def call(args: dict) -> ToolResult:
            args = dict(args)
            if builtin:
                wd = self.get_workdir()
                for k in PATH_ARGS:
                    v = args.get(k)
                    if isinstance(v, str) and not os.path.isabs(os.path.expanduser(v)):
                        args[k] = str(Path(wd, v))
                if handle.name in ("shell", "code") and "cwd" not in args:
                    args["cwd"] = wd
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self.loop.call_soon_threadsafe(handle.queue.put_nowait, (tool, args, fut))
            try:
                res = fut.result(self.call_timeout)
            except concurrent.futures.TimeoutError:
                return ToolResult(f"MCP call {handle.name}/{tool} timed out", is_error=True)
            except Exception as e:
                return ToolResult(f"MCP call {handle.name}/{tool} failed: {e}", is_error=True)
            return self._convert(res, handle.name, tool, args)
        return call

    @staticmethod
    def _convert(res: Any, server: str, tool: str, args: dict) -> ToolResult:
        texts, images = [], []
        for c in res.content or []:
            if getattr(c, "type", "") == "text":
                texts.append(c.text)
            elif getattr(c, "type", "") == "image":
                images.append({"mime": c.mime_type, "data": c.data})
        text = "\n".join(texts)
        if not texts and res.structured_content is not None:
            text = json.dumps(res.structured_content)
        data: dict[str, Any] = {"mcp_server": server}
        if server in ("shell", "code"):
            data["command"] = args.get("command") or args.get("code", "")[:500]
            data["cwd"] = args.get("cwd")
            try:
                data["exit_code"] = json.loads(text).get("exit_code")
            except (json.JSONDecodeError, AttributeError):
                data["exit_code"] = None
        if "path" in args:
            data["path"] = args["path"]
        is_err = bool(res.is_error) or (server in ("shell", "code") and data.get("exit_code") not in (0,))
        return ToolResult(text, is_error=is_err, images=images, data=data)
