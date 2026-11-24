# Wright data formats

Every format below is plain JSON on disk, and each has a JSON Schema in
`schemas/`. `tests/test_formats.py` validates real artifacts against those
schemas.

## Check (`schemas/check.schema.json`)

Acceptance criteria, baseline dimensions and skill tests all use the same
deterministic check format. Running a check never involves a model.

| kind | required fields | passes when |
|---|---|---|
| `command` | `run` | the exit code equals `expect_exit` (default 0), and the output matches `expect_regex` if one is given |
| `file_exists` | `path` | the path exists |
| `file_contains` | `path`, and `text` or `regex` | the file contains the text or matches the regex |
| `http` | `url` | the status equals `expect_status` (default 200), with optional `expect_regex` |
| `tcp` | `host`, `port` | a connection opens |
| `metric` | `run` | the command prints a number. The baseline compares it, using a direction and a tolerance. |
| `ledger_ran` | `pattern` | the harness's own execution ledger shows a matching command that really ran and exited as required |

Example:

```json
{"name": "tests pass", "kind": "command", "run": "python3 -m pytest -q"}
```

## Knowledge pack (`schemas/knowledge-pack.schema.json`)

```json
{"id": "kp-20261124-…", "domain": "ssh", "task_type": "harden", "version": 1,
 "system_versions": {"openssh": {"version": "OpenSSH_9.6p1", "evidence": "ssh -V"}},
 "sources": [{"id": "S1c41ab1d", "locator": "https://…/sshd_config.5", "tier": 1, "reachable": true,
              "sha256": "…", "excerpt": "PasswordAuthentication: Specifies whether…",
              "version_applies": "9.6", "component": "openssh"}],
 "decisions": [{"id": "D1", "statement": "Set PasswordAuthentication no", "tag": "SOURCED",
                "confidence": 0.9, "sources": ["S1c41ab1d"], "supports": ["password auth off"],
                "mode": "normal", "grade": "held"}]}
```

- **Source tiers:**
  1. vendor documentation;
  2. a standard or specification;
  3. primary source code;
  4. a maintainer-written guide;
  5. a community source.
- **`excerpt`:** must occur verbatim in the content the harness fetched.
- **`SOURCED`:** needs a reachable tier 1–4 source, or two independent tier-5
  hosts with different content. Its `version_applies` must match the version
  read from the system.
- **`INFERRED`:** needs a `chain` and at least one source.
- **`UNKNOWN`:** needs a `mitigation`, and its mode becomes
  `sandbox-test-first`.
- **`grade`:** set at the end of the task from the first verification after
  the decision was made.

## Regression baseline manifest (`schemas/baseline.schema.json`)

Each dimension has:

- `check`: a check, as above;
- `direction`: `pass`, `higher` or `lower`;
- tolerances: `tolerance` (relative) and `abs_tolerance`.

`frozen` holds the known-good measurements. A change is rejected if any
dimension is `worse`, even when others improved. A dimension that looks worse
is re-run `flaky_runs` times and decided by majority, and the votes are
recorded in `flakes`. Fixed cases are added with `origin: "fixed-case"`, and
`version` goes up every time the manifest changes.

## Role contract (`schemas/role-contract.schema.json`)

| field | meaning |
|---|---|
| `allowed_tools`, `forbidden_tools` | Exact tool names, or anchored globs such as `fs__*`. Forbidden wins over allowed. |
| `param_bounds` | `{tool or "*": {param: {max, min, max_len, enum, pattern}}}` |
| `read_scope`, `write_scope` | Path roots. `{workdir}` and `{sandbox}` are filled in per task. The audit log, secrets, hidden eval and self store are always protected, whatever the scope says. |
| `hosts` | Network hosts that open-world tools may reach (`*` = any). |
| `expected_tools`, `drift_window`, `drift_threshold` | The soft drift signal: off-profile calls are held until justified. |

The default contract lives in `~/.wright/self/contract.json`. It can only be
changed through the self-modification gate.

## Plan graph (`schemas/plan-graph.schema.json`)

These are the states of the procedural lock (default:
`wright/plans/default.json`). Each state lists:

- `tools`: exact names, `<read>` (all read-only tools), or `<all>` (all
  non-core tools);
- `next`: the states it may move to;
- `exit_guard`: one guard for the state, or one per next state.

| guard | holds when |
|---|---|
| `intent_recorded` | an intent paragraph and at least one machine-checkable criterion are recorded |
| `grounded` | the knowledge pack has at least one tagged decision |
| `baseline_frozen` | the baseline is frozen |
| `plan_done` | every subtask is done |
| `hooks_green` | the latest verify was green and nothing changed after it |
| `hooks_green_nothing_to_promote` | verify is green and there are no changes to promote |
| `promoted` | the promotion succeeded and its probes passed |
| `promotion_rolled_back` | the promotion was rolled back |
| `completed` | `task__complete` accepted the report |

## Episodic log entry (`schemas/episodic-entry.schema.json`)

```json
{"seq": 3, "ts": 1795500000.12, "prev": "<sha256 of entry 2>", "hash": "<sha256(prev|seq|ts|entry)>",
 "entry": {"type": "task", "task_id": "…", "goal": "…", "domain": "…", "task_type": "…", "intent": "…",
           "sources": [...], "decisions": [...], "actions": [...], "outcome": "done", "result": "…",
           "failures": {"tests pass": 1}, "out_of_scope": [], "flags": [], "dangers": [], "steps": 21}}
```

- **Files:** `log.jsonl` (mode 0444). `.mirror/log.jsonl` is a full copy.
  `.anchor.json` holds the entry count and the head hash.
- **When it's checked:** at session start and before every append.
- **If verification fails:** the log is repaired from the mirror and a
  `{"type": "tamper_detected"}` entry is recorded.
- **Secrets:** redacted before anything is written.

## Calibration table (`schemas/calibration.schema.json`)

- **Buckets:** keyed `domain|tool|task_type`. Each holds `held`, `wrong`, the
  same two counts split by SOURCED and INFERRED, and `confidence_sum`.
- **Measured accuracy:** `(held + 1) / (held + wrong + 2)`.
- **Downgrade rule:** fires when stated confidence minus measured accuracy is
  greater than `gap` (0.2), and the bucket has at least `min_samples` (5)
  graded claims.
- **Rebuilt, never edited:** the table is recalculated from the episodic log
  after every task.

## Skill definition (`schemas/skill.schema.json`)

```json
{"name": "slugify", "description": "slug from text", "version": 2, "status": "active",
 "params": {"text": {"type": "string"}},
 "steps": [{"tool": "shell__run", "args": {"command": "python3 slug.py '{{text}}' > slug.txt"}}],
 "test": {"params": {"text": "Hello World"}, "setup": [],
          "checks": [{"kind": "file_contains", "path": "slug.txt", "regex": "^hello-world\\n?$"}]},
 "history": [{"version": 1, "status": "active", "note": "created"},
             {"version": 2, "status": "active", "note": "self-modification accepted: …"}]}
```

- **Storage:** `~/.wright/memory/procedural/skills/<name>/v<N>.json`, plus a
  `current` pointer.
- **Testing:** the test runs in a scratch folder at session start and before
  each use. A failing test demotes the skill automatically.
- **Changing a skill:** a self-edit must pass its visible test, the hidden
  evaluation and the self-baseline.
