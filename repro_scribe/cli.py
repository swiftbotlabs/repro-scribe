"""repro-scribe: run one failing command, check it, search for duplicates, and write a ready-to-file bug report.

  repro-scribe --command "openminion doctor --json" --note "doctor crashes without a config"            # your OpenMinion setup
  repro-scribe --backend portal --consensus 2 --command "..."     # Cortensor Portal via the routing gateway, 2-model consensus
  repro-scribe --backend loopback --approve yes --command "..."   # offline: the full pipeline with a scripted model, no key
  repro-scribe probe --backend loopback                           # the building-block probe matrix (see probe.py)

Pipeline (all OpenMinion SDK): investigator Agent (tools, forced reproduce, streamed progress, Handoff to a needs-info peer when
the failure does not reproduce) -> duplicate-hunter subagent(s) with read-only tools, one per model when --consensus 2 -> writer
Agent with output_type=BugReport -> deterministic self-check against the recorded tool calls -> publisher Agent whose only tool
writes, so OpenMinion's action policy asks a human (approval_callback) -> MemoryBundle of drafts for cross-run dedup.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

from .report import BugReport, to_markdown
from . import memory
from .tools import (CALLS, PENDING, capture_environment, find_existing_issues, find_related_prs, record_verdict, reproduce,
                    save_draft)

# Kept under ~600 characters on purpose: OpenMinion caps Agent instructions at a small identity token budget and cuts the rest
# with "...[truncated]" (finding F3, issue #811). The full rules live in the tool docstrings and the BugReport schema.
INSTRUCTIONS = """You investigate OpenMinion bugs. Steps: 1) capture_environment; 2) reproduce with exactly the given command;
3) only if reproduce exited 0, transfer_to_needs_info. Then summarise in 3-4 lines: did it fail, the decisive output line,
likely cause. Facts only. A task_update with operation start must include objective."""
NEEDS_INFO = """The reported failure did not reproduce on a clean install. Write 3 short questions for the reporter (version,
config, exact output) that would let a maintainer reproduce it. A task_update with operation start must include objective."""
HUNTER = """You look for existing reports of one failure. Call find_existing_issues with 2-5 distinctive words from the error,
find_related_prs once, then record_verdict once with one line per real match: "DUP #<number>" (same symptom) or
"RELATED #<number>" (related, or a fix in flight), or "NONE". Only numbers your searches returned.
A task_update with operation start must include objective."""
PUBLISHER = """Call save_draft exactly once with a one-line reason, then reply with what it returned.
A task_update with operation start must include objective."""
WRITER = """Turn the tool results into one bug report. Reply with only a JSON object that matches the given schema. Use only facts
from the tool results; quote the decisive output line; list only duplicates that the search returned; unknown fix = "unknown"."""


def check(report: BugReport) -> list[str]:
    """Cross-check the model's report with what the tools really returned. Returns problems (empty = consistent)."""
    problems = []
    runs = [c for c in CALLS if c["tool"] == "reproduce"]
    if not runs:
        problems.append("the model never ran the reproduction")
    else:
        last = runs[-1]["result"]
        if report.reproduced and last.get("exit_code") == 0:
            problems.append("report says reproduced, but the command succeeded")
        if report.exit_code is not None and last.get("exit_code") is not None and report.exit_code != last["exit_code"]:
            problems.append(f"exit code {report.exit_code} in the report, {last['exit_code']} from the tool")
    seen = {i["number"]: i["url"] for c in CALLS if c["tool"] in ("find_existing_issues", "find_related_prs")
            for i in c["result"].get("items", [])}
    for d in report.possible_duplicates:
        if seen.get(d.number) != d.url:
            problems.append(f"duplicate #{d.number} was not a search result")
    return problems


