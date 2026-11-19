## Agent(instructions=...) longer than ~860 characters is truncated silently; later rules never reach the model

### Summary

Text passed as `Agent(instructions=...)` is rendered into the `[IDENTITY]` block of the system prompt and capped by the context
budget `identity_tokens` (160 for `decide`, 180-220 for other purposes in `modules/context/schemas/budgets.py`). Anything past the
cap is cut and replaced by `...[truncated]`. Nothing is logged to the SDK caller or returned in `AgentRunResult`, so rules placed at the
end of the instructions (output format, "never do X") silently stop working.

### Reproduce

Self-contained, no model needed (a 20-line local OpenAI-compatible endpoint records the request):

```bash
python findings/capture_server.py /tmp/req.jsonl &
export OPENMINION_HOME=$(mktemp -d) OPENAI_COMPATIBLE_API_KEY=dummy
openminion setup --provider custom-openai-compatible --model capture --base-url http://127.0.0.1:18099/v1 --no-chat
python findings/f3_instructions_truncated.py /tmp/req.jsonl
```

(Files: https://github.com/swiftbotlabs/repro-scribe/tree/main/findings)

### Expected

All 40 numbered rules (2,159 characters) reach the model, or the SDK warns that the instructions were shortened.

### Actual

```
instructions: 2159 chars, 40 rules; the last rule that reached the model: 16
```

The identity block in the captured request ends with `...[truncated]` after rule 16.

### Environment

openminion 0.1.23 (a55a98a), Python 3.14.4, Linux aarch64; provider-independent (seen with Gemini and with the capture endpoint).

### Suggested fix

1. When SDK-provided instructions exceed the identity budget, emit a warning (log + `AgentRunResult.metadata["instructions_truncated"]`
   with the kept/total characters).
2. Document the limit next to `instructions=` in the SDK docs and examples.
3. Optionally let `Agent(...)` raise the identity budget for caller instructions (e.g. `instructions_budget_tokens=`), since SDK
   instructions are the caller's contract, not optional persona text.

We are happy to send a PR for (1) and (2) once you confirm the direction.
