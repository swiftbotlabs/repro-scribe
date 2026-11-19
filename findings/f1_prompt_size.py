"""F1 repro: size of the request a minimal turn sends ("Reply with OK", no user tools). Needs capture_server.py running and an
OpenMinion home configured with: openminion setup --provider custom-openai-compatible --model capture --base-url http://127.0.0.1:18099/v1"""
import json, sys
from openminion import Agent
with Agent() as a:
    a.run("Reply with OK")
for line in open(sys.argv[1]):
    d = json.loads(line)
    system = sum(len(m.get("content") or "") for m in d["messages"] if m["role"] == "system")
    tools = d.get("tools", [])
    print(f"request {len(line):,} bytes (~{len(line)//4:,} tokens): system prompt {system:,} chars, {len(tools)} tools, tool schemas {len(json.dumps(tools)):,} chars")
