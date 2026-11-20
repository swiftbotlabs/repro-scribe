"""repro-scribe probe: one small check per OpenMinion building block that Repro Scribe depends on, with timings and the number
of model requests each block costs (counted by the gateway).

  repro-scribe probe --backend loopback            # offline, ~10 s, no key: what judges can run first
  repro-scribe probe --backend portal --only agent_run,tool_call,output_type   # live on Cortensor Portal (slow: minutes per check)
  repro-scribe probe --json > probe.json

Each check passes only on the documented behaviour (a check that cannot run is FAIL, never "skipped")."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel

from openminion import tool

TRACE: list[str] = []


@tool(min_scope="READ_ONLY", idempotent=True)
def probe_lookup(term: str) -> str:
    """Look up a term in the probe dictionary. Returns JSON with its meaning."""
    TRACE.append(f"probe_lookup:{term}")
    return json.dumps({"term": term, "meaning": "a probe value"})


@tool(min_scope="READ_ONLY", idempotent=True)
def probe_clock() -> str:
    """Return the probe clock reading as JSON."""
    TRACE.append("probe_clock")
    return json.dumps({"clock": "12:00"})


@tool(idempotent=False)
def probe_write(note: str) -> str:
    """Write a note to the probe log. Needs the user's approval. Returns JSON."""
    TRACE.append(f"probe_write:{note[:40]}")
    return json.dumps({"written": True})


class Verdict(BaseModel):
    answer: str
    confident: bool


def _agent(**kw):
    from openminion import Agent
    return Agent(**kw)


def check_agent_run(ctx):
    with _agent(instructions="Answer in one short sentence.", model=ctx["model"], session_id=ctx["sid"]("run")) as a:
        r = a.run("Say hello to the probe.", timeout_seconds=ctx["timeout"])
    return bool(r.text and r.run_id), f"run_id={r.run_id} text={r.text[:60]!r}"


def check_tool_call(ctx):
    TRACE.clear()
    with _agent(instructions="Use probe_lookup for any term, then answer.", tools=[probe_lookup], model=ctx["model"],
                session_id=ctx["sid"]("tool")) as a:
        a.run("What does the term 'zeta' mean? Use the tool.", timeout_seconds=ctx["timeout"])
    return any(t.startswith("probe_lookup") for t in TRACE), f"tool calls={TRACE}"


def check_forced_tools(ctx):
    TRACE.clear()
    with _agent(instructions="Answer briefly.", tools=[probe_clock, probe_lookup], forced_tools=["probe_clock"], model=ctx["model"],
                session_id=ctx["sid"]("forced")) as a:
        a.run("Look up the term 'alpha'.", timeout_seconds=ctx["timeout"])
    return "probe_clock" in TRACE, f"tool calls={TRACE}"


def check_output_type(ctx):
    schema = json.dumps(Verdict.model_json_schema(), separators=(",", ":"))
    with _agent(instructions="Reply with only a JSON object that matches the given schema.", output_type=Verdict, model=ctx["model"],
                session_id=ctx["sid"]("typed")) as a:
        r = a.run(f"Schema: {schema} -- Question: is the sky blue on a clear day?", timeout_seconds=ctx["timeout"])
    return isinstance(r.output, Verdict), f"output={r.output!r}"[:120]


def check_sessions(ctx):
    sid = ctx["sid"]("session")
    with _agent(instructions="Answer briefly.", model=ctx["model"], session_id=sid) as a:
        r1 = a.run("Remember the word 'cobalt'.", timeout_seconds=ctx["timeout"])
        r2 = a.run("Which word did I ask you to remember?", timeout_seconds=ctx["timeout"])
    ok = r1.session_id == r2.session_id == sid and r1.run_id != r2.run_id
    return ok, f"session={r2.session_id} runs={r1.run_id}->{r2.run_id} reply2={r2.text[:50]!r}"


