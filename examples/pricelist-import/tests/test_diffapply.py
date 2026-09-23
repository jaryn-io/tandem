"""Tests for the S02 diff/apply layer. Run with: python3 -m unittest discover -s tests"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pricelist_import import (
    CatalogStore,
    apply_import,
    compute_diff,
    load_profile,
    parse_file,
)


def make_profile(supplier_id="acme") -> dict:
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
        "code_normalization": {"strip_prefix": "AC-", "uppercase": True},
        "defaults": {"currency": "EUR"},
    }


class DiffApplyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = CatalogStore(self.dir / "catalog.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def write_csv(self, text: str, name: str = "listino.csv") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def import_file(self, path: Path, supplier_id="acme"):
        profile = load_profile(make_profile(supplier_id))
        report = parse_file(path, profile, store=self.store)
        return apply_import(report, self.store)


class TestComputeDiff(DiffApplyTestCase):
    def test_empty_catalog_is_all_new(self):
        path = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\nAC-2;Bullone;2,50\n")
        report = parse_file(path, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual([r.article_code for r in diff.new], ["1", "2"])
        self.assertEqual(diff.changed, [])
        self.assertEqual(diff.no_longer_listed, [])
        self.assertTrue(diff.has_changes)

    def test_after_import_everything_is_unchanged(self):
        path = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n")
        self.import_file(path)
        report = parse_file(path, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual(diff.new, [])
        self.assertEqual(diff.changed, [])
        self.assertEqual([r.article_code for r in diff.unchanged], ["1"])
        self.assertFalse(diff.has_changes)

    def test_updated_file_classifies_new_changed_and_no_longer_listed(self):
        first = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\nAC-2;Bullone;2,50\nAC-3;Rondella;0,75\n",
            name="v1.csv",
        )
        self.import_file(first)
        second = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-1;Vite;1,10\nAC-2;Bullone;2,50\nAC-9;Tassello;4,20\n",
            name="v2.csv",
        )
        report = parse_file(second, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual([r.article_code for r in diff.new], ["9"])
        self.assertEqual(len(diff.changed), 1)
        change = diff.changed[0]
        self.assertEqual(change.article_code, "1")
        self.assertEqual(change.old_price, "1.00")
        self.assertEqual(change.new_price, "1.10")
        self.assertEqual([r.article_code for r in diff.unchanged], ["2"])
        self.assertEqual([a.article_code for a in diff.no_longer_listed], ["3"])

    def test_no_longer_listed_is_scoped_to_the_report_supplier(self):
        acme_file = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", name="a.csv")
        self.import_file(acme_file, "acme")
        other_file = self.write_csv("Codice;Descrizione;Prezzo\nAC-9;Screw;2,00\n", name="b.csv")
        self.import_file(other_file, "nordwind")
        # Re-diffing acme's file must not flag nordwind's article.
        report = parse_file(acme_file, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual(diff.no_longer_listed, [])

    def test_cross_supplier_rows_are_never_classified_or_applied(self):
        first = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-SHARED;Vite;10,00\n", name="a.csv"
        )
        self.import_file(first, "acme")
        hostile = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-SHARED;Hostile;1,00\n", name="b.csv"
        )
        # Parsed WITHOUT a store on purpose: the diff/apply layer itself must
        # still refuse to touch another supplier's article.
        report = parse_file(hostile, load_profile(make_profile("other")))
        diff = compute_diff(report, self.store)
        self.assertEqual(diff.new, [])
        self.assertEqual(diff.changed, [])
        self.assertEqual(diff.unchanged, [])
        self.assertEqual(diff.description_changed, [])
        self.assertFalse(diff.has_changes)
        self.assertEqual(
            [c.article_code for c in diff.cross_supplier], ["SHARED"]
        )
        self.assertEqual(diff.cross_supplier[0].owner_supplier_id, "acme")

        result = apply_import(report, self.store)
        self.assertEqual(
            [c.article_code for c in result.cross_supplier_skipped], ["SHARED"]
        )
        article = self.store.get_article("SHARED")
        self.assertEqual(article.supplier_id, "acme")
        self.assertEqual(article.current_price, "10.00")
        self.assertEqual(article.description, "Vite")
        self.assertEqual(len(self.store.price_history("SHARED")), 1)

    def test_price_formatting_differences_are_not_changes(self):
        first = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n")
        self.import_file(first)
        second = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,0\n", name="v2.csv")
        report = parse_file(second, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual(diff.changed, [])
        self.assertEqual(len(diff.unchanged), 1)

    def test_description_only_change_is_previewed(self):
        first = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", name="v1.csv")
        self.import_file(first)
        second = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite M4 inox;1,00\n", name="v2.csv")
        report = parse_file(second, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual(diff.new, [])
        self.assertEqual(diff.changed, [])
        self.assertEqual(diff.unchanged, [])
        self.assertTrue(diff.has_changes)
        self.assertEqual(len(diff.description_changed), 1)
        change = diff.description_changed[0]
        self.assertEqual(change.article_code, "1")
        self.assertEqual(change.old_description, "Vite")
        self.assertEqual(change.new_description, "Vite M4 inox")

    def test_combined_price_and_description_change_discloses_both(self):
        first = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", name="v1.csv")
        self.import_file(first)
        second = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite M4 inox;1,10\n", name="v2.csv")
        report = parse_file(second, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual(diff.new, [])
        self.assertEqual(diff.unchanged, [])
        self.assertEqual(diff.description_changed, [])
        self.assertEqual(len(diff.changed), 1)
        change = diff.changed[0]
        self.assertEqual(change.article_code, "1")
        self.assertEqual((change.old_price, change.new_price), ("1.00", "1.10"))
        self.assertEqual(change.old_description, "Vite")
        self.assertEqual(change.new_description, "Vite M4 inox")
        self.assertTrue(change.description_updated)

        # Apply performs exactly what the preview disclosed: one price-history
        # row and the description rewrite.
        result = apply_import(report, self.store)
        self.assertEqual((result.inserted, result.updated, result.unchanged), (0, 1, 0))
        self.assertEqual(result.descriptions_refreshed, 0)
        self.assertEqual(self.store.get_article("1").description, "Vite M4 inox")
        self.assertEqual(len(self.store.price_history("1")), 2)

    def test_price_change_with_same_description_discloses_no_description_update(self):
        first = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", name="v1.csv")
        self.import_file(first)
        second = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,10\n", name="v2.csv")
        report = parse_file(second, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertEqual(len(diff.changed), 1)
        change = diff.changed[0]
        self.assertEqual(change.old_description, "Vite")
        self.assertEqual(change.new_description, "Vite")
        self.assertFalse(change.description_updated)
        self.assertEqual(diff.description_changed, [])


class TestApplyImport(DiffApplyTestCase):
    def test_apply_writes_only_differences_and_keeps_history(self):
        first = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\nAC-2;Bullone;2,50\n",
            name="v1.csv",
        )
        result1 = self.import_file(first)
        self.assertFalse(result1.skipped_identical)
        self.assertEqual((result1.inserted, result1.updated, result1.unchanged), (2, 0, 0))

        second = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-1;Vite;1,10\nAC-2;Bullone;2,50\nAC-9;Tassello;4,20\n",
            name="v2.csv",
        )
        result2 = self.import_file(second)
        self.assertEqual((result2.inserted, result2.updated, result2.unchanged), (1, 1, 1))
        self.assertEqual(result2.no_longer_listed, [])

        # History: AC-1 has two entries, unchanged AC-2 still one.
        self.assertEqual(len(self.store.price_history("1")), 2)
        self.assertEqual(len(self.store.price_history("2")), 1)
        # No-longer-listed articles are kept, not deleted.
        article = self.store.get_article("2")
        self.assertEqual(article.current_price, "2.50")

    def test_reimporting_identical_file_is_a_noop(self):
        path = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n")
        result1 = self.import_file(path)
        runs_before = self.store._conn.execute("SELECT COUNT(*) c FROM import_runs").fetchone()["c"]

        result2 = self.import_file(path)
        self.assertTrue(result2.skipped_identical)
        self.assertEqual(result2.previous_run_id, result1.run_id)
        self.assertIsNone(result2.run_id)
        runs_after = self.store._conn.execute("SELECT COUNT(*) c FROM import_runs").fetchone()["c"]
        self.assertEqual(runs_before, runs_after)
        self.assertEqual(len(self.store.price_history("1")), 1)

    def test_same_content_under_new_name_is_still_a_noop(self):
        path = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", name="september.csv")
        self.import_file(path)
        copy = self.dir / "october-resend.csv"
        copy.write_bytes(path.read_bytes())
        result = self.import_file(copy)
        self.assertTrue(result.skipped_identical)

    def test_rejected_rows_never_reach_the_catalog(self):
        path = self.write_csv(
            "Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\nAC-2;Rotto;abc\n;SenzaCodice;3,00\n"
        )
        result = self.import_file(path)
        self.assertEqual(result.rows_rejected, 2)
        self.assertEqual(result.inserted, 1)
        self.assertEqual([a.article_code for a in self.store.list_articles()], ["1"])

    def test_apply_is_transactional_on_failure(self):
        path = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\nAC-2;Bullone;2,50\n")
        profile = load_profile(make_profile())
        report = parse_file(path, profile, store=self.store)

        original = self.store.upsert_article
        calls = {"n": 0}

        def failing_upsert(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated mid-import failure")
            return original(*args, **kwargs)

        self.store.upsert_article = failing_upsert
        with self.assertRaises(RuntimeError):
            apply_import(report, self.store)
        self.store.upsert_article = original

        self.assertEqual(self.store.list_articles(), [])
        runs = self.store._conn.execute("SELECT COUNT(*) c FROM import_runs").fetchone()["c"]
        self.assertEqual(runs, 0)
        # After rollback the same file can be applied cleanly.
        result = apply_import(report, self.store)
        self.assertEqual(result.inserted, 2)

    def test_description_only_change_updates_without_history(self):
        first = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite;1,00\n", name="v1.csv")
        self.import_file(first)
        second = self.write_csv("Codice;Descrizione;Prezzo\nAC-1;Vite M4 inox;1,00\n", name="v2.csv")
        result = self.import_file(second)
        self.assertEqual((result.inserted, result.updated), (0, 0))
        self.assertEqual(result.descriptions_refreshed, 1)
        self.assertEqual(result.unchanged, 0)
        self.assertEqual(self.store.get_article("1").description, "Vite M4 inox")
        self.assertEqual(len(self.store.price_history("1")), 1)
        # After the refresh the same file previews as fully unchanged.
        report = parse_file(second, load_profile(make_profile()), store=self.store)
        diff = compute_diff(report, self.store)
        self.assertFalse(diff.has_changes)


if __name__ == "__main__":
    unittest.main()
