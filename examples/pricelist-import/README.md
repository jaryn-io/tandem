> **About this folder.** This is the application produced by a Tandem session on 23 September 2026, published as delivered. The complete record of that session, every message, is at [`sessions/2026-09-23-pricelist-import`](../../sessions/2026-09-23-pricelist-import/00-summary.md). It is an internal test deliverable, not client work and not a product. Changes made for publication, and nothing else: an MIT licence file added, since the session delivered none. Tests: 78/78 (`openpyxl` required for the Excel samples).
>
> Questions: tandem@jaryn.io

# Supplier price-list import tool

A locally runnable command-line tool that imports supplier price lists into a
company article catalog. Each supplier delivers files in its own layout — CSV
or Excel, different columns, separators, decimal formats and article-code
shapes — so the tool is driven by a **mapping profile per supplier**. An
import validates every row, rejects invalid rows with a clear per-row error
report instead of failing the whole file, detects duplicates inside the file
and against the catalog, and shows what would change (new articles, price
changes, articles no longer listed) before applying. Re-importing the same
file is a no-op; importing an updated file applies only the differences and
keeps the full price history.

Everything runs offline: the catalog, import ledger and price history live in
one local SQLite database file.

## Requirements

- Python 3.10+
- `openpyxl` (only needed to read Excel files):

```bash
pip install -r requirements.txt
```

## Quickstart with the bundled samples

Run from this directory. Three sample suppliers are included under
`samples/`: **acme** (CSV, Italian layout with `;` separator and decimal
comma), **nordwind** (Excel workbook) and **bricomania** (a deliberately
dirty CSV with a banner line, latin-1 encoding and five bad rows).

```bash
# 1. Preview the September acme file against an empty catalog (writes nothing)
python3 -m pricelist_import diff samples/acme/listino_2026-09.csv \
    --profile samples/profiles/acme.json

# 2. Apply it
python3 -m pricelist_import import samples/acme/listino_2026-09.csv \
    --profile samples/profiles/acme.json

# 3. Re-importing the identical file is a no-op
python3 -m pricelist_import import samples/acme/listino_2026-09.csv \
    --profile samples/profiles/acme.json
# -> "Identical file already imported (run 1); nothing to do."

# 4. Apply the October update: 2 price changes, 1 new article,
#    1 article no longer listed
python3 -m pricelist_import import samples/acme/listino_2026-10.csv \
    --profile samples/profiles/acme.json

# 5. Inspect the catalog and one article's price history
python3 -m pricelist_import catalog
python3 -m pricelist_import history 102

# 6. The dirty supplier: 5 rows rejected with per-row reasons,
#    3 valid rows still imported
python3 -m pricelist_import import samples/bricomania/listino_2026-09.csv \
    --profile samples/profiles/bricomania.json
```

All commands accept `--db PATH` (default `./catalog.db`) before the
subcommand, e.g. `python3 -m pricelist_import --db /tmp/test.db catalog`.

## Commands

| Command | What it does |
|---|---|
| `diff FILE --profile P` | Validates the file, prints the rejection report and the diff preview. **Writes nothing** (does not even create the database file). |
| `import FILE --profile P` | Same output as `diff`, then applies the differences to the catalog. |
| `catalog` | Lists current catalog articles with supplier. |
| `history CODE` | Shows the price history of one (normalized) article code. |

Exit code is `0` on success and `1` on file-level errors (missing file, bad
profile, unreadable workbook). Row-level rejections are normal operation and
do not change the exit code.

## How imports behave

- **Row validation**: every row is validated independently; a bad row never
  aborts the file. Rejected rows are reported with their physical row number,
  a machine-readable reason code (`missing_article_code`, `invalid_price`,
  `ambiguous_price`, `negative_price`, `invalid_currency`,
  `duplicate_in_file`, `cross_supplier_code`, ...) and the raw source values.
- **Price parsing**: prices are interpreted through the profile's separators.
  A text value using the declared thousands separator in strict 3-digit
  groups is resolved accordingly (`1.234` under a `.`-thousands profile is
  1234); a text value that uses `.` while the profile declares a different
  decimal separator and gives no unambiguous reading is rejected as
  `ambiguous_price` instead of being silently reinterpreted. Numeric Excel
  cells are already machine values and are accepted as-is.
