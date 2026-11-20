# Release Notes

## v0.2.0 - 2026-11-20
- Cortensor Portal backend through a local gateway (`gateway.py`): alias routing, retries/backoff, circuit breaker, JSONL ledger with
  Portal request ids; offline loopback model so the full pipeline runs with no key.
- Duplicate hunters as `subagent()`s with read-only tools, one per model, combined by consensus (`--consensus 2`).
- `forced_tools`, `run_stream` progress, `Handoff` to a needs-info peer, approval-gated publisher (`--approve`), MemoryBundle dedup.
- `repro-scribe probe`: 13 building-block checks (13/13 offline). Tests 7 → 20, all offline.
- Findings F5-F10 (Portal 502s with request ids, request count per turn, Handoff approval, config override, text pseudo-call, reply prefix).

## v0.1.0 - 2026-11-19
- First release for Cortensor Hackathon #5, pinned to `openminion==0.1.23`.
- Investigator agent with three tools (reproduce in a clean temp home, capture environment, search existing issues), writer agent with a
  pydantic `BugReport` output, and a deterministic self-check that rejects any report claiming more than the tools showed.
- Three real runs on the Gemini free tier (`examples/reports/`), each matched to an existing issue instead of a duplicate.
- Findings F1-F4 with standalone repros (`findings/`); F3 filed upstream as openminion#811 (assigned to us) with fix PR openminion#812.
