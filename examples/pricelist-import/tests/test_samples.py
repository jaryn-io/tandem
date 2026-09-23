"""Regression tests over the shipped sample suppliers.

These tests pin the promised behavior of the samples: clean files import
without rejections, the dirty file is rejected row-by-row with the expected
reasons, and every shipped profile loads.
Run with: python3 -m unittest discover -s tests
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pricelist_import import CatalogStore, apply_import, load_profile, parse_file

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
PROFILES = SAMPLES / "profiles"


class TestSampleProfiles(unittest.TestCase):
    def test_all_shipped_profiles_load(self):
        names = sorted(p.name for p in PROFILES.glob("*.json"))
        self.assertEqual(names, ["acme.json", "bricomania.json", "nordwind.json"])
        for name in names:
            profile = load_profile(PROFILES / name)
            self.assertEqual(profile.supplier_id, name.removesuffix(".json"))


class TestCleanSamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CatalogStore(Path(self.tmp.name) / "catalog.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def import_sample(self, file: Path, profile_name: str):
        profile = load_profile(PROFILES / profile_name)
        report = parse_file(file, profile, store=self.store)
        return report, apply_import(report, self.store)

    def test_acme_september_imports_clean(self):
        report, result = self.import_sample(SAMPLES / "acme/listino_2026-09.csv", "acme.json")
        self.assertEqual(report.rows_total, 8)
        self.assertEqual(report.rows_rejected, 0)
        self.assertEqual(result.inserted, 8)

    def test_nordwind_september_imports_clean(self):
        report, result = self.import_sample(SAMPLES / "nordwind/preise_2026-09.xlsx", "nordwind.json")
        self.assertEqual(report.rows_total, 6)
        self.assertEqual(report.rows_rejected, 0)
        self.assertEqual(result.inserted, 6)
        # Mixed cell types: numeric 6.95 and string "0.32" both parse.
        prices = {r.article_code: r.price for r in report.accepted}
        self.assertEqual(prices["NW-1001"], Decimal("6.95"))
        self.assertEqual(prices["NW-1004"], Decimal("0.32"))

    def test_acme_october_applies_only_differences(self):
        self.import_sample(SAMPLES / "acme/listino_2026-09.csv", "acme.json")
        report, result = self.import_sample(SAMPLES / "acme/listino_2026-10.csv", "acme.json")
        self.assertEqual((result.inserted, result.updated, result.unchanged), (1, 2, 5))
        self.assertEqual(result.no_longer_listed, ["104"])
        history = self.store.price_history("102")
        self.assertEqual([price for price, *_ in history], ["0.25", "0.28"])

    def test_both_suppliers_share_one_catalog_without_cross_flagging(self):
        self.import_sample(SAMPLES / "acme/listino_2026-09.csv", "acme.json")
        self.import_sample(SAMPLES / "nordwind/preise_2026-09.xlsx", "nordwind.json")
        self.assertEqual(len(self.store.list_articles()), 14)
        self.assertEqual(len(self.store.list_articles(supplier_id="acme")), 8)
        self.assertEqual(len(self.store.list_articles(supplier_id="nordwind")), 6)


class TestDirtySample(unittest.TestCase):
    def test_dirty_file_rejected_row_by_row(self):
        profile = load_profile(PROFILES / "bricomania.json")
        report = parse_file(SAMPLES / "bricomania/listino_2026-09.csv", profile)
        self.assertEqual(report.rows_total, 8)
        self.assertEqual(report.rows_accepted, 3)
        self.assertEqual(
            [r.article_code for r in report.accepted], ["BR-501", "BR-502", "BR-506"]
        )
        reasons = {r.row_number: r.reason_code for r in report.rejections}
        self.assertEqual(
            reasons,
            {
                5: "missing_article_code",
                6: "invalid_price",
                7: "negative_price",
                8: "duplicate_in_file",
                9: "invalid_currency",
            },
        )
        # Rejections carry the raw values for the error report.
        by_row = {r.row_number: r for r in report.rejections}
        self.assertEqual(by_row[6].raw["price"], "n.d.")
        self.assertEqual(by_row[9].raw["currency"], "EURO")


if __name__ == "__main__":
    unittest.main()
