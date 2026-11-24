"""Anti-regression gate on self-modification.

The agent may propose changes to its own skills, prompt, role contract or config. A proposal is a
software change: schema validation, its visible checks, a hidden evaluation the proposer never
sees (outside the model's data scope, hash-pinned), and the same fail-closed regression rule as
external work (any self-baseline dimension that drops rejects it). Accepted changes are commits in
a versioned store; rollback is one command (`wright self rollback <rev>`).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .config import PACKAGE_DIR, Paths
from .contract import RoleContract
from .skills import Skill, SkillLibrary

KINDS = ("skill", "prompt", "contract", "config")
SEED_HIDDEN = PACKAGE_DIR / "seed" / "hidden_eval"
CEILINGS = {"max_steps_per_task": 500, "max_steps_per_subtask": 60, "mutations_per_gate": 10,
            "fix_attempts": 5, "wall_clock_seconds": 6 * 3600, "max_tokens_per_task": 10_000_000}


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={"GIT_AUTHOR_NAME": "wright", "GIT_AUTHOR_EMAIL": "wright@localhost",
                            "GIT_COMMITTER_NAME": "wright", "GIT_COMMITTER_EMAIL": "wright@localhost",
                            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


class SelfStore:
    """Versioned store of everything the agent may change about itself."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.root = paths.self_repo

    def init(self) -> None:
        if (self.root / ".git").exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "prompts").mkdir(exist_ok=True)
        shutil.copy(PACKAGE_DIR / "prompts" / "system.md", self.root / "prompts" / "system.md")
        (self.root / "contract.json").write_text(json.dumps(RoleContract().to_dict(), indent=1))
        (self.root / "config.json").write_text(json.dumps({"harness": {}}, indent=1))
        (self.root / "skills").mkdir(exist_ok=True)
        _git(self.root, "init", "-q")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "initial self state")

    def head(self) -> str:
        return _git(self.root, "rev-parse", "--short", "HEAD")

    def log(self, n: int = 20) -> list[str]:
        return _git(self.root, "log", f"-{n}", "--format=%h %s").splitlines()

    def path_for(self, kind: str, name: str = "") -> Path:
        return {"prompt": self.root / "prompts" / "system.md", "contract": self.root / "contract.json",
                "config": self.root / "config.json",
                "skill": self.root / "skills" / f"{name}.json"}[kind]

    def read(self, kind: str, name: str = "") -> str | None:
        p = self.path_for(kind, name)
        return p.read_text() if p.exists() else None

    def commit(self, kind: str, name: str, content: str, message: str) -> str:
        p = self.path_for(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        return self.head()

    def rollback(self, rev: str) -> str:
        """One command: restore the whole self state as of `rev`, recorded as a new commit."""
        _git(self.root, "rev-parse", "--verify", rev)
        _git(self.root, "rm", "-r", "-q", "--ignore-unmatch", ".")       # exact tree of rev, incl. deletions
        _git(self.root, "checkout", rev, "--", ".")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "--allow-empty", "-m", f"rollback to {rev}")
        return self.head()


