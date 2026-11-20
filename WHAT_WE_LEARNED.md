# What We Learned

Building Repro Scribe on `openminion 0.1.23` (commit a55a98a, Python 3.14, Linux aarch64) with free-tier models (Gemini
`gemini-flash-lite-latest`; Groq `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`). Each finding has a repro, the evidence we captured, and an
actionable suggestion. We searched the issue tracker for each one (with Repro Scribe itself) before writing it down.

## F1. A minimal turn sends ~10.8k tokens, so free-tier models refuse it (HTTP 413)

- **Repro:** `findings/f1_prompt_size.py` against the local capture endpoint (see findings/README.md).
- **Evidence:** "Reply with OK" with no user tools sends one 43,294-byte request: a 17,717-character system prompt and 8 built-in tool
  schemas (24,922 characters; `plan` alone is 5,881). `openminion doctor --check-turn` on Groq's free tier fails with
  `HTTP 413: Request too large ... on tokens per minute` on both `openai/gpt-oss-20b` and `qwen/qwen3.8-27b`.
- **Impact:** a new builder on a free tier sees the doctor smoke test fail before writing any code.
- **Suggestion:** a documented "lean" entry profile (only `tool_request`/`respond`/`clarify` exposed on the first call), or lazy loading
  of the large planning schemas; at least name the request size in the 413 error and in the provider setup docs.
- **Related:** #619 (inactive tool directory adds ~2.1k tokens).

## F2. A model that omits `objective` in `task_update` ends the run with a clarify question

- **Evidence (logged provider traffic):** Gemini sent `{"task_update":{"operation":"start","expected_revision":0,"user_quote":"..."}}`.
  The JSON schema the runtime sends lists `required: ["operation","expected_revision","user_quote"]`; `objective` is only required
  in prose ("start/replace requires a resolved objective"). `updated_task_context` then raises "A new task requires an objective" and the
  turn ends with "I could not safely update the task conditions. Please restate the current task and its required conditions."
  Every run before our workaround (5 of 5) ended this way; the two we logged both lacked `objective`. Workaround: one line in our
  instructions.
- **Suggestion:** express the rule in the schema (`if operation in [start, replace] then required objective`), or retry once with
  the validation error instead of ending the turn.
- **Related:** #719 (invalid optional task_update on a first turn) is the same family; our evidence adds the schema cause.

## F3. Long `Agent(instructions=...)` are cut silently: in our test 24 of 40 rules never reached the model

- **Repro:** `findings/f3_instructions_truncated.py` (2,159 characters, 40 numbered rules).
- **Evidence:** only rules 1-16 appear in the provider request; the identity block ends with `...[truncated]`. The cap comes from the
  context budgets (`identity_tokens` = 160 for `decide`, 180-220 for other purposes). The identity budget owner does record the
  truncation internally, but the SDK caller is not shown it: no log line, no error, nothing in the run result. The cut-off point is not
  a fixed character limit: it depends on the turn's purpose, configuration, the other identity sections and the token estimator.
- **Impact:** output rules placed at the end of the instructions (formats, "never do X") silently stop working. Our first version lost
  its JSON rules this way.
- **Suggestion:** carry the existing truncation facts through to the caller (run metadata/telemetry, no instruction text in
  diagnostics), and say in the `instructions=` docs that long instructions share a bounded identity budget. Keep the budget itself
  owned by the context service: no automatic increase and no silent rewriting.
- **Status:** reported as openminion#811; a maintainer confirmed the bounded-context path at the component level (2,039 characters
  in, 655 kept on the `decide` budget, `truncated=True`) and asked for a bounded caller-visibility change. Our fix PR #812 does that:
  the identity manifest and turn telemetry report that the identity budget truncated, with the cap and the tokens used, and 4 tests
  (3 of them fail when the change is reverted).

## F4. `output_type` is validated but never shown to the model; with tools the final text is a closure summary

- **Evidence:** across all 9 provider requests of one run, the BugReport field names never appear, no `response_format` is sent, and the
  turn's final text was the closure's `final_answer` prose, so `AgentOutputValidationError` was raised although the model had produced
  JSON in an earlier step.
- **Workaround we use:** two passes. An investigator Agent with tools, then a tool-less writer Agent with `output_type` whose message
  carries the JSON schema and the tool results.
