"""Dispatcher: before any work starts, decide what kind of request this is and where it should run,
and estimate how long it will take — in plain words, instantly, without asking a model.

  mode    quick  — a question or research: read-only, answered without the 8-stage build process
          build  — makes or changes something: the full process (copy first, checks, undo)
  private        — mentions passwords, keys, money, camera footage, router configs …: stays on this Mac
  size    small | medium | large
  route   fast | auto | deep | local   (see config.normalize_routes)

The estimate learns from finished jobs (the episodic log records mode, size, where and seconds) and
from the measured speed of this Mac's own model."""
from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

PRIVATE = re.compile(
    r"\b(passwords?|passcodes?|pass ?phrases?|pin ?codes?|2fa|one[- ]time codes?|api ?keys?|tokens?|secrets?|"
    r"private keys?|ssh keys?|credentials?|log ?ins?|bank(ing)?|credit cards?|debit cards?|card numbers?|"
    r"accounts? numbers?|tax(es| returns?)?|t4|sin number|social insurance|social security|ssn|salary|"
    r"payroll|invoices?|budgets?|finances?|financial|accounting|bookkeeping|medical|health records?|"
    r"prescriptions?|passports?|driver'?s licen[cs]e|security cameras?|camera footage|recordings? of|"
    r"router config(uration)?|wi-?fi password|psk|wpa ?key|keep (this|it) private|private only|"
    r"don'?t send|do not send|offline only|stay(s)? on (this|my) mac)\b", re.I)
REQUEST_PREFIX = re.compile(r"^\s*(please\s+|(can|could|would|will) you\s+(please\s+)?|i (want|need|would like) "
                            r"(you )?to\s+|i'?d like (you )?to\s+|let'?s\s+|help me\s+(to\s+)?)", re.I)
EXPLAIN = re.compile(r"^\s*(how (do|does|can|could|should|would) (i|you|we|one|it)|how to|what|whats|what's|why|when|"
                     r"where|who|which|is|are|was|were|does|do|did|should|explain|tell me|teach me|compare|"
                     r"recommend|suggest|find out|look up|look into|research|summari[sz]e|list|give me (ideas|"
                     r"a list|some)|show me (how|what|why)|help me understand|i wonder)\b", re.I)
ACTION = re.compile(r"\b(build|make|create|write|code|fix|repair|install|set ?up|configure|change|update|upgrade|"
                    r"delete|remove|clean( up)?|move|rename|add|connect|deploy|design|print|generate|automate|"
                    r"organi[sz]e|convert|edit|modify|replace|download|back ?up|restore|flash|reset|tune|"
                    r"optimi[sz]e|speed up|turn (on|off)|enable|disable|start|stop|restart|run|extract|sort|"
                    r"merge|dedupe|de-duplicate|scan|map|draw|model|scaffold|apply)\b", re.I)
RESEARCH = re.compile(r"\b(research|find out|look into|dig into|compare|best way|options|everything about|"
                      r"in depth|deep dive|how does .* work|reverse[- ]engineer|underneath|alternatives?)\b", re.I)
BIG = re.compile(r"\b(app|application|website|web ?site|robot|bot|autonomous|system|integrat\w*|dashboard|game|"
                 r"server|from scratch|full|complete|firmware|reverse[- ]engineer\w*|platform|project|"
                 r"home assistant|automation[s]?|smart home|network)\b", re.I)
SMALL = re.compile(r"\b(rename|move|delete|remove|turn (on|off)|restart|install|add|copy|open|show)\b", re.I)
DEEP = re.compile(r"\b(think (hard|carefully|deeply)|carefully|thorough(ly)?|deep(ly)? (think|analy\w+)|"
                  r"best possible|use the (big|best|smartest) model)\b", re.I)

# Defaults until Wright has its own history: tool steps a job of this kind usually takes, and seconds
# per step for each place it can run (online fast model, online big model, this Mac).
DEFAULT_STEPS = {("quick", "small"): 2, ("quick", "medium"): 4, ("quick", "large"): 10,
                 ("build", "small"): 12, ("build", "medium"): 30, ("build", "large"): 60}
SECONDS_PER_STEP = {"online": 6.0, "online-big": 15.0}
LOCAL_SECONDS_PER_STEP_UNKNOWN = 75.0      # an older Mac with no measurement yet
TYPICAL_NEW_PROMPT_TOKENS = 2500           # what one step usually asks the model to read
TYPICAL_REPLY_TOKENS = 200


