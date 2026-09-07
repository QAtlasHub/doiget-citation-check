#!/usr/bin/env python3
"""Resolve each reference's registered TITLE and emit `{ref, title}` JSON-Lines.

`doiget verify` reports only whether a reference resolves. That is not enough on its own: a
one-character slip in a DOI usually lands on somebody else's real paper, which resolves, so
every check built on resolution alone passes it. `verify_references_gate.py --titles` compares
titles to see that; this is what hands it the resolved side.

Reads `doiget verify`'s JSON-Lines on stdin and shells out to `doiget cite`, so resolution goes
through doiget's own resolver and cache rather than a second HTTP client.

Emits a record for every entry, including the ones whose title could not be obtained
(`title: null` plus a `reason`). A silent omission would make "the check could not run" read
exactly like "the check ran and agreed" — which for a gate is worse than not having it.
"""
import argparse
import json
import re
import subprocess
import sys

# Brace-matched across lines: a title carrying inline maths is rendered over several.
_TITLE_START = re.compile(r"\btitle\s*=\s*\{", re.IGNORECASE)

from verify_references_gate import BROKEN  # noqa: E402 — one definition, one file


def title_of(ref, timeout):
    """`(title, None)`, or `(None, reason)` — never a bare None, so a failure can be reported."""
    try:
        proc = subprocess.run(
            ["doiget", "cite", ref],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, "doiget not on PATH"
    except OSError as e:
        return None, f"doiget failed to run: {e}"
    except subprocess.TimeoutExpired:
        return None, f"doiget cite timed out after {timeout}s"
    if proc.returncode != 0:
        return None, f"doiget cite exited {proc.returncode}"
    m = _TITLE_START.search(proc.stdout)
    if not m:
        return None, "no title field in the citation"
    depth, start = 1, m.end()
    for i in range(start, len(proc.stdout)):
        if proc.stdout[i] == "{":
            depth += 1
        elif proc.stdout[i] == "}":
            depth -= 1
            if depth == 0:
                return " ".join(proc.stdout[start:i].split()), None
    return None, "unterminated title field"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per `doiget cite`")
    timeout = ap.parse_args().timeout
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
        # A broken entry already fails; asking again spends a request to learn nothing.
        if not ref or rec.get("status") in BROKEN or ref.lower() in seen:
            continue
        seen.add(ref.lower())
        t, why = title_of(ref, timeout)
        # A record either way, so the gate can tell "could not check" from "agreed".
        rec = {"ref": ref, "title": t} if t else {"ref": ref, "title": None, "reason": why}
        if not t:
            print(f"resolve_titles: {ref}: {why}", file=sys.stderr)
        print(json.dumps(rec, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