def hunter_numbers(text: str, calls: list[dict] | None = None) -> dict[int, str]:
    """One hunter's verdict {number: DUP|RELATED}, from its record_verdict call (or its reply text), keeping only numbers that
    its own searches returned."""
    calls = CALLS if calls is None else calls
    seen = {i["number"] for c in calls if c["tool"] in ("find_existing_issues", "find_related_prs") for i in c["result"].get("items", [])}
    recorded = "\n".join(c["args"]["lines"] for c in calls if c["tool"] == "record_verdict")
    out: dict[int, str] = {}
    for kind, num in re.findall(r"\b(DUP|RELATED)\s*#?(\d+)", (recorded or "") + "\n" + (text or ""), re.I):
        if int(num) in seen and out.get(int(num)) != "DUP":
            out[int(num)] = kind.upper()
    return out


def consensus(verdicts: list[dict[int, str]]) -> dict[int, str]:
    """Combine independent hunters (one per model), like redundant miners on Cortensor: a number is DUP only if every hunter
    says DUP; any number at least one hunter confirmed stays RELATED."""
    if not verdicts:
        return {}
    numbers = set().union(*verdicts)
    return {n: "DUP" if all(v.get(n) == "DUP" for v in verdicts) else "RELATED" for n in sorted(numbers)}


def disagreements(report: BugReport, verdict: dict[int, str]) -> list[str]:
    """The writer may list only duplicates the independent hunter(s) confirmed (DUP or RELATED)."""
    return [f"duplicate #{d.number} was not confirmed by the duplicate hunter" for d in report.possible_duplicates
            if d.number not in verdict]


def ask_human(mode: str):
    """approval_callback for OpenMinion: --approve yes|no, or ask on the terminal (no terminal = no)."""
    def cb(tool_name: str, args: dict, approval_id: str) -> bool:
        if mode in ("yes", "no"):
            ok = mode == "yes"
        elif not sys.stdin.isatty():
            ok = False
        else:
            ok = input(f"\nSave the draft '{PENDING.get('title', '?')}' to {PENDING.get('path', '?')}? [y/N] ").strip().lower() in ("y", "yes")
        CALLS.append({"tool": "approval", "args": {"tool": tool_name, "id": approval_id}, "result": {"approved": ok, "mode": mode}})
        return ok
    return cb


def speech(text: str) -> str:
    """OpenMinion prefixes every reply with "<agent id>: " (e.g. "openminion: ..."); drop it for reports."""
    return re.sub(r"^[\w.-]{1,40}:\s", "", (text or "").strip(), count=1)


def allow_only(*names: str):
    """approval_callback that allows only the named tools (logged), and denies every other tool."""
    def cb(tool_name: str, args: dict, approval_id: str) -> bool:
        ok = tool_name in names
        CALLS.append({"tool": "approval", "args": {"tool": tool_name, "id": approval_id}, "result": {"approved": ok, "mode": "policy"}})
        return ok
    return cb


class Progress:
    """on_delta sink for Agent.run_stream: one terminal line per runtime event, and a count for the trace."""

    def __init__(self, label: str, quiet: bool = False):
        self.label, self.quiet, self.events, self.t0 = label, quiet, [], time.monotonic()

    def __call__(self, event: object) -> None:
        kind = None
        for attr in ("label", "status_key", "type", "kind", "event"):
            kind = getattr(event, attr, None) or (event.get(attr) if isinstance(event, dict) else None)
            if kind:
                break
        kind = str(getattr(kind, "value", kind) or type(event).__name__)
        tool = getattr(event, "tool_name", None) or (event.get("tool_name") or event.get("tool") if isinstance(event, dict) else None)
        line = f"{kind} {tool}" if tool else kind
        repeat = bool(self.events) and self.events[-1] == line
        self.events.append(line)
        if not self.quiet and not repeat:
            print(f"  [{self.label} +{time.monotonic() - self.t0:5.1f}s] {line}", file=sys.stderr, flush=True)


