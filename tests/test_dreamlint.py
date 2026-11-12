"""DreamLint tests — stdlib unittest only.

Fixture corpora are built in temp directories: planted duplicates,
contradictions, supersede chains, stale notes, and a clean corpus that
must yield zero findings. Also covers exit codes and the read-only
guarantee (scanned files are never modified, proposal never lands in
the scanned folder).
"""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dreamlint  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "dreamlint.py"


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def scan(root: Path, stale_days: int = 90) -> dict:
    return dreamlint.run_scan(root, stale_days)


def kinds(result: dict) -> list:
    return [f["kind"] for f in result["findings"]]


class CorpusTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "notes"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()


class TestDuplicates(CorpusTestCase):
    def test_exact_duplicate_across_files(self):
        write(self.root, "2026-10-01.md",
              "# Day one\n\nThe user prefers tea over coffee in the morning always.\n")
        write(self.root, "2026-10-02.md",
              "# Day two\n\nThe user prefers tea over coffee in the morning always.\n")
        r = scan(self.root)
        dups = [f for f in r["findings"] if f["kind"] == "duplicate"]
        self.assertEqual(len(dups), 1)
        self.assertIn("exact duplicate", dups[0]["summary"])

    def test_exact_duplicate_within_one_file(self):
        write(self.root, "a.md",
              "# A\n\nRepeated fact about the project deadline being Friday.\n\n"
              "Repeated fact about the project deadline being Friday.\n")
        r = scan(self.root)
        self.assertIn("duplicate", kinds(r))

    def test_near_duplicate_above_threshold(self):
        write(self.root, "a.md",
              "# A\n\nThe assistant should always confirm before sending any email message.\n")
        write(self.root, "b.md",
              "# B\n\nThe assistant should always confirm before sending any email.\n")
        r = scan(self.root)
        dups = [f for f in r["findings"] if f["kind"] == "duplicate"]
        self.assertTrue(any("near-duplicate" in f["summary"] for f in dups))

    def test_similar_topic_but_different_text_not_flagged(self):
        write(self.root, "a.md",
              "# A\n\nCats are small domestic animals that sleep most of the day.\n")
        write(self.root, "b.md",
              "# B\n\nDogs need daily walks and enjoy playing fetch in the park.\n")
        r = scan(self.root)
        self.assertNotIn("duplicate", kinds(r))

    def test_short_paragraphs_ignored(self):
        write(self.root, "a.md", "# A\n\nSee above.\n")
        write(self.root, "b.md", "# B\n\nSee above.\n")
        r = scan(self.root)
        self.assertNotIn("duplicate", kinds(r))

    def test_keeper_prefers_priority_file(self):
        write(self.root, "2026-10-01.md",
              "# Old\n\nThe user prefers tea over coffee in the morning always.\n")
        write(self.root, "MEMORY.md",
              "# Memory\n\nThe user prefers tea over coffee in the morning always.\n")
        r = scan(self.root)
        dups = [f for f in r["findings"] if f["kind"] == "duplicate"]
        self.assertTrue(any("keep MEMORY.md" in d for d in dups[0]["details"]))


