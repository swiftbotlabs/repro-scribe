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
  context budgets (`identity_tokens` = 160 for `decide`, 180-220 for other purposes). Nothing is logged or raised to the SDK user.
- **Impact:** output rules placed at the end of the instructions (formats, "never do X") silently stop working. Our first version lost
  its JSON rules this way.
- **Suggestion:** warn (log + `AgentRunResult.metadata`) when instructions are truncated, document the limit next to `instructions=`,
  and/or let `Agent(...)` raise the identity budget for SDK-provided instructions.
- **Related:** none found.

## F4. `output_type` is validated but never shown to the model; with tools the final text is a closure summary

- **Evidence:** across all 9 provider requests of one run, the BugReport field names never appear, no `response_format` is sent, and the
  turn's final text was the closure's `final_answer` prose, so `AgentOutputValidationError` was raised although the model had produced
  JSON in an earlier step.
- **Workaround we use:** two passes. An investigator Agent with tools, then a tool-less writer Agent with `output_type` whose message
  carries the JSON schema and the tool results.
- **Suggestion:** pass the pydantic schema as `response_format`/`json_schema` where the provider supports it, or append it to the final
  answer instruction when `output_type` is set.
- **Related:** #720 (closure judge can replace a valid output_type answer), #803.

## Smaller notes

- `openminion doctor --json` prints plain text and exits 2 when the config is missing; scripts parsing `--json` break (Repro Scribe
  report in examples/reports; covered by #573's missing-config family).
- `doctor --json` reports `"provider": "openai"` in its summary when the configured provider is Gemini (it uses the OpenAI-compatible
  adapter); the configured provider id would be clearer.
- Gemini `gemini-flash-latest`, `gemini-3.6-flash` and `gemini-3.8-flash` hit the 90 s turn timeout under load (`TurnTimeoutError`);
  `gemini-flash-lite-latest` answered in 40-90 s per Repro Scribe run.
- Groq's edge rejects Python's default user agent; OpenMinion's adapter is fine, but our own probes had to send one.
