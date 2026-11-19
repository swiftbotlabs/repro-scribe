# Repro Scribe

**An OpenMinion agent that turns a failing command into a bug report maintainers can act on, and that checks itself.**

**Demo (91 s):** [docs/repro-scribe-demo.mp4](docs/repro-scribe-demo.mp4), a replay of the real run's trace.

Give it the command that fails and one sentence about what you expected. It:
1. captures the environment (Python, platform, package versions),
2. reproduces the failure itself, in a fresh temp directory with an empty OpenMinion home (a clean install), with an allowlist
   (only `openminion` or `python`, no shell, 60 s),
3. searches the project's existing issues for duplicates (public GitHub API),
4. writes a structured report (pydantic `BugReport` → Markdown) with the exact output, and
5. **cross-checks the report against what its tools really returned**: if the model claims a failure the command did not show, or
   lists a "duplicate" the search never returned, the report is saved as `*.REJECTED.md` instead.

Why: the Cortensor hackathon wants reproducible bug reports for OpenMinion, and the cheapest bad report is a duplicate. In our three
real runs below, Repro Scribe found that all three fresh-install failures were already covered by open issue #573, so nobody files them twice.

## Run it

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"                     # openminion==0.1.23, pydantic, pytest
pytest -q                                    # 7 offline tests: tools + the self-check, no model, no network

export GEMINI_API_KEY=...                    # free tier is enough
openminion setup --provider gemini --model gemini-flash-lite-latest --no-chat
repro-scribe --command "openminion doctor --json" --note "With --json I expect JSON on stdout even when the config is missing."
# -> reports/<title>.md, plus <title>.trace.json with every tool call and the self-check result
```

Any OpenMinion provider works (`--agent <profile>` to pick one). Exit codes: 0 report accepted, 3 report rejected by the self-check,
2 model output did not fit the schema, 1 provider error, 4 turn timeout.

## Real runs (Gemini free tier, 19 Nov 2026)

| Command (fresh install) | Reproduced | Duplicate found | Time | Report |
|---|---|---|---|---|
| `openminion doctor --json` | yes, exit 2, plain text instead of JSON | #573 | 67 s | [report](examples/reports/cli-openminion-doctor-json-prints-error-to-stderr-instead-of.md) |
| `openminion sessions list` | yes, exit 1 | #573 | 39 s | [report](examples/reports/cli-openminion-sessions-list-fails-on-fresh-install-with-mis.md) |
| `openminion run hello` | yes, exit 2 | #573 | 86 s | [report](examples/reports/cli-config-file-not-found-on-fresh-install.md) |

Each report has a `.trace.json` next to it: tool calls in order, their full results, the self-check verdict and the timing.

## How it is built (OpenMinion SDK)

- **Investigator**: `Agent(instructions=..., tools=[capture_environment, reproduce, find_existing_issues], session_id=...)`; the model
  decides the calls; every call is recorded for the self-check.
- **Writer**: a tool-less `Agent(output_type=BugReport)` that gets the schema and the tool results and returns the report.
- **Self-check** (`cli.check`): deterministic Python comparing the report with the recorded tool results.
- See [BUILDING_BLOCKS_TESTED.md](BUILDING_BLOCKS_TESTED.md) for what each SDK piece did, and
  [WHAT_WE_LEARNED.md](WHAT_WE_LEARNED.md) for four SDK findings with repros (`findings/`).

## Safety

`reproduce` never runs a shell, never runs anything but `openminion`/`python`, works in a throwaway directory and a throwaway
OpenMinion home, and times out after 60 s. The issue search is read-only and unauthenticated. Reports are drafts: filing is a human step.

License: MIT.
