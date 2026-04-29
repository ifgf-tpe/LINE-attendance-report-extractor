"""Check annual attendance CSV files for anomalies and attempt to fix them.

Anomalies detected
------------------
1. Status=OK with all breakdown columns zero  (data was not extracted)
2. Status=Missing for a date that actually has parseable messages in the chat
3. Total value inconsistent with breakdown sum  (|Total - sum| > 5)
4. Runs of N or more consecutive Missing rows  (default N=4)

For each anomaly the script shows the raw chat message(s) and the counts
that *would* be extracted with the corrected split-combined-message logic.

With --fix the script rewrites the CSV with corrected rows.

Usage examples
--------------
  python check_csv_errors.py --year 2025 --loc Taipei
  python check_csv_errors.py --year 2025 --loc Taipei --fix
  python check_csv_errors.py --all
  python check_csv_errors.py --all --fix
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from pathlib import Path
from typing import Iterable

from .filter_line_reports import parse_line_export
from .export_attendance_csv import (
    ExtractedCounts,
    extract_counts,
    extract_counts_from_lines,
    looks_like_report,
    split_combined_locations,
    detect_location_loose,
    parse_time_sort_key,
    write_csv,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_INPUT = "LINE chat report.txt"
DEFAULT_OUTDIR = "results"
CONSECUTIVE_MISSING_THRESHOLD = 4


def location_code(loc: str) -> str:
    return "TPE" if loc == "Taipei" else "ZL"


def _combined_online_local(counts: ExtractedCounts) -> int | None:
    vals = [v for v in (counts.online, counts.zoom) if v is not None]
    return sum(vals) if vals else None


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _row_total(row: dict[str, str]) -> int:
    try:
        return int(row.get("Total", "0") or "0")
    except ValueError:
        return 0


def _row_breakdown_sum(row: dict[str, str]) -> int:
    total = 0
    for col in ("Adult", "College", "Youth/TY", "Kids", "Online"):
        v = row.get(col, "") or ""
        try:
            total += int(v)
        except ValueError:
            pass
    return total


def _is_all_zero_ok(row: dict[str, str]) -> bool:
    return row.get("Status") == "OK" and _row_total(row) == 0 and _row_breakdown_sum(row) == 0


def _total_inconsistent(row: dict[str, str]) -> bool:
    if row.get("Status") != "OK":
        return False
    total = _row_total(row)
    breakdown = _row_breakdown_sum(row)
    if total == 0 or breakdown == 0:
        return False  # Cannot judge; one side is unknown
    return abs(total - breakdown) > 5


# ---------------------------------------------------------------------------
# Core anomaly detector
# ---------------------------------------------------------------------------

def find_anomalies(rows: list[dict[str, str]]) -> list[dict]:
    """Return a list of anomaly dicts, each with keys: type, date, row_index."""
    anomalies: list[dict] = []
    missing_run = 0

    for i, row in enumerate(rows):
        date = row.get("Date", "")
        status = row.get("Status", "")

        if _is_all_zero_ok(row):
            anomalies.append({"type": "zero_ok", "date": date, "row_index": i})

        if _total_inconsistent(row):
            anomalies.append({"type": "total_mismatch", "date": date, "row_index": i,
                               "total": _row_total(row), "breakdown": _row_breakdown_sum(row)})

        if status == "Missing":
            missing_run += 1
            if missing_run == CONSECUTIVE_MISSING_THRESHOLD:
                # Report the start of this run
                start_idx = i - CONSECUTIVE_MISSING_THRESHOLD + 1
                anomalies.append({
                    "type": "consecutive_missing",
                    "date": rows[start_idx]["Date"],
                    "row_index": start_idx,
                    "count": missing_run,
                })
            elif missing_run > CONSECUTIVE_MISSING_THRESHOLD:
                anomalies[-1]["count"] = missing_run  # update count in last run anomaly
        else:
            missing_run = 0

    return anomalies


# ---------------------------------------------------------------------------
# Raw message lookup
# ---------------------------------------------------------------------------

def _build_sunday_message_index(
    input_path: Path,
    year: int,
) -> dict[dt.date, list]:
    """Parse the LINE export and return a {date: [messages]} index for the given year."""
    raw = input_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    msgs, _ = parse_line_export(raw)
    index: dict[dt.date, list] = {}
    for m in msgs:
        if m.dow == "Sun" and m.date.year == year:
            index.setdefault(m.date, []).append(m)
    return index


def _sender(msg) -> str:
    if not msg.lines:
        return ""
    parts = msg.lines[0].split("\t")
    return parts[1] if len(parts) >= 2 else ""


def _source_time(msg) -> str:
    if not msg.lines:
        return ""
    return msg.lines[0].split("\t")[0]


def _counts_to_row_dict(
    date: dt.date,
    counts: ExtractedCounts,
    msg,
    inferred: str = "",
) -> dict[str, object]:
    adult_val: object = counts.adult
    college_val: object = counts.college
    if counts.adult_plus_college is not None and counts.adult is None and counts.college is None:
        adult_val = counts.adult_plus_college
        college_val = "Combined with Adult"

    return {
        "Date": date.isoformat(),
        "Status": "OK",
        "Inferred": inferred,
        "Adult": adult_val,
        "College": college_val,
        "Youth/TY": counts.youth,
        "Kids": counts.kids,
        "Online": _combined_online_local(counts),
        "Total": counts.total,
        "SourceTime": _source_time(msg),
        "Reporter": _sender(msg),
    }


def try_recover_row(
    date_str: str,
    loc: str,
    messages_on_date: list,
) -> dict | None:
    """Try to find a parseable report for this date+location from raw messages.

    Returns a corrected row dict, or None if no data found.
    """
    best_counts: ExtractedCounts | None = None
    best_msg = None
    best_score = -1

    for m in messages_on_date:
        # 1. Try split-combined approach
        splits = split_combined_locations(m)
        if splits:
            for split_loc, section_lines in splits:
                if split_loc != loc:
                    continue
                counts = extract_counts_from_lines(section_lines)
                if not looks_like_report(counts):
                    continue
                score = parse_time_sort_key(m) * 100 + counts.score()
                if score > best_score:
                    best_score = score
                    best_counts = counts
                    best_msg = m
            continue

        # 2. Try direct message if its location matches
        msg_loc = detect_location_loose(m)
        if msg_loc != loc:
            continue
        counts = extract_counts(m)
        if not looks_like_report(counts):
            continue
        score = parse_time_sort_key(m) * 100 + counts.score()
        if score > best_score:
            best_score = score
            best_counts = counts
            best_msg = m

    if best_counts is None or best_msg is None:
        return None

    try:
        date = dt.date.fromisoformat(date_str)
    except ValueError:
        return None

    return _counts_to_row_dict(date, best_counts, best_msg)


# ---------------------------------------------------------------------------
# Main report + fix logic
# ---------------------------------------------------------------------------

def check_csv(
    csv_path: Path,
    loc: str,
    input_path: Path,
    fix: bool = False,
    verbose: bool = True,
) -> int:
    """Check one CSV. Returns number of anomalies found."""

    if not csv_path.exists():
        print(f"[SKIP] CSV not found: {csv_path}")
        return 0

    rows = load_csv(csv_path)
    if not rows:
        print(f"[SKIP] Empty CSV: {csv_path}")
        return 0

    year_str = rows[0].get("Date", "")[:4]
    try:
        year = int(year_str)
    except ValueError:
        print(f"[SKIP] Cannot determine year from CSV: {csv_path}")
        return 0

    anomalies = find_anomalies(rows)

    if not anomalies:
        if verbose:
            print(f"[OK]  {csv_path.name} — no anomalies found.")
        return 0

    print(f"\n{'='*70}")
    print(f"Anomalies in {csv_path.name}  ({len(anomalies)} found)")
    print(f"{'='*70}")

    msg_index = _build_sunday_message_index(input_path, year)
    corrections: dict[int, dict] = {}  # row_index → corrected row dict

    reported_dates: set[str] = set()

    for anom in anomalies:
        date_str = anom["date"]
        atype = anom["type"]

        if atype == "consecutive_missing":
            print(f"\n  [CONSECUTIVE_MISSING] Starting {date_str}: "
                  f"{anom['count']} consecutive Missing rows")
            continue

        if date_str in reported_dates:
            continue
        reported_dates.add(date_str)

        if atype == "zero_ok":
            print(f"\n  [ZERO_OK] {date_str}  — Status=OK but all counts are zero")
        elif atype == "total_mismatch":
            print(f"\n  [TOTAL_MISMATCH] {date_str}  — "
                  f"Total={anom['total']} but breakdown sums to {anom['breakdown']}")

        # Show raw messages for this date
        try:
            date_obj = dt.date.fromisoformat(date_str)
        except ValueError:
            continue

        candidates = msg_index.get(date_obj, [])
        if not candidates:
            print(f"    No raw messages found for {date_str}.")
            continue

        # Show relevant candidates
        shown = 0
        for m in sorted(candidates, key=parse_time_sort_key):
            body_preview = " | ".join(m.body_lines())[:200]
            loc_tag = detect_location_loose(m) or "?"
            splits = split_combined_locations(m)
            if splits:
                loc_tag = "+".join(l for l, _ in splits)
            sender_name = _sender(m)
            print(f"    {_source_time(m)}  [{loc_tag}]  {sender_name}: {body_preview}")
            shown += 1
            if shown >= 4:
                break

        # Attempt recovery
        recovered = try_recover_row(date_str, loc, candidates)
        if recovered:
            row_idx = anom["row_index"]
            corrections[row_idx] = recovered
            c = recovered
            print(f"    → Recovered: Adult={c.get('Adult')} College={c.get('College')} "
                  f"Youth/TY={c.get('Youth/TY')} Kids={c.get('Kids')} "
                  f"Online={c.get('Online')} Total={c.get('Total')}  "
                  f"by {c.get('Reporter')}")
        else:
            print(f"    → Could not recover data for {date_str} / {loc}.")

    # Also check for Missing rows that can now be recovered
    missing_recovered = 0
    for i, row in enumerate(rows):
        if row.get("Status") != "Missing":
            continue
        date_str = row["Date"]
        try:
            date_obj = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        candidates = msg_index.get(date_obj, [])
        if not candidates:
            continue
        recovered = try_recover_row(date_str, loc, candidates)
        if recovered:
            corrections[i] = recovered
            missing_recovered += 1
            if verbose:
                c = recovered
                print(f"\n  [MISSING→RECOVERED] {date_str}: "
                      f"Adult={c.get('Adult')} College={c.get('College')} "
                      f"Youth/TY={c.get('Youth/TY')} Kids={c.get('Kids')} "
                      f"Online={c.get('Online')} Total={c.get('Total')}  "
                      f"by {c.get('Reporter')}")

    print(f"\n  Summary: {len(corrections)} rows correctable "
          f"({missing_recovered} previously-Missing rows recovered).")

    if fix and corrections:
        updated_rows = []
        for i, row in enumerate(rows):
            updated_rows.append(corrections[i] if i in corrections else row)
        write_csv(csv_path, updated_rows)
        print(f"  [FIXED] Wrote corrected CSV: {csv_path}")

    return len(anomalies)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check attendance CSV files for parsing errors and attempt to fix them."
    )
    parser.add_argument("--year", type=int, help="Year to check (e.g. 2025)")
    parser.add_argument("--loc", choices=["Taipei", "Zhongli"],
                        help="Location to check (Taipei or Zhongli)")
    parser.add_argument("--all", action="store_true",
                        help="Check all CSV files in the output directory")
    parser.add_argument("--fix", action="store_true",
                        help="Write corrected CSVs (default: report only)")
    parser.add_argument("--input", default=DEFAULT_INPUT,
                        help="Path to LINE export txt")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR,
                        help="Directory containing CSV files")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress OK messages")
    args = parser.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)

    if not input_path.exists():
        print(f"Error: input file not found: {input_path}")
        return 1

    pairs: list[tuple[int, str]] = []

    if args.all:
        # Discover all CSV files
        for csv_file in sorted(outdir.glob("Absen-*-*.csv")):
            stem = csv_file.stem  # e.g. "Absen-2025-TPE"
            parts = stem.split("-")
            if len(parts) != 3:
                continue
            try:
                yr = int(parts[1])
            except ValueError:
                continue
            loc = "Taipei" if parts[2] == "TPE" else "Zhongli"
            pairs.append((yr, loc))
    elif args.year and args.loc:
        pairs = [(args.year, args.loc)]
    elif args.year:
        pairs = [(args.year, "Taipei"), (args.year, "Zhongli")]
    else:
        parser.print_help()
        return 1

    total_anomalies = 0
    for year, loc in pairs:
        csv_path = outdir / f"Absen-{year}-{location_code(loc)}.csv"
        total_anomalies += check_csv(
            csv_path=csv_path,
            loc=loc,
            input_path=input_path,
            fix=args.fix,
            verbose=not args.quiet,
        )

    if total_anomalies == 0:
        print("All checked CSVs look clean.")
    else:
        print(f"\nTotal anomalies found: {total_anomalies}")
        if not args.fix:
            print("Run with --fix to apply corrections.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
