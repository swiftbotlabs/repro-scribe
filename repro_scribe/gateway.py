"""A small OpenAI-compatible gateway that OpenMinion talks to instead of the model endpoint directly.

Why: the OpenMinion setup preset for Cortensor Portal says "Portal can be slow or unstable and requests do not automatically retry",
and one OpenMinion turn makes several model requests (classifier, memory extraction, tool loop, completion critic; finding F5). The
gateway gives every request of every agent in a Repro Scribe run, without changing OpenMinion:

* routing: the requested Portal alias is used while it is healthy; after a failure its circuit opens for a cool-down and the
  request goes to the healthiest other alias (lowest recent latency), so one stuck model does not stall the run;
* retries with backoff on 429 / 5xx / timeouts, before the first byte (streams are never replayed half way);
* a ledger (JSONL) of every request: which agent step asked (tool_choice / tools), requested vs served model, attempts, status,
  seconds, prompt size and the Portal gateway request id (`x-request-id`), the evidence the hackathon asks for in a bug report;
* an offline mode (`upstream="loopback"`): a scripted, deterministic model so the whole SDK pipeline (tools, subagent, handoff,
  approvals, output_type, streaming) runs with no key and no network, for tests and for judges.

Nothing here prints or stores the API key; the ledger has no headers except the upstream request id."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORTAL_URL = "https://api.cortensor.app/v1"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


@dataclass
class ModelHealth:
    name: str
    ok: int = 0
    failed: int = 0
    latency_ewma: float | None = None
    open_until: float = 0.0          # circuit breaker: skip this model until then
    last_error: str = ""

    def record(self, ok: bool, secs: float, error: str = "", cooldown: float = 120.0) -> None:
        if ok:
            self.ok += 1
            self.latency_ewma = secs if self.latency_ewma is None else 0.7 * self.latency_ewma + 0.3 * secs
            self.open_until = 0.0
        else:
            self.failed += 1
            self.last_error = error[:200]
            self.open_until = time.monotonic() + cooldown

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.open_until

    def as_dict(self) -> dict:
        return {"model": self.name, "ok": self.ok, "failed": self.failed,
                "latency_ewma_s": None if self.latency_ewma is None else round(self.latency_ewma, 2),
                "circuit": "open" if not self.available else "closed", "last_error": self.last_error}


@dataclass
class Router:
    """Chooses the model for each attempt. Pure logic, so it is unit-tested without a network."""
    models: list[str]
    cooldown_s: float = 120.0
    health: dict[str, ModelHealth] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for m in self.models:
            self.health.setdefault(m, ModelHealth(m))

    def plan(self, requested: str | None, attempts: int) -> list[str]:
        """Order of models to try: the requested one first if its circuit is closed, then the others by health and latency."""
        def score(m: str) -> tuple:
            h = self.health[m]
            return (not h.available, h.failed - h.ok, h.latency_ewma if h.latency_ewma is not None else 1e9)
        others = sorted((m for m in self.models if m != requested), key=score)
        first = [requested] if requested in self.health and self.health[requested].available else []
        rest = [m for m in others if self.health[m].available] + [m for m in others if not self.health[m].available]
        order = first + rest
        if requested in self.health and not first:
            order.append(requested)       # open circuit: last resort only
        if not order:
            order = [requested or self.models[0]]
        return [order[i % len(order)] for i in range(max(1, attempts))]

    def record(self, model: str, ok: bool, secs: float, error: str = "") -> None:
        self.health.setdefault(model, ModelHealth(model)).record(ok, secs, error, self.cooldown_s)

    def snapshot(self) -> list[dict]:
        return [h.as_dict() for h in self.health.values()]


def describe_request(body: dict) -> dict:
    """Which OpenMinion step sent this request, from its shape (no message text kept)."""
    tools = [t.get("function", {}).get("name", "") for t in body.get("tools") or []]
    choice = body.get("tool_choice")
    forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
    props = []
    if forced and tools:
        props = sorted(((body["tools"][0].get("function", {}).get("parameters") or {}).get("properties") or {}).keys())
    step = "tool loop" if tools and not forced else ("structured: " + _structured_kind(props) if forced else "chat")
    return {"step": step, "tools": tools[:12], "forced": forced, "messages": len(body.get("messages") or []),
            "prompt_chars": sum(len(json.dumps(m.get("content", ""))) for m in body.get("messages") or []),
            "stream": bool(body.get("stream"))}


def _structured_kind(props: list[str]) -> str:
    known = {"time_sensitive": "freshness classifier", "next_action": "completion critic", "items": "memory extraction"}
    for k, v in known.items():
        if k in props:
            return v
    return ",".join(props[:4]) or "output"


# ---------------------------------------------------------------------------------------------------------------- loopback model
INTERNAL_TOOLS = {"tool_request", "respond", "coding", "plan", "research", "decompose", "clarify", "review_diff"}


def _fill(schema: dict, defs: dict, key: str = "") -> object:
    """A minimal valid value for a JSON schema, with sensible answers for OpenMinion's internal structured steps."""
    if "$ref" in schema:
        return _fill(defs.get(schema["$ref"].split("/")[-1], {}), defs, key)
    for alt in ("anyOf", "oneOf"):
        if alt in schema:
            opts = [o for o in schema[alt] if o.get("type") != "null"] or schema[alt]
            return _fill(opts[0], defs, key)
    preset = {"next_action": "close", "satisfied": True, "mutation_claimed": False, "time_sensitive": False,
              "needs_sources": False, "needs_live_data": False, "needs_exact_date": False, "confidence": 1}
    if key in preset:
        want = preset[key]
        if "enum" not in schema or want in schema["enum"]:
            return want
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        req = schema.get("required") or list(props)
        return {k: _fill(props[k], defs, k) for k in req if k in props}
    if t == "array":
        n = schema.get("minItems", 0)
        return [_fill(schema.get("items") or {}, defs, key) for _ in range(n)]
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return schema.get("minimum", 0) if schema.get("minimum", 0) > 0 else (1 if key == "confidence" else 0)
    if key == "final_answer":
        return "Done: the evidence was gathered with the tools."
    if key == "answer_mode":
        return "direct"
    return schema.get("default") if isinstance(schema.get("default"), str) else "n/a"


