#!/usr/bin/env python3
"""Regenerate the Nordwind sample .xlsx files under samples/nordwind/.

Run from the repository root of this deliverable:

    python3 samples/regenerate_xlsx.py

Requires openpyxl (see requirements.txt). The CSV samples are plain text and
shipped as-is; only the Excel files need a generator.
"""

from pathlib import Path

import openpyxl

OUT_DIR = Path(__file__).resolve().parent / "nordwind"

HEADER = ["Artikel-Nr.", "Bezeichnung", "Preis (EUR)", "Lagerbestand"]

SEPTEMBER = [
    ("NW-1001", "Holzschraube 5x60 (200 Stk.)", 6.95, 320),
    ("NW-1002", "Dübel 8mm (100 Stk.)", 4.50, 540),
    ("NW-1003", "Winkelverbinder 40x40", 0.85, 1200),
    ("NW-1004", "Schlossschraube M6x50", "0.32", 3000),
    ("NW-1005", "Dichtungsband 15mm", 2.10, 180),
    ("NW-1006", "Kabelbinder 300mm (100 Stk.)", 5.75, 260),
]

OCTOBER = [
    ("NW-1001", "Holzschraube 5x60 (200 Stk.)", 6.95, 300),
    ("NW-1002", "Dübel 8mm (100 Stk.)", 4.90, 510),   # price change
    ("NW-1003", "Winkelverbinder 40x40", 0.85, 1150),
    ("NW-1004", "Schlossschraube M6x50", "0.32", 2800),
    # NW-1005 no longer listed
    ("NW-1006", "Kabelbinder 300mm (100 Stk.)", 5.75, 240),
    ("NW-1007", "Montagekleber 310ml", 7.30, 90),     # new article
]


def build(path: Path, title: str, rows) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Listino"
    ws.append([title])
    ws.append(HEADER)
    for row in rows:
        ws.append(list(row))
    wb.save(path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build(OUT_DIR / "preise_2026-09.xlsx", "Nordwind GmbH - Preisliste September 2026", SEPTEMBER)
    build(OUT_DIR / "preise_2026-10.xlsx", "Nordwind GmbH - Preisliste Oktober 2026", OCTOBER)
    print(f"wrote 2 files under {OUT_DIR}")


if __name__ == "__main__":
    main()
