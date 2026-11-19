"""The structured bug report the agent must return, and its Markdown rendering."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PossibleDuplicate(BaseModel):
    number: int
    url: str
    why: str = Field(description="One sentence: same symptom, or only related")


class BugReport(BaseModel):
    title: str = Field(description="Short, specific: component + symptom")
    summary: str
    reproduction_command: str
    reproduced: bool = Field(description="True only if the reproduce tool showed the failure")
    exit_code: int | None = None
    expected: str
    actual: str = Field(description="What happened, quoting the decisive output line")
    environment: str = Field(description="One line from capture_environment")
    possible_duplicates: list[PossibleDuplicate] = []
    severity: str = Field(description="blocker | major | minor | cosmetic")
    suggested_fix: str = Field(description="Where in the code or docs to look, or 'unknown'")


def to_markdown(r: BugReport, raw_output: str = "") -> str:
    dups = "\n".join(f"- #{d.number} {d.url}: {d.why}" for d in r.possible_duplicates) or "- none found"
    out = (f"## {r.title}\n\n{r.summary}\n\n### Reproduce\n\n```bash\n{r.reproduction_command}\n```\n\n"
           f"Reproduced here: **{'yes' if r.reproduced else 'no'}** (exit code {r.exit_code})\n\n"
           f"### Expected\n\n{r.expected}\n\n### Actual\n\n{r.actual}\n\n")
    if raw_output:
        out += f"<details><summary>Output (tail)</summary>\n\n```text\n{raw_output}\n```\n\n</details>\n\n"
    out += f"### Environment\n\n{r.environment}\n\n### Possible duplicates\n\n{dups}\n\n### Severity\n\n{r.severity}\n\n### Suggested fix\n\n{r.suggested_fix}\n"
    return out