class TestContradictions(CorpusTestCase):
    def test_key_value_contradiction_newer_wins(self):
        write(self.root, "2026-10-01.md", "# A\n\n- Preferred editor: vim\n")
        write(self.root, "2026-11-01.md", "# B\n\n- Preferred editor: emacs\n")
        r = scan(self.root)
        cons = [f for f in r["findings"] if f["kind"] == "contradiction"]
        self.assertEqual(len(cons), 1)
        joined = " ".join(cons[0]["details"])
        self.assertIn("vim", joined)
        self.assertIn("emacs", joined)
        winner_line = [d for d in cons[0]["details"] if "wins" in d]
        self.assertEqual(len(winner_line), 1)
        self.assertIn("2026-11-01.md", winner_line[0])

    def test_same_value_is_not_a_contradiction(self):
        write(self.root, "2026-10-01.md", "# A\n\n- Preferred editor: vim\n")
        write(self.root, "2026-11-01.md", "# B\n\n- Preferred editor: vim\n")
        r = scan(self.root)
        self.assertNotIn("contradiction", kinds(r))

    def test_bolded_lead_claim(self):
        write(self.root, "2026-10-01.md", "# A\n\n- **Deploy target:** Vercel\n")
        write(self.root, "2026-11-01.md", "# B\n\n- **Deploy target:** Netlify\n")
        r = scan(self.root)
        self.assertIn("contradiction", kinds(r))

    def test_em_dash_claim(self):
        write(self.root, "2026-10-01.md", "# A\n\n- Home city — Denver\n")
        write(self.root, "2026-11-01.md", "# B\n\n- Home city — Orem\n")
        r = scan(self.root)
        self.assertIn("contradiction", kinds(r))

    def test_id_valued_log_field_not_a_contradiction(self):
        write(self.root, "2026-10-01.md",
              "# A\n\n- Claim: 587562dbdf2a408ed6e40ed7805d946d\n")
        write(self.root, "2026-11-01.md",
              "# B\n\n- Claim: 3d905a23d29b60a7e1760a8b116cedcb\n")
        r = scan(self.root)
        self.assertNotIn("contradiction", kinds(r))

    def test_high_cardinality_field_ignored_openly(self):
        for i in range(7):
            write(self.root, f"2026-10-0{i + 1}.md",
                  f"# D{i}\n\n- Topic: subject number {i} here\n")
        r = scan(self.root)
        self.assertNotIn("contradiction", kinds(r))
        self.assertEqual(r["ignored_log_fields"], 1)

    def test_narrative_value_not_a_fact_claim(self):
        long_a = "a very long narrative sentence " * 6
        long_b = "an entirely different narrative " * 6
        write(self.root, "2026-10-01.md", f"# A\n\n- Note: {long_a}\n")
        write(self.root, "2026-11-01.md", f"# B\n\n- Note: {long_b}\n")
        r = scan(self.root)
        self.assertNotIn("contradiction", kinds(r))

    def test_contradiction_within_one_file_not_flagged(self):
        write(self.root, "a.md",
              "# A\n\n- Preferred editor: vim\n\n- Preferred editor: emacs\n")
        r = scan(self.root)
        self.assertNotIn("contradiction", kinds(r))


class TestSuperseded(CorpusTestCase):
    def test_same_title_newer_note_supersedes(self):
        write(self.root, "2026-10-01.md", "# Project status\n\nOld status text here.\n")
        write(self.root, "2026-11-01.md", "# Project status\n\nNew status text here.\n")
        r = scan(self.root)
        sup = [f for f in r["findings"] if f["kind"] == "superseded" and not f["info"]]
        self.assertEqual(len(sup), 1)
        self.assertIn("2026-10-01.md", sup[0]["summary"])
        self.assertIn("2026-11-01.md", sup[0]["details"][0])

    def test_supersede_chain_flags_all_but_newest(self):
        write(self.root, "2026-09-01.md", "# Plan\n\nVersion one of the plan text.\n")
        write(self.root, "2026-10-01.md", "# Plan\n\nVersion two of the plan text.\n")
        write(self.root, "2026-11-01.md", "# Plan\n\nVersion three of the plan text.\n")
        r = scan(self.root)
        sup = [f for f in r["findings"] if f["kind"] == "superseded" and not f["info"]]
        self.assertEqual(len(sup), 2)

    def test_explicit_marker_is_informational(self):
        write(self.root, "a.md",
              "# A\n\n- Old address: 1 Main St — SUPERSEDED by the move in June.\n")
        r = scan(self.root)
        sup = [f for f in r["findings"] if f["kind"] == "superseded"]
        self.assertTrue(sup)
        self.assertTrue(all(f["info"] for f in sup))

    def test_different_titles_not_superseded(self):
        write(self.root, "2026-10-01.md", "# Apples\n\nText about apples here now.\n")
        write(self.root, "2026-11-01.md", "# Oranges\n\nText about oranges here now.\n")
        r = scan(self.root)
        sup = [f for f in r["findings"] if f["kind"] == "superseded" and not f["info"]]
        self.assertEqual(sup, [])


