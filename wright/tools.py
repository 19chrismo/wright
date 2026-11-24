"""Tool registry. Tools come from two places: MCP servers (shell, fs, browser, web, ... and anything
the user attaches) and the harness core (intent, knowledge, plan, verify, complete...). Both register
here, dynamically, so adding a capability never touches the loop."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    images: list[dict] = field(default_factory=list)   # [{"mime": "image/png", "data": b64}]
    data: dict[str, Any] = field(default_factory=dict)  # structured extras (ledger rows, etc.)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[[dict], ToolResult]
    source: str = "core"               # "core" or "mcp:<server>"
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False           # talks to the outside world (network)
    mutating: bool | None = None       # overrides: does this call change state?

    def spec(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.schema}}

    @property
    def is_mutating(self) -> bool:
        if self.mutating is not None:
            return self.mutating
        return not self.read_only


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.listeners: list[Callable[[str, str], None]] = []

    def register(self, tool: Tool) -> None:
        if not NAME_RE.match(tool.name):
            raise ValueError(f"illegal tool name {tool.name!r}")
        self._tools[tool.name] = tool
        for fn in self.listeners:
            fn("added", tool.name)

    def unregister_source(self, source: str) -> None:
        for name in [n for n, t in self._tools.items() if t.source == source]:
            del self._tools[name]
            for fn in self.listeners:
                fn("removed", name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, only: set[str] | None = None) -> list[dict]:
        return [t.spec() for n, t in sorted(self._tools.items()) if only is None or n in only]

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in sorted(self._tools)]


def obj(props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


def local_shell_tool(cwd_default: str, deny_write: list | None = None,
                     deny_read: list | None = None) -> Tool:
    """Shell tool registered in-process (offline harness and tests). The MCP shell server exposes
    the same function over MCP; both run under the same OS write protection."""
    from .servers.shell import run_command

    def run(args: dict) -> ToolResult:
        cmd = args["command"]
        cwd = args.get("cwd") or cwd_default
        if not cwd.startswith("/"):
            cwd = str(__import__("pathlib").Path(cwd_default, cwd))
        out = run_command(cmd, cwd, float(args.get("timeout", 120)), deny_write, deny_read)
        return ToolResult(json.dumps(out), is_error=out.get("exit_code") != 0,
                          data={"command": cmd, "cwd": cwd, "exit_code": out.get("exit_code")})

    return Tool("shell__run", "Run a shell command. Returns exit_code, stdout, stderr.",
                obj({"command": {"type": "string"}, "cwd": {"type": "string"},
                     "timeout": {"type": "number"}}, ["command"]),
                run, source="local:shell", destructive=True, open_world=True)


def local_fs_tools(cwd_default: str) -> list[Tool]:
    from pathlib import Path
    from .servers import fs

    def a(p: str) -> str:
        return p if Path(p).is_absolute() else str(Path(cwd_default, p))

    def wrap(fn, mutating: bool):
        def run(args: dict) -> ToolResult:
            try:
                out = fn(args)
                return ToolResult(out, data={"path": args.get("path")})
            except Exception as e:
                return ToolResult(f"{type(e).__name__}: {e}", is_error=True)
        return run

    s = {"type": "string"}
    return [
        Tool("fs__read", "Read a text file.", obj({"path": s}), wrap(lambda x: fs.fs_read(a(x["path"])), False),
             source="local:fs", read_only=True),
        Tool("fs__write", "Create or overwrite a file.", obj({"path": s, "content": s}),
             wrap(lambda x: fs.fs_write(a(x["path"]), x["content"]), True), source="local:fs", destructive=True),
        Tool("fs__edit", "Replace an exact string in a file.", obj({"path": s, "old": s, "new": s}),
             wrap(lambda x: fs.fs_edit(a(x["path"]), x["old"], x["new"]), True), source="local:fs", destructive=True),
        Tool("fs__list", "List files.", obj({"path": s}, []),
             wrap(lambda x: fs.fs_list(a(x.get("path", "."))), False), source="local:fs", read_only=True),
    ]