- **Suggestion:** pass the pydantic schema as `response_format`/`json_schema` where the provider supports it, or append it to the final
  answer instruction when `output_type` is set.
- **Related:** #720 (closure judge can replace a valid output_type answer), #803.

## F5. One short tool turn is 8 model requests; a 4-agent pipeline is ~40, which is minutes on Portal

- **What we saw:** the gateway ledger counts every request. A one-tool turn (`probe_lookup` then answer) made 8 requests: memory
  extraction, 3 tool-loop steps, freshness classifier, a "blocking_reason/remaining_work/status" step, the completion critic and a
  final-answer step (`AgentRunResult.stats["llm_calls"]` agrees: 8). One Repro Scribe run with two hunters made 39.
- **Why it matters on Portal:** a tiny direct Portal call took 18-22 s (20 Nov). At that latency an 8-request turn is 3-4 minutes
  before any retry, so a plain `Agent.run("Capital of France?")` on Portal oss-20b took 272 s for us.
- **Ask:** a documented "lean turn" switch (skip memory extraction / freshness / critic for tool-less or structured-output agents),
  and per-step counts in `stats`. Repro: `repro-scribe probe --backend loopback --json` (column `model_requests`).

## F6. Editing a model connection's `base_url` does not change where `Agent(...)` sends requests

- `openminion setup --provider cortensor-portal` writes the endpoint twice: `agents.<id>.model_connections.<conn>.provider_config_overrides.base_url`
  and `providers.openai.base_url`. We pointed only the connection at a local gateway; SDK `Agent.run()` still went to
  `https://api.cortensor.app/v1` (Portal answered 401 for our placeholder key, with a `gw-…` request id). Pointing
  `providers.openai.base_url` at the gateway worked. Expected: the connection override applies, or setup writes one place.
  (Repro Scribe's `gateway.write_home` patches both; see the comment there.)

## F7. oss-20b on Portal: the reply can be a text pseudo-call, returned to the caller as the answer

- `Agent(instructions="Answer in one word.", model="oss-20b").run("Capital of France?")` through the `cortensor-portal` preset
  returned `'openminion: Respond(answer=Paris, freshness=Known)'` after 272 s (`stats`: 1 LLM call). The model wrote OpenMinion's
  `respond` tool call as plain text; the hybrid tool-call parser did not pick it up, so the raw text became the reply.
  Ask: parse `Name(key=value, …)` text calls for the internal `respond` tool, or retry with `tool_choice` forced.

## F8. A `Handoff` is classed as a local write of unknown reversibility, so it needs approval

- Without an `approval_callback`, `Agent(handoffs=[Handoff(target=peer)]).run(...)` ends with the reply
  `Policy confirmation required. transfer_to_peer · Risk: write · Side effects: local · Reversibility: UNKNOWN_REVERSIBILITY`, and the
  peer never runs. With a callback that returns True it works (probe `handoff`). A delegation to a peer agent is not a write; at
  least `Handoff` could carry its own risk class, or the docs could say a callback is required. Repro: `repro-scribe probe --only handoff`
  (it passes because the probe supplies the callback; remove it in `check_handoff` to see the policy reply).

## F9. Replies start with the agent id

- `AgentRunResult.text` is `"openminion: ok"`, not `"ok"`. Every caller that shows or parses the text has to strip the prefix
  (`cli.speech`). Expected: the prefix only on console surfaces, or a documented field without it.

## F10. Cortensor Portal answers 502 when the model returns a function the request did not offer, or wrong tool arguments

From our live run (20 Nov 2026, 17:53-18:01 UTC, `examples/portal-run/`), on OpenMinion's forced structured step (`tool_choice`
= `submit_output`, memory extraction) and on a tool-loop step:

