"""Tools for Repro Scribe. Every tool is deterministic Python; the model only decides what to call and writes the report."""
from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from importlib import metadata

from openminion import tool

# Commands the agent may run to reproduce a bug: only these programs, never a shell, always in a fresh temp dir.
ALLOWED_PROGRAMS = {"openminion", "python", "python3"}
MAX_OUTPUT = 4000
REPO = os.environ.get("REPRO_SCRIBE_REPO", "openminion/openminion")
# Every tool call is logged here, so the CLI can check the model's report against what the tools really returned.
CALLS: list[dict] = []


def _tail(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    return text if len(text) <= limit else "...[cut " + str(len(text) - limit) + " chars]...\n" + text[-limit:]


def _http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "repro-scribe/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_command(command: str, timeout_s: int = 60) -> dict:
    """Plain function behind the tool, so tests can call it."""
    argv = shlex.split(command)
    if not argv or os.path.basename(argv[0]) not in ALLOWED_PROGRAMS:
        return {"refused": True, "reason": f"only {sorted(ALLOWED_PROGRAMS)} may run", "command": command}
    if os.path.basename(argv[0]) in {"python", "python3"}:
        argv[0] = sys.executable
    with tempfile.TemporaryDirectory(prefix="repro-") as work:
        started = time.monotonic()
        try:
            env = {**os.environ, "NO_COLOR": "1"}
            # A clean install by default: the command sees an empty OpenMinion home (no config, no state), like a new user.
            if not os.environ.get("REPRO_SCRIBE_KEEP_HOME"):
                env["OPENMINION_HOME"] = os.path.join(work, "home")
            proc = subprocess.run(argv, cwd=work, capture_output=True, text=True, timeout=timeout_s, env=env)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = None, exc.stdout or "", (exc.stderr or "") + f"\n[timed out after {timeout_s}s]"
        except FileNotFoundError as exc:
            code, out, err = None, "", str(exc)
    return {"command": command, "exit_code": code, "seconds": round(time.monotonic() - started, 2),
            "stdout_tail": _tail(out if isinstance(out, str) else out.decode("utf-8", "replace")),
            "stderr_tail": _tail(err if isinstance(err, str) else err.decode("utf-8", "replace"))}


def environment() -> dict:
    def ver(pkg: str) -> str:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "not installed"
    return {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
            "packages": {p: ver(p) for p in ("openminion", "pydantic", "httpx")}}


def search_issues(query: str, limit: int = 5) -> dict:
    q = f"repo:{REPO} is:issue {query}"
    url = "https://api.github.com/search/issues?" + urllib.parse.urlencode({"q": q, "per_page": max(1, min(limit, 10))})
    try:
        data = _http_json(url)
    except Exception as exc:  # network or rate limit: the report says so instead of guessing
        return {"query": query, "error": f"{type(exc).__name__}: {exc}", "items": []}
    items = [{"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"],
              "labels": [l["name"] for l in i.get("labels", [])], "created_at": i["created_at"][:10]}
             for i in data.get("items", [])]
    return {"query": query, "total": data.get("total_count", 0), "items": items}


@tool(min_scope="READ_ONLY", idempotent=False)
def reproduce(command: str) -> str:
    """Run one reproduction command (openminion or python only, no shell, fresh temp dir with an empty OpenMinion home, 60 s limit). Returns exit code and output tails as JSON."""
    r = run_command(command)
    CALLS.append({"tool": "reproduce", "args": {"command": command}, "result": r})
    return json.dumps(r)


@tool(min_scope="READ_ONLY", idempotent=True)
def capture_environment() -> str:
    """Return Python, platform and package versions (openminion, pydantic, httpx) as JSON."""
    r = environment()
    CALLS.append({"tool": "capture_environment", "args": {}, "result": r})
    return json.dumps(r)


@tool(min_scope="READ_ONLY", idempotent=True)
def find_existing_issues(query: str) -> str:
    """Search the OpenMinion GitHub issues (public API, read-only) for possible duplicates. Use 2-5 distinctive words. Returns JSON."""
    r = search_issues(query)
    CALLS.append({"tool": "find_existing_issues", "args": {"query": query}, "result": r})
    return json.dumps(r)
