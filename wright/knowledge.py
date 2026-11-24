"""Knowledge packs: sources captured with hashes, decisions tagged [SOURCED]/[INFERRED]/[UNKNOWN].

Grounding is enforced, not requested:
  * a source is only recorded from content the harness itself fetched or read (hash + excerpt);
  * the excerpt must literally occur in that content, so a quote cannot be invented;
  * [SOURCED] needs a reachable tier 1-4 source (or two independent tier-5 hosts) whose version
    matches the version read from the system;
  * [INFERRED] needs a chain and at least one source it reasons from;
  * [UNKNOWN] needs a mitigation, and becomes sandbox-test-first.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import web

TIERS = {1: "vendor documentation / manuals / release notes",
         2: "published standard or specification",
         3: "primary source code and its tests",
         4: "maintainer-authored guide or changelog",
         5: "community forum or blog"}
TAGS = ("SOURCED", "INFERRED", "UNKNOWN")
MITIGATIONS = ("sandbox_test", "research", "escalate")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass
class Source:
    id: str
    locator: str                     # URL or file path
    tier: int
    retrieved_at: float
    reachable: bool
    sha256: str = ""
    title: str = ""
    excerpt: str = ""
    version_applies: str = ""        # e.g. "openwrt 23.05", "macos 15", "" = not version-specific
    component: str = ""              # which system component the version refers to
    note: str = ""

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.locator).netloc or "file"


@dataclass
class Decision:
    id: str
    statement: str
    tag: str
    confidence: float
    sources: list[str] = field(default_factory=list)
    chain: str = ""
    mitigation: str = ""
    supports: list[str] = field(default_factory=list)   # acceptance criteria names it underpins
    tool: str = ""
    mode: str = "normal"                                # normal | sandbox-test-first
    grade: str | None = None                            # held | wrong | None (ungraded)
    made_at: float = field(default_factory=time.time)


@dataclass
class KnowledgePack:
    id: str
    domain: str
    task_type: str
    intent: str = ""
    system_versions: dict[str, dict] = field(default_factory=dict)  # component -> {version, evidence}
    sources: list[Source] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    version: int = 1
    created: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ queries
    def source(self, sid: str) -> Source | None:
        return next((s for s in self.sources if s.id == sid), None)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "KnowledgePack":
        d = dict(d)
        d["sources"] = [Source(**s) for s in d.get("sources", [])]
        d["decisions"] = [Decision(**x) for x in d.get("decisions", [])]
        return cls(**d)

    def summary(self) -> str:
        lines = [f"Knowledge pack {self.id} v{self.version} — domain {self.domain}"]
        for c, v in self.system_versions.items():
            lines.append(f"  version  {c} = {v['version']}  (read via: {v['evidence'][:80]})")
        for s in self.sources:
            state = "ok" if s.reachable else "UNREACHABLE"
            lines.append(f"  source   {s.id} tier{s.tier} {state} {s.locator} — “{s.excerpt[:90]}”")
        for d in self.decisions:
            src = ",".join(d.sources) or "-"
            extra = f" mode={d.mode}" if d.mode != "normal" else ""
            lines.append(f"  [{d.tag}] {d.statement} (conf {d.confidence:.2f}; sources {src}{extra})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ mutations (validated)
    def add_source(self, src: Source) -> Source:
        for s in self.sources:
            if s.locator == src.locator and s.sha256 == src.sha256 and s.excerpt == src.excerpt:
                return s                                      # dedupe
        self.sources.append(src)
        return src

    def validate_decision(self, d: Decision) -> str | None:
        if d.tag not in TAGS:
            return f"tag must be one of {TAGS}"
        if not 0.0 <= d.confidence <= 1.0:
            return "confidence must be within 0..1"
        missing = [s for s in d.sources if not self.source(s)]
        if missing:
            return f"unknown source ids {missing}; capture the source first"
        srcs = [self.source(s) for s in d.sources]
        if d.tag == "SOURCED":
            good = [s for s in srcs if s and s.reachable and s.sha256 and s.excerpt]
            if not good:
                return ("[SOURCED] requires a captured, reachable source with an excerpt. "
                        "No source means [INFERRED] or [UNKNOWN] — never a confident guess.")
            strong = [s for s in good if s.tier <= 4]
            community = [s for s in good if s.tier == 5]
            # independent = different hosts AND different content (byte-identical copies are mirrors)
            independent = len({s.host for s in community}) >= 2 and len({s.sha256 for s in community}) >= 2
            if not strong and not independent:
                return ("[SOURCED] on community sources needs two independent hosts (different content) "
                        "or a tier 1-4 source")
            for s in good:
                if s.version_applies and s.component in self.system_versions:
                    have = self.system_versions[s.component]["version"]
                    if _norm(s.version_applies) not in _norm(have) and _norm(have) not in _norm(s.version_applies):
                        return (f"version mismatch: source {s.id} applies to {s.version_applies!r} but "
                                f"{s.component} reports {have!r}; find docs for the running version")
        elif d.tag == "INFERRED":
            if not d.chain.strip():
                return "[INFERRED] must state its reasoning chain"
            if not srcs:
                return "[INFERRED] must reason from at least one captured source; otherwise it is [UNKNOWN]"
        else:
            if d.mitigation not in MITIGATIONS:
                return f"[UNKNOWN] needs mitigation in {MITIGATIONS} (sandbox test, research, escalate)"
            d.mode = "sandbox-test-first"
        return None

    def add_decision(self, d: Decision) -> str | None:
        err = self.validate_decision(d)
        if err:
            return err
        self.decisions.append(d)
        return None


# ====================================================================== capture
def _sid(locator: str, sha: str) -> str:
    return "S" + hashlib.sha1((locator + sha).encode()).hexdigest()[:8]


def capture_url(pack: KnowledgePack, url: str, tier: int, excerpt: str,
                version_applies: str = "", component: str = "",
                fetcher: Callable[[str], web.Page] = web.fetch,
                cache_dir: Path | None = None) -> tuple[Source | None, str]:
    if tier not in TIERS:
        return None, f"tier must be 1-5 ({TIERS})"
    page = fetcher(url)
    if not page.ok:
        src = Source(_sid(url, "unreachable"), url, tier, time.time(), False,
                     note=page.error or f"status {page.status}")
        pack.add_source(src)
        return src, f"recorded as UNREACHABLE ({src.note}); it cannot ground a [SOURCED] decision"
    if not excerpt.strip() or _norm(excerpt) not in _norm(page.text):
        return None, ("excerpt not found in the fetched page; quote the page verbatim "
                      "(the harness compares it against what it actually fetched)")
    src = Source(_sid(url, page.sha256), url, tier, time.time(), True, page.sha256,
                 page.title[:200], excerpt.strip()[:1000], version_applies, component)
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{page.sha256}.txt").write_text(page.text)
    return pack.add_source(src), "captured"


def capture_file(pack: KnowledgePack, path: str, tier: int, excerpt: str,
                 version_applies: str = "", component: str = "") -> tuple[Source | None, str]:
    p = Path(path)
    if not p.is_file():
        return None, f"{path} is not a readable file"
    raw = p.read_bytes()
    text = raw.decode(errors="replace")
    if not excerpt.strip() or _norm(excerpt) not in _norm(text):
        return None, "excerpt not found in the file; quote it verbatim"
    sha = hashlib.sha256(raw).hexdigest()
    src = Source(_sid(str(p), sha), str(p), tier, time.time(), True, sha, p.name,
                 excerpt.strip()[:1000], version_applies, component)
    return pack.add_source(src), "captured"


def record_version(pack: KnowledgePack, component: str, command: str, cwd: str | None = None,
                   runner: Callable[..., Any] = subprocess.run) -> tuple[bool, str]:
    """Version check: read the version from the system itself, never assume it."""
    try:
        p = runner(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return False, f"version command failed: {e}"
    out = (p.stdout or p.stderr or "").strip()
    if p.returncode != 0 or not out:
        return False, f"version command exited {p.returncode}: {out[:200]}"
    pack.system_versions[component] = {"version": out.splitlines()[0][:200], "evidence": command,
                                       "read_at": time.time()}
    return True, out.splitlines()[0][:200]


# ====================================================================== semantic store
class SemanticStore:
    """Versioned knowledge packs on disk, grouped by domain; retrieval by domain + keywords."""

    def __init__(self, root: Path):
        self.root = root

    def save(self, pack: KnowledgePack) -> Path:
        d = self.root / re.sub(r"[^\w.-]", "_", pack.domain)
        d.mkdir(parents=True, exist_ok=True)
        existing = sorted(d.glob(f"{pack.id}.v*.json"))
        if existing:
            prev = json.loads(existing[-1].read_text())
            if prev == {**pack.as_dict(), "version": prev["version"]}:
                return existing[-1]                        # unchanged: no new version
            pack.version = int(prev["version"]) + 1
        path = d / f"{pack.id}.v{pack.version}.json"
        path.write_text(json.dumps(pack.as_dict(), indent=1))
        return path

    def load(self, domain: str, pack_id: str, version: int | None = None) -> KnowledgePack | None:
        d = self.root / re.sub(r"[^\w.-]", "_", domain)
        files = sorted(d.glob(f"{pack_id}.v*.json"), key=lambda p: int(p.stem.rsplit(".v", 1)[1]))
        if version is not None:
            files = [f for f in files if f.stem.endswith(f".v{version}")]
        return KnowledgePack.from_dict(json.loads(files[-1].read_text())) if files else None

    def retrieve(self, domain: str, query: str = "", limit: int = 3) -> list[KnowledgePack]:
        d = self.root / re.sub(r"[^\w.-]", "_", domain)
        latest: dict[str, Path] = {}
        for f in d.glob("*.v*.json"):
            pid, v = f.stem.rsplit(".v", 1)
            if pid not in latest or int(v) > int(latest[pid].stem.rsplit(".v", 1)[1]):
                latest[pid] = f
        packs = [KnowledgePack.from_dict(json.loads(f.read_text())) for f in latest.values()]
        words = set(_norm(query).split())

        def score(p: KnowledgePack) -> int:
            blob = _norm(p.intent + " " + " ".join(x.statement for x in p.decisions))
            return sum(w in blob for w in words)

        packs.sort(key=score, reverse=True)
        return packs[:limit]


# ====================================================================== model-facing tools
def knowledge_tools(get_task: Callable[[], Any], fetcher: Callable[[str], web.Page] = web.fetch,
                    cache_dir: Path | None = None) -> list:
    from .tools import Tool, ToolResult, obj

    def pack() -> KnowledgePack:
        t = get_task()
        if "pack" not in t.state:
            t.state["pack"] = KnowledgePack(id=f"kp-{t.id}", domain=t.state.get("domain", "general"),
                                            task_type=t.state.get("task_type", "general"),
                                            intent=t.state.get("intent", ""))
        return t.state["pack"]

    def capture(a: dict) -> ToolResult:
        loc = a["locator"]
        if loc.startswith(("http://", "https://")):
            src, msg = capture_url(pack(), loc, int(a["tier"]), a.get("excerpt", ""),
                                   a.get("version_applies", ""), a.get("component", ""), fetcher, cache_dir)
        else:
            src, msg = capture_file(pack(), loc, int(a["tier"]), a.get("excerpt", ""),
                                    a.get("version_applies", ""), a.get("component", ""))
        if src is None:
            return ToolResult(f"REJECTED: {msg}", is_error=True)
        return ToolResult(json.dumps({"source_id": src.id, "reachable": src.reachable, "status": msg}))

    def version(a: dict) -> ToolResult:
        ok, out = record_version(pack(), a["component"], a["command"], str(get_task().workdir))
        return ToolResult(("recorded " if ok else "FAILED ") + out, is_error=not ok)

    def decide(a: dict) -> ToolResult:
        d = Decision(id=f"D{len(pack().decisions) + 1}", statement=a["statement"], tag=a["tag"].strip("[]").upper(),
                     confidence=float(a.get("confidence", 0.5)), sources=list(a.get("sources", [])),
                     chain=a.get("chain", ""), mitigation=a.get("mitigation", ""),
                     supports=list(a.get("supports", [])), tool=a.get("tool", ""))
        hook = get_task().state.get("on_decision")
        if hook:
            hook(d)                         # calibration may downgrade the decision (step 8)
        err = pack().add_decision(d)
        if err:
            return ToolResult(f"REJECTED: {err}", is_error=True)
        return ToolResult(json.dumps({"decision_id": d.id, "tag": d.tag, "mode": d.mode}))

    def show(a: dict) -> ToolResult:
        return ToolResult(pack().summary())

    return [
        Tool("knowledge__capture", "Capture a source (URL or local file) into the knowledge pack. "
             "tier: 1 vendor docs, 2 standard/spec, 3 primary source code, 4 maintainer guide, 5 community. "
             "excerpt must be a verbatim quote from the source.",
             obj({"locator": {"type": "string"}, "tier": {"type": "integer"},
                  "excerpt": {"type": "string"}, "version_applies": {"type": "string"},
                  "component": {"type": "string"}}, ["locator", "tier", "excerpt"]),
             capture, read_only=True, open_world=True, mutating=False),
        Tool("knowledge__version", "Read a component's version from the system itself by running a "
             "read-only command (e.g. 'sw_vers -productVersion').",
             obj({"component": {"type": "string"}, "command": {"type": "string"}}),
             version, read_only=True, mutating=False),
        Tool("knowledge__decide", "Record a decision with a grounding tag SOURCED|INFERRED|UNKNOWN and a "
             "confidence 0..1. SOURCED cites captured source ids; INFERRED states the chain; UNKNOWN names "
             "a mitigation (sandbox_test|research|escalate).",
             obj({"statement": {"type": "string"}, "tag": {"type": "string"},
                  "confidence": {"type": "number"}, "sources": {"type": "array", "items": {"type": "string"}},
                  "chain": {"type": "string"}, "mitigation": {"type": "string"},
                  "supports": {"type": "array", "items": {"type": "string"}}, "tool": {"type": "string"}},
                 ["statement", "tag", "confidence"]),
             decide, read_only=True, mutating=False),
        Tool("knowledge__show", "Show the current knowledge pack.", obj({}), show, read_only=True,
             mutating=False),
    ]


def grounding_gate(task: Any, call: Any, tool: Any) -> Any:
    """No mutation before at least one tagged decision exists (subsumed by the lock in step 7)."""
    from .harness import ALLOW, Verdict
    if tool is None or tool.source == "core" or not tool.is_mutating:
        return ALLOW
    pack = task.state.get("pack")
    if not pack or not pack.decisions:
        return Verdict(False, "build the knowledge pack first: capture sources and tag at least one "
                              "decision before any mutating action")
    return ALLOW


grounding_gate.label = "grounding"  # type: ignore[attr-defined]
