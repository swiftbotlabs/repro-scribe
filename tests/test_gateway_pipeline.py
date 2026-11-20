"""Offline tests of the gateway (routing, retries, ledger), the consensus rules, the full SDK pipeline on the loopback model, and
the probe matrix. No key and no network: the loopback model is scripted and GitHub search reads recorded fixtures."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from repro_scribe import cli, probe
from repro_scribe.gateway import Gateway, Router, describe_request, loopback_reply
from repro_scribe.tools import CALLS


# ---------------------------------------------------------------- router
def test_router_prefers_requested_model_then_fastest_healthy():
    r = Router(["a", "b", "c"])
    r.record("b", True, 9.0)
    r.record("c", True, 2.0)
    assert r.plan("a", 3) == ["a", "c", "b"]


def test_router_skips_an_open_circuit_and_keeps_it_as_last_resort():
    r = Router(["a", "b"], cooldown_s=60)
    r.record("a", False, 1.0, "502")
    assert r.plan("a", 2) == ["b", "a"]
    r.record("a", True, 1.0)           # success closes the circuit
    assert r.plan("a", 1) == ["a"]


def test_describe_request_names_openminion_steps():
    tools = [{"type": "function", "function": {"name": "submit_output", "parameters": {"properties": {"next_action": {}}}}}]
    d = describe_request({"messages": [{"role": "user", "content": "x"}], "tools": tools,
                          "tool_choice": {"type": "function", "function": {"name": "submit_output"}}})
    assert d["step"] == "structured: completion critic" and d["forced"] == "submit_output"
    assert describe_request({"messages": [], "tools": [{"function": {"name": "reproduce"}}], "tool_choice": "auto"})["step"] == "tool loop"


# ---------------------------------------------------------------- gateway transport
class _Flaky(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Flaky.calls.append(body["model"])
        if body["model"] == "bad":
            self.send_response(502); self.send_header("x-request-id", "gw-bad-1"); self.send_header("content-length", "2")
            self.end_headers(); self.wfile.write(b"{}"); return
        out = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}).encode()
        self.send_response(200); self.send_header("x-request-id", "gw-good-1"); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_gateway_retries_on_502_reroutes_and_logs_request_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTENSOR_API_KEY", "test")
    srv = HTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Flaky.calls.clear()
    ledger = tmp_path / "l.jsonl"
    try:
        with Gateway(upstream=f"http://127.0.0.1:{srv.server_address[1]}/v1", models=["bad", "good"], ledger=ledger, backoff_s=0) as gw:
            status, payload, _, headers = gw.handle({"model": "bad", "messages": [{"role": "user", "content": "hi"}]})
    finally:
        srv.shutdown()
    assert status == 200 and _Flaky.calls == ["bad", "good"] and headers["x-request-id"] == "gw-good-1"
    entry = json.loads(ledger.read_text().splitlines()[0])
    assert entry["served"] == "good" and [a["request_id"] for a in entry["attempts"]] == ["gw-bad-1", "gw-good-1"]
    assert "hi" not in ledger.read_text()                      # the ledger keeps shapes and ids, never message text


def test_gateway_does_not_retry_a_client_error(monkeypatch):
    with Gateway(upstream="loopback", models=["auth-fail", "b"]) as gw:
        status, *_ = gw.handle({"model": "auth-fail", "messages": []})
        assert status == 401 and len(gw.entries[0]["attempts"]) == 1


def test_loopback_puts_task_update_on_the_first_tool_call_only():
    body = {"messages": [{"role": "system", "content": "include task_update"}, {"role": "user", "content": "Failing command: python -c 1 -- x"}],
            "tools": [{"function": {"name": "reproduce", "parameters": {"properties": {"command": {"type": "string"}}}}}]}
    args = json.loads(loopback_reply(body)["tool_calls"][0]["function"]["arguments"])
    assert args["command"] == "python -c 1" and args["task_update"]["operation"] == "start"


# ---------------------------------------------------------------- consensus
def test_consensus_needs_every_hunter_to_say_dup():
    assert cli.consensus([{1: "DUP", 2: "RELATED"}, {1: "DUP"}]) == {1: "DUP", 2: "RELATED"}
    assert cli.consensus([{1: "DUP"}, {1: "RELATED"}]) == {1: "RELATED"}
    assert cli.consensus([]) == {}


def test_hunter_numbers_reads_record_verdict_and_drops_numbers_it_never_searched():
    calls = [{"tool": "find_existing_issues", "args": {}, "result": {"items": [{"number": 5, "url": "u5"}]}},
             {"tool": "record_verdict", "args": {"lines": "DUP #5\nDUP #99"}, "result": {}}]
    assert cli.hunter_numbers("closure summary without numbers", calls) == {5: "DUP"}


# ---------------------------------------------------------------- the full pipeline, offline
@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENMINION_HOME", raising=False)
    return tmp_path


def _run(*extra):
    return cli.main(["--backend", "loopback", "--quiet", "--command", "openminion doctor --json",
                     "--note", "I expect JSON on stdout", *extra])


def test_pipeline_saves_a_checked_report_with_consensus_and_approval(workdir):
    assert _run("--approve", "yes", "--consensus", "2") == 0
    trace = json.loads(next((workdir / "reports").glob("*.trace.json")).read_text())
    assert trace["checks"] == ["ok"] and trace["consensus"]["573"] == "DUP" and len(trace["hunter_verdicts"]) == 2
    tools = [c["tool"] for c in trace["calls"]]
    assert tools[0] == "reproduce"                                   # forced_tools
    assert tools.count("record_verdict") == 2                        # one per hunter (subagents)
    assert any(c["tool"] == "approval" and c["args"]["tool"] == "save_draft" for c in CALLS)
    md = next(p for p in (workdir / "reports").glob("*.md") if not p.name.endswith(".trace.json"))
    assert "#573" in md.read_text() and "Reproduced here: **yes** (exit code 2)" in md.read_text()
    ledger = [json.loads(l) for l in Path(trace["ledger"]).read_text().splitlines()]
    assert len(ledger) >= 20 and {e["served"] for e in ledger} == {"oss-20b", "gemma4"}


def test_a_human_no_means_nothing_is_saved_or_remembered(workdir):
    assert _run("--approve", "no") == 6
    assert not list((workdir / "reports").glob("*.md")) and not (workdir / "reports" / "memory.bundle.json").exists()


def test_memory_answers_a_repeat_without_any_model_call(workdir, capsys):
    assert _run("--approve", "yes") == 0
    capsys.readouterr()
    assert _run("--approve", "yes") == 5
    assert "already drafted" in capsys.readouterr().out


def test_a_failure_that_does_not_reproduce_goes_to_the_needs_info_peer(workdir):
    rc = cli.main(["--backend", "loopback", "--quiet", "--approve", "yes", "--command", "python -c \"print('fine')\""])
    assert rc == 0
    trace = json.loads(next((workdir / "reports").glob("*.trace.json")).read_text())
    assert trace["handoff_used"] is True
    md = next(p for p in (workdir / "reports").glob("*.md") if not p.name.endswith(".trace.json")).read_text()
    assert "did not reproduce" in md and "Questions for the reporter" in md


def test_probe_matrix_passes_offline(workdir):
    assert probe.main(["--backend", "loopback"]) == 0
