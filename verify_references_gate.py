#!/usr/bin/env python3
"""Gate `doiget verify` output against an allowlist and emit a Markdown report.

Reads doiget's JSON-Lines `verify` output on stdin (one record per reference,
with a `status` field), plus an optional allowlist file. Classifies each entry:

    valid                        -> OK, resolves on Crossref / arXiv
    illegal | absent             -> BROKEN: malformed id, or does not resolve
                                    (fabricated / mistyped / not indexed)
    unreachable | unverifiable   -> TRANSIENT: network / 429 / no-id / coverage
                                    gap; NOT gated (would make CI flaky)

A BROKEN entry fails the gate (exit 1) UNLESS its DOI / arXiv id *or* its bibkey
appears in the allowlist — i.e. a human has explicitly vouched for it. The
allowlist (`docs/references.allow` by default) is one id/key per line, `#` starts
a comment; put the reason in the comment so every exception is self-documenting.

Completeness (the "no verification left out" guarantee): when `--bib` is given,
every reference entry in the bibliography must have produced a verify record. If
doiget aborted, timed out, or skipped an entry, that entry is UNVERIFIED and
fails the gate — a truncated run can never masquerade as a clean one. Unverified
entries are NOT allowlist-downgradable (the check simply did not run).

So a reference that does not exist — or that was never checked at all — can never
pass silently. Always writes a Markdown report (for the PR comment / job summary)
regardless of outcome.

Usage (also runnable locally):
    doiget verify docs/references.bib --mode json \\
      | python3 .github/scripts/verify_references_gate.py \\
          --bib docs/references.bib --allow docs/references.allow
"""
import argparse
import json
import os
import re
import sys

BROKEN = {"illegal", "absent"}
TRANSIENT = {"unreachable", "unverifiable"}

# `@type{key,` — a reference entry. @comment / @string / @preamble / @set are
# BibTeX machinery, not references, so they are excluded from the completeness count.
_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s}]+)", re.IGNORECASE)
_NON_REF_TYPES = {"comment", "string", "preamble", "set"}


def load_allow(path):
    allow = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                token = line.split("#", 1)[0].strip()
                if token:
                    allow.add(token.lower())
    return allow


def bib_keys(path):
    keys = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for m in _ENTRY_RE.finditer(fh.read()):
                if m.group(1).lower() not in _NON_REF_TYPES:
                    keys.append(m.group(2))
    return keys


# A valid BibTeX cite key: a letter, then letters/digits/`:_.+-/` — no whitespace, no leading
# digit. A malformed key (e.g. a leading space, `@article{ Key,`) still PASSES verification because
# `_ENTRY_RE`'s `\{\s*` silently strips the whitespace, but DocumenterCitations then rejects the
# entry ("the entry key is invalid") and every `[Key](@cite)` to it fails "not found", terminating
# the docs build. Validate the RAW key text so the defect is caught HERE instead of at doc-build time.
_RAW_ENTRY_RE = re.compile(r"^@(\w+)\s*\{([^,}\n]*)", re.IGNORECASE | re.MULTILINE)
_VALID_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9:_.+/-]*$")


def malformed_keys(path):
    """Reference entry keys that are not a well-formed BibTeX key (whitespace / bad char)."""
    bad = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for m in _RAW_ENTRY_RE.finditer(fh.read()):
                if m.group(1).lower() in _NON_REF_TYPES:
                    continue
                if not _VALID_KEY_RE.match(m.group(2)):
                    bad.append(m.group(2))
    return bad


# Titles arrive as inline MathML or HTML on one side and LaTeX on the other; strip both.
_TAG_RE = re.compile(r"<[^>]*>")
_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_LATEX_RE = re.compile(r"\\[a-zA-Z]+")
# Where a human abbreviates, they drop a subtitle, which begins at one of these.
_SUBTITLE_RE = re.compile(r"[:.;\u2014\u2013]|\s[-(\[]")


def normalise_title(s):
    # Whitespace is dropped, not normalised: resolvers can run words together.
    s = _LATEX_RE.sub(" ", _ENTITY_RE.sub(" ", _TAG_RE.sub(" ", s)))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _subtitle_prefixes(title):
    """Every prefix of `title` that ends where a subtitle begins, normalised."""
    return {normalise_title(title[: m.start()]) for m in _SUBTITLE_RE.finditer(title)}


def titles_agree(declared, resolved):
    """Whether the two name the same paper.

    Neither side being a prefix of the other is a wrong id. Both directions of prefix do
    occur and are not, but for DIFFERENT reasons, so they get different rules:

      * the bibliography abbreviates by dropping a SUBTITLE, so its title must end where one
        begins — otherwise "Quantum Phase Transitions" would match an unrelated paper that
        merely opens with those words;
      * a publisher truncates its own metadata at an arbitrary point, mid-word included, so
        there is no boundary to require.
    """
    d, r = normalise_title(declared), normalise_title(resolved)
    if not d or not r:
        return False
    if d == r:
        return True
    if len(d) < len(r):
        return r.startswith(d) and d in _subtitle_prefixes(resolved)
    return d.startswith(r)