| Step | Alias | Status | Portal message | Request id |
|---|---|---|---|---|
| memory extraction | oss-20b | 502 | Model returned a function that was not advertised in this request | gw-5faa3e1f-21ca-4d2c-ac3f-5b1b00f21b41 |
| memory extraction | gemma4 | 502 | same | gw-967b3cda-3319-4433-8191-ef78b5a444f6 |
| memory extraction | oss-20b | 502 | same | gw-1867fffe-b067-40b6-8322-fd1531bf5f8e |
| tool loop | gemma4 | (none) | client read timeout after 150 s | - |
| tool loop | oss-20b | 502 | Model tool arguments do not match the advertised schema | gw-fe6ad957-4d8a-4dec-a649-73381ac2b2da |
| tool loop | gemma4 | 200 | rerouted by the gateway; the turn went on | gw-7c161f6e-4905-4c59-a797-6be26794d29e |
| freshness classifier | gemma4 | 502 | Model tool arguments do not match the advertised schema | gw-c6fde689-4911-468e-8cd0-84fcc51eef6c |
| freshness classifier | oss-20b | 502 | same | gw-1497727a-2f22-4ba3-81ec-a0341acd6857 |
| freshness classifier | gemma4 | 500 | Router V4 request failed before model completion (`router_v4_internal_error`) | gw-43bc06fe-c290-4e14-bb21-b76d93ea03b2 |
| tool loop | gemma4 | 200 | first try | gw-5a04849c… |

Outcome, stated plainly: the investigator ran its tools (reproduce + environment), but its turn hit our 900 s turn limit after 4
model requests took 823 s in total, so this live run did not reach the writer. On Portal today a full 4-agent run (~40 requests)
does not fit any sensible timeout; this is why the offline loopback backend exists and why F5 matters.

A 502 tells a client to retry, but these are model-output validation errors: a retry on the same alias usually fails the same way.
Ask: a 4xx/422-style status (or an error `type` field the SDK can branch on), and for forced `tool_choice`, constrained decoding or a
single server-side retry. On the OpenMinion side, the preset note "requests do not automatically retry" is exactly what the
gateway's retry + reroute covers.

## F11. A complete live Portal run: it finished, and it showed why the self-check must cover more than duplicates

Second live run (20 Nov 2026, 18:08-19:27 UTC; `examples/portal-run/live2/`), `--backend portal --models gemma4,oss-20b
--consensus 1 --timeout 3000`, search results from the recorded fixture:

- **It completed end to end**: investigator → duplicate hunter → writer → approval → saved draft, in 61 minutes.
- **The gateway did the heavy lifting**: 19 model requests, 4,688 s of model time, 13 retried, 9 rerouted to the other alias,
  9 still failed after 3 attempts (OpenMinion carried on past failed memory-extraction / classifier steps). Attempt errors:
  17 client timeouts (150 s), 9 × 502 "Model tool arguments do not match the advertised schema", 4 × 502 "Model returned a
  function that was not advertised", 1 × "Model response did not contain the requested…". 24 Portal request ids are in the ledger.
- **The model cut corners, and v0.2's self-check let the report through** (`checks: ["ok"]`): the investigator called only
  `reproduce` (never `capture_environment`), the hunter never called a search tool, and the writer then said "Reproduced here: no"
  for an exit-2 failure and invented "Python 3.11, OpenMinion 0.3.0" (real: 3.14 / 0.1.23).
- **Fixed in v0.2.1**: `check()` now also rejects a "not reproduced" claim for a failing command, an environment line without a
  `capture_environment` call (or with a different openminion version), and "no duplicates" when no search ran. The test
  `test_check_rejects_the_real_portal_live2_report` replays the real trace of this run (tests/fixtures) and must find all three.
- **Ask (OpenMinion)**: `forced_tools` forces one tool; a way to require a *set* of tools before the turn may close (here:
  `reproduce` and `capture_environment`; for the hunter, at least one search) would stop the model closing early.

## Smaller notes

- `openminion doctor --json` prints plain text and exits 2 when the config is missing; scripts parsing `--json` break (Repro Scribe
  report in examples/reports; covered by #573's missing-config family).
- `doctor --json` reports `"provider": "openai"` in its summary when the configured provider is Gemini (it uses the OpenAI-compatible
  adapter); the configured provider id would be clearer.
- Gemini `gemini-flash-latest`, `gemini-3.6-flash` and `gemini-3.8-flash` hit the 90 s turn timeout under load (`TurnTimeoutError`);
  `gemini-flash-lite-latest` answered in 40-90 s per Repro Scribe run.
- Groq's edge rejects Python's default user agent; OpenMinion's adapter is fine, but our own probes had to send one.
