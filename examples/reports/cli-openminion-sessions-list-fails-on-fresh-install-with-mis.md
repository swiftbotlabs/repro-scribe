## cli: openminion sessions list fails on fresh install with missing agents.json config file error

Running `openminion sessions list` on a fresh install before any setup fails with a startup error because it expects an `agents.json` configuration file to be present.

### Reproduce

```bash
openminion sessions list
```

Reproduced here: **yes** (exit code 1)

### Expected

Openminion should either initialize a default configuration or gracefully prompt the user to run setup rather than crashing with a startup error.

### Actual

openminion sessions: startup error — config file not found: /tmp/repro-telrjwr2/home/.openminion/agents.json

<details><summary>Output (tail)</summary>

```text
openminion sessions: startup error — config file not found: /tmp/repro-telrjwr2/home/.openminion/agents.json
```

</details>

### Environment

Python 3.14.4 on Linux-7.0.0-43-generic-aarch64-with-glibc2.43 (openminion 0.1.23, pydantic 2.14.3, httpx 0.28.1)

### Possible duplicates

- #573 https://github.com/openminion/openminion/issues/573: Same symptom of a first-run missing-config error when running CLI commands without setup.

### Severity

major

### Suggested fix

unknown

<!-- repro-scribe trace: {"seconds": 38.5, "tool_calls": [{"tool": "capture_environment", "args": {}}, {"tool": "reproduce", "args": {"command": "openminion sessions list"}}, {"tool": "find_existing_issues", "args": {"query": "config file not found agents.json sessions list"}}], "checks": ["ok"], "session_id": "rs-openminion-sessions-list-writer"} -->