class TestStale(CorpusTestCase):
    def test_stale_note_flagged(self):
        write(self.root, "2020-01-15.md", "# Ancient\n\nThis fact was written long ago.\n")
        write(self.root, "2020-01-16.md", "# Other\n\nUnrelated newer-ish content.\n")
        r = scan(self.root, stale_days=90)
        self.assertIn("stale", kinds(r))

    def test_recent_note_not_stale(self):
        import datetime
        today = datetime.date.today().isoformat()
        write(self.root, f"{today}.md", "# Fresh\n\nWritten today, definitely fresh.\n")
        r = scan(self.root, stale_days=90)
        self.assertNotIn("stale", kinds(r))

    def test_large_threshold_suppresses_stale(self):
        write(self.root, "2020-01-15.md", "# Ancient\n\nThis fact was written long ago.\n")
        r = scan(self.root, stale_days=100000)
        self.assertNotIn("stale", kinds(r))

    def test_referenced_old_note_not_stale(self):
        write(self.root, "2020-01-15.md", "# Ancient\n\nThis fact was written long ago.\n")
        write(self.root, "2020-06-15.md",
              "# Followup\n\nAs noted in 2020-01-15.md, that fact still stands.\n")
        r = scan(self.root, stale_days=90)
        stale = [f for f in r["findings"] if f["kind"] == "stale"
                 and "2020-01-15" in f["summary"]]
        self.assertEqual(stale, [])


class TestCleanCorpus(CorpusTestCase):
    def test_clean_corpus_zero_findings(self):
        import datetime
        base = datetime.date.today()
        d1 = (base - datetime.timedelta(days=2)).isoformat()
        d2 = (base - datetime.timedelta(days=1)).isoformat()
        write(self.root, f"{d1}.md",
              "# Garden\n\nThe tomatoes were planted in the raised bed this spring.\n\n"
              "- Watering day: Saturday\n")
        write(self.root, f"{d2}.md",
              "# Travel\n\nThe train to the coast leaves from the central station.\n\n"
              "- Watering day: Saturday\n")
        r = scan(self.root)
        self.assertEqual(r["findings_total"], 0)


class TestCli(unittest.TestCase):
    def run_cli(self, *argv, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *argv],
                              capture_output=True, text=True, cwd=cwd)

    def test_exit_0_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            import datetime
            today = datetime.date.today().isoformat()
            write(root, f"{today}.md", "# Fresh\n\nA single fresh note with unique text.\n")
            proc = self.run_cli(str(root))
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_exit_1_findings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            write(root, "a.md", "# A\n\nThe user prefers tea over coffee in the morning always.\n")
            write(root, "b.md", "# B\n\nThe user prefers tea over coffee in the morning always.\n")
            proc = self.run_cli(str(root))
            self.assertEqual(proc.returncode, 1)

    def test_exit_2_missing_folder(self):
        proc = self.run_cli("/nonexistent/folder/xyz")
        self.assertEqual(proc.returncode, 2)

    def test_report_header_states_coverage_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            write(root, "a.md", "# A\n\nA single note with some unique text in it.\n")
            proc = self.run_cli(str(root))
            self.assertIn("Semantic contradictions are not checked in v1", proc.stdout)

    def test_json_format(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            write(root, "a.md", "# A\n\nThe user prefers tea over coffee in the morning always.\n")
            write(root, "b.md", "# B\n\nThe user prefers tea over coffee in the morning always.\n")
            proc = self.run_cli(str(root), "--format", "json")
            self.assertEqual(proc.returncode, 1)
            data = json.loads(proc.stdout)
            self.assertEqual(data["notes_scanned"], 2)
            self.assertEqual(data["counts"]["duplicate"], 1)

    def test_readonly_and_propose_goes_to_out_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            write(root, "a.md", "# A\n\nThe user prefers tea over coffee in the morning always.\n")
            write(root, "b.md", "# B\n\nThe user prefers tea over coffee in the morning always.\n")

            def hashes():
                return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in root.glob("*.md")}

            before = hashes()
            out_dir = Path(td) / "out"
            proc = self.run_cli(str(root), "--propose", "--out", str(out_dir))
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(hashes(), before)                      # untouched
            self.assertTrue((out_dir / "dreamlint-proposal.md").exists())
            self.assertFalse((root / "dreamlint-proposal.md").exists())

    def test_propose_inside_scanned_folder_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            write(root, "a.md", "# A\n\nSome note text that is reasonably unique here.\n")
            proc = self.run_cli(str(root), "--propose", "--out", str(root))
            self.assertEqual(proc.returncode, 2)
            self.assertFalse((root / "dreamlint-proposal.md").exists())


if __name__ == "__main__":
    unittest.main()
