"""End-to-end tests for the CLI. Run with: python3 -m unittest discover -s tests"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def run_cli(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pricelist_import", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def make_profile_dict(supplier_id: str) -> dict:
    return {
        "supplier_id": supplier_id,
        "format": "csv",
        "csv": {"delimiter": ";", "encoding": "utf-8"},
        "columns": {
            "article_code": "Codice",
            "description": "Descrizione",
            "price": "Prezzo",
        },
        "decimal": {"decimal_separator": ","},
        "defaults": {"currency": "EUR"},
    }


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "catalog.db")

    def tearDown(self):
        self.tmp.cleanup()

    def acme(self, *args):
        return run_cli("--db", self.db, *args, cwd=ROOT)

    def test_diff_is_a_dry_run_and_creates_no_db(self):
        result = self.acme(
            "diff", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEW (8)", result.stdout)
        self.assertFalse(Path(self.db).exists(), "diff must not create the database")

    def test_import_then_catalog_then_history(self):
        result = self.acme(
            "import", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("8 new, 0 changed", result.stdout)
        self.assertTrue(Path(self.db).exists())

        catalog = self.acme("catalog")
        self.assertEqual(catalog.returncode, 0, catalog.stderr)
        self.assertIn("101", catalog.stdout)
        self.assertIn("(supplier: acme)", catalog.stdout)

        history = self.acme("history", "102")
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIn("current: 0.25 EUR", history.stdout)
        self.assertEqual(history.stdout.count("run 1"), 1)

    def test_reimport_identical_file_is_noop(self):
        self.acme("import", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json")
        again = self.acme(
            "import", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json"
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Identical file already imported", again.stdout)

    def test_updated_file_applies_only_differences(self):
        self.acme("import", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json")
        updated = self.acme(
            "import", "samples/acme/listino_2026-10.csv", "--profile", "samples/profiles/acme.json"
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn("NEW (1)", updated.stdout)
        self.assertIn("CHANGED (2)", updated.stdout)
        self.assertIn("0.25 -> 0.28 EUR", updated.stdout)
        self.assertIn("12.90 -> 11.50 EUR", updated.stdout)
        self.assertIn("NO LONGER LISTED (1): 104", updated.stdout)
        self.assertIn("1 new, 2 changed, 5 unchanged", updated.stdout)

        history = self.acme("history", "102")
        self.assertIn("0.25 EUR", history.stdout)
        self.assertIn("0.28 EUR", history.stdout)

        # A no-longer-listed article stays in the catalog with its history.
        gone = self.acme("history", "104")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertIn("current: 0.12 EUR", gone.stdout)

    def test_dirty_supplier_reports_every_bad_row(self):
        result = self.acme(
            "import",
            "samples/bricomania/listino_2026-09.csv",
            "--profile",
            "samples/profiles/bricomania.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Rejected rows (5 of 8)", result.stdout)
        self.assertIn("[missing_article_code]", result.stdout)
        self.assertIn("[invalid_price]", result.stdout)
        self.assertIn("[negative_price]", result.stdout)
        self.assertIn("[duplicate_in_file]", result.stdout)
        self.assertIn("[invalid_currency]", result.stdout)
        self.assertIn("3 new, 0 changed, 0 unchanged, 5 rejected", result.stdout)

    def test_excel_supplier_imports_and_diffs(self):
        first = self.acme(
            "import", "samples/nordwind/preise_2026-09.xlsx", "--profile", "samples/profiles/nordwind.json"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("6 new, 0 changed", first.stdout)

        second = self.acme(
            "import", "samples/nordwind/preise_2026-10.xlsx", "--profile", "samples/profiles/nordwind.json"
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("CHANGED (1)", second.stdout)
        self.assertIn("4.5 -> 4.9 EUR", second.stdout)
        self.assertIn("NEW (1)", second.stdout)
        self.assertIn("NO LONGER LISTED (1): NW-1005", second.stdout)

        # Other suppliers' articles are never flagged as no-longer-listed:
        # acme's September file contains all 8 acme articles, so nothing may
        # be flagged even though nordwind's NW-1005 is absent from its file.
        self.acme("import", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json")
        acme_again = self.acme(
            "diff", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json"
        )
        self.assertIn("NO LONGER LISTED (0)", acme_again.stdout)

    def test_combined_change_preview_discloses_description_update(self):
        v1 = self.dir / "v1.csv"
        v1.write_text("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", encoding="utf-8")
        self.acme("import", str(v1), "--profile", "samples/profiles/acme.json")
        v2 = self.dir / "v2.csv"
        v2.write_text("Codice;Descrizione;Prezzo\nAC-1;Vite M4 inox;1,10\n", encoding="utf-8")
        preview = self.acme("diff", str(v2), "--profile", "samples/profiles/acme.json")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("CHANGED (1)", preview.stdout)
        self.assertIn("1.00 -> 1.10 EUR", preview.stdout)
        self.assertIn("(description: 'Vite' -> 'Vite M4 inox')", preview.stdout)
        self.assertIn("DESCRIPTION CHANGED (0)", preview.stdout)

    def test_history_unknown_article_fails_with_message(self):
        self.acme("import", "samples/acme/listino_2026-09.csv", "--profile", "samples/profiles/acme.json")
        result = self.acme("history", "NOPE")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)

    def test_missing_file_and_bad_profile_exit_1(self):
        result = self.acme("diff", "nope.csv", "--profile", "samples/profiles/acme.json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)

        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = self.acme("diff", "samples/acme/listino_2026-09.csv", "--profile", str(bad))
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)


class TestCliCrossSupplier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "catalog.db")

    def tearDown(self):
        self.tmp.cleanup()

    def acme(self, *args):
        return run_cli("--db", self.db, *args, cwd=ROOT)

    def write_profile(self, supplier_id: str) -> Path:
        path = self.dir / f"{supplier_id}.json"
        path.write_text(json.dumps(make_profile_dict(supplier_id)), encoding="utf-8")
        return path

    def write_csv(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_cross_supplier_code_is_rejected_and_never_applied(self):
        profile_a = self.write_profile("suppliera")
        profile_b = self.write_profile("supplierb")
        a_csv = self.write_csv(
            "a.csv", "Codice;Descrizione;Prezzo\nSHARED;Original;10,00\n"
        )
        b_csv = self.write_csv(
            "b.csv", "Codice;Descrizione;Prezzo\nSHARED;Hostile;1,00\n"
        )
        first = self.acme("import", str(a_csv), "--profile", str(profile_a))
        self.assertEqual(first.returncode, 0, first.stderr)

        preview = self.acme("diff", str(b_csv), "--profile", str(profile_b))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("[cross_supplier_code]", preview.stdout)
        self.assertIn("CHANGED (0)", preview.stdout)
        self.assertIn("NEW (0)", preview.stdout)

        second = self.acme("import", str(b_csv), "--profile", str(profile_b))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 new, 0 changed", second.stdout)

        catalog = self.acme("catalog")
        self.assertIn(
            "SHARED  Original  10.00 EUR  (supplier: suppliera)", catalog.stdout
        )
        history = self.acme("history", "SHARED")
        self.assertEqual(history.stdout.count("run "), 1)


class TestCliOutputEscaping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "catalog.db")

    def tearDown(self):
        self.tmp.cleanup()

    def acme(self, *args):
        return run_cli("--db", self.db, *args, cwd=ROOT)

    def test_control_characters_are_escaped_everywhere(self):
        profile = self.dir / "suppliera.json"
        profile.write_text(
            json.dumps(make_profile_dict("suppliera")), encoding="utf-8"
        )
        csv_path = self.dir / "evil.csv"
        csv_path.write_text(
            "Codice;Descrizione;Prezzo\n"
            "AC-1;Safe \x1b[2J text;1,00\n"
            "AC-2;Bad price row;\x1b[31mabc\n",
            encoding="utf-8",
        )
        result = self.acme("import", str(csv_path), "--profile", str(profile))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\x1b[2J", result.stdout)
        self.assertIn("[invalid_price]", result.stdout)

        catalog = self.acme("catalog")
        self.assertNotIn("\x1b", catalog.stdout)
        self.assertIn("\\x1b[2J", catalog.stdout)

        history = self.acme("history", "AC-1")
        self.assertNotIn("\x1b", history.stdout)

        # A description change preview is escaped too.
        changed = self.dir / "evil2.csv"
        changed.write_text(
            "Codice;Descrizione;Prezzo\nAC-1;Other \x1b[1G desc;1,00\n",
            encoding="utf-8",
        )
        preview = self.acme("diff", str(changed), "--profile", str(profile))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertNotIn("\x1b", preview.stdout)
        self.assertIn("DESCRIPTION CHANGED (1)", preview.stdout)


if __name__ == "__main__":
    unittest.main()
