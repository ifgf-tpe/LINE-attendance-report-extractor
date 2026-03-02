"""Diagnostics for Missing attendance rows.

Scans the raw LINE export for dates marked Missing in a generated CSV and prints
potential report-like messages for those dates.

Usage:
  ./data/.venv/Scripts/python.exe data/diagnose_missing_reports.py --year 2025 --loc Taipei
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from filter_line_reports import parse_line_export


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--loc", choices=["Taipei", "Zhongli"], required=True)
    parser.add_argument(
        "--csv",
        default=None,
        help="Path to annual CSV (defaults to data/attendance_csv_annual/{year}_{loc}.csv)",
    )
    parser.add_argument(
        "--input",
        default=str(Path("data") / "LINE chat report.txt"),
        help="Path to LINE export txt",
    )
    parser.add_argument("--limit", type=int, default=12, help="How many Missing dates to show")
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else Path("data") / "attendance_csv_annual" / f"{args.year}_{args.loc}.csv"

    missing_dates: list[str] = []
    with csv_path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("Status") == "Missing":
                missing_dates.append(row["Date"])

    missing_set = set(missing_dates)

    raw = Path(args.input).read_text(encoding="utf-8", errors="replace").splitlines(True)
    msgs, _ = parse_line_export(raw)

    by_date: dict[str, list] = {}
    for m in msgs:
        if m.dow != "Sun":
            continue
        if m.date.year != args.year:
            continue
        by_date.setdefault(str(m.date), []).append(m)

    # Look for likely report signals.
    kw = re.compile(r"(adult|college|youth|kids?|child|children|total|zoom|online|\b\d{2,}\b)", re.I)

    def body_text(m) -> str:
        return "\n".join(m.body_lines())

    shown = 0
    for d in sorted(missing_set):
        if shown >= args.limit:
            break
        candidates = []
        for m in by_date.get(d, []):
            text = body_text(m)
            if not kw.search(text):
                continue
            # Skip if the body explicitly mentions the location (then it should have been parsed).
            if re.search(rf"\b{re.escape(args.loc)}\b", text, re.I):
                continue
            preview = text.replace("\n", " ")
            preview = re.sub(r"\s+", " ", preview).strip()
            if len(preview) > 220:
                preview = preview[:217] + "..."
            time = m.lines[0].split("\t")[0] if m.lines else ""
            sender = ""
            if m.lines:
                parts = m.lines[0].split("\t")
                if len(parts) >= 2:
                    sender = parts[1]
            candidates.append((time, sender, preview))

        print(f"{d} candidates: {len(candidates)}")
        for time, sender, preview in candidates[:5]:
            print(f"  - {time} {sender}: {preview}")
        shown += 1

    print(f"Missing dates total: {len(missing_dates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
