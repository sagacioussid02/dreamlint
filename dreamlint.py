#!/usr/bin/env python3
"""DreamLint — lint an AI assistant's memory folder.

Point it at a folder of markdown notes and it reports which notes are
outdated, duplicated, or contradicted by newer ones, and proposes a
smaller, cleaner set. It never modifies the scanned folder: it reports,
and (with --propose) writes a per-change action list for human approval.

    python3 dreamlint.py <folder> [--stale-days 90] [--format text|json] [--propose] [--out DIR]

Exit codes: 0 = clean, 1 = findings, 2 = usage/scan error.
Stdlib only. Deterministic: no LLM calls, no network, runs offline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

COVERAGE_LINE = (
    "Coverage: deterministic checks only (duplicates, structured contradictions, "
    "superseded, stale). Semantic contradictions are not checked in v1."
)

NEAR_DUP_THRESHOLD = 0.85
MIN_PARAGRAPH_TOKENS = 4
HISTORICAL_MARKERS = ("superseded", "no longer", "moved to", "replaced by")
# Files treated as the curated, higher-priority copy when choosing a keeper.
PRIORITY_FILES = {"memory.md"}

DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
TOKEN_RE = re.compile(r"[a-z0-9']+")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
BOLD_CLAIM_RE = re.compile(r"^\*\*(.+?)\*\*\s*(?::|—|–|-)?\s*(.+)$")
KV_CLAIM_RE = re.compile(r"^([^:—–]{2,60}?)\s*(?::|—|–)\s+(.+)$")


# ---------------------------------------------------------------- model

@dataclass
class Paragraph:
    note: "Note"
    line: int            # 1-based line where the paragraph starts
    text: str            # raw text
    norm: str            # normalized text
    tokens: frozenset    # token set for Jaccard


@dataclass
class Claim:
    note: "Note"
    line: int
    key: str             # normalized attribute key
    key_raw: str
    value: str           # normalized value
    value_raw: str


@dataclass
class Note:
    path: Path
    rel: str             # path relative to scanned root, posix style
    text: str
    note_date: date | None
    paragraphs: list = field(default_factory=list)
    claims: list = field(default_factory=list)

    @property
    def title(self) -> str:
        """First markdown heading, else the filename stem (normalized)."""
        for line in self.text.splitlines():
            m = re.match(r"^#\s+(.*\S)\s*$", line)
            if m:
                return normalize_key(m.group(1))
        return normalize_key(self.path.stem)


@dataclass
class Finding:
    kind: str            # duplicate | contradiction | superseded | stale
    summary: str
    details: list = field(default_factory=list)
    info: bool = False   # informational only (e.g. explicit historical markers)


# ---------------------------------------------------------------- helpers

def normalize_text(text: str) -> str:
    return " ".join(TOKEN_RE.findall(text.lower()))


def token_set(text: str) -> frozenset:
    return frozenset(TOKEN_RE.findall(text.lower()))


def normalize_key(text: str) -> str:
    return " ".join(text.lower().replace("**", " ").split()).strip(" :—–-")


def normalize_value(text: str) -> str:
    return " ".join(text.strip().strip("`\"'").lower().split()).rstrip(".")


def parse_date(y: str, m: str, d: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def first_date_in(text: str) -> date | None:
    m = DATE_RE.search(text)
    return parse_date(*m.groups()) if m else None


def frontmatter_date(text: str) -> date | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:2000]
    m = re.search(r"^date:\s*\"?(\d{4}-\d{2}-\d{2})", block, re.M)
    return first_date_in(m.group(1)) if m else None


def note_date_for(path: Path, text: str) -> date | None:
    """Frontmatter date, else filename date, else first in-text date, else mtime.

    Filename date is checked before mtime (a deviation from the spec's listed
    order) because copying a corpus resets every mtime to the copy time, which
    would destroy the ordering of dated note files like 2026-10-13.md.
    """
    d = frontmatter_date(text)
    if d:
        return d
    m = DATE_RE.search(path.name)
    if m:
        d = parse_date(*m.groups())
        if d:
            return d
    d = first_date_in(text)
    if d:
        return d
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


# ---------------------------------------------------------------- parsing

def split_paragraphs(note: Note) -> list:
    """Split note text into paragraphs (blank-line separated), skipping
    fenced code blocks and heading-only / horizontal-rule blocks."""
    paragraphs = []
    lines = note.text.splitlines()
    buf: list[str] = []
    buf_start = 0
    in_fence = False

    def flush(end_line: int):
        if not buf:
            return
        raw = "\n".join(buf).strip()
        if raw and not re.fullmatch(r"[#>\s\-=*_]+", raw):
            norm = normalize_text(raw)
            if len(norm.split()) >= 1:
                paragraphs.append(Paragraph(note, buf_start, raw, norm, token_set(raw)))

    for i, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            flush(i)
            buf = []
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.strip() == "":
            flush(i)
            buf = []
            continue
        if not buf:
            buf_start = i
        buf.append(line)
    flush(len(lines))
    return paragraphs


def extract_claims(note: Note) -> list:
    """Structured claims from bullet lines: `key: value`, `key — value`,
    and bolded-lead bullets (`**Key:** value`)."""
    claims = []
    for i, line in enumerate(note.text.splitlines(), start=1):
        m = BULLET_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        claim = None
        b = BOLD_CLAIM_RE.match(body)
        if b:
            claim = (b.group(1), b.group(2))
        else:
            kv = KV_CLAIM_RE.match(body)
            if kv and len(kv.group(1).split()) <= 6:
                claim = (kv.group(1), kv.group(2))
        if not claim:
            continue
        key_raw, value_raw = claim[0].strip(), claim[1].strip()
        key = normalize_key(key_raw)
        if len(key) < 2 or not value_raw:
            continue
        claims.append(Claim(note, i, key, key_raw, normalize_value(value_raw), value_raw))
    return claims


def load_notes(root: Path) -> list:
    notes = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        note = Note(path=path, rel=path.relative_to(root).as_posix(),
                    text=text, note_date=note_date_for(path, text))
        note.paragraphs = split_paragraphs(note)
        note.claims = extract_claims(note)
        notes.append(note)
    return notes


# ---------------------------------------------------------------- checks

def keep_rank(p: Paragraph) -> tuple:
    """Lower is a better keeper: priority file first, then newest note."""
    priority = 0 if p.note.path.name.lower() in PRIORITY_FILES else 1
    d = p.note.note_date or date.min
    return (priority, -d.toordinal(), p.note.rel, p.line)


def check_duplicates(notes: list) -> list:
    findings = []
    substantive = [p for n in notes for p in n.paragraphs
                   if len(p.tokens) >= MIN_PARAGRAPH_TOKENS]

    # Exact duplicates: identical normalized text in 2+ places.
    by_norm: dict[str, list] = {}
    for p in substantive:
        by_norm.setdefault(p.norm, []).append(p)
    exact_groups = []
    in_exact = set()
    for norm, group in by_norm.items():
        if len(group) > 1:
            exact_groups.append(group)
            in_exact.update(id(p) for p in group)

    for group in exact_groups:
        group.sort(key=keep_rank)
        keeper, copies = group[0], group[1:]
        locs = ", ".join(f"{p.note.rel}:{p.line}" for p in group)
        findings.append(Finding(
            "duplicate",
            f"exact duplicate paragraph in {len(group)} places: {locs}",
            [f"keep {keeper.note.rel}:{keeper.line}"]
            + [f"drop copy at {p.note.rel}:{p.line}" for p in copies],
        ))

    # Near duplicates: paragraph token-set Jaccard >= threshold, across notes.
    seen_pairs = set()
    pool = [p for p in substantive if id(p) not in in_exact]
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a, b = pool[i], pool[j]
            if a.note is b.note or not a.tokens or not b.tokens:
                continue
            sim = len(a.tokens & b.tokens) / len(a.tokens | b.tokens)
            if sim >= NEAR_DUP_THRESHOLD:
                key = tuple(sorted((id(a), id(b))))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                keeper = min((a, b), key=keep_rank)
                findings.append(Finding(
                    "duplicate",
                    f"near-duplicate paragraphs ({sim:.0%} similar): "
                    f"{a.note.rel}:{a.line} ≈ {b.note.rel}:{b.line}",
                    [f"keep {keeper.note.rel}:{keeper.line}"],
                ))
    return findings


MAX_FACT_VALUES = 5        # more distinct values than this = log field, not a fact
MAX_VALUE_CHARS = 120      # longer "values" are narrative prose, not attributes
ID_TOKEN_RE = re.compile(r"^[0-9a-f]{16,}$|^[0-9a-f]{8}-[0-9a-f-]{27,}$")


def is_fact_claim(c: Claim) -> bool:
    """A bullet claim counts as a fact claim only if its value looks like an
    attribute value: short, and not an opaque record ID. Daily-note corpora
    reuse labels like `- Claim: <uuid>` or `- Quote: <paragraph>` as log
    fields; those are records, not competing statements of one fact."""
    if len(c.value) > MAX_VALUE_CHARS:
        return False
    tokens = c.value.split()
    if len(tokens) == 1 and ID_TOKEN_RE.match(tokens[0]):
        return False
    return True


def check_contradictions(notes: list) -> tuple:
    """Returns (findings, ignored_field_count). A key with more than
    MAX_FACT_VALUES distinct short values is a high-cardinality log or
    category field, not a single fact — it is counted as ignored, openly."""
    findings = []
    ignored = 0
    by_key: dict[str, list] = {}
    for n in notes:
        for c in n.claims:
            if is_fact_claim(c):
                by_key.setdefault(c.key, []).append(c)
    for key, claims in sorted(by_key.items()):
        values = {c.value for c in claims}
        files = {c.note.rel for c in claims}
        if len(values) > MAX_FACT_VALUES:
            ignored += 1
            continue
        if len(values) < 2 or len(files) < 2:
            continue
        ordered = sorted(claims,
                         key=lambda c: (c.note.note_date or date.min, c.note.rel, c.line))
        winner = ordered[-1]
        details = []
        for c in ordered:
            d = c.note.note_date.isoformat() if c.note.note_date else "unknown date"
            mark = " ← wins (newest)" if c is winner else ""
            details.append(f"{c.note.rel}:{c.line} ({d}): {c.value_raw}{mark}")
        findings.append(Finding(
            "contradiction",
            f'"{key}" stated with {len(values)} different values',
            details,
        ))
    return findings, ignored


def check_superseded(notes: list) -> list:
    findings = []

    # Explicit historical markers: informational, never an error.
    for n in notes:
        for i, line in enumerate(n.text.splitlines(), start=1):
            low = line.lower()
            if any(marker in low for marker in HISTORICAL_MARKERS):
                marker = next(m for m in HISTORICAL_MARKERS if m in low)
                findings.append(Finding(
                    "superseded",
                    f"historical marker '{marker}' at {n.rel}:{i} (info)",
                    [line.strip()[:120]],
                    info=True,
                ))

    # Same subject covered again by a strictly newer note.
    by_title: dict[str, list] = {}
    for n in notes:
        if n.title:
            by_title.setdefault(n.title, []).append(n)
    for title, group in sorted(by_title.items()):
        if len(group) < 2:
            continue
        dated = [n for n in group if n.note_date]
        if len(dated) < 2:
            continue
        newest = max(dated, key=lambda n: (n.note_date, n.rel))
        for n in dated:
            if n is not newest and n.note_date < newest.note_date:
                findings.append(Finding(
                    "superseded",
                    f'"{title}" in {n.rel} ({n.note_date}) is covered again by a newer note',
                    [f"newer: {newest.rel} ({newest.note_date})"],
                ))
    return findings


def check_stale(notes: list, stale_days: int, today: date) -> list:
    findings = []
    titles = {n.title for n in notes}
    for n in notes:
        if not n.note_date:
            continue
        age = (today - n.note_date).days
        if age <= stale_days:
            continue
        # Referenced or updated by a newer note? (filename mentioned, or a
        # newer note shares the subject and so supersedes the staleness.)
        referenced = any(
            other is not n and other.note_date and other.note_date > n.note_date
            and (n.path.name in other.text or other.title == n.title)
            for other in notes
        )
        if referenced:
            continue
        findings.append(Finding(
            "stale",
            f"{n.rel} is {age} days old (dated {n.note_date}) and no newer note "
            f"references or updates it — review prompt, not a verdict",
            [],
        ))
    return findings


# ---------------------------------------------------------------- proposal

def proposed_note_count(notes: list, findings: list) -> int:
    """Estimate the cleaned-set size: notes minus notes that are fully
    superseded or whose every substantive paragraph duplicates a kept copy."""
    removable = set()
    for f in findings:
        if f.kind == "superseded" and not f.info:
            m = re.search(r"in (\S+) \(", f.summary)
            if m:
                removable.add(m.group(1))
    substantive = {n.rel: [p for p in n.paragraphs if len(p.tokens) >= MIN_PARAGRAPH_TOKENS]
                   for n in notes}
    dup_norms_kept_elsewhere = set()
    by_norm: dict[str, list] = {}
    for n in notes:
        for p in substantive[n.rel]:
            by_norm.setdefault(p.norm, []).append(p)
    for norm, group in by_norm.items():
        if len(group) > 1:
            group.sort(key=keep_rank)
            for p in group[1:]:
                dup_norms_kept_elsewhere.add((p.note.rel, p.norm))
    for n in notes:
        paras = substantive[n.rel]
        if paras and all((n.rel, p.norm) in dup_norms_kept_elsewhere for p in paras):
            removable.add(n.rel)
    return max(0, len(notes) - len(removable))


def build_proposal(notes: list, findings: list, proposed: int) -> str:
    lines = [
        "# DreamLint proposal — NOT applied",
        "",
        "Per-change actions for human approval. DreamLint applies nothing itself.",
        f"Current set: {len(notes)} notes → proposed {proposed} notes.",
        "",
    ]
    if not findings:
        lines.append("No changes proposed — the set is clean.")
        return "\n".join(lines) + "\n"
    for f in findings:
        if f.kind == "duplicate":
            lines.append(f"- MERGE: {f.summary}")
            for d in f.details:
                lines.append(f"  - {d}")
        elif f.kind == "contradiction":
            lines.append(f"- DROP older claim: {f.summary}")
            for d in f.details:
                lines.append(f"  - {d}")
        elif f.kind == "superseded" and not f.info:
            lines.append(f"- DROP (superseded): {f.summary}")
            for d in f.details:
                lines.append(f"  - {d}")
        elif f.kind == "superseded" and f.info:
            lines.append(f"- KEEP as history (info): {f.summary}")
        elif f.kind == "stale":
            lines.append(f"- REVIEW (stale): {f.summary}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- report

def run_scan(root: Path, stale_days: int) -> dict:
    notes = load_notes(root)
    today = date.today()
    findings = []
    findings += check_duplicates(notes)
    contra, ignored_fields = check_contradictions(notes)
    findings += contra
    findings += check_superseded(notes)
    findings += check_stale(notes, stale_days, today)
    proposed = proposed_note_count(notes, findings)
    counts = {k: sum(1 for f in findings if f.kind == k)
              for k in ("duplicate", "contradiction", "superseded", "stale")}
    return {
        "folder": str(root),
        "notes_scanned": len(notes),
        "findings_total": len(findings),
        "counts": counts,
        "proposed_notes": proposed,
        "ignored_log_fields": ignored_fields,
        "coverage": COVERAGE_LINE,
        "findings": [
            {"kind": f.kind, "summary": f.summary, "details": f.details, "info": f.info}
            for f in findings
        ],
        "_notes": notes,
        "_findings": findings,
    }


def render_text(result: dict) -> str:
    counts = result["counts"]
    out = [
        f"DreamLint — {result['notes_scanned']} notes scanned, "
        f"{result['findings_total']} findings",
        COVERAGE_LINE,
    ]
    if result.get("ignored_log_fields"):
        out.append(
            f"Ignored {result['ignored_log_fields']} high-cardinality log field(s) "
            "(labels reused with many different values across notes — records, not facts)."
        )
    out.append("")
    for kind in ("duplicate", "contradiction", "superseded", "stale"):
        items = [f for f in result["_findings"] if f.kind == kind]
        if not items:
            continue
        out.append(f"{kind.upper()}S ({len(items)})")
        for f in items:
            out.append(f"  {f.summary}")
            for d in f.details:
                out.append(f"    {d}")
        out.append("")
    out.append(
        f"Summary: {result['notes_scanned']} notes → proposed {result['proposed_notes']} | "
        f"duplicates {counts['duplicate']}, contradictions {counts['contradiction']}, "
        f"superseded {counts['superseded']}, stale {counts['stale']}"
    )
    return "\n".join(out)


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dreamlint",
        description="Lint an AI assistant's memory folder (read-only).")
    parser.add_argument("folder", help="folder of markdown notes to scan")
    parser.add_argument("--stale-days", type=int, default=90,
                        help="age threshold for the stale check (default 90)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--propose", action="store_true",
                        help="also write dreamlint-proposal.md (never into the scanned folder)")
    parser.add_argument("--out", default=None,
                        help="directory for dreamlint-proposal.md (default: current directory)")
    args = parser.parse_args(argv)

    root = Path(args.folder)
    if not root.is_dir():
        print(f"error: {args.folder} is not a folder", file=sys.stderr)
        return 2

    result = run_scan(root.resolve(), args.stale_days)

    if args.propose:
        out_dir = Path(args.out).resolve() if args.out else Path.cwd()
        proposal_path = out_dir / "dreamlint-proposal.md"
        scanned = root.resolve()
        if scanned == proposal_path.parent or scanned in proposal_path.parents:
            print("error: refusing to write the proposal inside the scanned folder",
                  file=sys.stderr)
            return 2
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            proposal_path.write_text(
                build_proposal(result["_notes"], result["_findings"],
                               result["proposed_notes"]),
                encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write proposal: {e}", file=sys.stderr)
            return 2

    if args.format == "json":
        public = {k: v for k, v in result.items() if not k.startswith("_")}
        print(json.dumps(public, indent=2))
    else:
        print(render_text(result))
        if args.propose:
            print(f"Proposal written to {proposal_path} (not applied)")

    return 1 if result["findings_total"] else 0


if __name__ == "__main__":
    sys.exit(main())
