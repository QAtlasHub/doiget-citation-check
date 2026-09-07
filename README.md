# doiget Citation Check

A GitHub Action that hard-gates a BibTeX bibliography: every DOI / arXiv id must
resolve to real metadata on Crossref / arXiv (via [`doiget`](https://github.com/QAtlasHub/doiget)),
or the check fails. A fabricated or mistyped reference can never pass silently —
it either fails, or is explicitly vouched for in an allow-list. The result is
posted as a **sticky PR comment** and the job summary.

## The title cross-check

A DOI that **resolves** is not a DOI that resolves to **the right paper**. A one-character
slip usually lands on somebody else's real paper, so every check built on resolution alone
passes it. Measured on one consumer's bibliography, 2 of 18 sampled DOIs have a resolving
neighbour one digit away — about one slip in nine.

So the action also resolves each entry's registered title (via `doiget cite`, so it goes
through doiget's own resolver and cache) and compares it with the one the entry declares.
`mistitled` is gated alongside `broken` and `unverified`.

### What counts as agreement

Neither side being a prefix of the other is a wrong id. Both directions of prefix occur and
are **not** wrong, but for different reasons, so they get different rules:

- **The bibliography abbreviates** by dropping a subtitle — `…Irreversible Processes` for
  Kubo 1957's `…Irreversible Processes. I. General Theory and…`. Its title must therefore end
  where a subtitle begins. Without that boundary, `Quantum Phase Transitions` would match an
  unrelated paper that merely opens with those words — a real pair found in a real
  bibliography.
- **A publisher truncates its own metadata** at an arbitrary point, mid-word included
  (`…Heisenberg chain with 1/`). There is no boundary to require.

Whitespace is **dropped, not normalised**: resolvers strip inline MathML without putting a
space back, so `the<math>XY</math>Model` arrives as `theXYModel`.

One case is irreducible: a book and a paper differing only by a subtitle
(`The One-Dimensional Hubbard Model` / `…: a reminiscence`) agree under any rule that tolerates
a dropped subtitle at all.

### When it cannot run

A title that could not be resolved is reported as `title_inconclusive` and **not** gated — the
same treatment `transient` gets on the resolution side. The report always carries the
denominator (`titles compared: N of M`), so a run where nothing could be resolved does not
render as a run where everything agreed. `resolve_titles.py` writes the reason per entry to
stderr.

Set `titles: 'false'` to turn the check off. Genuine metadata defects — a typo in the
registered title itself — go in the `title-allow` file, kept **separate** from `allow` so that
exempting an entry from the title check does not also exempt it from the resolution gate.

## Usage

```yaml
name: Verify references
on:
  pull_request:
    paths: ['docs/references.bib', 'docs/references.allow']
  push:
    branches: [main]
    paths: ['docs/references.bib', 'docs/references.allow']
permissions:
  contents: read
  pull-requests: write   # for the sticky comment
jobs:
  citations:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: QAtlasHub/doiget-citation-check@v1
        with:
          bib: docs/references.bib
          allow: docs/references.allow
```

## Inputs

| input | default | description |
|---|---|---|
| `bib` | `docs/references.bib` | BibTeX file to verify |
| `allow` | `docs/references.allow` | allow-list of acknowledged-broken refs (one DOI/arXiv id or bibkey per line; `#` = comment/reason) |
| `titles` | `true` | also check that each id resolves to the title the entry names |
| `title-allow` | `docs/references.title-allow` | allow-list for the title check only — kept separate from `allow` |
| `title-timeout` | `60` | seconds to wait for each `doiget cite` |
| `doiget-version` | `v0.8.6` | doiget release tag whose prebuilt binary is used |
| `comment` | `true` | post the sticky PR comment |
| `token` | `${{ github.token }}` | token for the PR comment |

## Outputs

`broken`, `unverified`, `mistitled`, `fail`, `ok` — the per-class counts (see gating below),
plus `titles_compared` and `title_inconclusive`, the denominator for `mistitled` and the
entries it could not be computed for.

## How it works

1. Downloads the prebuilt, checksum-verified `doiget` binary for the runner
   (linux/macOS × x86_64/aarch64) — no Rust build.
2. `doiget verify <bib> --format auto --mode json` resolves every entry
   (resolver cache in `~/.cache/doiget`, keyed on the bib).
3. With `titles: true`, `resolve_titles.py` runs `doiget cite` on each entry that
   resolved and emits `{ref, title}` for the gate to compare — see
   [The title cross-check](#the-title-cross-check).
3. The bundled `verify_references_gate.py` classifies each entry and is the sole
   authority on pass/fail:

   | class | meaning | gated? |
   |---|---|---|
   | `valid` | resolves on Crossref / arXiv | OK |
   | `illegal` / `absent` | malformed, or does not resolve (fabricated / mistyped) | **FAIL** unless the id/bibkey is in the allow-list |
   | `unreachable` / `unverifiable` | transient network / 429 / id-less / coverage gap | warn only (never flaky-blocks) |
   | *missing* | an entry produced no verify record (truncated / aborted run) | **FAIL** (a check that did not run can't pass) |

4. Posts / updates the sticky PR comment and job summary.
5. Fails the job iff `fail != 0`.

## Notes

- Fork PRs get a read-only token; the comment step is `continue-on-error`, so
  fork PRs still gate (via the summary) without erroring on the comment.
- The allow-list is the audit trail: every acknowledged-broken reference carries
  a `#` reason, so exceptions are self-documenting.