- **Duplicates and supplier ownership**: a repeated article code inside one
  file keeps the first occurrence and rejects the later ones. Codes already
  in the catalog *for the same supplier* are not rejections — they become
  updates. A code owned by a **different** supplier is rejected as
  `cross_supplier_code`: one supplier's file can never change another
  supplier's article, price or description.
- **Diff preview**: accepted rows are classified as NEW / CHANGED /
  DESCRIPTION CHANGED / UNCHANGED against the catalog, plus the articles of
  *that supplier* that are no longer listed in the file. DESCRIPTION CHANGED
  covers rows whose price stands still but whose description differs from the
  catalog; when price and description move together, the single CHANGED entry
  also shows the description update in parentheses. Either way the preview
  accounts for everything applying the file would write. Other suppliers'
  articles are never flagged.
- **Idempotency**: imports are keyed by the SHA-256 of the file content.
  Re-importing a byte-identical file (even under a different name) writes
  nothing.
- **Differential apply**: only new and changed articles are written; a price
  history row is appended only when the price (or currency) actually moved.
  Description-only changes — previewed as DESCRIPTION CHANGED — update the
  description without touching the history. Articles no longer listed are
  **kept** in the catalog with their history — they are reported, never
  deleted.
- **Atomicity**: one import is one database transaction; a mid-import failure
  rolls back everything.
- **Input bounds**: supplier files are untrusted input. A source file larger
  than 50 MB, more than 200 000 data rows, or an .xlsx archive expanding
  beyond 250 MB (or 2 000 000 cells) is refused with a file-level error
  before it can exhaust local resources.
- **Safe output**: supplier-controlled text (descriptions, codes, raw values)
  is escaped before printing, so a crafted file cannot inject terminal
  control sequences into previews, rejection reports, catalog or history
  output.

## Adding a new supplier profile

A profile is a small JSON file. Copy the closest sample from
`samples/profiles/` and adjust it:

1. **Identify the format**: `"format": "csv"` or `"format": "xlsx"`.
2. **Map the columns** under `"columns"`: keys are the canonical fields
   (`article_code`, `description`, `price`, `currency`), values are the exact
   header names in the supplier file. Only `article_code` and `price` are
   required. Extra columns in the file are ignored, and column order does not
   matter — the mapping is by header name.
3. **Describe the layout**:
   - CSV: `"csv": {"delimiter": ";", "encoding": "utf-8-sig", "skip_rows": 0}`
     — `skip_rows` discards banner lines above the header.
   - Excel: `"xlsx": {"sheet": "Listino", "header_row": 1}` — `sheet` is
     optional (defaults to the active sheet).
4. **Describe the numbers**: `"decimal": {"decimal_separator": ",",
   "thousands_separator": "."}` for `1.234,56` style. Textual prices are read
   strictly through these separators: `1.234` with `.` as thousands
   separator means 1234, and a text like `12.90` that only makes sense under
   a *different* separator convention is rejected as ambiguous rather than
   guessed. (Numeric Excel cells carry their own value and need no
   separators.)
5. **Normalize article codes** under `"code_normalization"`: `strip_prefix`
   removes a supplier prefix, `strip_whitespace` removes internal spaces,
   `uppercase` uppercases. Optionally add `"code_pattern"`, a regex the
   normalized code must match.
6. **Set defaults**: `"defaults": {"currency": "EUR"}` fills a missing
   currency column.

Validate the profile against a real file with `diff` first — it shows exactly
which rows would be accepted, rejected and changed, without writing anything.
When the preview looks right, run `import`.

## Project layout

```
pricelist_import/
  profiles.py     mapping-profile schema and loader
  readers.py      profile-driven CSV and Excel readers
  validation.py   row validation, price parsing, code normalization
  duplicates.py   in-file, cross-supplier and against-catalog duplicate detection
  engine.py       parse pipeline: file -> validated ParseReport (read-only)
  store.py        SQLite catalog, price history, import ledger
  diffapply.py    diff computation and idempotent differential apply
  cli.py          command-line interface
samples/
  profiles/       one JSON profile per sample supplier
  acme/ nordwind/ bricomania/
  regenerate_xlsx.py   rebuilds the Excel samples (needs openpyxl)
tests/            regression suite (stdlib unittest, no pytest needed)
```

## Running the tests

```bash
python3 -m unittest discover -s tests
```

The suite covers the engine, the diff/apply layer, the CLI end-to-end and the
shipped samples (including the dirty supplier's exact rejection report).