@dataclass
class Dispatch:
    mode: str
    size: str
    private: bool
    route: str
    where: str                    # "online" | "online-big" | "this-mac"
    reasons: list[str] = field(default_factory=list)
    estimate: dict[str, Any] = field(default_factory=dict)
    warning: str = ""             # e.g. private but the local model isn't installed

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def classify(text: str, *, images: int = 0, has_cloud: bool = True, has_local: bool = True,
             local_only: bool = False) -> Dispatch:
    full = text or ""
    head = full.split("\n\n---", 1)[0]               # dropped files are appended after a "---" line
    ask = REQUEST_PREFIX.sub("", head, count=1).strip()
    reasons: list[str] = []

    explain = bool(EXPLAIN.match(ask)) and not REQUEST_PREFIX.match(head)
    if re.match(r"^\s*how (do|can|could|should) (i|we)\b", ask, re.I):
        explain = True                                # "how do I build X" asks for an explanation
    action = bool(ACTION.search(ask))
    if images and not head.strip():
        mode = "build"
    elif explain:
        mode = "quick"
    elif action:
        mode = "build"
    else:
        mode = "quick" if ask.rstrip().endswith("?") else "build"
    reasons.append("a question — I'll look it up and answer; nothing on your Mac changes" if mode == "quick"
                   else "makes or changes something — I'll work on a copy first, check it, then hand it over "
                        "with undo")

    n = _words(head)
    if mode == "quick":
        size = "large" if RESEARCH.search(ask) else "small" if n <= 25 else "medium"
    else:
        size = "large" if (BIG.search(ask) or n > 60) else "small" if (SMALL.search(ask) and n <= 20) else "medium"

    private = bool(PRIVATE.search(full))
    warning = ""
    if local_only:
        route, where = "local", "this-mac"
        reasons.append("private mode is on — everything stays on this Mac")
    elif private:
        hit = PRIVATE.search(full).group(0)  # type: ignore[union-attr]
        reasons.append(f"looks private (mentions “{hit}”) — stays on this Mac")
        route, where = "local", "this-mac"
        if not has_local:
            warning = ("This looks private, so it should stay on this Mac, but the free local model isn't "
                       "installed yet.")
    elif not has_cloud:
        route, where = "local", "this-mac"
        reasons.append("no online key yet — runs on this Mac (slower)")
    elif DEEP.search(ask):
        route, where = "deep", "online-big"
        reasons.append("you asked for careful thinking — uses the bigger online model")
    elif mode == "quick" and size != "large":
        route, where = "fast", "online"
    else:
        route, where = "auto", "online"
    return Dispatch(mode=mode, size=size, private=private, route=route, where=where, reasons=reasons,
                    warning=warning)


def local_seconds_per_step(gen_tps: float = 0.0, prefill_tps: float = 0.0) -> float:
    if gen_tps > 0 and prefill_tps > 0:
        return TYPICAL_NEW_PROMPT_TOKENS / prefill_tps + TYPICAL_REPLY_TOKENS / gen_tps
    return LOCAL_SECONDS_PER_STEP_UNKNOWN


def human(seconds: float) -> str:
    s = max(1, int(round(seconds)))
    if s < 60:
        return f"{max(5, int(round(s / 5.0)) * 5)} s"
    m = s / 60
    if m < 90:
        return f"{int(round(m))} min"
    return f"{m / 60:.1f} h".replace(".0 h", " h")


def estimate(d: Dispatch, history: Iterable[dict] = (), *, gen_tps: float = 0.0,
             prefill_tps: float = 0.0) -> dict[str, Any]:
    """Seconds as a low–high range, plus where the number came from. Uses finished jobs of the same
    kind when there are any, otherwise the defaults above (local speed: as measured on this Mac)."""
    per_step = (SECONDS_PER_STEP.get(d.where) if d.where != "this-mac"
                else local_seconds_per_step(gen_tps, prefill_tps))
    steps = DEFAULT_STEPS[(d.mode, d.size)]
    same = [h for h in history if h.get("mode") == d.mode and h.get("where") == d.where
            and h.get("outcome") == "done" and h.get("seconds")]
    exact = [h for h in same if h.get("size") == d.size]
    if len(exact) >= 2:
        mid = statistics.median(h["seconds"] for h in exact)
        steps = int(statistics.median(h.get("steps") or steps for h in exact))
        basis = f"from {len(exact)} similar jobs"
    elif same:
        rate = statistics.median(h["seconds"] / max(1, h.get("steps") or 1) for h in same)
        mid = rate * steps
        basis = f"from {len(same)} earlier job{'s' if len(same) > 1 else ''} and the usual size"
    else:
        mid = per_step * steps
        basis = "first guess (it learns from each job)"
    low, high = mid * 0.6, mid * 1.8
    lo, hi = human(low), human(high)
    if high - low < 45 or lo == hi:
        text = f"about {human(mid)}"
    elif lo.split()[-1] == hi.split()[-1]:
        text = f"{lo.split()[0]}–{hi}"                   # "4–11 min"
    else:
        text = f"{lo}–{hi}"                               # "35 s–2 min"
    where_text = {"online": "online (Gemini, fast)", "online-big": "online (bigger Gemini model)",
                  "this-mac": "on this Mac (private, slower)"}[d.where]
    return {"seconds": round(mid), "low": round(low), "high": round(high), "steps": steps, "text": text,
            "where_text": where_text, "basis": basis}


def plan_request(text: str, *, images: int = 0, has_cloud: bool = True, has_local: bool = True,
                 local_only: bool = False, history: Iterable[dict] = (), gen_tps: float = 0.0,
                 prefill_tps: float = 0.0) -> Dispatch:
    d = classify(text, images=images, has_cloud=has_cloud, has_local=has_local, local_only=local_only)
    d.estimate = estimate(d, history, gen_tps=gen_tps, prefill_tps=prefill_tps)
    return d
