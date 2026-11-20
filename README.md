# Repro Scribe

**An OpenMinion agent that turns a failing command into a bug report maintainers can act on, and that checks itself.**

**Demo (2:32, narrated):** [docs/repro-scribe-demo.mp4](docs/repro-scribe-demo.mp4). The pipeline, a live Cortensor Portal run (real
gateway ledger with Portal request ids), the 13-check probe, and the findings.

Give it the command that fails and one sentence about what you expected. Five OpenMinion agents work in turn:
1. an **investigator** captures the environment and reproduces the failure itself, in a fresh temp directory with an empty
   OpenMinion home (a clean install), with an allowlist (only `openminion` or `python`, no shell, 60 s). Its progress streams live.
   If the command does *not* fail, it hands the case to a **needs-info** peer that writes the questions to ask the reporter;
2. one or two **duplicate hunters** (sub-agents, read-only search tools only, one per Portal model) search issues and pull requests
   and record a verdict; a match is a duplicate only when every hunter says so (`--consensus 2`);
3. a **writer** returns a typed `BugReport` (pydantic);
4. plain Python **cross-checks the report against what the tools really returned** (failure shown or not? exit code? environment
   from the tool, not invented? did a search run? every duplicate a real search hit that the hunters confirmed?) and saves an overclaiming report as `*.REJECTED.md`;
5. a **publisher** whose only tool writes the draft, so OpenMinion's action policy asks a human first; a **MemoryBundle** of drafts
   answers a repeat of the same command without any model call and flags a new command that fails the same way.

Every model request goes through a small **gateway** in front of Cortensor Portal (`repro_scribe/gateway.py`): alias routing
(gemma4 / oss-20b), retries with backoff on 429/5xx/timeouts, a circuit breaker that rests a failing alias, and a JSONL ledger of
every request with the Portal `x-request-id` of every attempt (never message text or headers). The same gateway has an offline
**loopback** mode, a scripted model, so the whole SDK pipeline runs with no key and no network.

Why: the Cortensor hackathon wants reproducible bug reports for OpenMinion, and the cheapest bad report is a duplicate. In our three
real runs below, Repro Scribe found that all three fresh-install failures were already covered by open issue #573, so nobody files them twice.

## Try it offline in one minute (no key, no network)

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"                     # openminion==0.1.23, pydantic, pytest
pytest -q                                    # 22 offline tests: the full SDK pipeline on the loopback model, gateway, probe
repro-scribe probe --backend loopback        # 13 building blocks, ~10 s, 13/13 PASS
repro-scribe --backend loopback --consensus 2 --approve yes --command "openminion doctor --json"
```

Loopback answers like a model (it calls each offered tool once, fills OpenMinion's internal structured steps from their schemas,
writes the BugReport only from the evidence in its message) and reads GitHub search results from a recorded fixture
(`repro_scribe/fixtures/search.json`). It proves the wiring, not model quality: use Portal or your own provider for that.

## Run it on Cortensor Portal

```bash
export CORTENSOR_API_KEY=ctp_...             # free key: portal.cortensor.network
repro-scribe --backend portal --models oss-20b,gemma4 --consensus 2 --timeout 900 \
  --command "openminion doctor --json" --note "With --json I expect JSON on stdout even when the config is missing."