# ====================================================================== hidden evaluation
class HiddenEval:
    """Lives in WRIGHT_HOME/hidden_eval (outside the model's data scope), seeded from the package and
    hash-pinned so tampering is detected. Results come back as counts, never as the cases."""

    def __init__(self, paths: Paths):
        self.dir = paths.hidden_eval
        self.pins = self.dir / "pins.json"

    def install(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.pins.exists() and SEED_HIDDEN.exists():
            pins = {}
            for f in sorted(SEED_HIDDEN.glob("*.json")):
                shutil.copy(f, self.dir / f.name)
                pins[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
            self.pins.write_text(json.dumps(pins, indent=1))

    def _load(self, name: str) -> dict:
        self.install()
        pins = json.loads(self.pins.read_text())
        f = self.dir / name
        if name not in pins or not f.exists():
            return {}
        if hashlib.sha256(f.read_bytes()).hexdigest() != pins[name]:
            raise RuntimeError(f"hidden eval {name} failed its hash pin (tampered)")
        return json.loads(f.read_text())

    def skill_cases(self, skill: str) -> list[dict]:
        return self._load("skills.json").get(skill, [])

    def prompt_rules(self) -> dict:
        return self._load("prompt.json")

    def contract_rules(self) -> dict:
        return self._load("contract.json")


# ====================================================================== the gate
Executor = Callable[[str, dict, Path], tuple[bool, str]]


class SelfModGate:
    def __init__(self, paths: Paths, library: SkillLibrary, execute: Executor):
        self.paths = paths
        self.store = SelfStore(paths)
        self.hidden = HiddenEval(paths)
        self.lib = library
        self.execute = execute
        self.store.init()
        self.hidden.install()

    # ---------------------------------------------------------------- per-kind evaluations
    def _skill_scores(self, skill: Skill) -> dict[str, bool]:
        scores: dict[str, bool] = {}
        ok, _ = self.lib.test(skill, self.execute)
        scores[f"skill:{skill.name}:visible"] = ok
        for i, case in enumerate(self.hidden.skill_cases(skill.name)):
            probe = Skill(**{**skill.__dict__, "test": case})
            ok, _ = self.lib.test(probe, self.execute)
            scores[f"skill:{skill.name}:hidden{i}"] = ok
        return scores

    def _prompt_scores(self, text: str) -> dict[str, bool]:
        r = self.hidden.prompt_rules()
        s = {"prompt:visible:nonempty": 50 < len(text) < 40_000,
             "prompt:visible:lock": "procedural lock" in text.lower()}
        for i, rx in enumerate(r.get("required", [])):
            s[f"prompt:hidden:req{i}"] = re.search(rx, text, re.I | re.S) is not None
        for i, rx in enumerate(r.get("forbidden", [])):
            s[f"prompt:hidden:forb{i}"] = re.search(rx, text, re.I | re.S) is None
        return s

    def _contract_scores(self, d: dict) -> dict[str, bool]:
        s: dict[str, bool] = {}
        try:
            c = RoleContract.from_dict(d)
            s["contract:visible:schema"] = True
        except Exception:
            return {"contract:visible:schema": False}
        r = self.hidden.contract_rules()
        for i, tool in enumerate(r.get("must_forbid", [])):
            s[f"contract:hidden:forbid{i}"] = RoleContract._match(tool, c.forbidden_tools) or \
                not RoleContract._match(tool, c.allowed_tools)
        s["contract:hidden:write_scope"] = not any(x in ("*", "/", "~", "$HOME") for x in c.write_scope)
        t = c.param_bounds.get("*", {}).get("timeout", {}).get("max", 10 ** 9)
        s["contract:hidden:timeout"] = t <= r.get("max_timeout", 600)
        s["contract:hidden:drift"] = c.drift_threshold <= r.get("max_drift_threshold", 0.8)
        return s

    def _config_scores(self, d: dict) -> dict[str, bool]:
        h = d.get("harness", {})
        s = {"config:visible:object": isinstance(d, dict)}
        for k, ceiling in CEILINGS.items():
            s[f"config:hidden:{k}"] = not (k in h and (not isinstance(h[k], (int, float)) or h[k] > ceiling or h[k] <= 0))
        return s

    def _self_baseline(self) -> dict[str, bool]:
        scores: dict[str, bool] = {}
        for sk in self.lib.all():
            if sk.status == "active":
                scores.update(self._skill_scores(sk))
        cur_prompt = self.store.read("prompt") or ""
        scores.update(self._prompt_scores(cur_prompt))
        scores.update(self._contract_scores(json.loads(self.store.read("contract") or "{}")))
        scores.update(self._config_scores(json.loads(self.store.read("config") or "{}")))
        return scores

    # ---------------------------------------------------------------- propose
    def propose(self, kind: str, name: str, content: Any, reason: str) -> dict:
        if kind not in KINDS:
            return {"accepted": False, "stage": "schema", "reason": f"kind must be one of {KINDS}"}
        before = self._self_baseline()
        try:
            if kind == "skill":
                data = content if isinstance(content, dict) else json.loads(content)
                old = self.lib.get(name)
                data = {**(old.__dict__ if old else {}), **data, "name": name, "status": "active"}
                cand = Skill(**{k: v for k, v in data.items() if k in Skill.__dataclass_fields__})
                err = cand.validate()
                if err:
                    return {"accepted": False, "stage": "schema", "reason": err}
                after_part = self._skill_scores(cand)
                text = json.dumps({k: v for k, v in cand.__dict__.items() if k not in ("history", "version")},
                                  indent=1)
            elif kind == "prompt":
                text = content if isinstance(content, str) else str(content)
                after_part = self._prompt_scores(text)
            elif kind == "contract":
                data = content if isinstance(content, dict) else json.loads(content)
                after_part = self._contract_scores(data)
                text = json.dumps(data, indent=1)
            else:
                data = content if isinstance(content, dict) else json.loads(content)
                after_part = self._config_scores(data)
                text = json.dumps(data, indent=1)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return {"accepted": False, "stage": "schema", "reason": f"unparseable: {e}"}
        visible = {k: v for k, v in after_part.items() if ":visible" in k}
        if not all(visible.values()):
            return {"accepted": False, "stage": "visible",
                    "reason": "visible checks failed: " + ", ".join(k for k, v in visible.items() if not v)}
        hidden = {k: v for k, v in after_part.items() if ":hidden" in k}
        failed_hidden = [k for k, v in hidden.items() if not v]
        if failed_hidden:
            # The proposer learns only how many held-out cases failed — never which, never their content.
            return {"accepted": False, "stage": "hidden_eval",
                    "reason": f"rejected by the hidden evaluation ({len(failed_hidden)} of {len(hidden)} "
                              "held-out checks failed). Visible checks passed; the change overfits or "
                              "weakens a safeguard."}
        after = {**before, **after_part}
        regressions = [k for k, v in before.items() if v and not after.get(k, False)]
        if regressions:
            return {"accepted": False, "stage": "regression",
                    "reason": f"self-baseline regressed on {len(regressions)} dimension(s)"}
        if kind == "skill":
            saved = self.lib.save(cand, f"self-modification accepted: {reason[:120]}")
            text = json.dumps({**json.loads(text), "library_version": saved.version}, indent=1)
        rev = self.store.commit(kind, name, text, f"{kind} {name}: {reason[:120]}")
        return {"accepted": True, "stage": "committed", "rev": rev}

    def rollback(self, rev: str) -> str:
        """One command: restore prompt, contract, config and skills to their state at `rev`."""
        head = self.store.rollback(rev)
        for sk in self.lib.all():
            stored = self.store.read("skill", sk.name)
            if stored:
                target = json.loads(stored).get("library_version")
            else:
                target = max((h["version"] for h in sk.history
                              if not str(h.get("note", "")).startswith("self-modification")), default=None)
            if target and target != sk.version:
                self.lib.rollback(sk.name, target)
        return head


def selfmod_tools(gate: SelfModGate) -> list:
    from .tools import Tool, ToolResult, obj

    def propose(a: dict) -> ToolResult:
        r = gate.propose(a["kind"], a.get("name", ""), a["content"], a.get("reason", ""))
        return ToolResult(json.dumps(r), is_error=not r["accepted"])

    return [Tool("self__propose", "Propose a change to your own skills, prompt, role contract or config. "
                 "It must pass schema, visible checks, a hidden evaluation and the self-baseline; accepted "
                 "changes are versioned commits with one-command rollback.",
                 obj({"kind": {"type": "string"}, "name": {"type": "string"},
                      "content": {"type": ["string", "object"]}, "reason": {"type": "string"}},
                     ["kind", "content", "reason"]), propose, read_only=True, mutating=False)]