# Brace-matched, not regex-terminated: a title value may itself contain braces, and BibTeX
# puts no constraint on line breaks.
_TITLE_START_RE = re.compile(r"\btitle\s*=\s*", re.IGNORECASE)


def _braced_value(text, pos):
    """The `{...}` or `"..."` value starting at `pos`, or None."""
    if pos >= len(text):
        return None
    if text[pos] == '"':
        end = text.find('"', pos + 1)
        return None if end < 0 else text[pos + 1 : end]
    if text[pos] != "{":
        return None
    depth = 0
    for i in range(pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i]
    return None


def bib_titles(path):
    """`{bibkey: title}` for every entry that declares one."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    out = {}
    for block in re.split(r"(?=^@)", text, flags=re.MULTILINE):
        m = _RAW_ENTRY_RE.match(block)
        if not m or m.group(1).lower() in _NON_REF_TYPES:
            continue
        t = _TITLE_START_RE.search(block)
        if not t:
            continue
        val = _braced_value(block, t.end())
        if val is not None:
            out[m.group(2).strip()] = " ".join(val.split())
    return out


def load_titles(path):
    """`{ref: title or None}` from the JSONL the action writes with `doiget cite`."""
    if not path:
        return {}
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref = (rec.get("ref") or "").lower()
                if ref:
                    out[ref] = rec.get("title") or None
    except OSError:
        return {}
    return out


def detail_of(entry):
    err = entry.get("error")
    if isinstance(err, dict):
        return str(err.get("message", ""))[:90]
    return ""


def render(
    ok, transient, excepted, broken, unverified, malformed=(), mistitled=(),
    inconclusive=(), compared=0, titles_on=False,
):
    out = ["<!-- verify-references-gate -->", "## Reference check — `doiget verify`", ""]
    out.append(f"- ✅ resolved: **{len(ok)}**")
    if transient:
        out.append(f"- ⚠️ transient (network / no-id / not-yet-indexed — not gated): **{len(transient)}**")
    if excepted:
        out.append(f"- 🟡 allowlisted exceptions: **{len(excepted)}**")
    out.append(f"- {'❌' if broken else '☑️'} broken (unresolved / malformed): **{len(broken)}**")
    if unverified:
        out.append(f"- ❌ unverified (no record — check did not run): **{len(unverified)}**")
    if malformed:
        out.append(f"- ❌ malformed bib keys (invalid — DocumenterCitations will reject): **{len(malformed)}**")
    if mistitled:
        out.append(f"- ❌ wrong paper (resolves, but not to this title): **{len(mistitled)}**")
    if titles_on:
        # The denominator: without it, a run that compared nothing renders as one that agreed.
        out.append(f"- 🔎 titles compared: **{compared}** of {len(ok)} resolved entries")
    if inconclusive:
        out.append(
            f"- ⚠️ title check inconclusive (no title resolved — not gated): "
            f"**{len(inconclusive)}**"
        )
    out.append("")

    def table(title, rows):
        if not rows:
            return []
        block = [f"### {title}", "", "| bibkey | ref | status | detail |", "|---|---|---|---|"]
        for e in rows:
            block.append(
                f"| `{e.get('entry_key') or ''}` | `{e.get('ref') or ''}` | "
                f"{e.get('status') or ''} | {detail_of(e)} |"
            )
        block.append("")
        return block

    out += table("❌ Broken references — fix the id, or add an explicit exception", broken)
    if unverified:
        out += ["### ❌ Unverified references — the check did not run on these", ""]
        out += ["| bibkey |", "|---|"]
        out += [f"| `{k}` |" for k in unverified]
        out += [""]
    if mistitled:
        out += [
            "### ❌ Resolves to a different paper",
            "",
            "| bibkey | ref | title in the bibliography | title the id resolves to |",
            "|---|---|---|---|",
        ]
        out += [
            f"| `{e['entry_key']}` | `{e['ref']}` | {e['bib']} | {e['doi']} |" for e in mistitled
        ]
        out += [
            "",
            "These resolve, so the checks above pass them. A one-character slip in a DOI "
            "usually lands on somebody else's real paper, and only the title sees it. Fix "
            "the id, or — if the registered metadata is what is wrong (a truncated title, a "
            "dropped subtitle) — add the bibkey to the `title-allow` file **with a comment "
            "saying why**.",
            "",
        ]
    if inconclusive:
        out += ["### ⚠️ Title check inconclusive (not gated)", "", "| bibkey | ref |", "|---|---|"]
        out += [f"| `{e['entry_key']}` | `{e['ref']}` |" for e in inconclusive]
        out += [
            "",
            "No title came back for these, so nothing was compared. The reason is on stderr, "
            "per entry. A whole run landing here means the title check did not run at all.",
            "",
        ]
    out += table("⚠️ Transient (not gated)", transient)
    out += table("🟡 Allowlisted exceptions", excepted)

    if broken:
        out += [
            "A broken reference does not resolve on Crossref / arXiv — it is "
            "**fabricated**, mistyped, or genuinely not indexed. Fix the id in the "
            "bibliography, or, if it is real but unresolvable, add its DOI / arXiv id "
            "(or bibkey) to `docs/references.allow` **with a comment saying why**.",
            "",
        ]
    if unverified:
        out += [
            "An unverified reference produced no result — doiget aborted, timed out, "
            "or could not parse it, so it was **never checked**. Re-run the job; if it "
            "persists, the bibliography entry is malformed. This is not allowlistable — "
            "the point is that every reference is actually verified.",
            "",
        ]
    if malformed:
        out += ["### ❌ Malformed bibliography keys — fix the entry key", ""]
        out += ["| raw key |", "|---|"]
        out += [f"| `{k}` |" for k in malformed]
        out += [
            "",
            "A BibTeX key with a leading/trailing space or an invalid character (e.g. "
            "`@article{ Key,`) makes the entry unregistrable: `doiget verify` tolerates it but "
            "DocumenterCitations rejects it and every `[key](@cite)` to it fails, breaking the "
            "docs build. Fix the key to match `^[A-Za-z][A-Za-z0-9:_.+/-]*$`.",
            "",
        ]
    return "\n".join(out)


def print_report(report):
    """Print the report without letting the console encoding decide the exit code.

    The report contains non-ASCII characters (em dashes, status emoji). On a
    console whose encoding cannot represent them — cp932 on a Japanese Windows
    shell, or any environment with PYTHONIOENCODING=ascii — a plain print()
    raises UnicodeEncodeError, so the gate exits non-zero even when every
    reference resolved. The file outputs are already written with an explicit
    encoding; make stdout equally forgiving.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # stdout replaced (tests) or not reconfigurable — fall through
    try:
        print(report)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(report.encode(enc, "replace").decode(enc, "replace"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bib", default="", help="bibliography path, for the completeness check")
    ap.add_argument("--allow", default="docs/references.allow", help="allowlist file")
    ap.add_argument("--report", default="", help="write the Markdown report to this path")
    ap.add_argument("--github-output", default="", help="write broken=/unverified=/fail=/ok= here")
    ap.add_argument(
        "--titles",
        default="",
        help="JSONL of {ref,title} resolved metadata; enables the title cross-check",
    )
    ap.add_argument(
        "--title-allow",
        default="",
        help="allowlist for the title cross-check, kept separate from --allow so that "
        "exempting an entry here does not also exempt it from the resolution gate",
    )
    args = ap.parse_args()

    allow = load_allow(args.allow)

    ok, transient, excepted, broken = [], [], [], []
    seen = set()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip any non-JSON human-summary line
        if not isinstance(entry, dict) or "status" not in entry:
            continue
        # `ref` is JSON null for id-less entries, so `.get(k, "")` returns None,
        # not "" — coerce with `or ""` before touching .lower().
        key = (entry.get("entry_key") or "").lower()
        ref = (entry.get("ref") or "").lower()
        seen.add(key)
        status = entry.get("status", "")
        allowed = ref in allow or key in allow
        if status in BROKEN:
            (excepted if allowed else broken).append(entry)
        elif status in TRANSIENT:
            transient.append(entry)
        else:
            ok.append(entry)

    # Completeness: every reference entry in the bib must have a verify record.
    unverified = [k for k in bib_keys(args.bib) if k.lower() not in seen]
    malformed = malformed_keys(args.bib)

    resolved = load_titles(args.titles) if args.titles else None
    title_allow = load_allow(args.title_allow) if args.title_allow else set()
    mistitled, inconclusive, compared = [], [], 0
    if resolved is not None:
        declared = bib_titles(args.bib)
        for entry in ok:
            key = entry.get("entry_key") or ""
            ref = (entry.get("ref") or "").lower()
            if key.lower() in title_allow or ref in title_allow:
                continue
            want, got = declared.get(key), resolved.get(ref)
            if not want or not got:
                # Reported, not gated.
                inconclusive.append({"entry_key": key, "ref": ref})
                continue
            compared += 1
            if not titles_agree(want, got):
                mistitled.append({"entry_key": key, "ref": ref, "bib": want, "doi": got})

    report = render(
        ok, transient, excepted, broken, unverified, malformed, mistitled,
        inconclusive, compared, resolved is not None,
    )
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    fail = len(broken) + len(unverified) + len(malformed) + len(mistitled)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(
                f"broken={len(broken)}\nunverified={len(unverified)}\n"
                f"malformed={len(malformed)}\nmistitled={len(mistitled)}\n"
                f"title_inconclusive={len(inconclusive)}\ntitles_compared={compared}\n"
                f"fail={fail}\nok={len(ok)}\ntransient={len(transient)}\n"
            )
    print_report(report)

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