def check_run_stream(ctx):
    events = []
    with _agent(instructions="Use probe_lookup, then answer.", tools=[probe_lookup], model=ctx["model"], session_id=ctx["sid"]("stream")) as a:
        a.run_stream("Look up 'beta'.", on_delta=events.append, timeout_seconds=ctx["timeout"])
    tool_events = [e for e in events if "tool_started" in repr(e) or "tool_completed" in repr(e)]
    phases = {str(getattr(e, "label", "") or "") for e in events} - {""}
    return bool(tool_events), f"{len(events)} events, {len(tool_events)} tool events, phases={sorted(phases)[:4]}"


def check_subagent(ctx):
    from openminion import subagent
    TRACE.clear()
    with _agent(instructions="Coordinate.", model=ctx["model"], name="parent", session_id=ctx["sid"]("parent")) as parent:
        child = subagent(parent, name="child", instructions="Use probe_lookup, then answer.", tools=[probe_lookup], model=ctx["model"],
                         timeout_seconds=int(ctx["timeout"]))
        child.run("Look up 'gamma'.", session_id=ctx["sid"]("child"), timeout_seconds=ctx["timeout"])
        lineage = child.subagent_context
    ok = lineage is not None and lineage.parent_agent_id == "parent" and lineage.tool_allowlist == ("probe_lookup",) and \
        any(t.startswith("probe_lookup") for t in TRACE)
    return ok, f"lineage={getattr(lineage, 'child_run_id', None)} allowlist={getattr(lineage, 'tool_allowlist', None)} calls={TRACE}"


def check_handoff(ctx):
    from openminion import Handoff
    ran = []

    # A real Agent whose run() is wrapped so the probe can see that the handoff reached it.
    peer = _agent(instructions="Answer in one word.", model=ctx["model"], name="peer", session_id=ctx["sid"]("peer"))
    real_run = peer.run
    peer.run = lambda message, **kw: (ran.append(message), real_run(message, **kw))[1]
    with _agent(instructions="Hand every question to the peer with transfer_to_peer, then repeat its answer.",
                handoffs=[Handoff(target=peer, name="transfer_to_peer", description="Ask the peer agent.")],
                model=ctx["model"], session_id=ctx["sid"]("handoff")) as a:
        # A Handoff is classed as a local write of unknown reversibility, so it needs an approval (finding F8).
        asked = []
        a.run("Ask the peer what colour grass is.", approval_callback=lambda n, args, i: (asked.append(n), n.startswith("transfer_to_"))[1],
              timeout_seconds=ctx["timeout"])
    peer.close()
    return bool(ran), f"peer received {len(ran)} message(s); approval asked for {asked}"


def _approval(ctx, answer: bool):
    TRACE.clear()
    asked = []
    with _agent(instructions="Call probe_write once with the note, then reply with what it returned.", tools=[probe_write],
                model=ctx["model"], session_id=ctx["sid"](f"approve-{answer}")) as a:
        a.run("Write the note 'probe'.", approval_callback=lambda name, args, aid: (asked.append(name), answer)[1],
              timeout_seconds=ctx["timeout"])
    wrote = any(t.startswith("probe_write") for t in TRACE)
    return asked, wrote


def check_approval_deny(ctx):
    asked, wrote = _approval(ctx, False)
    return bool(asked) and not wrote, f"asked={asked} executed={wrote}"


def check_approval_allow(ctx):
    asked, wrote = _approval(ctx, True)
    return bool(asked) and wrote, f"asked={asked} executed={wrote}"


def check_memory_bundle(ctx):
    from openminion.modules.memory.portability.bundle import export_bundle, load_bundle, save_bundle
    items = [{"command_key": "k1", "title": "t", "signature": "s"}]
    with tempfile.TemporaryDirectory(prefix="probe-mem-", dir=ctx["workdir"]) as d:
        p = Path(d) / "m.json"
        save_bundle(export_bundle(items, metadata={"tool": "probe"}), p)
        back = load_bundle(p)
    return list(back.items) == items and back.metadata.get("tool") == "probe", f"items={len(back.items)}"


