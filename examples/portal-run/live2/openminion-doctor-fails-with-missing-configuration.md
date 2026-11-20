## `openminion doctor` fails with missing configuration

Attempting to run `openminion doctor --json` when the OpenMinion configuration file is absent causes the command to exit immediately with code 2 and no JSON output, contrary to the user’s expectation of a JSON report indicating the failure.

### Reproduce

```bash
openminion doctor --json
```

Reproduced here: **no** (exit code 2)

### Expected

A JSON‑formatted report on stdout, even when the configuration file is missing.

### Actual

The command terminates with `openminion: config file not found: /tmp/repro-pyqehff5/home/.openminion/agents.json` and exits without producing any JSON.

<details><summary>Output (tail)</summary>

```text
openminion: config file not found: /tmp/repro-pyqehff5/home/.openminion/agents.json
```

</details>

### Environment

Operating system: Linux, Python: 3.11, OpenMinion version: 0.3.0

### Possible duplicates

- none found

### Severity

blocker

### Suggested fix

Modify the CLI’s configuration loading logic: catch FileNotFoundError and either create a default config or return a JSON payload indicating a missing config. Alternatively, create an empty `~/.openminion/agents.json` before invoking the command.

<!-- repro-scribe trace: {"seconds": 3663.8, "backend": "portal", "tool_calls": [{"tool": "reproduce", "args": {"command": "openminion doctor --json"}}], "checks": ["ok"], "hunter_verdicts": [{}], "consensus": {}, "handoff_used": false, "signature": "49f5fa54761491ff", "stream_events": 29, "session_id": "repro-scribe-writer", "ledger": "runs/20261120T180827Z-portal.ledger.jsonl"} -->
