"""Shell MCP server. Commands run in /bin/sh under OS write protection for Wright's protected
paths (sandbox-exec on macOS, bwrap on Linux when available)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..sandboxing import wrap


def run_command(command: str, cwd: str, timeout: float = 120, deny_write: list[Path] | None = None,
                deny_read: list[Path] | None = None) -> dict:
    argv = wrap(command, deny_write or [], deny_read or [])
    env = {k: v for k, v in os.environ.items() if not k.startswith("WRIGHT_KEY")}
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=min(timeout, 600),
                           env=env)
        return {"exit_code": p.returncode, "stdout": p.stdout[-8000:], "stderr": p.stderr[-4000:]}
    except subprocess.TimeoutExpired as e:
        return {"exit_code": None, "error": f"timeout after {timeout}s",
                "stdout": (e.stdout or "")[-4000:] if isinstance(e.stdout, str) else ""}
    except FileNotFoundError as e:
        return {"exit_code": None, "error": str(e)}


def _protected() -> tuple[list[Path], list[Path]]:
    w = [Path(p) for p in json.loads(os.environ.get("WRIGHT_DENY_WRITE", "[]"))]
    r = [Path(p) for p in json.loads(os.environ.get("WRIGHT_DENY_READ", "[]"))]
    return w, r


def main() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("wright-shell", instructions="Run shell commands in the task sandbox.")

    @server.tool(name="run", description="Run a shell command (/bin/sh). Returns exit_code, stdout, stderr.",
                 annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
    def run(command: str, cwd: str = ".", timeout: float = 120) -> str:
        w, r = _protected()
        return json.dumps(run_command(command, cwd, timeout, w, r))

    server.run("stdio")


if __name__ == "__main__":
    main()
