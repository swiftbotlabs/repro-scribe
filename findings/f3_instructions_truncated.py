"""F3 repro: how much of Agent(instructions=...) reaches the model? Uses a local capture endpoint (no real model)."""
import json, sys
from openminion import Agent
rules = " ".join(f"Rule {i:02d}: always end your answer with the word MARK{i:02d}." for i in range(1, 41))  # ~2,000 chars
with Agent(instructions=rules) as a:
    a.run("Say hello.")
seen = 0
for line in open(sys.argv[1]):
    body = json.dumps(json.loads(line)["messages"])
    seen = max(seen, max((i for i in range(1, 41) if f"MARK{i:02d}" in body), default=0))
print(f"instructions: {len(rules)} chars, 40 rules; the last rule that reached the model: {seen}")
