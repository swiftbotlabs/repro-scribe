## CLI: config file not found on fresh install

Running 'openminion run hello' on a fresh install before any setup fails because the required config file is missing.

### Reproduce

```bash
openminion run hello
```

Reproduced here: **yes** (exit code 2)

### Expected

OpenMinion should initialize default configuration or provide setup instructions on first run.

### Actual

openminion: config file not found: /tmp/repro-saglpnrs/home/.openminion/agents.json

<details><summary>Output (tail)</summary>

```text
openminion: config file not found: /tmp/repro-saglpnrs/home/.openminion/agents.json
```

</details>

### Environment

Python 3.14.4, Linux-7.0.0-43-generic-aarch64-with-glibc2.43, openminion 0.1.23

### Possible duplicates

- #573 https://github.com/openminion/openminion/issues/573: Covers the first-run missing-config error where the CLI gives no next step.

### Severity

major

### Suggested fix

unknown

<!-- repro-scribe trace: {"seconds": 85.8, "tool_calls": [{"tool": "capture_environment", "args": {}}, {"tool": "reproduce", "args": {"command": "openminion run hello"}}, {"tool": "find_existing_issues", "args": {"query": "openminion run hello fresh install"}}, {"tool": "find_existing_issues", "args": {"query": "config file not found agents.json"}}], "checks": ["ok"], "session_id": "rs-openminion-run-hello-writer"} -->
