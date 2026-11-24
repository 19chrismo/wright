"""Filesystem MCP server: read, write, exact-string edit, list, search."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

MAX_READ = 400_000


def fs_read(path: str, offset: int = 0, limit: int = MAX_READ) -> str:
    p = Path(path)
    data = p.read_bytes()
    text = data.decode(errors="replace")
    return text[offset:offset + limit]


def fs_write(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content)
    return json.dumps({"path": str(p), "bytes": len(content.encode()), "created": not existed})


def fs_edit(path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = Path(path)
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        raise ValueError("old string not found")
    if n > 1 and not replace_all:
        raise ValueError(f"old string occurs {n} times; make it unique or set replace_all")
    p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
    return json.dumps({"path": str(p), "replacements": n if replace_all else 1})


def fs_list(path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
    p = Path(path)
    it = p.rglob(pattern) if recursive else p.glob(pattern)
    out = []
    for i, x in enumerate(sorted(it)):
        if i >= 2000:
            out.append("… truncated")
            break
        out.append(str(x.relative_to(p)) + ("/" if x.is_dir() else ""))
    return "\n".join(out)


def fs_search(path: str, regex: str, glob: str = "*") -> str:
    rx = re.compile(regex)
    hits = []
    for f in Path(path).rglob(glob):
        if not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line[:200]}")
                    if len(hits) >= 500:
                        return "\n".join(hits)
        except OSError:
            continue
    return "\n".join(hits)


def _abs(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def main() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("wright-fs", instructions="Read and modify files in the task sandbox.")
    cwd = os.getcwd()
    ro = ToolAnnotations(read_only_hint=True)

    @server.tool(name="read", description="Read a text file.", annotations=ro)
    def read(path: str, offset: int = 0, limit: int = MAX_READ) -> str:
        return fs_read(_abs(path, cwd), offset, limit)

    @server.tool(name="write", description="Create or overwrite a file with content.",
                 annotations=ToolAnnotations(destructive_hint=True))
    def write(path: str, content: str) -> str:
        return fs_write(_abs(path, cwd), content)

    @server.tool(name="edit", description="Replace an exact string in a file.",
                 annotations=ToolAnnotations(destructive_hint=True))
    def edit(path: str, old: str, new: str, replace_all: bool = False) -> str:
        return fs_edit(_abs(path, cwd), old, new, replace_all)

    @server.tool(name="list", description="List files (glob pattern, optional recursion).", annotations=ro)
    def list_(path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
        return fs_list(_abs(path, cwd), pattern, recursive)

    @server.tool(name="search", description="Search file contents with a regex.", annotations=ro)
    def search(path: str, regex: str, glob: str = "*") -> str:
        return fs_search(_abs(path, cwd), regex, glob)

    server.run("stdio")


if __name__ == "__main__":
    main()
