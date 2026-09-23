"""Command-line interface for the supplier price-list import tool.

Commands:
    diff     FILE --profile PROFILE.json   Dry-run: validate the file, print
                                           the rejection report and the diff
                                           preview. Writes nothing.
    import   FILE --profile PROFILE.json   Same output as diff, then applies
                                           the differences to the catalog.
    catalog                              List the current catalog.
    history  CODE                          Price history of one article.

All commands accept --db PATH (default: ./catalog.db).
Exit code 0 on success, 1 on file-level errors (bad profile, unreadable or
malformed source file). Row-level rejections are normal operation and do not
change the exit code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .diffapply import ApplyResult, CatalogDiff, apply_import, compute_diff
from .engine import parse_file
from .models import ParseReport
from .profiles import ProfileError, load_profile
from .readers import ReaderError
from .store import CatalogStore

DEFAULT_DB = "catalog.db"


def _safe(value: object) -> str:
    """Render a supplier-derived value safe for the terminal.

    Supplier files are external input: descriptions, codes and raw values may
    contain terminal control sequences (cursor moves, screen clears, color
    changes). Anything not printable is escaped to its ``\\xNN`` / ``\\uNNNN``
    form so the operator sees the hostile bytes instead of executing them.
    """
    text = "" if value is None else str(value)
    return "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii")
        for ch in text
    )


def _open_store(db_path: str, create: bool) -> CatalogStore:
    """Open the catalog. For read-only commands on a missing database, use an
    empty in-memory catalog instead of creating a stray file."""
    if not create and not Path(db_path).is_file():
        return CatalogStore(":memory:")
    return CatalogStore(db_path)


def _print_rejection_report(report: ParseReport, out) -> None:
    if not report.rejections:
        return
    print(f"Rejected rows ({report.rows_rejected} of {report.rows_total}):", file=out)
    for rej in report.rejections:
        raw = ", ".join(
            f"{k}={_safe(v)}" for k, v in rej.raw.items() if v not in (None, "")
        )
        print(
            f"  row {rej.row_number} [{rej.reason_code}]: {_safe(rej.message)}",
            file=out,
        )
        if raw:
            print(f"    raw: {raw}", file=out)


def _print_diff(diff: CatalogDiff, out) -> None:
    print(f"Diff preview for supplier {_safe(diff.supplier_id)!r}:", file=out)
    print(f"  NEW ({len(diff.new)}):", file=out)
    for row in diff.new:
        print(
            f"    {_safe(row.article_code)}  {_safe(row.description)}  "
            f"{row.price} {_safe(row.currency)}",
            file=out,
        )
    print(f"  CHANGED ({len(diff.changed)}):", file=out)
    for change in diff.changed:
        line = (
            f"    {_safe(change.article_code)}  {change.old_price} -> "
            f"{change.new_price} {_safe(change.currency)}"
        )
        if change.description_updated:
            line += (
                f"  (description: {_safe(change.old_description)!r} -> "
                f"{_safe(change.new_description)!r})"
            )
        print(line, file=out)
    print(f"  DESCRIPTION CHANGED ({len(diff.description_changed)}):", file=out)
    for change in diff.description_changed:
        print(
            f"    {_safe(change.article_code)}  {_safe(change.old_description)!r} -> "
            f"{_safe(change.new_description)!r}",
            file=out,
        )
    codes = ", ".join(_safe(row.article_code) for row in diff.unchanged) or "-"
    print(f"  UNCHANGED ({len(diff.unchanged)}): {codes}", file=out)
    gone = ", ".join(_safe(a.article_code) for a in diff.no_longer_listed) or "-"
    print(f"  NO LONGER LISTED ({len(diff.no_longer_listed)}): {gone}", file=out)
    if diff.cross_supplier:
        print(f"  CROSS-SUPPLIER REJECTED ({len(diff.cross_supplier)}):", file=out)
        for conflict in diff.cross_supplier:
            print(
                f"    {_safe(conflict.article_code)}  "
                f"(owned by supplier {_safe(conflict.owner_supplier_id)})",
                file=out,
            )


def _print_apply_summary(result: ApplyResult, out) -> None:
    if result.skipped_identical:
        print(
            f"Identical file already imported (run {result.previous_run_id}); "
            "nothing to do.",
            file=out,
        )
        return
    print(
        f"Applied run {result.run_id}: {result.inserted} new, "
        f"{result.updated} changed, {result.unchanged} unchanged, "
        f"{result.rows_rejected} rejected.",
        file=out,
    )
    if result.descriptions_refreshed:
        print(
            f"Descriptions refreshed (no price history): "
            f"{result.descriptions_refreshed}",
            file=out,
        )
    if result.no_longer_listed:
        print(
            f"No longer listed (kept in catalog): "
            f"{', '.join(_safe(code) for code in result.no_longer_listed)}",
            file=out,
        )
    if result.cross_supplier_skipped:
        codes = ", ".join(
            _safe(c.article_code) for c in result.cross_supplier_skipped
        )
        print(
            f"Cross-supplier codes not applied (owned by another supplier): "
            f"{codes}",
            file=out,
        )


def _cmd_diff(args, out, err) -> int:
    profile = load_profile(args.profile)
    with _open_store(args.db, create=False) as store:
        report = parse_file(args.file, profile, store=store)
        _print_rejection_report(report, out)
        diff = compute_diff(report, store)
        _print_diff(diff, out)
    return 0


def _cmd_import(args, out, err) -> int:
    profile = load_profile(args.profile)
    with _open_store(args.db, create=True) as store:
        report = parse_file(args.file, profile, store=store)
        _print_rejection_report(report, out)
        diff = compute_diff(report, store)
        _print_diff(diff, out)
        result = apply_import(report, store)
        _print_apply_summary(result, out)
    return 0


def _cmd_catalog(args, out, err) -> int:
    with _open_store(args.db, create=False) as store:
        articles = store.list_articles()
        if not articles:
            print("Catalog is empty.", file=out)
            return 0
        for a in articles:
            print(
                f"{_safe(a.article_code)}  {_safe(a.description)}  "
                f"{a.current_price} {_safe(a.currency)}  "
                f"(supplier: {_safe(a.supplier_id)})",
                file=out,
            )
    return 0


def _cmd_history(args, out, err) -> int:
    with _open_store(args.db, create=False) as store:
        article = store.get_article(args.code)
        if article is None:
            print(f"Article {_safe(args.code)!r} not found in catalog.", file=err)
            return 1
        print(
            f"{_safe(article.article_code)}  {_safe(article.description)}  "
            f"current: {article.current_price} {_safe(article.currency)}",
            file=out,
        )
        history = store.price_history(article.article_code)
        for price, currency, recorded_at, run_id in history:
            print(
                f"  {recorded_at}  run {run_id}: {price} {_safe(currency)}",
                file=out,
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pricelist_import",
        description="Import supplier price lists into a local article catalog.",
    )
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"catalog database path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, helptext in (
        ("diff", "validate a file and preview changes without applying"),
        ("import", "validate a file, preview changes and apply them"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("file", help="supplier price-list file (CSV or .xlsx)")
        cmd.add_argument("--profile", required=True,
                         help="supplier mapping profile (JSON)")

    sub.add_parser("catalog", help="list current catalog articles")

    hist = sub.add_parser("history", help="price history of one article")
    hist.add_argument("code", help="normalized article code")

    return parser


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    args = build_parser().parse_args(argv)
    handlers = {
        "diff": _cmd_diff,
        "import": _cmd_import,
        "catalog": _cmd_catalog,
        "history": _cmd_history,
    }
    try:
        return handlers[args.command](args, out, err)
    except (ProfileError, ReaderError) as exc:
        print(f"error: {exc}", file=err)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=err)
        return 1
