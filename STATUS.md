# Project Status

- **Project:** `repro-scribe` (`v0.1.0`), MIT
- **OpenMinion SDK:** `0.1.23` (pinned)
- **Status:** active. 7 offline tests pass (`pytest -q`, no model, no network). Real runs recorded on the Gemini free tier.

## Implemented
- Reproduces a failing command in a fresh temp directory with an empty OpenMinion home (allow-listed programs only, no shell, 60 s timeout).
- Captures versions and platform; searches the target repo's issues for duplicates (public GitHub API, no auth).
- Writes a structured `BugReport` and cross-checks it against the recorded tool calls; overclaiming reports are saved as `*.REJECTED.md`.

## Upstream
- openminion#811 (instructions truncated after ~865 characters): filed from finding F3, acknowledged and assigned to us.
- openminion#812 (bounded fix, refs #811): in review.

## Known issues / next
- Free-tier limits: Groq answers 413 on the default tool set (finding F1); Gemini flash-lite is the tested path.
- Cortensor Portal run pending access.