@contextlib.contextmanager
def model_backend(backend: str, models: list[str], ledger: str | None, home: str | None):
    """config: the user's own OpenMinion setup. portal / loopback: a local gateway (gateway.py) in front of Cortensor Portal or
    of the scripted offline model, with an OpenMinion home written by `openminion setup --provider cortensor-portal`."""
    if backend == "config":
        yield None
        return
    from .gateway import Gateway, write_home
    if backend == "loopback":
        os.environ.setdefault("REPRO_SCRIBE_SEARCH_FIXTURE", str(Path(__file__).with_name("fixtures") / "search.json"))
        os.environ.setdefault("CORTENSOR_API_KEY", "loopback")   # OpenMinion wants a key; the scripted model ignores it
    elif not os.environ.get("CORTENSOR_API_KEY"):
        raise SystemExit("--backend portal needs CORTENSOR_API_KEY (a free Portal key: portal.cortensor.network)")
    with Gateway(upstream=backend, models=models, ledger=ledger) as gw:
        h = Path(home or f".repro-scribe/home-{backend}").resolve()
        write_home(h, gw.base_url, models)
        old = os.environ.get("OPENMINION_HOME")
        os.environ["OPENMINION_HOME"] = str(h)
        try:
            yield gw
        finally:
            if old is None:
                os.environ.pop("OPENMINION_HOME", None)
            else:
                os.environ["OPENMINION_HOME"] = old


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["probe"]:
        from .probe import main as probe_main
        return probe_main(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--command", required=True, help="the failing command (openminion ... or python ...)")
    ap.add_argument("--note", default="", help="one sentence: what you expected instead")
    ap.add_argument("--out", default=None, help="Markdown file to write (default reports/<slug>.md)")
    ap.add_argument("--agent", default=None, help="OpenMinion agent/profile id (default: the configured default)")
    ap.add_argument("--session", default="repro-scribe", help="session id (reports in one session share context)")
    ap.add_argument("--approve", choices=("ask", "yes", "no"), default="ask", help="saving the draft needs a human yes (ask), or yes/no for CI")
    ap.add_argument("--memory", default=str(memory.DEFAULT), help="MemoryBundle of earlier drafts (cross-run dedup)")
    ap.add_argument("--again", action="store_true", help="run even if this command was drafted before")
    ap.add_argument("--backend", choices=("config", "portal", "loopback"), default="config",
                    help="config = your OpenMinion setup; portal = Cortensor Portal through the routing gateway; loopback = offline")
    ap.add_argument("--models", default="oss-20b,gemma4", help="Portal aliases the gateway may route between (first = default)")
    ap.add_argument("--consensus", type=int, choices=(1, 2, 3), default=1, help="independent duplicate hunters, one per model")
    ap.add_argument("--ledger", default=None, help="JSONL of every model request (gateway backends; default runs/<time>.ledger.jsonl)")
    ap.add_argument("--home", default=None, help="OpenMinion home for the gateway backends (default .repro-scribe/home-<backend>)")
    ap.add_argument("--timeout", type=float, default=420, help="seconds per agent turn")
    ap.add_argument("--quiet", action="store_true", help="no streamed progress lines")
    a = ap.parse_args(argv)
    CALLS.clear(); PENDING.clear()
    t0 = time.monotonic()
    mem_path = Path(a.memory)
    bundle = memory.load(mem_path)
    earlier = memory.find(bundle, command=a.command)
    if earlier and not a.again:
        print(f"already drafted: {earlier['path']} ({earlier['drafted_at']}, '{earlier['title']}'). No model call made; --again to redo.")
        return 5
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    use_models = a.backend != "config"
    ledger = a.ledger or (f"runs/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{a.backend}.ledger.jsonl" if use_models else None)
    from openminion import Agent, AgentOutputValidationError, Handoff, ProviderError, TurnTimeoutError, subagent
    # One line: the runtime checks that task_update.user_quote is a verbatim substring of the input, so keep the input simple to quote.
    prompt = f"Failing command: {a.command} -- Reporter's note: {a.note or '(none)'}"
    model = models[0] if use_models else None
    progress = Progress("investigator", a.quiet)
    run_tag = time.strftime("%Y%m%dT%H%M%S", time.gmtime())   # hunters get a fresh session each run: independent judgement
    verdicts: list[dict[int, str]] = []
    needs_info, handed, sig, same, raw = "", [], None, None, ""
    with model_backend(a.backend, models, ledger, a.home) as gw:
        try:
            peer = Agent(agent_id=a.agent, instructions=NEEDS_INFO, model=model, name="needs_info", session_id=a.session + "-needs-info")
            peer_replies: list[str] = []
            peer_run = peer.run
            peer.run = lambda m, **kw: (lambda r: (peer_replies.append(r.text), r)[1])(peer_run(m, **kw))
            with Agent(agent_id=a.agent, instructions=INSTRUCTIONS, tools=[capture_environment, reproduce], forced_tools=["reproduce"],
                       handoffs=[Handoff(target=peer, name="transfer_to_needs_info",
                                         description="Use ONLY if reproduce exited 0: ask the reporter for what is missing.")],
                       model=model, session_id=a.session, name="investigator") as investigator:
                # 1) investigator, streamed: every runtime event is printed as it happens.
                # The Handoff counts as a local write of unknown reversibility (finding F8), so OpenMinion asks before it runs:
                # this callback allows exactly the needs-info delegation and denies anything else.
                notes = investigator.run_stream(prompt, on_delta=progress, timeout_seconds=a.timeout,
                                                approval_callback=allow_only("transfer_to_needs_info"))
                runs = [c for c in CALLS if c["tool"] == "reproduce"]
                last = runs[-1]["result"] if runs else {}
                raw = ((last.get("stderr_tail") or "") + "\n" + (last.get("stdout_tail") or "")).strip()[-2500:]
                sig = memory.signature(raw)
                same = memory.find(bundle, sig=sig) if sig else None
                decisive = next((l for l in reversed(raw.splitlines()) if re.search(r"error|exception|failed|not found|invalid", l, re.I)),
                                raw[-300:])
                # 2) duplicate hunters: subagents of the investigator with only read-only search tools and no parent transcript.
                hunt_models = (models * 3)[:a.consensus] if use_models else [None] * a.consensus
                for i, hm in enumerate(hunt_models):
                    mark = len(CALLS)
                    hunter = subagent(investigator, name=f"dup_hunter_{i + 1}", instructions=HUNTER, model=hm,
                                      tools=[find_existing_issues, find_related_prs, record_verdict], timeout_seconds=int(a.timeout))
                    vr = hunter.run(f"Failure to look up: {decisive.strip()[:300]} -- Command: {a.command}",
                                    session_id=f"{a.session}-hunter-{i + 1}-{run_tag}", timeout_seconds=a.timeout)
                    verdicts.append(hunter_numbers(vr.text, CALLS[mark:]))
                verdict = consensus(verdicts)
                handed = [c for c in CALLS if c["tool"] == "approval" and c["args"]["tool"] == "transfer_to_needs_info" and c["result"]["approved"]]
                if last.get("exit_code") == 0:
                    needs_info = speech(peer_replies[-1] if peer_replies else notes.text)
            evidence = json.dumps([{"tool": c["tool"], "args": c["args"], "result": c["result"]} for c in CALLS], default=str)[:12000]
            schema = json.dumps(BugReport.model_json_schema(), separators=(",", ":"))
            confirmed = ", ".join(f"#{n} {k}" for n, k in sorted(verdict.items())) or "none"
            # 3) writer: tool-less, output_type=BugReport (OpenMinion validates it; the schema goes in the message: finding F4).
            with Agent(agent_id=a.agent, instructions=WRITER, output_type=BugReport, model=model, session_id=a.session + "-writer",
                       name="writer") as writer:
                result = writer.run(f"Schema: {schema} -- Command: {a.command} -- Reporter's note: {a.note or '(none)'} -- "
                                    f"Investigator notes: {notes.text[:1500]} -- Duplicates confirmed by the hunter: {confirmed} -- "
                                    f"Tool results: {evidence}", timeout_seconds=a.timeout)
        except AgentOutputValidationError as exc:
            print(f"model output did not match the BugReport schema:\n{exc.raw_text[:2000]}", file=sys.stderr)
            return 2
        except ProviderError as exc:
            print(f"model provider error: {exc}", file=sys.stderr)
            return 1
        except TurnTimeoutError as exc:
            print(f"model turn timed out ({exc}); try again or pick a faster model", file=sys.stderr)
            return 4
        report: BugReport = result.output
        problems = check(report) + disagreements(report, verdict)
        slug = re.sub(r"[^a-z0-9]+", "-", report.title.lower()).strip("-")[:60] or "report"
        out = Path(a.out or f"reports/{slug}.md")
        if problems:
            out = out.with_suffix(".REJECTED.md")
            print("report NOT accepted, it claims more than the tools showed:\n- " + "\n- ".join(problems), file=sys.stderr)
        md = to_markdown(report, raw)
        if same:
            md += f"\n> Same failure signature as an earlier draft: {same['path']} ({same['title']}).\n"
        if needs_info:
            md += f"\n### Questions for the reporter (did not reproduce)\n\n{needs_info}\n"
        trace = {"seconds": round(time.monotonic() - t0, 1), "backend": a.backend,
                 "tool_calls": [{"tool": c["tool"], "args": c["args"]} for c in CALLS], "checks": problems or ["ok"],
                 "hunter_verdicts": [{str(k): v for k, v in v_.items()} for v_ in verdicts],
                 "consensus": {str(k): v for k, v in verdict.items()}, "handoff_used": bool(handed), "signature": sig,
                 "stream_events": len(progress.events), "session_id": result.session_id}
        if gw is not None:
            trace["ledger"] = ledger
        PENDING.update(path=str(out), title=report.title, markdown=md + "\n<!-- repro-scribe trace: " + json.dumps(trace) + " -->\n",
                       trace=json.dumps({**trace, "calls": CALLS}, indent=1, default=str))
        print(md)
        # 4) publisher: its only tool writes to disk, so OpenMinion's action policy asks the human first.
        try:
            with Agent(agent_id=a.agent, instructions=PUBLISHER, tools=[save_draft], model=model, session_id=a.session + "-publisher",
                       name="publisher") as publisher:
                publisher.run(f"Save the draft '{report.title}'.", approval_callback=ask_human(a.approve), timeout_seconds=a.timeout)
        except (ProviderError, TurnTimeoutError) as exc:
            print(f"publisher step failed ({exc}); draft not saved", file=sys.stderr)
        if gw is not None:
            s = gw.summary()
            print(f"model requests: {s['requests']} ({s['model_seconds']} s), retried {s['retried']}, rerouted {s['rerouted']}, "
                  f"failed {s['failed']}; ledger {ledger}", file=sys.stderr)
    saved = any(c["tool"] == "save_draft" and c["result"].get("saved") for c in CALLS)
    approved = [c["result"]["approved"] for c in CALLS if c["tool"] == "approval" and c["args"]["tool"] == "save_draft"]
    print(f"human approval for save_draft: {'asked, ' + ('yes' if any(approved) else 'no') if approved else 'not asked'}", file=sys.stderr)
    if saved and not problems:
        memory.remember(bundle, command=a.command, sig=sig, title=report.title, path=str(out),
                        duplicates=sorted(verdict), path_out=mem_path)
    state = "saved" if saved else ("declined by the human" if approved and not any(approved) else "not saved")
    print(f"{'REJECTED' if problems else 'ok'} ({state}): {out}  ({round(time.monotonic() - t0, 1)} s, {len(CALLS)} tool calls)")
    if not saved:
        return 6
    return 3 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
