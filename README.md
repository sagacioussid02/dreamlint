# DreamLint

**Lint your agent's memory before it starts hallucinating its own past.**

Point DreamLint at a folder of markdown memory notes and it reports which
notes are outdated, duplicated, or contradicted by newer ones — then proposes
a smaller, cleaner set. It is named after Anthropic's "dreaming" finding:
agents whose memory is periodically pruned and corrected outperform agents
that hoard everything.

DreamLint **never modifies the scanned folder**. It reports; a human disposes.

## Usage

```
python3 dreamlint.py <folder> [--stale-days 90] [--format text|json] [--propose] [--out DIR]
```

- Default: scan and print the report. Read-only, always.
- `--propose`: also write `dreamlint-proposal.md` — a per-change action list
  (merge / drop / keep / review) for human approval. Written to the current
  directory, or `--out DIR`. It is never written into the scanned folder
  (DreamLint refuses, exit 2) and it applies nothing.
- Exit codes: `0` clean · `1` findings · `2` usage/scan error.
- Single file, Python stdlib only. No install, no network, runs offline.

## The four checks

1. **Duplicates** — exact duplicate paragraphs (identical normalized text in
   2+ places) and near-duplicates (paragraph token-set Jaccard ≥ 0.85).
   Each group names a keeper: a curated priority file (`MEMORY.md`) first,
   else the newest copy.
2. **Structured contradictions** — claims extracted from bullet lines
   (`key: value`, `key — value`, `**Key:** value`). The same key with
   different values in different notes is flagged; the newest-dated note
   wins, and both citations are shown. Labels reused as log fields (opaque
   ID values, narrative-length values, or >5 distinct values) are ignored
   as records rather than facts — and counted openly in the report.
3. **Superseded** — a note whose subject is covered again by a strictly
   newer note, citing the newer one. Lines carrying explicit historical
   markers (`SUPERSEDED`, `no longer`, `moved to`, `replaced by`) are
   reported as informational, never as errors.
4. **Stale** — notes older than `--stale-days` (default 90) that no newer
   note references or updates. The lowest-severity check: a review prompt,
   not a verdict.

Note dates are resolved as: frontmatter `date:` → date in the filename →
first in-text date → file mtime.

## v1 limits (stated in every report)

- Deterministic checks only. **Semantic contradictions are not checked in
  v1** — two notes can say the same fact in different words and v1 will not
  connect them. A fuzzy/LLM contradiction pass is the v2 candidate.
- Markdown note folders only. Other memory stores need adapters — e.g.
  sutra's memory is a SQLite store (`memories` table), which is a v2 adapter.
- Contradiction precision on log-style corpora (daily journals with
  templated field labels) is imperfect: expect a few residual log-field
  flags among the real findings. Every flag carries file + line evidence
  so a human can judge it in seconds.

## Dogfood

`my-memory-report-2026-11-12.md` is a real run against a copy of the
author's own assistant-memory corpus (32 notes): 1 exact duplicate,
3 contradiction candidates, 27 informational historical markers, 0 stale.

## Tests

```
python3 -m unittest discover -s tests
```

30 tests: one suite per check, a zero-finding clean corpus, exit codes,
JSON output, and the read-only guarantee (scanned-file hashes unchanged
after a `--propose` run; proposal refused inside the scanned folder).

Spec: `../files/dreamlint-spec-2026-11-12.md`
