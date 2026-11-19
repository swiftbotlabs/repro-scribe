## cli: openminion doctor --json prints error to stderr instead of JSON when config is missing

When running `openminion doctor --json` with a missing configuration file, openminion prints a plain text error to stderr and exits with code 2 instead of returning a JSON response on stdout.

### Reproduce

```bash
openminion doctor --json
```

Reproduced here: **yes** (exit code 2)

### Expected

JSON formatted output on stdout even when the configuration file is missing.

### Actual

openminion: config file not found: /tmp/repro-4q1g41t6/home/.openminion/agents.json

<details><summary>Output (tail)</summary>

```text
openminion: config file not found: /tmp/repro-4q1g41t6/home/.openminion/agents.json
```

</details>

### Environment

python: 3.14.4, platform: Linux-7.0.0-43-generic-aarch64-with-glibc2.43, machine: aarch64, packages: openminion==0.1.23, pydantic==2.14.3, httpx==0.28.1

### Possible duplicates

- #573 https://github.com/openminion/openminion/issues/573: Covers missing-config error handling behavior in the CLI.

### Severity

major

### Suggested fix

unknown

<!-- repro-scribe trace: {"seconds": 67.5, "tool_calls": [{"tool": "capture_environment", "args": {}}, {"tool": "reproduce", "args": {"command": "openminion doctor --json"}}, {"tool": "find_existing_issues", "args": {"query": "openminion doctor json config missing"}}], "checks": ["ok"], "session_id": "repro-scribe-writer"} -->
