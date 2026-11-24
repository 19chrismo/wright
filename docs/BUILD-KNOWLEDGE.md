# Knowledge pack for building Wright

This file applies the brief's expertise protocol to Wright's own design
decisions. It records which sources were consulted, how strong each one is,
and what was done where no source could be reached. The build environment
could not reach ai.google.dev, docs.ollama.com, modelcontextprotocol.io or
home-assistant.io. Those gaps are recorded below instead of being filled from
memory.

| Decision | Tag | Grounding | How a wrong call is made cheap |
|---|---|---|---|
| Use the official MCP Python SDK (`mcp` 2.3.0, protocol `2026-07-28`) for the client and every built-in server: `Client(StdioServerParameters…)`, `MCPServer.tool(annotations=ToolAnnotations…)`, streamable HTTP by URL | [SOURCED] | Tier 3: the installed SDK's own source (`mcp/client/client.py`, `mcp/server/mcpserver/server.py`, `mcp_types`) | `tests/test_step10_mcp.py` drives stdio and streamable HTTP servers end to end. |
| Pool design: quota groups, cooldown on 429, LRU or sequential rotation, per-model daily caps with a reset time | [SOURCED] | Tier 4: the GemiNitro README (github.com/jmvbambico/geminitro) and the GeminiHydra README (github.com/LikithMeruvu/Hydra-gemini), as read through the fetch tool | Behaviour is covered by `tests/test_step02_pool.py`. |
| Gemini limits are enforced per Google Cloud project, not per key | [INFERRED] | The original brief and the GeminiHydra README ("per Google Cloud Project") agree. Google's rate-limit page was unreachable. | The pool doesn't depend on this: it reacts to whatever 429s it gets. |
| Gemini's OpenAI-compatible base URL is `https://generativelanguage.googleapis.com/v1beta/openai` with Bearer auth | [INFERRED] | From memory of Google's compatibility docs, which were unreachable here. GemiNitro documents an OpenAI-compatible front for Gemini. | `wright doctor` probes `/models` with each key, reports the HTTP status and fixes the route from the live model list. |
| A 429 body carries `RetryInfo.retryDelay`, and daily quotas mention "PerDay" | [INFERRED] | From memory of Google API error details | Unknown shapes fall back to a 60 s cooldown (`DEFAULT_COOLDOWN`). |
| Ollama serves an OpenAI-compatible API at `http://127.0.0.1:11434/v1` and lists models at `/api/tags` | [INFERRED] | Ollama's docs were unreachable, and the GitHub path returned 404. | `wright doctor` checks `/api/tags`, and the base URL is configurable per lane. |
| Google's API terms forbid trying to get around usage limits | [INFERRED] | From memory of the Google APIs Terms of Service ("API Limitations") | Advisory only. The code doesn't block any key. It labels each key with its owner and enforces each owner's daily share, so nobody's quota is used without their agreement. Verify the terms before relying on this. |
| Household members' keys from their own accounts are their own per-project quota | [INFERRED] | Follows from "limits are per project"; whether pooling them is acceptable to Google is [UNKNOWN] | Owner labels and daily shares (`tests/test_step02_pool.py::test_household_keys_each_keep_their_owner_and_daily_share`). |
| macOS `sandbox-exec` with an SBPL profile denying `file-write*` on subpaths still works, although Apple has deprecated it | [INFERRED] | From memory | `test_macos_sandbox_denies_writes_to_protected_paths` runs on the Mac. Hash-chain tamper detection plus repair is the backstop. |
| `caffeinate -i -w <pid>` holds an idle-sleep assertion until that process exits | [INFERRED] | From memory of `man caffeinate` | Verify with `man caffeinate` on the Mac. `test_macos_sleep_assertion_command` pins the argv. |
| Python's `.pyc` cache is invalidated by source mtime and size, so a same-size edit within one second can run stale bytecode | [INFERRED], then observed | Reproduced by `tests/test_step12_stop_hooks.py` before the fix | Checks and shells set `PYTHONDONTWRITEBYTECODE=1`. |
| Home Assistant exposes MCP over HTTP at `/api/mcp`, authenticated with a long-lived token | [UNKNOWN] | Home Assistant's docs were unreachable. | The README marks the path as an assumption. Any URL plus a keyref header works, and the hub reports failures at attach time. |
| Gemini model names `gemini-2.5-pro` and `gemini-2.5-flash` | [UNKNOWN] for the current date | Defaults only | `wright doctor` replaces the route with the newest pro and flash models the key can see. |
