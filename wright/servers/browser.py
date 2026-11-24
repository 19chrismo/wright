"""Browser MCP server (Playwright, Chromium): open pages, read text, click, type, screenshot.
Browser path: WRIGHT_CHROMIUM if set, else Playwright's own installed Chromium."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class Browser:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self.page = None

    def ensure(self):
        if self.page is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            exe = os.environ.get("WRIGHT_CHROMIUM") or None
            self._browser = self._pw.chromium.launch(executable_path=exe, headless=True,
                                                     args=["--no-sandbox"] if exe else [])
            self.page = self._browser.new_page(viewport={"width": 1280, "height": 800})
        return self.page

    def open(self, url: str) -> dict:
        p = self.ensure()
        resp = p.goto(url, wait_until="load", timeout=30000)
        return {"url": p.url, "status": resp.status if resp else None, "title": p.title(),
                "text": p.inner_text("body")[:20000]}

    def screenshot(self, path: str | None = None, full_page: bool = False) -> str:
        p = self.ensure()
        path = path or tempfile.mktemp(suffix=".png", prefix="wright-shot-")
        p.screenshot(path=path, full_page=full_page)
        return path

    def click(self, selector: str) -> dict:
        p = self.ensure()
        p.click(selector, timeout=10000)
        p.wait_for_load_state("load")
        return {"url": p.url, "title": p.title()}

    def fill(self, selector: str, text: str) -> dict:
        p = self.ensure()
        p.fill(selector, text, timeout=10000)
        return {"filled": selector}

    def text(self, selector: str = "body") -> str:
        return self.ensure().inner_text(selector)[:20000]


def main() -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.utilities.types import Image
    from mcp.types import ToolAnnotations

    server = MCPServer("wright-browser", instructions="Drive a headless Chromium browser.")
    b = Browser()
    ow = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    @server.tool(name="open", description="Open a URL; returns status, title and page text.", annotations=ow)
    def open_(url: str) -> str:
        return json.dumps(b.open(url))

    @server.tool(name="text", description="Text of the current page or of a CSS selector.", annotations=ow)
    def text(selector: str = "body") -> str:
        return b.text(selector)

    @server.tool(name="screenshot", description="Screenshot the current page; returns the image.", annotations=ow)
    def screenshot(full_page: bool = False) -> list:
        path = b.screenshot(full_page=full_page)
        return [json.dumps({"saved": path}), Image(path=path)]

    @server.tool(name="click", description="Click an element (CSS selector).",
                 annotations=ToolAnnotations(open_world_hint=True))
    def click(selector: str) -> str:
        return json.dumps(b.click(selector))

    @server.tool(name="fill", description="Type text into an input (CSS selector).",
                 annotations=ToolAnnotations(open_world_hint=True))
    def fill(selector: str, text: str) -> str:
        return json.dumps(b.fill(selector, text))

    server.run("stdio")


if __name__ == "__main__":
    main()
