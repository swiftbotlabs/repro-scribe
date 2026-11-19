# Findings: how to reproduce each one

All four were found while building Repro Scribe on `openminion 0.1.23` (commit a55a98a) with Gemini (free tier) and Groq (free tier).
`capture_server.py` is a 20-line local OpenAI-compatible endpoint that records each request body, so the numbers below need no model.

```bash
python findings/capture_server.py /tmp/req.jsonl &
export OPENMINION_HOME=$(mktemp -d) OPENAI_COMPATIBLE_API_KEY=dummy
openminion setup --provider custom-openai-compatible --model capture --base-url http://127.0.0.1:18099/v1 --no-chat
python findings/f1_prompt_size.py /tmp/req.jsonl             # F1
: > /tmp/req.jsonl; python findings/f3_instructions_truncated.py /tmp/req.jsonl   # F3
```
