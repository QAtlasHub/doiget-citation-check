# doiget Citation Check

A GitHub Action that hard-gates a BibTeX bibliography: every DOI / arXiv id must
resolve to real metadata on Crossref / arXiv (via [`doiget`](https://github.com/QAtlasHub/doiget)),
or the check fails. A fabricated or mistyped reference can never pass silently —
it either fails, or is explicitly vouched for in an allow-list. The result is
posted as a **sticky PR comment** and the job summary.

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
| `doiget-version` | `v0.8.6` | doiget release tag whose prebuilt binary is used |
| `comment` | `true` | post the sticky PR comment |
| `token` | `${{ github.token }}` | token for the PR comment |

## Outputs

`broken`, `unverified`, `fail`, `ok` — the per-class counts (see gating below).

## How it works

1. Downloads the prebuilt, checksum-verified `doiget` binary for the runner
   (linux/macOS × x86_64/aarch64) — no Rust build.
2. `doiget verify <bib> --format auto --mode json` resolves every entry
   (resolver cache in `~/.cache/doiget`, keyed on the bib).
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
