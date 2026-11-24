"""Danger protocol: classify each step, require a pre-mortem with a REHEARSED recovery before a risky
step runs, and ask the user only for danger that cannot be snapshotted:
  money · irreversible hardware writes · destructive actions outside the sandbox with no tested
  restore · sending/publishing to other people.
Everything else is autonomous. A confirmation counts only when it comes from the user through the
UI callback; text inside tool output can never confirm anything.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .harness import ALLOW, Task, Verdict
from .llm import ToolCall
from .tools import Tool, ToolResult, obj

CONFIRM = {"money": "spending money", "hardware": "an irreversible hardware write",
           "outside_destructive": "a destructive action outside the sandbox with no tested restore",
           "disclosure": "sending or publishing to other people"}

RULES: list[tuple[str, str, re.Pattern]] = [
    ("irreducible", "hardware", re.compile(
        r"\b(sysupgrade|mtd\s+(-\w+\s+)*write|flashrom|dfu-util|esptool(\.py)?\s+.*write|fwupdmgr\s+(update|install)|"
        r"diskutil\s+(eraseDisk|eraseVolume|partitionDisk|zeroDisk|secureErase)|mkfs(\.\w+)?\s|"
        r"dd\s+[^|;]*of=/dev/(disk|sd|nvme|mmcblk|rdisk)|nvram\s+-c|firmware\s+flash)", re.I)),
    ("irreducible", "disclosure", re.compile(
        r"(\bgit\s+push\b|\bgh\s+(pr|issue|release|gist)\s+create|\bsendmail\b|\bmail\s+-s\b|"
        r"osascript\s+.*\b(Mail|Messages)\b|hooks\.slack\.com|discord(app)?\.com/api/webhooks|"
        r"api\.(sendgrid|mailgun|twilio)\.|\bnpm\s+publish|\btwine\s+upload|\bpod\s+trunk\s+push)", re.I)),
    ("irreducible", "money", re.compile(
        r"(\bstripe\s+(charges|payment_intents)\s+create|/v1/(charges|payment_intents)\b|"
        r"\b(purchase|checkout|place[_-]?order)\b)", re.I)),
    ("destructive", "outside_destructive", re.compile(
        r"(\brm\s+(-\w*[rR]\w*\s+)+|\bDROP\s+(TABLE|DATABASE|SCHEMA)\b|\bTRUNCATE\s+TABLE\b|"
        r"\bgit\s+(reset\s+--hard|clean\s+-\w*f)|\bkubectl\s+delete\b|\bdocker\s+(volume\s+)?rm\b|"
        r"\bsrm\b|\bshred\b)", re.I)),
    ("risky", "service", re.compile(
        r"(\b(ifconfig|networksetup|nmcli|iptables|ip6tables|nft|pfctl|ufw|scutil)\b|\bip\s+(link|addr|route)\b|"
        r"\buci\s+(set|commit|delete|add)|/etc/init\.d/\S+\s+(restart|stop|reload)|"
        r"\bsystemctl\s+(restart|stop|disable|mask|reload)|\blaunchctl\s+(unload|bootout|kickstart|disable)|"
        r"\bservice\s+\S+\s+(restart|stop)|\b(reboot|shutdown|halt)\b|\bpasswd\b|\busermod\b|"
        r"\b(chmod|chown)\s+-R\b|\bcrontab\s+-r\b|\bbrew\s+(upgrade|uninstall)|\bpip\d?\s+install\s+(-U|--upgrade)|"
        r"\bapt(-get)?\s+(upgrade|remove|purge|dist-upgrade)|\bha\s+(core|supervisor|host)\s+(restart|update|reboot)|"
        r"\bdocker\s+(stop|restart|compose\s+down)|\bssh\s+\S+\s+.*\b(uci|iptables|reboot|passwd|sysupgrade)\b)", re.I)),
]
NAME_RULES: list[tuple[str, str, re.Pattern]] = [
    ("irreducible", "money", re.compile(r"(purchase|checkout|pay(ment)?|order|buy|subscribe)", re.I)),
    ("irreducible", "disclosure", re.compile(r"(send|post|publish|email|message|tweet|share|notify)", re.I)),
    ("irreducible", "hardware", re.compile(r"(flash|firmware|format|erase|wipe)", re.I)),
]


@dataclass
class Risk:
    severity: str          # risky | destructive | irreducible
    category: str
    reason: str


def classify(call: ToolCall, tool: Tool | None, sandbox_root: str | None) -> list[Risk]:
    risks: list[Risk] = []
    text = " ".join(str(call.args.get(k, "")) for k in ("command", "code"))
    for sev, cat, rx in RULES:
        m = rx.search(text)
        if not m:
            continue
        if cat == "outside_destructive":
            # destructive inside the sandbox is reversible (the sandbox is disposable)
            from .sandboxing import path_tokens
            outside = [t for t in path_tokens(text) if t.startswith(("/", "~", "$HOME"))
                       and not (sandbox_root and t.startswith(sandbox_root))
                       and not t.startswith("/dev/null")]
            if not outside and not re.search(r"\b(DROP|TRUNCATE|kubectl|docker)\b", text, re.I):
                continue
        risks.append(Risk(sev, cat, f"matched {m.group(0)!r}"))
    if tool is not None and tool.source.startswith("mcp:") and tool.source not in (
            "mcp:shell", "mcp:fs", "mcp:code", "mcp:web", "mcp:image", "mcp:browser"):
        local = call.name.split("__", 1)[-1]
        for sev, cat, rx in NAME_RULES:
            if rx.search(local):
                risks.append(Risk(sev, cat, f"tool {call.name} looks like {CONFIRM[cat]}"))
        if tool.destructive and not tool.read_only and not risks:
            risks.append(Risk("risky", "external_change", f"{call.name} changes an external system"))
    return risks


Confirmer = Callable[[str, str, dict], bool]   # (category, description, details) -> user said yes


class DangerGate:
    label = "danger protocol"

    def __init__(self, confirm: Confirmer | None = None, events: Any = None):
        self.confirm = confirm or (lambda cat, desc, d: False)
        self.events = events

    def __call__(self, task: Task, call: ToolCall, tool: Tool | None) -> Verdict:
        if tool is None or tool.source == "core":
            return ALLOW
        risks = classify(call, tool, task.state.get("sandbox_root"))
        if not risks:
            return ALLOW
        cover = self._premortem_for(task, call)
        irreducible = [r for r in risks if r.severity == "irreducible"]
        destructive = [r for r in risks if r.severity == "destructive"]
        risky = [r for r in risks if r.severity == "risky"]
        if destructive and not (cover and cover.get("restore_tested")):
            irreducible += [Risk("irreducible", "outside_destructive", destructive[0].reason)]
        elif destructive:
            risky += destructive
        if irreducible:
            r = irreducible[0]
            desc = f"{call.name}: {json.dumps(call.args)[:300]}"
            self._emit(task, "confirm_request", category=r.category, description=desc)
            if self.confirm(r.category, desc, {"tool": call.name, "args": call.args, "reason": r.reason}):
                task.state.setdefault("confirmations", []).append(
                    {"category": r.category, "call": desc, "t": time.time(), "by": "user"})
                return Verdict(True, flag=f"confirmed by user: {CONFIRM[r.category]}")
            task.state.setdefault("dangers", []).append(
                {"call": desc, "category": r.category, "status": "declined_or_unanswered"})
            return Verdict(False, f"this step is {CONFIRM[r.category]}; it cannot be snapshotted and the user "
                                  "did not confirm it. Redesign the step to avoid it, or stop and state "
                                  "exactly what you need from the user.")
        if risky and not cover:
            return Verdict(False, f"risky step ({risky[0].category}: {risky[0].reason}). Record a pre-mortem "
                                  "with danger__premortem (failure modes, a mitigation for each, and a recovery "
                                  "whose rehearse_command proves it works) whose `applies_to` regex matches "
                                  "this call, then retry.")
        if cover:
            cover["used"] = cover.get("used", 0) + 1
        return ALLOW

    def _premortem_for(self, task: Task, call: ToolCall) -> dict | None:
        text = call.name + " " + json.dumps(call.args)
        for pm in task.state.get("premortems", []):
            if pm.get("status") == "rehearsed" and re.search(pm["applies_to"], text):
                return pm
        return None

    def _emit(self, task: Task, kind: str, **kw: Any) -> None:
        if self.events:
            self.events.emit(kind, task=task.id, **kw)


def danger_tools(get_task: Callable[[], Task], run_in_sandbox: Callable[[str], tuple[int, str]],
                 events: Any = None) -> list[Tool]:
    def premortem(a: dict) -> ToolResult:
        t = get_task()
        try:
            re.compile(a["applies_to"])
        except (re.error, KeyError):
            return ToolResult("REJECTED: applies_to must be a regex matching the risky call", is_error=True)
        modes = a.get("failure_modes") or []
        mits = {m.get("mode"): m.get("action") for m in (a.get("mitigations") or [])}
        rec = a.get("recovery") or {}
        if not modes:
            return ToolResult("REJECTED: enumerate failure modes (include the boring ones: lockout, dropped "
                              "session, silent breakage)", is_error=True)
        missing = [m.get("mode") for m in modes if not mits.get(m.get("mode"))]
        if missing:
            return ToolResult(f"REJECTED: no mitigation for failure mode(s) {missing}", is_error=True)
        if not rec.get("procedure") or not rec.get("rehearse_command"):
            return ToolResult("REJECTED: a recovery needs a procedure and a rehearse_command that proves it "
                              "works before it is needed", is_error=True)
        code, out = run_in_sandbox(rec["rehearse_command"])
        oob = a.get("out_of_band_check")
        oob_ok = None
        if oob:
            oob_code, oob_out = run_in_sandbox(oob)
            oob_ok = oob_code == 0
        status = "rehearsed" if code == 0 and oob_ok is not False else "rehearsal_failed"
        pm = {"applies_to": a["applies_to"], "failure_modes": modes, "mitigations": a.get("mitigations"),
              "recovery": rec, "out_of_band_ok": oob_ok, "status": status,
              "restore_tested": code == 0 and bool(rec.get("rollback_command")),
              "rehearsal_output": out[-400:], "t": time.time()}
        t.state.setdefault("premortems", []).append(pm)
        t.state.setdefault("dangers", []).append(
            {"step": a["applies_to"], "failure_modes": [m.get("mode") for m in modes],
             "mitigations": [f"{k}: {v}" for k, v in mits.items()], "recovery": rec.get("procedure"),
             "rehearsal": status, "out_of_band": oob_ok})
        if events:
            events.emit("danger", task=t.id, step=a["applies_to"], status=status,
                        failure_modes=[m.get("mode") for m in modes])
        if status != "rehearsed":
            return ToolResult(f"REHEARSAL FAILED (exit {code}): the recovery does not work yet, so the risky "
                              f"step stays blocked. Output: {out[-300:]}", is_error=True)
        return ToolResult(json.dumps({"premortem": "rehearsed", "applies_to": a["applies_to"]}))

    o = {"type": "object"}
    return [Tool("danger__premortem", "Record a pre-mortem for a risky step: failure_modes [{mode, impact, "
                 "detect}], mitigations [{mode, action}], recovery {procedure, rehearse_command, "
                 "rollback_command}, optional out_of_band_check; applies_to is a regex over the risky call. "
                 "The harness runs the rehearsal; only a passing rehearsal unblocks the step.",
                 obj({"applies_to": {"type": "string"}, "failure_modes": {"type": "array", "items": o},
                      "mitigations": {"type": "array", "items": o}, "recovery": o,
                      "out_of_band_check": {"type": "string"}},
                     ["applies_to", "failure_modes", "mitigations", "recovery"]),
                 premortem, read_only=True, mutating=False)]
