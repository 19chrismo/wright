"""Image MCP server: hand an image file (screenshot, diagram, photo) to the model, with basic info."""
from __future__ import annotations

import json
import struct
from pathlib import Path

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp"}


def image_info(path: str) -> dict:
    p = Path(path)
    data = p.read_bytes()
    info = {"path": str(p), "bytes": len(data), "mime": MIME.get(p.suffix.lower(), "application/octet-stream")}
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        info["width"], info["height"] = struct.unpack(">II", data[16:24])
    return info


def main() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.utilities.types import Image
    from mcp.types import ToolAnnotations

    server = MCPServer("wright-image", instructions="Look at image files.")
    ro = ToolAnnotations(read_only_hint=True)

    @server.tool(name="view", description="Return an image file so you can see it (png, jpg, gif, webp).",
                 annotations=ro)
    def view(path: str) -> list:
        return [json.dumps(image_info(path)), Image(path=path)]

    @server.tool(name="info", description="Image size and type without viewing it.", annotations=ro)
    def info(path: str) -> str:
        return json.dumps(image_info(path))

    server.run("stdio")


if __name__ == "__main__":
    main()
