# Project Status

- **Project:** `repro-scribe` (`v0.2.0`), MIT
- **OpenMinion SDK:** `0.1.23` (pinned)
- **Status:** active. 20 offline tests pass (`pytest -q`, no key, no network). Probe 13/13 offline. Real runs on Gemini (19 Nov) and Cortensor Portal (20 Nov).

## Implemented
- Reproduces a failing command in a fresh temp directory with an empty OpenMinion home (allow-listed programs only, no shell, 60 s timeout).
- Captures versions and platform; searches the target repo's issues for duplicates (public GitHub API, no auth).
- Writes a structured `BugReport` and cross-checks it against the recorded tool calls; overclaiming reports are saved as `*.REJECTED.md`.

- Portal gateway with routing, retries, circuit breaker and a request-id ledger; offline loopback model.
- Duplicate-hunter sub-agents with 2-model consensus; Handoff to a needs-info peer; approval-gated publisher; MemoryBundle dedup.
- `repro-scribe probe`: 13 building-block checks.

## Upstream
- openminion#811 (instructions truncated after ~865 characters): filed from finding F3, acknowledged and assigned to us.
- openminion#812 (bounded fix, refs #811): in review.

## Known issues / next
- Free-tier limits: Groq answers 413 on the default tool set (finding F1); Gemini flash-lite is the tested path.
- Portal: slow (minutes per turn) and 502s on model-output errors (F10); the gateway retries and reroutes.
