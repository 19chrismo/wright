"""Code execution MCP server: run Python (or Node) snippets in the task sandbox, time-limited,
under the same OS write protection as the shell."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .shell import _protected, run_command


def run_snippet(language: str, code: str, cwd: str, timeout: float = 60) -> dict:
    lang = language.lower()
    interp = {"python": shutil.which("python3") or "python3", "node": shutil.which("node") or "node"}.get(lang)
    if not interp:
        return {"exit_code": None, "error": f"unsupported language {language!r} (python, node)"}
    suffix = ".py" if lang == "python" else ".js"
    fd, path = tempfile.mkstemp(suffix=suffix, dir=cwd, prefix=".wright-snippet-")
    with os.fdopen(fd, "w") as f:
        f.write(code)
    try:
        w, r = _protected()
        return run_command(f"{interp} {Path(path).name}", cwd, timeout, w, r)
    finally:
        os.unlink(path)


def main() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("wright-code", instructions="Execute code snippets in the sandbox.")

    @server.tool(name="run", description="Run a python or node snippet in the sandbox. Returns exit_code, stdout, stderr.",
                 annotations=ToolAnnotations(destructive_hint=True))
    def run(code: str, language: str = "python", cwd: str = ".", timeout: float = 60) -> str:
        return json.dumps(run_snippet(language, code, cwd, timeout))

    server.run("stdio")


if __name__ == "__main__":
    main()
