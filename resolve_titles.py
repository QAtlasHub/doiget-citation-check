#!/usr/bin/env python3
"""Resolve each reference's registered TITLE and emit `{ref, title}` JSON-Lines.

`doiget verify` reports only whether a reference resolves. That is not enough on its own: a
one-character slip in a DOI usually lands on somebody else's real paper, which resolves, so
every check built on resolution alone passes it. `verify_references_gate.py --titles` compares
titles to see that; this is what hands it the resolved side.

Reads `doiget verify`'s JSON-Lines on stdin and shells out to `doiget cite`, so resolution goes
through doiget's own resolver and cache rather than a second HTTP client. A reference whose
title cannot be obtained is simply omitted — the gate skips what it has no counterpart for,
because "could not resolve the title" is the transient case, not a wrong citation.
"""
import json
import re
import subprocess
import sys

# `title = {...}` in the BibTeX `doiget cite` prints. Non-greedy to the first closing brace at
# depth zero is enough: doiget emits one field per line.
_TITLE_RE = re.compile(r"^\s*title\s*=\s*\{(.*)\}\s*,?\s*$", re.MULTILINE)

BROKEN = {"illegal", "absent"}


def title_of(ref, timeout):
    try:
        out = subprocess.run(
            ["doiget", "cite", ref],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _TITLE_RE.search(out)
    return " ".join(m.group(1).split()) if m else None


def main():
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    seen = set()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ref = (rec.get("ref") or "").strip()
        # Only entries that resolved: a broken one already fails, and asking again wastes a
        # request per entry on the very refs that cannot answer.
        if not ref or rec.get("status") in BROKEN or ref.lower() in seen:
            continue
        seen.add(ref.lower())
        t = title_of(ref, timeout)
        if t:
            print(json.dumps({"ref": ref, "title": t}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