def check_gateway_failover(ctx):
    gw = ctx["gateway"]
    if gw is None:
        return False, "needs a gateway backend (portal or loopback)"
    if gw.upstream == "loopback":
        gw.fail_plan[ctx["model"]] = 1            # the next request to the default model gets a 503
    before = len(gw.entries)
    with _agent(instructions="Answer briefly.", model=ctx["model"], session_id=ctx["sid"]("failover")) as a:
        r = a.run("Say ok.", timeout_seconds=ctx["timeout"])
    new = gw.entries[before:]
    rerouted = [e for e in new if len(e["attempts"]) > 1]
    ok = bool(r.text) and (bool(rerouted) if gw.upstream == "loopback" else all(e["status"] == 200 for e in new))
    first = rerouted[0]["attempts"] if rerouted else []
    return ok, f"{len(new)} requests; first reroute: " + " -> ".join(f"{x['model']}:{x['status']}" for x in first)


def check_provider_error(ctx):
    from openminion import ProviderError
    gw = ctx["gateway"]
    if gw is None or gw.upstream != "loopback":
        return False, "loopback only (plants a 401)"
    gw.router.models.append("auth-fail")
    gw.router.__post_init__()
    try:
        with _agent(instructions="Answer.", model="auth-fail", session_id=ctx["sid"]("autherr")) as a:
            a.run("Say ok.", timeout_seconds=ctx["timeout"])
    except ProviderError as exc:
        msg = str(exc)
        return "loop-auth-fail" in msg, f"ProviderError surfaced, request id in message: {'loop-auth-fail' in msg}"
    finally:
        gw.router.models.remove("auth-fail")
    return False, "no ProviderError raised for a 401"


CHECKS = {
    "agent_run": check_agent_run, "tool_call": check_tool_call, "forced_tools": check_forced_tools,
    "output_type": check_output_type, "sessions": check_sessions, "run_stream": check_run_stream, "subagent": check_subagent,
    "handoff": check_handoff, "approval_deny": check_approval_deny, "approval_allow": check_approval_allow,
    "memory_bundle": check_memory_bundle, "gateway_failover": check_gateway_failover, "provider_error": check_provider_error,
}


def main(argv: list[str] | None = None) -> int:
    from .cli import model_backend
    ap = argparse.ArgumentParser(prog="repro-scribe probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=("loopback", "portal", "config"), default="loopback")
    ap.add_argument("--models", default="oss-20b,gemma4")
    ap.add_argument("--only", default="", help="comma list of checks: " + ",".join(CHECKS))
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--home", default=None)
    a = ap.parse_args(argv)
    names = [n for n in (a.only.split(",") if a.only else CHECKS) if n]
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        ap.error(f"unknown checks: {unknown}")
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    tag = time.strftime("%H%M%S", time.gmtime())
    rows = []
    workdir = Path(tempfile.mkdtemp(prefix="repro-scribe-probe-", dir=str(Path.cwd())))
    with model_backend(a.backend, models, a.ledger, a.home or str(workdir / "home")) as gw:
        ctx = {"model": models[0] if a.backend != "config" else None, "timeout": a.timeout, "gateway": gw, "workdir": str(workdir),
               "sid": lambda s: f"probe-{s}-{tag}"}
        for name in names:
            before = len(gw.entries) if gw else 0
            t0 = time.monotonic()
            try:
                ok, detail = CHECKS[name](ctx)
            except Exception as exc:  # a crash is a FAIL with its type, never a skip
                ok, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
            new = gw.entries[before:] if gw else []
            row = {"check": name, "ok": bool(ok), "seconds": round(time.monotonic() - t0, 1), "model_requests": len(new),
                   "request_ids": [x["request_id"] for e in new for x in e["attempts"] if x.get("request_id")][:3], "detail": detail}
            rows.append(row)
            if not a.json:
                print(f"{'PASS' if ok else 'FAIL'}  {name:<17} {row['seconds']:>6.1f}s  {row['model_requests']:>3} req  {detail[:90]}", flush=True)
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
    summary = {"backend": a.backend, "models": models, "passed": sum(r["ok"] for r in rows), "total": len(rows), "checks": rows}
    if a.json:
        print(json.dumps(summary, indent=1))
    else:
        print(f"{summary['passed']}/{summary['total']} passed ({a.backend})")
    return 0 if summary["passed"] == summary["total"] else 1
