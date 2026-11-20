# Release Notes

## v0.1.0 - 2026-11-19
- First release for Cortensor Hackathon #5, pinned to `openminion==0.1.23`.
- Investigator agent with three tools (reproduce in a clean temp home, capture environment, search existing issues), writer agent with a
  pydantic `BugReport` output, and a deterministic self-check that rejects any report claiming more than the tools showed.
- Three real runs on the Gemini free tier (`examples/reports/`), each matched to an existing issue instead of a duplicate.
- Findings F1-F4 with standalone repros (`findings/`); F3 filed upstream as openminion#811 (assigned to us) with fix PR openminion#812.
