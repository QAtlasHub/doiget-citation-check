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
# Below this, a resolved title too short to be a truncation of anything in particular.
MIN_SHORTER_CHARS = 12


def normalise_title(s):
    # Whitespace is dropped, not normalised: resolvers can run words together.
    s = _LATEX_RE.sub(" ", _ENTITY_RE.sub(" ", _TAG_RE.sub(" ", s)))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _subtitle_prefixes(title):
    """Every prefix of `title` that ends where a subtitle begins, normalised."""
    return {normalise_title(title[: m.start()]) for m in _SUBTITLE_RE.finditer(title)}


def compare_titles(declared, resolved):
    """`"same"`, `"shorter"` or `"different"`.

    The two directions of prefix are not the same thing:

      * the bibliography abbreviating by dropping a SUBTITLE is normal, so its title must end
        where one begins — without that, "Quantum Phase Transitions" matches an unrelated
        paper that merely opens with those words;
      * a resolved title shorter than the declared one is `"shorter"`, not `"same"`. It is
        usually the publisher truncating its own metadata, but a wrong id whose paper has a
        shorter title is indistinguishable from that — measured on a real bibliography, the
        length ratios of the two overlap completely (0.39-0.92 against 0.37-0.69). So it is
        reported for a human rather than decided here.
    """
    d, r = normalise_title(declared), normalise_title(resolved)
    if not d or not r:
        # Nothing left to compare on one side — a title that is all maths, or a garbled
        # record. Not a wrong paper, and not an agreement either.
        return "uncomparable"
    if d == r:
        return "same"
    if len(d) < len(r):
        return "same" if r.startswith(d) and d in _subtitle_prefixes(resolved) else "different"
    # A floor, because "shorter" is not gated: without one, a wrong id landing on any short
    # generic record — Erratum, Comment, Reply, Corrigendum — that happens to prefix the real
    # title produces no signal at all.
    if d.startswith(r) and len(r) >= MIN_SHORTER_CHARS:
        return "shorter"
    return "different"


# Found at the entry's own brace depth, so a `title = {…}` written inside a `note` value is
# not mistaken for it — a comma before it is not enough, and `note` sorts before `title`.
_TITLE_AT_RE = re.compile(r"title\s*=\s*", re.IGNORECASE)


def _title_field_pos(block):
    """Where the entry's own `title =` value starts, or None."""
    depth, quoted = 0, False
    for i, ch in enumerate(block):
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            continue
        # A quoted value delimits at the entry's own level only; inside braces `"` is literal.
        if depth == 1 and ch == '"':
            quoted = not quoted
            continue
        if quoted or depth != 1 or ch not in "tT":
            continue
        m = _TITLE_AT_RE.match(block, i)
        if m and (i == 0 or not (block[i - 1].isalnum() or block[i - 1] == "_")):
            return m.end()
    return None


def _one_value(text, pos):
    """The `{...}` or `"..."` starting at `pos`, and the index just past it."""
    if pos >= len(text):
        return None, pos
    if text[pos] == '"':
        end = text.find('"', pos + 1)
        return (None, pos) if end < 0 else (text[pos + 1 : end], end + 1)
    if text[pos] != "{":
        return None, pos
    depth = 0
    for i in range(pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i + 1
    return None, pos


# A brace-matched value that reaches the entry's own closing brace balances, but it has eaten
# every field in between. Nothing distinguishes that from a title except its shape.
_ATE_A_FIELD_RE = re.compile(r",\s*\w+\s*=\s*[{\"]")


def _braced_value(text, pos):
    """The whole value at `pos`, following BibTeX's `#` concatenation."""
    parts, i = [], pos
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        val, nxt = _one_value(text, i)
        if val is None:
            break
        parts.append(val)
        i = nxt
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] == "#":
            i += 1
            continue
        break
    return "".join(parts) if parts else None


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
        pos = _title_field_pos(block)
        if pos is None:
            continue
        val = _braced_value(block, pos)
        if val is not None and _ATE_A_FIELD_RE.search(val):
            continue  # unterminated: no declared title, so the entry is inconclusive, not wrong
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
    inconclusive=(), compared=0, titles_on=False, shorter=(),
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
    if shorter:
        out.append(
            f"- ⚠️ resolved title is shorter than the entry's (not gated): **{len(shorter)}**"
        )
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
    if shorter:
        out += [
            "### ⚠️ The resolved title is shorter than the entry's (not gated)",
            "",
            "| bibkey | ref | title in the bibliography | title the id resolves to |",
            "|---|---|---|---|",
        ]
        out += [f"| `{e['entry_key']}` | `{e['ref']}` | {e['bib']} | {e['doi']} |" for e in shorter]
        out += [
            "",
            "Usually the publisher truncating its own metadata. It can also be an id that "
            "slipped to a paper with a shorter title, and the two are not distinguishable "
            "from the strings — read these rather than trusting them.",
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
    # print() only fills a buffer, so a closed stdout does not surface until the interpreter
    # flushes at shutdown — long after the caller has written its verdict and exited 0.
    sys.stdout.flush()


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
    mistitled, inconclusive, shorter, compared = [], [], [], 0
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
            verdict = compare_titles(want, got)
            if verdict == "uncomparable":
                compared -= 1
                inconclusive.append({"entry_key": key, "ref": ref})
            elif verdict == "different":
                mistitled.append({"entry_key": key, "ref": ref, "bib": want, "doi": got})
            elif verdict == "shorter":
                shorter.append({"entry_key": key, "ref": ref, "bib": want, "doi": got})

    report = render(
        ok, transient, excepted, broken, unverified, malformed, mistitled,
        inconclusive, compared, resolved is not None, shorter,
    )
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    fail = len(broken) + len(unverified) + len(malformed) + len(mistitled)
    # Last, and after the report: a verdict written before the run finishes is a verdict that
    # survives the run crashing, and `fail=0` outliving a crash reads to the caller as a pass.
    print_report(report)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(
                f"broken={len(broken)}\nunverified={len(unverified)}\n"
                f"malformed={len(malformed)}\nmistitled={len(mistitled)}\n"
                f"title_inconclusive={len(inconclusive)}\ntitles_compared={compared}\n"
                f"title_shorter={len(shorter)}\n"
                f"fail={fail}\nok={len(ok)}\ntransient={len(transient)}\n"
            )

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