repro-scribe probe --backend portal --only agent_run,tool_call,output_type
```

`--backend portal` writes an OpenMinion home with OpenMinion's own `cortensor-portal` setup preset and points it at the local
gateway. The ledger lands in `runs/<time>-portal.ledger.jsonl`. `examples/portal-run/live2/` is our complete live run (20 Nov 2026:
61 min, 19 Portal requests, 13 retried, 9 rerouted, report saved): it also showed the model skipping tools, which the self-check now
rejects (WHAT_WE_LEARNED.md F11). Plan for minutes per agent turn on Portal today.

## SDK feature → where it is used

| Feature | OpenMinion SDK call | Where in Repro Scribe | Checked by |
|---|---|---|---|
| Agent turns | `Agent(instructions, model, name, session_id).run()` | all five agents (`cli.py`) | probe `agent_run`, pipeline tests |
| Tools | `@tool(min_scope="READ_ONLY", idempotent=...)` | `reproduce`, `capture_environment`, `find_existing_issues`, `find_related_prs`, `record_verdict` | probe `tool_call`, `test_offline.py` |
| Forced tool | `Agent(forced_tools=["reproduce"])` | investigator always reproduces first | probe `forced_tools`; pipeline test asserts the first call |
| Streaming progress | `Agent.run_stream(on_delta=...)` | live `[investigator +5.0s] tool_started reproduce` lines | probe `run_stream` |
| Sub-agents | `subagent(parent, tools=[...], model=..., timeout_seconds=...)` | 1-3 duplicate hunters with read-only tools, own sessions | probe `subagent` (lineage + allowlist), pipeline test |
| Handoff | `Handoff(target=peer, name="transfer_to_needs_info")` | not-reproduced cases go to the needs-info peer | probe `handoff`, `test_a_failure_that_does_not_reproduce…` |
| Human approval | `run(approval_callback=...)`, action policy `require_confirm` | publisher's `save_draft` (`--approve ask/yes/no`); handoff auto-allowed by name only | probes `approval_deny` / `approval_allow`, `test_a_human_no…` |
| Typed output | `Agent(output_type=BugReport)`, `AgentOutputValidationError` | writer | probe `output_type` |
| Sessions | `session_id`, `AgentRunResult.session_id/run_id` | investigator session shared across runs; hunters get a fresh one per run | probe `sessions` |
| Memory | `MemoryBundle`, `export_bundle/save_bundle/load_bundle` | cross-run dedup: same command → no model call; same failure signature → flagged | probe `memory_bundle`, `test_memory_answers…` |
| Errors | `ProviderError`, `TurnTimeoutError` | exit codes 1 / 4; gateway request id surfaces in the message | probe `provider_error` |
| Providers / Portal | `openminion setup --provider cortensor-portal`, `Agent(model="gemma4"/"oss-20b")` | gateway backend, per-hunter model for consensus | probe `gateway_failover`, live run |

## Run it with your own OpenMinion provider

```bash
export GEMINI_API_KEY=...                    # free tier is enough
openminion setup --provider gemini --model gemini-flash-lite-latest --no-chat
repro-scribe --command "openminion doctor --json" --note "With --json I expect JSON on stdout even when the config is missing."
# -> reports/<title>.md, plus <title>.trace.json with every tool call, approvals, hunter verdicts and the self-check result
```

Any OpenMinion provider works (`--backend config`, the default; `--agent <profile>` to pick one).

## Real runs (Gemini free tier, 19 Nov 2026)

| Command (fresh install) | Reproduced | Duplicate found | Time | Report |
|---|---|---|---|---|
| `openminion doctor --json` | yes, exit 2, plain text instead of JSON | #573 | 67 s | [report](examples/reports/cli-openminion-doctor-json-prints-error-to-stderr-instead-of.md) |
| `openminion sessions list` | yes, exit 1 | #573 | 39 s | [report](examples/reports/cli-openminion-sessions-list-fails-on-fresh-install-with-mis.md) |
| `openminion run hello` | yes, exit 2 | #573 | 86 s | [report](examples/reports/cli-config-file-not-found-on-fresh-install.md) |

Each report has a `.trace.json` next to it: tool calls in order, their full results, the self-check verdict and the timing.

## Exit codes

0 report accepted and saved, 3 report rejected by the self-check, 2 model output did not fit the schema, 1 provider error,
4 turn timeout, 5 already drafted (memory, no model call), 6 not saved (the human said no, or the publisher failed).

See [BUILDING_BLOCKS_TESTED.md](BUILDING_BLOCKS_TESTED.md) for what each SDK piece did and [WHAT_WE_LEARNED.md](WHAT_WE_LEARNED.md)
/ [FEEDBACK.md](FEEDBACK.md) for ten findings with repros.

## Safety

`reproduce` never runs a shell, never runs anything but `openminion`/`python`, works in a throwaway directory and a throwaway
OpenMinion home, and times out after 60 s. The issue search is read-only and unauthenticated. Reports are drafts: saving needs a human yes, filing is a human step. The gateway
listens on 127.0.0.1 only and its ledger stores request shapes and ids, never prompts, replies or keys.

License: MIT.