def _user_text(messages: list[dict]) -> str:
    texts = [m.get("content") for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return "\n".join(texts)


def _unwrap(content: str) -> str:
    """OpenMinion wraps a tool's return value as {"status", "summary", "outputs": {"data": "<the tool's JSON>"}}."""
    try:
        d = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content or ""
    inner = d.get("outputs", {}).get("data") if isinstance(d, dict) and isinstance(d.get("outputs"), dict) else None
    return inner if isinstance(inner, str) else content


def _tool_results(messages: list[dict]) -> list[str]:
    return [_unwrap(m.get("content") or "") for m in messages if m.get("role") == "tool"]


def _called(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        for c in m.get("tool_calls") or []:
            out.append(c.get("function", {}).get("name", ""))
    return out


def _bug_report_from(text: str) -> dict:
    """The writer's JSON, built only from the evidence in its message (the same rule the real model is given)."""
    cmd = (re.search(r"Command: (.*?) -- ", text) or re.search(r"Failing command: (.*?) -- ", text))
    cmd = cmd.group(1).strip() if cmd else "unknown"
    ev = re.search(r"Tool results: (\[.*\])\s*$", text, re.S)
    calls = []
    if ev:
        try:
            calls = json.loads(ev.group(1))
        except json.JSONDecodeError:
            calls = []
    rep = next((c["result"] for c in reversed(calls) if c.get("tool") == "reproduce"), {})
    env = next((c["result"] for c in calls if c.get("tool") == "capture_environment"), {})
    code = rep.get("exit_code")
    out = ((rep.get("stderr_tail") or "") + "\n" + (rep.get("stdout_tail") or "")).strip()
    lines = [l for l in out.splitlines() if l.strip()]
    decisive = next((l for l in reversed(lines) if re.search(r"error|exception|failed|not found|invalid", l, re.I)),
                    lines[-1] if lines else "(no output)")
    confirmed = {int(n): k for n, k in re.findall(r"#(\d+) (DUP|RELATED)", text)}
    dups = []
    for c in calls:
        if c.get("tool") in ("find_existing_issues", "find_related_prs"):
            for i in c.get("result", {}).get("items", []):
                if i["number"] in confirmed and all(d["number"] != i["number"] for d in dups):
                    why = ("same symptom" if confirmed[i["number"]] == "DUP" else "related, or a fix in flight") + f": {i.get('title', '')[:90]}"
                    dups.append({"number": i["number"], "url": i["url"], "why": why})
    pk = env.get("packages", {})
    short = re.sub(r"(/[^\s'\"]+)+", "<path>", re.sub(r"^\w+:\s*", "", decisive.strip()))
    title = f"cli: `{cmd}` fails on a clean install: {short[:70]}" if code not in (0, None) else f"cli: `{cmd}` did not reproduce"
    return {"title": title,
            "summary": f"`{cmd}` exits {code} on a clean install.",
            "reproduction_command": cmd, "reproduced": bool(code not in (0, None)), "exit_code": code,
            "expected": "The command succeeds or fails with an actionable message.", "actual": decisive.strip()[:300],
            "environment": f"Python {env.get('python', '?')}, {env.get('platform', '?')}, openminion {pk.get('openminion', '?')}",
            "possible_duplicates": dups, "severity": "minor", "suggested_fix": "unknown"}


def loopback_reply(body: dict) -> dict:
    """Deterministic stand-in for a model. Answers tool loops by calling each offered tool once, in the offered order."""
    messages = body.get("messages") or []
    tools = [t.get("function", {}) for t in body.get("tools") or []]
    choice = body.get("tool_choice")
    forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
    user = _user_text(messages)
    if forced:
        fn = next((t for t in tools if t.get("name") == forced), {})
        params = fn.get("parameters") or {}
        args = _fill(params, params.get("$defs") or params.get("definitions") or {})
        return _tool_call(forced, args)
    ours = [t for t in tools if t.get("name") not in INTERNAL_TOOLS]
    done = _called(messages)
    system = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
    # OpenMinion's tool contract: the first tool call of a turn carries task_update (start, verbatim user_quote, objective).
    task = None
    if not done and "task_update" in system:
        quote = next((m.get("content") for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        task = {"user_quote": quote, "operation": "start", "objective": quote[:160] or "answer the request", "expected_revision": 0}
    for t in ours:
        name = t.get("name", "")
        if name in done:
            continue
        if name == "save_draft" and done:
            continue
        if name.startswith("transfer_to_") and any(o.get("name") == "reproduce" for o in ours) and not _reproduced_clean(messages):
            continue
        args = _args_for(name, t.get("parameters") or {}, user, messages)
        return _tool_call(name, {**args, "task_update": task} if task else args)
    text = _final_text(messages, system, user, done)
    respond = next((t for t in tools if t.get("name") == "respond"), None)
    if respond and not ours:
        # Tool-less agents answer through OpenMinion's internal respond tool (no task_update, as real models do).
        params = respond.get("parameters") or {}
        return _tool_call("respond", {**_fill(params, params.get("$defs") or {}), "answer": text})
    return _text(text)


def _reproduced_clean(messages: list[dict]) -> bool:
    """True when a reproduce result in the conversation exited 0 (the failure did not reproduce)."""
    for r in _tool_results(messages):
        try:
            d = json.loads(r)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(d, dict) and "exit_code" in d and d.get("exit_code") == 0 and "stdout_tail" in d:
            return True
    return False


def _schema_in(user: str) -> dict | None:
    i = user.find("Schema: {")
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(user[i + len("Schema: "):])[0]
    except json.JSONDecodeError:
        return None


def _final_text(messages: list[dict], system: str, user: str, done: list[str]) -> str:
    schema = _schema_in(user)
    if schema is not None and schema.get("title") == "BugReport":
        return json.dumps(_bug_report_from(user))
    if schema is not None:
        return json.dumps(_fill(schema, schema.get("$defs") or {}))
    results = _tool_results(messages)
    if "questions for the reporter" in system:
        return ("1. Which openminion version and Python version do you run?\n2. Is there an agents.json in your OpenMinion home, and "
                "which provider does it use?\n3. What exact output do you see (paste the last 20 lines)?")
    if "DUP #<number>" in system:
        nums = []
        for r in results:
            try:
                nums += [i["number"] for i in json.loads(r).get("items", [])]
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        return "\n".join(f"RELATED #{n}" for n in dict.fromkeys(nums)) or "NONE"
    if "save_draft" in done:
        return results[-1] if results else "saved"
    if results:
        return "Investigated: " + " | ".join(r[:160].replace("\n", " ") for r in results[-2:])
    return "ok"


def _args_for(name: str, params: dict, user: str, messages: list[dict] | None = None) -> dict:
    props = params.get("properties") or {}
    if name == "record_verdict":
        # Deterministic stand-in for judgement: the top-ranked issue hit is DUP, every other hit (issues and PRs) RELATED.
        lines = []
        for r in _tool_results(messages or []):
            try:
                d = json.loads(r)
            except (json.JSONDecodeError, TypeError):
                continue
            for i in d.get("items", []) if isinstance(d, dict) else []:
                if not any(l.endswith(f"#{i['number']}") for l in lines):
                    is_pr = "/pull/" in i.get("url", "")
                    lines.append(f"{'DUP' if not lines and not is_pr else 'RELATED'} #{i['number']}")
        return {"lines": "\n".join(lines) or "NONE"}
    cmd = re.search(r"Failing command: (.*?) -- ", user) or re.search(r"Command: (.*)$", user, re.M)
    words = re.search(r"Failure to look up: (.*?) -- ", user)
    out = {}
    for k in props:
        if k == "command":
            out[k] = cmd.group(1).strip() if cmd else "python -c 1"
        elif k == "query":
            src = words.group(1) if words else (cmd.group(1) if cmd else "error")
            toks = [w for w in re.findall(r"(?<![\w/.-])[A-Za-z][A-Za-z_]{3,}(?![\w/.-])", src)
                    if w.lower() not in {"error", "openminion", "home", "json"}]
            out[k] = " ".join(toks[:4]) or "error"
        elif k == "reason":
            out[k] = "the report passed the self-check"
        elif k == "message":
            out[k] = (re.findall(r"^(?!CURRENT TASK).+$", user, re.M) or [user])[-1][:400]
        elif (props[k].get("type") == "string") and (q := re.findall(r"'([^']{1,40})'", user)):
            out[k] = q[-1]
        else:
            out[k] = _fill(props[k], {}, k)
    return out


def _tool_call(name: str, args: dict) -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"call_{name}_{int(time.time() * 1000) % 100000}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def _text(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _completion(model: str, message: dict) -> dict:
    return {"id": f"chatcmpl-loop-{int(time.time() * 1000)}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message,
                                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _sse(model: str, message: dict) -> bytes:
    """The same reply as an OpenAI-style stream (one delta + DONE), for run_stream."""
    delta = {k: v for k, v in message.items() if v is not None}
    if delta.get("tool_calls"):
        delta["tool_calls"] = [{**c, "index": i} for i, c in enumerate(delta["tool_calls"])]
    chunk = {"id": "chatcmpl-loop", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    end = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}
    return ("data: " + json.dumps(chunk) + "\n\n" + "data: " + json.dumps(end) + "\n\ndata: [DONE]\n\n").encode()


# ---------------------------------------------------------------------------------------------------------------- the server
class Gateway:
    """Run with `with Gateway(...) as gw:` and point OpenMinion's base_url at gw.base_url."""

    def __init__(self, upstream: str = "portal", models: list[str] | None = None, ledger: str | Path | None = None,
                 api_key_env: str = "CORTENSOR_API_KEY", attempts: int = 3, timeout_s: float = 150.0,
                 backoff_s: float = 2.0, port: int = 0, fail_plan: dict[str, int] | None = None):
        self.upstream = upstream
        self.upstream_url = PORTAL_URL if upstream == "portal" else upstream
        self.router = Router(models or ["oss-20b", "gemma4"])
        self.ledger_path = Path(ledger) if ledger else None
        self.entries: list[dict] = []
        self.api_key_env, self.attempts, self.timeout_s, self.backoff_s = api_key_env, attempts, timeout_s, backoff_s
        self.fail_plan = dict(fail_plan or {})     # loopback only: {model: n} = fail the next n requests to that model with 503
        self._lock = threading.Lock()
        gw = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    data = [{"id": m, "object": "model", "owned_by": "repro-scribe-gateway"} for m in gw.router.models]
                    return self._send(200, json.dumps({"object": "list", "data": data}).encode())
                if self.path.rstrip("/").endswith("/health"):
                    return self._send(200, json.dumps(gw.router.snapshot()).encode())
                self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, b'{"error":"bad json"}')
                status, payload, ctype, headers = gw.handle(body)
                self._send(status, payload, ctype, headers)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "Gateway":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    # one request, with routing and retries
    def handle(self, body: dict) -> tuple[int, bytes, str, dict]:
        requested = body.get("model")
        info = describe_request(body)
        attempts, t0 = [], time.monotonic()
        status, payload, ctype, served, rid = 502, b'{"error":"no attempt"}', "application/json", None, None
        for i, model in enumerate(self.router.plan(requested if requested in self.router.health else None, self.attempts)):
            if i:
                time.sleep(self.backoff_s * (2 ** (i - 1)) if self.upstream != "loopback" else 0)
            a0 = time.monotonic()
            status, payload, ctype, rid, err = self._forward({**body, "model": model}, model)
            secs = round(time.monotonic() - a0, 2)
            ok = status == 200
            self.router.record(model, ok, secs, err)
            attempts.append({"model": model, "status": status, "seconds": secs, "request_id": rid, "error": err[:160] if err else ""})
            served = model
            if ok or status not in RETRY_STATUSES and status != -1:
                break
        entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "requested": requested, "served": served,
                 "status": status, "seconds": round(time.monotonic() - t0, 2), "attempts": attempts, **info}
        with self._lock:
            self.entries.append(entry)
            if self.ledger_path:
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
        headers = {"x-repro-scribe-served-model": str(served)}
        if rid:
            headers["x-request-id"] = rid
        if status == -1:
            status = 504
        return status, payload, ctype, headers

    def _forward(self, body: dict, model: str) -> tuple[int, bytes, str, str | None, str]:
        if self.upstream == "loopback":
            if model == "auth-fail":
                return 401, b'{"error":{"message":"API key is not valid.","type":"auth"}}', "application/json", "loop-auth-fail", "401"
            if self.fail_plan.get(model, 0) > 0:
                self.fail_plan[model] -= 1
                return 503, b'{"error":"planned failure"}', "application/json", f"loop-fail-{model}", "planned 503"
            msg = loopback_reply(body)
            if body.get("stream"):
                return 200, _sse(model, msg), "text/event-stream", f"loop-{len(self.entries)}", ""
            return 200, json.dumps(_completion(model, msg)).encode(), "application/json", f"loop-{len(self.entries)}", ""
        key = os.environ.get(self.api_key_env, "")
        req = urllib.request.Request(self.upstream_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, resp.read(), resp.headers.get("content-type", "application/json"), resp.headers.get("x-request-id"), ""
        except urllib.error.HTTPError as exc:
            data = exc.read()
            return exc.code, data, exc.headers.get("content-type", "application/json"), exc.headers.get("x-request-id"), data[:200].decode("utf-8", "replace")
        except Exception as exc:  # timeout or connection error: retried, then reported as 504
            return -1, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json", None, f"{type(exc).__name__}: {exc}"

    def summary(self) -> dict:
        by_step: dict[str, int] = {}
        for e in self.entries:
            by_step[e["step"]] = by_step.get(e["step"], 0) + 1
        return {"requests": len(self.entries), "retried": sum(1 for e in self.entries if len(e["attempts"]) > 1),
                "rerouted": sum(1 for e in self.entries if e["served"] != e["requested"]),
                "failed": sum(1 for e in self.entries if e["status"] != 200),
                "model_seconds": round(sum(e["seconds"] for e in self.entries), 1), "by_step": by_step,
                "request_ids": [a["request_id"] for e in self.entries for a in e["attempts"] if a.get("request_id")][-20:],
                "health": self.router.snapshot()}


def write_home(home: Path, base_url: str, models: list[str]) -> Path:
    """An OpenMinion home whose model connection is OpenMinion's own "cortensor-portal" preset (written by `openminion setup`),
    with its base_url pointed at the gateway. Agents then pick a Portal alias with Agent(model=...)."""
    import subprocess
    import sys
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / ".openminion" / "agents.json"
    env = {**os.environ, "OPENMINION_HOME": str(home)}
    env.setdefault("CORTENSOR_API_KEY", "loopback")
    exe = Path(sys.executable).with_name("openminion")
    if not cfg.exists():
        for i, m in enumerate(models):
            args = [str(exe), "setup", "--provider", "cortensor-portal", "--model", m, "--no-chat"] + (["--add-model"] if i else [])
            r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"openminion setup failed: {r.stderr[-400:]}")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for agent in data.get("agents", {}).values():
        for conn in (agent.get("model_connections") or {}).values():
            conn["models"] = list(dict.fromkeys(models))
            conn.setdefault("provider_config_overrides", {})["base_url"] = base_url
    # The SDK's Agent runs read providers.openai, not the connection override (finding F6), so point both at the gateway.
    data.setdefault("providers", {}).setdefault("openai", {})["base_url"] = base_url
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return cfg
