"""What Repro Scribe already drafted, kept across runs as an OpenMinion MemoryBundle (reports/memory.bundle.json).

Two fingerprints per draft: the command (a re-run of the same command is answered from memory, with no model call) and the failure
signature (the decisive output line with numbers, paths and hex ids blanked), so a different command that fails the same way is
flagged as "same failure as an earlier draft"."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from openminion import MemoryBundle
from openminion.modules.memory.portability.bundle import export_bundle, load_bundle, save_bundle

DEFAULT = Path("reports/memory.bundle.json")


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def command_key(command: str) -> str:
    return _h(" ".join(command.split()))


def signature(output: str) -> str | None:
    """Fingerprint of the decisive line: the last line that names an error, with volatile parts blanked."""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    hits = [l for l in lines if re.search(r"error|exception|traceback|failed|not found|invalid", l, re.I)]
    if not hits:
        return None
    line = hits[-1]
    line = re.sub(r"0x[0-9a-fA-F]+|[0-9a-f]{8,}", "<id>", line)
    line = re.sub(r"(/[^\s'\"]+)+", "<path>", line)
    line = re.sub(r"\d+", "<n>", line)
    return _h(line.lower())


def load(path: Path = DEFAULT) -> MemoryBundle:
    try:
        return load_bundle(path)
    except FileNotFoundError:
        return export_bundle([], metadata={"tool": "repro-scribe", "kind": "drafted-reports"})


def find(bundle: MemoryBundle, *, command: str | None = None, sig: str | None = None) -> dict | None:
    for item in bundle.items:
        if command is not None and item.get("command_key") == command_key(command):
            return item
        if sig and item.get("signature") == sig:
            return item
    return None


def remember(bundle: MemoryBundle, *, command: str, sig: str | None, title: str, path: str, duplicates: list[int],
             path_out: Path = DEFAULT) -> MemoryBundle:
    items = [i for i in bundle.items if i.get("command_key") != command_key(command)]
    items.append({"command_key": command_key(command), "command": command, "signature": sig, "title": title, "path": path,
                  "duplicates": duplicates, "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    out = export_bundle(items, metadata=dict(bundle.metadata))
    path_out.parent.mkdir(parents=True, exist_ok=True)
    save_bundle(out, path_out)
    return out
