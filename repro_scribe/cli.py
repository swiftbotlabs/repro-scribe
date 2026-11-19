"""repro-scribe: run one failing command, check it, search for duplicates, and write a ready-to-file bug report.

  python -m repro_scribe.cli --command "openminion doctor --json" --note "doctor crashes without a config" [--out reports/x.md]

The model (any OpenMinion-configured provider) decides which tools to call and writes the report; the CLI then checks the report
against the recorded tool results (did the command really fail? are the duplicate links real search hits?) and refuses to save a
report that claims more than the tools showed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from openminion import Agent, AgentOutputValidationError, ProviderError, TurnTimeoutError

from .report import BugReport, to_markdown
from .tools import CALLS, capture_environment, find_existing_issues, reproduce

# Kept under ~600 characters on purpose: OpenMinion caps Agent instructions at a small identity token budget and cuts the rest
# with "...[truncated]" (finding F3 in WHAT_WE_LEARNED.md). The full rules live in the tool docstrings and the BugReport schema.
INSTRUCTIONS = """You investigate OpenMinion bugs. Steps: 1) capture_environment; 2) reproduce with exactly the given command;
3) find_existing_issues with 2-5 distinctive words from the error. Then summarise in 3-5 lines: did it fail, the decisive
output line, likely duplicates (number + url), likely cause. Facts only.
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
        failed = last.get("refused") is not True and (last.get("exit_code") not in (0,) or "Traceback" in (last.get("stderr_tail") or ""))
        if report.reproduced and not failed and not report.actual:
            problems.append("report says reproduced, but the command succeeded")
        if report.exit_code is not None and last.get("exit_code") is not None and report.exit_code != last["exit_code"]:
            problems.append(f"exit code {report.exit_code} in the report, {last['exit_code']} from the tool")
    seen = {i["number"]: i["url"] for c in CALLS if c["tool"] == "find_existing_issues" for i in c["result"].get("items", [])}
    for d in report.possible_duplicates:
        if seen.get(d.number) != d.url:
            problems.append(f"duplicate #{d.number} was not a search result")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--command", required=True, help="the failing command (openminion ... or python ...)")
    ap.add_argument("--note", default="", help="what you expected / what went wrong, in your words")
    ap.add_argument("--out", default=None, help="Markdown file to write (default reports/<slug>.md)")
    ap.add_argument("--agent", default=None, help="OpenMinion agent/profile id (default: the configured default)")
    ap.add_argument("--session", default="repro-scribe", help="session id (reports in one session share context)")
    a = ap.parse_args(argv)
    CALLS.clear()
    t0 = time.monotonic()
    # One line: the runtime checks that task_update.user_quote is a verbatim substring of the input, so keep the input simple to quote.
    prompt = f"Failing command: {a.command} -- Reporter's note: {a.note or '(none)'}"
    # Two passes. 1) The investigator: an Agent with tools gathers the evidence (environment, reproduction, duplicate search).
    # 2) The writer: a tool-less Agent with output_type=BugReport turns that evidence into the report. OpenMinion validates
    # output_type but does not send its schema to the model, and with tools the turn's final text is a closure summary rather than
    # the model's JSON (finding F4), so the writer gets the schema and the evidence in its message.
    try:
        with Agent(agent_id=a.agent, instructions=INSTRUCTIONS, tools=[capture_environment, reproduce, find_existing_issues],
                   session_id=a.session) as investigator:
            notes = investigator.run(prompt)
        evidence = json.dumps([{"tool": c["tool"], "args": c["args"], "result": c["result"]} for c in CALLS], default=str)[:12000]
        schema = json.dumps(BugReport.model_json_schema(), separators=(",", ":"))
        with Agent(agent_id=a.agent, instructions=WRITER, output_type=BugReport, session_id=a.session + "-writer") as writer:
            result = writer.run(f"Schema: {schema} -- Command: {a.command} -- Reporter's note: {a.note or '(none)'} -- "
                                f"Investigator notes: {notes.text[:1500]} -- Tool results: {evidence}")
    except AgentOutputValidationError as exc:
        print(f"model output did not match the BugReport schema:\n{exc.raw_text[:2000]}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"provider failed: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except TurnTimeoutError as exc:
        print(f"model turn timed out ({exc}); try again or pick a faster model", file=sys.stderr)
        return 4
    report: BugReport = result.output
    problems = check(report)
    runs = [c for c in CALLS if c["tool"] == "reproduce"]
    raw = ""
    if runs:
        r = runs[-1]["result"]
        raw = ((r.get("stderr_tail") or "") + "\n" + (r.get("stdout_tail") or "")).strip()[-2500:]
    slug = re.sub(r"[^a-z0-9]+", "-", report.title.lower()).strip("-")[:60] or "report"
    out = Path(a.out or f"reports/{slug}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    trace = {"seconds": round(time.monotonic() - t0, 1), "tool_calls": [{"tool": c["tool"], "args": c["args"]} for c in CALLS],
             "checks": problems or ["ok"], "session_id": result.session_id}
    if problems:
        out = out.with_suffix(".REJECTED.md")
        print("report NOT accepted, it claims more than the tools showed:\n- " + "\n- ".join(problems), file=sys.stderr)
    out.write_text(to_markdown(report, raw) + "\n<!-- repro-scribe trace: " + json.dumps(trace) + " -->\n", encoding="utf-8")
    out.with_suffix(".trace.json").write_text(json.dumps({**trace, "calls": CALLS}, indent=1, default=str), encoding="utf-8")
    print(f"{'REJECTED' if problems else 'ok'}: {out}  ({trace['seconds']} s, {len(CALLS)} tool calls)")
    return 3 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
