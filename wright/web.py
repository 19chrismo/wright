"""Web fetch and search used by the knowledge pipeline and the web MCP server."""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

UA = "Wright/0.1 (+local agent)"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "section",
             "article", "table", "blockquote", "dt", "dd"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if not self.skip:
            self.out.append(data)


def html_to_text(body: str) -> tuple[str, str]:
    p = _Text()
    p.feed(body)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text, html.unescape(p.title.strip())


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    text: str
    title: str
    sha256: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


def fetch(url: str, timeout: float = 30, max_bytes: int = 4_000_000) -> Page:
    if not url.startswith(("http://", "https://")):
        return Page(url, 0, "", "", "", "", "only http(s) URLs are fetched")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes)
            ctype = r.headers.get("Content-Type", "")
            status = r.status
    except urllib.error.HTTPError as e:
        return Page(url, e.code, "", "", "", "", f"HTTP {e.code}")
    except Exception as e:
        return Page(url, 0, "", "", "", "", f"unreachable: {e}")
    charset = re.search(r"charset=([\w-]+)", ctype)
    body = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
    if "html" in ctype or body.lstrip().lower().startswith(("<!doctype html", "<html")):
        text, title = html_to_text(body)
    else:
        text, title = body, url.rsplit("/", 1)[-1]
    return Page(url, status, ctype, text, title, hashlib.sha256(raw).hexdigest())


def search(query: str, backend: dict | None = None, timeout: float = 20) -> list[dict]:
    """Search backends: {"kind": "searxng", "url": ...} | {"kind": "brave", "key": ...} |
    default DuckDuckGo HTML. Results: [{title, url, snippet}]. Results are leads, not sources:
    a result only grounds a decision after the page itself is captured."""
    backend = backend or {"kind": "duckduckgo"}
    kind = backend.get("kind")
    try:
        if kind == "searxng":
            u = backend["url"].rstrip("/") + "/search?" + urllib.parse.urlencode(
                {"q": query, "format": "json"})
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}),
                                        timeout=timeout) as r:
                data = json.loads(r.read())
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("content", "")} for x in data.get("results", [])[:10]]
        if kind == "brave":
            u = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query})
            req = urllib.request.Request(u, headers={"X-Subscription-Token": backend["key"],
                                                     "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("description", "")} for x in data.get("web", {}).get("results", [])[:10]]
        u = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}),
                                    timeout=timeout) as r:
            body = r.read().decode(errors="replace")
        out = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                             r'class="result__snippet"[^>]*>(.*?)</', body, re.S):
            href = html.unescape(m.group(1))
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
            out.append({"url": q[0] if q else href,
                        "title": re.sub("<.*?>", "", html.unescape(m.group(2))),
                        "snippet": re.sub("<.*?>", "", html.unescape(m.group(3)))})
        return out[:10]
    except Exception as e:
        return [{"error": f"search backend {kind} failed: {e}"}]
