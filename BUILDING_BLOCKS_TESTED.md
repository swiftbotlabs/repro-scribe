# Building Blocks Tested

What Repro Scribe uses from the OpenMinion 0.1 Python SDK, how we exercised it, and what we saw. Version 0.1.23 (a55a98a).

| Building block | How Repro Scribe uses it | Result |
|---|---|---|
| `Agent(instructions=...)` | investigator persona + step list | works; **long instructions are cut** at the identity budget without telling the caller (F3, #811, fix PR #812); ours are kept to 423 characters |
| `@tool` (min_scope READ_ONLY) | `capture_environment`, `reproduce`, `find_existing_issues` | works; the model called all three in order in every run we logged |
| Tool-calling loop | 3-4 tool calls per run, then a summary | works on Gemini flash-lite; the entry `task_update` sidecar needs `objective` (F2) |
| `output_type` (pydantic) | `BugReport` from a tool-less writer Agent | works once the schema is in the message; not sent by the SDK itself (F4) |
| Sessions (`session_id`) | one session per investigation, `-writer` for the second pass | works; separate sessions keep the writer's context small |
| Providers | `gemini` preset, Groq, custom OpenAI-compatible (for our logging proxy and capture endpoint) | Gemini OK; Groq free tier 413 (F1); custom endpoint setup needs `OPENAI_COMPATIBLE_API_KEY` even for a local endpoint |
| `openminion doctor --check-turn --json` | first-run smoke test for each provider | good signal; plain-text error under `--json` when config is missing |
| `ProviderError`, `AgentOutputValidationError`, `TurnTimeoutError` | error paths in the CLI | the first two are catchable as documented; `TurnTimeoutError` surfaced as an uncaught exception from `agent.run` under provider load |
| Permissions | not delegated to the model: our `reproduce` tool enforces its own allowlist (openminion/python only, no shell, temp dir, empty OpenMinion home, 60 s) | deterministic and testable offline (tests/test_offline.py) |

| `forced_tools` | investigator must call `reproduce` | works: the first recorded call is always `reproduce` (pipeline test) |
| `Agent.run_stream(on_delta)` | live progress lines (`Analyzing request...`, `tool_started reproduce`) | works; 29 events for a one-tool turn |
| `subagent()` | 1-3 duplicate hunters, read-only tools, one Portal model each | works; lineage and `tool_allowlist` set as documented |
| `Handoff` | investigator → `needs_info` peer when the failure does not reproduce | works only with an `approval_callback` (F8) |
| `approval_callback` + action policy | publisher's `save_draft` needs a human yes; deny really skips the tool | works (probes `approval_deny`, `approval_allow`) |
| `MemoryBundle` | drafts remembered across runs: repeat command → no model call; same failure signature → flagged | works |
| Cortensor Portal (`cortensor-portal` preset) | gemma4 + oss-20b behind our gateway; consensus uses one alias per hunter | works with retries; 502s on model-output errors (F10); slow (F5); text pseudo-calls (F7) |
| Request accounting | gateway ledger vs `stats["llm_calls"]` | agree (8 for a one-tool turn) |

Probe matrix: `repro-scribe probe --backend loopback` = 13/13 PASS offline (~10 s). Not used: skills, scheduling, daemon/HTTP surfaces.
