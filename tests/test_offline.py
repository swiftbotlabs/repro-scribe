"""Offline tests: the tools and the report cross-check, no model and no network (search is stubbed)."""
from repro_scribe import tools
from repro_scribe.cli import check
from repro_scribe.report import BugReport, PossibleDuplicate, to_markdown


def _report(**kw):
    base = dict(title="t", summary="s", reproduction_command="python -c 1", reproduced=True, exit_code=3, expected="e",
                actual="a", environment="env", possible_duplicates=[], severity="minor", suggested_fix="unknown")
    base.update(kw)
    return BugReport(**base)


def test_refuses_non_allowlisted_programs():
    assert tools.run_command("bash -c 'rm -rf /'")["refused"] is True
    assert tools.run_command("sh -c ls")["refused"] is True


def test_runs_python_in_a_temp_dir_and_reports_exit_code():
    r = tools.run_command('python -c "import os,sys; print(os.getcwd()); sys.exit(3)"')
    assert r["exit_code"] == 3 and "repro-" in r["stdout_tail"]


def test_timeout_is_reported_not_raised():
    r = tools.run_command('python -c "import time; time.sleep(5)"', timeout_s=1)
    assert r["exit_code"] is None and "timed out" in r["stderr_tail"]


def test_check_accepts_a_consistent_report():
    tools.CALLS[:] = [{"tool": "reproduce", "args": {}, "result": {"exit_code": 3, "stderr_tail": ""}},
                      {"tool": "find_existing_issues", "args": {}, "result": {"items": [{"number": 7, "url": "u7"}]}}]
    assert check(_report(possible_duplicates=[PossibleDuplicate(number=7, url="u7", why="same")])) == []


def test_check_rejects_invented_duplicates_and_wrong_exit_codes():
    tools.CALLS[:] = [{"tool": "reproduce", "args": {}, "result": {"exit_code": 2, "stderr_tail": ""}},
                      {"tool": "find_existing_issues", "args": {}, "result": {"items": [{"number": 7, "url": "u7"}]}}]
    problems = check(_report(exit_code=3, possible_duplicates=[PossibleDuplicate(number=99, url="u99", why="x")]))
    assert any("#99" in p for p in problems) and any("exit code" in p for p in problems)


def test_check_rejects_a_report_without_a_reproduction():
    tools.CALLS[:] = []
    assert "the model never ran the reproduction" in check(_report())


def test_markdown_has_the_sections_maintainers_need():
    md = to_markdown(_report(), raw_output="Traceback ...")
    for h in ("### Reproduce", "### Expected", "### Actual", "### Environment", "### Possible duplicates", "Traceback"):
        assert h in md
