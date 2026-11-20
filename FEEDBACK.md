# Feedback for OpenMinion and Cortensor

From building and running Repro Scribe on `openminion==0.1.23` with Gemini (19 Nov) and Cortensor Portal (20 Nov 2026).
Each item has a repro in [WHAT_WE_LEARNED.md](WHAT_WE_LEARNED.md) or `findings/`; Portal items carry gateway request ids.

## Bugs (with repros)
| # | Area | Finding | Status |
|---|---|---|---|
| F3 | OpenMinion identity | `Agent(instructions=...)` cut after ~865 chars, silently (24 of 40 rules lost in our test) | issue #811 assigned to us; fix PR #812 in review |
| F2 | task context | a model that omits `objective` in `task_update` ends the run with a clarify question | #719 family |
| F4 | output_type | schema validated but never sent to the model; with tools the final text is a closure summary | #720 family |
| F6 | config | connection `base_url` override ignored by `Agent.run`; `providers.openai.base_url` used | new |
| F7 | Portal + hybrid tool calls | oss-20b reply `Respond(answer=Paris, freshness=Known)` returned as text | new |
| F10 | Portal | 502 for model-output validation errors ("function not advertised", "tool arguments do not match") | new, request ids listed |

## Rough edges
- F5: 8 model requests for a one-tool turn, 39 for one Repro Scribe run; minutes per turn at Portal latency. A lean-turn switch would help.
- F8: `Handoff` needs an `approval_callback` (classed as a local write); without one the reply is a policy prompt.
- F9: `AgentRunResult.text` starts with `"<agent id>: "`.
- F1: a minimal turn sends ~10.8k tokens; Groq free tier answers 413.
- `openminion doctor --json` prints plain text when the config is missing (covered by #573).
- The `cortensor-portal` preset says "requests do not automatically retry": Repro Scribe's gateway (retry + reroute + circuit
  breaker + request-id ledger) is a reusable answer and could be an SDK option.

## What worked well
- `subagent()` lineage and tool allowlists are explicit and testable (`subagent_context.tool_allowlist`).
- `approval_callback` is simple and reliable: deny really stops the tool (probe `approval_deny`).
- `run_stream(on_delta=...)` gives useful phase and tool events with no extra setup.
- `MemoryBundle` export/save/load round-trips cleanly for app-level memory.
