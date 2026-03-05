"""Filter LINE chat export for Sunday attendance reports.

Reads a LINE text export like `LINE chat report.txt` and keeps only Sunday
attendance report messages.

Current rule:
    - Keep only messages that have an "Adult" count, OR are "Total-only"
        (have a "Total" count and do not mention "Adult").

It also checks per Sunday whether there is at least one Zhongli report and at
least one Taipei report, and emits missing-coverage reports.

This script uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DATE_HEADER_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\d{2})/(\d{2})/(\d{4})\s*$")
MESSAGE_START_RE = re.compile(r"^(\d{1,2}):(\d{2})(AM|PM)\t")


@dataclass(frozen=True)
class LineMessage:
    date: dt.date
    dow: str
    date_header: str
    lines: tuple[str, ...]

    @property
    def raw_text(self) -> str:
        return "\n".join(self.lines)

    def body_lines(self) -> list[str]:
        """Message body lines (first line after sender + continuation lines)."""
        if not self.lines:
            return []
        first = self.lines[0]
        parts = first.split("\t")
        body_first = parts[2] if len(parts) >= 3 else ""
        # Preserve literal continuation lines as-is.
        return [body_first, *self.lines[1:]]


def parse_line_export(lines: Iterable[str]) -> tuple[list[LineMessage], set[dt.date]]:
    """Parse LINE export into message blocks.

    Returns:
      - list of messages
      - set of all dates that appear as a date header
    """

    messages: list[LineMessage] = []
    all_dates: set[dt.date] = set()

    current_date: dt.date | None = None
    current_dow: str | None = None
    current_date_header: str | None = None
    current_msg_lines: list[str] | None = None

    def flush_message() -> None:
        nonlocal current_msg_lines
        if current_msg_lines is None:
            return
        if current_date is None or current_dow is None or current_date_header is None:
            current_msg_lines = None
            return
        # Strip trailing newlines but preserve other whitespace.
        msg_lines = tuple(line.rstrip("\n") for line in current_msg_lines)
        messages.append(
            LineMessage(
                date=current_date,
                dow=current_dow,
                date_header=current_date_header,
                lines=msg_lines,
            )
        )
        current_msg_lines = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        date_match = DATE_HEADER_RE.match(line)
        if date_match:
            flush_message()
            dow, mm, dd, yyyy = date_match.groups()
            current_dow = dow
            current_date = dt.date(int(yyyy), int(mm), int(dd))
            current_date_header = line
            all_dates.add(current_date)
            continue

        if MESSAGE_START_RE.match(line):
            flush_message()
            current_msg_lines = [line]
            continue

        # Continuation line.
        if current_msg_lines is not None:
            current_msg_lines.append(line)

    flush_message()
    return messages, all_dates


def is_attendance_report(msg: LineMessage) -> bool:
    """True if this message looks like a Sunday attendance report.

    Current user rule:
      - Keep only messages that have an "Adult" count, OR are "Total-only"
        (have a "Total" count and do not mention "Adult").
      - Reports must mention a location (Zhongli/Taipei) so coverage can be checked.
    """

    body_lines = msg.body_lines()
    body_cf = "\n".join(body_lines).casefold()

    # Attendance reports in this workflow are for Zhongli/Taipei.
    if re.search(r"\b(zhongli|taipei)\b", body_cf) is None:
        return False

    def has_token_count(token: str) -> bool:
        # Require an explicit 1-3 digit count tied to the token.
        # Supports both "Adult 50" and "50 Adult" styles.
        # Avoid false positives on time/date fragments (e.g. "10:30", "3/2").
        count_pat = r"(\d{1,3})(?![:/]\d)"
        for raw in body_lines:
            line = raw.strip().strip('"')
            if not line:
                continue
            cf = line.casefold()
            if re.search(rf"\b{token}\b", cf) is None:
                continue

            # token then number
            if re.search(rf"\b{token}\b\s*[:=+\-]?\s*{count_pat}\b", cf):
                return True

            # number then token (keep this conservative)
            if re.search(rf"\b{count_pat}\b\s*[:=+\-]?\s*\b{token}\b", cf):
                return True
        return False

    def has_adult_plus_college_count() -> bool:
        # Common format: "Adult + college 29" (no separate Adult count)
        for raw in body_lines:
            line = raw.strip().strip('"')
            if not line:
                continue
            cf = line.casefold()
            if "adult" in cf and "college" in cf and "+" in cf and re.search(r"\b\d{1,3}\b", cf):
                return True
        return False

    has_adult_word = re.search(r"\badult\b", body_cf) is not None
    adult_ok = has_token_count("adult") or has_adult_plus_college_count()
    total_ok = has_token_count("total")

    # Keep only Adult-count reports, or Total-only reports.
    if adult_ok:
        return True
    if total_ok and not has_adult_word:
        return True
    return False


def detect_location(msg: LineMessage) -> str | None:
    # Location is usually in the first non-empty lines of the body.
    # Some reports start with a prefix like "IFGF Zhongli ..." so we match
    # whole words anywhere on the line.
    body = msg.body_lines()

    # Pass 1: try to find an explicit location mention.
    checked = 0
    for raw in body:
        cleaned = raw.strip().strip('"')
        if not cleaned:
            continue
        lowered = cleaned.casefold()
        if re.search(r"\bzhongli\b", lowered):
            return "Zhongli"
        if re.search(r"\btaipei\b", lowered):
            return "Taipei"
        checked += 1
        if checked >= 6:
            break

    # Pass 2: fall back to the first non-empty body line (helpful for debugging).
    for raw in body:
        cleaned = raw.strip().strip('"')
        if cleaned:
            return cleaned

    return None


def detect_locations(msg: LineMessage) -> set[str]:
    """Detect all locations mentioned in a message body.

    Some reports include BOTH Zhongli and Taipei in the same message bubble.
    For coverage checking we need to count such a message as covering both.
    """

    text = "\n".join(msg.body_lines()).casefold()
    locs: set[str] = set()
    if re.search(r"\bzhongli\b", text):
        locs.add("Zhongli")
    if re.search(r"\btaipei\b", text):
        locs.add("Taipei")
    return locs


def sunday_range(min_date: dt.date, max_date: dt.date) -> list[dt.date]:
    if min_date > max_date:
        return []

    # Find the first Sunday on/after min_date.
    days_ahead = (6 - min_date.weekday()) % 7  # Monday=0 ... Sunday=6
    first_sunday = min_date + dt.timedelta(days=days_ahead)

    sundays: list[dt.date] = []
    cur = first_sunday
    while cur <= max_date:
        sundays.append(cur)
        cur += dt.timedelta(days=7)
    return sundays


def format_date_header(date: dt.date) -> str:
    return f"Sun, {date:%m/%d/%Y}"


def parse_time_sort_key(msg: LineMessage) -> int:
    if not msg.lines:
        return -1
    m = MESSAGE_START_RE.match(msg.lines[0])
    if not m:
        return -1
    hh, mm, ampm = m.groups()
    hour = int(hh) % 12
    if ampm.upper() == "PM":
        hour += 12
    return hour * 60 + int(mm)


def recap_token_score(msg: LineMessage) -> int:
    """Heuristic score for how 'complete' a recap looks.

    Used only as a tie-breaker when multiple messages exist for the same
    date+location (common when afternoon sends a revision).
    """

    tokens = ("total", "adult", "college", "youth", "kids", "kid", "child", "children", "online", "zoom")
    count_pat = r"(\d{1,3})(?![:/]\d)"
    found: set[str] = set()
    for raw in msg.body_lines():
        line = raw.strip().strip('"')
        if not line:
            continue
        cf = line.casefold()
        for token in tokens:
            if token not in cf:
                continue
            if re.search(rf"\b{re.escape(token)}\b\s*[:=+\-]?\s*{count_pat}\b", cf) or re.search(
                rf"\b{count_pat}\b\s*[:=+\-]?\s*\b{re.escape(token)}\b", cf
            ):
                found.add(token)
    return len(found)


def choose_latest(messages: list[LineMessage]) -> LineMessage:
    # Prefer the latest timestamp; tie-breaker by how many recap tokens are present.
    return max(messages, key=lambda m: (parse_time_sort_key(m), recap_token_score(m)))


def choose_latest_for_location(messages: list[LineMessage], loc: str) -> LineMessage:
    """Pick the best message for a given location.

    Prefer a message that mentions ONLY that location (not both). If none exist,
    fall back to the latest among all messages that mention the location.
    """

    loc_only = [m for m in messages if detect_locations(m) == {loc}]
    return choose_latest(loc_only if loc_only else messages)


def _normalized_message_body(msg: LineMessage) -> str:
    """Normalize message body for de-dup comparisons.

    We intentionally ignore sender/time and focus on the report content.
    """

    text = "\n".join(line.strip().strip('"') for line in msg.body_lines()).strip()
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def _dedupe_consecutive_same_content(messages: list[LineMessage]) -> list[LineMessage]:
    """Drop consecutive duplicate-content messages, keeping the later one.

    Rule: if message i has the same (normalized) body as message i+1,
    remove message i.
    """

    if len(messages) <= 1:
        return messages

    ordered = sorted(messages, key=parse_time_sort_key)
    kept: list[LineMessage] = []
    for i, msg in enumerate(ordered):
        if i + 1 < len(ordered) and _normalized_message_body(msg) == _normalized_message_body(ordered[i + 1]):
            continue
        kept.append(msg)
    return kept


def _has_revisi(msg: LineMessage) -> bool:
    return "revisi" in _normalized_message_body(msg)


def _reporter_name(msg: LineMessage) -> str:
    if not msg.lines:
        return ""
    parts = msg.lines[0].split("\t")
    return parts[1].strip() if len(parts) >= 2 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter Sunday Adult/Youth/Kids attendance reports from LINE export")
    parser.add_argument(
        "--input",
        default=str(Path("LINE chat report.txt")),
        help="Path to LINE export txt",
    )
    parser.add_argument(
        "--output",
        default=str(Path("results") / "LINE chat report.final.filtered.txt"),
        help="Path to write final filtered txt",
    )
    parser.add_argument(
        "--sunday-output",
        default=str(Path("results") / "LINE chat report.sunday-only.filtered.txt"),
        help="Path to write all Sunday messages txt (unfiltered)",
    )
    parser.add_argument(
        "--sunday-recap-output",
        default=str(Path("results") / "LINE chat report.sunday-recap.filtered.txt"),
        help="Path to write Sunday attendance recap messages txt (duplicates allowed)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(Path("results")),
        help="Directory to write missing-report CSVs",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    sunday_output_path = Path(args.sunday_output)
    sunday_recap_output_path = Path(args.sunday_recap_output)
    results_dir = Path(args.results_dir)

    raw = input_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    messages, all_dates = parse_line_export(raw)

    # Write "Sunday-only" output (all messages on Sundays, unfiltered).
    sundays_all: dict[dt.date, list[LineMessage]] = {}
    for m in messages:
        if m.dow != "Sun":
            continue
        sundays_all.setdefault(m.date, []).append(m)
    for d, msgs in list(sundays_all.items()):
        sundays_all[d] = sorted(msgs, key=parse_time_sort_key)

    sunday_lines: list[str] = []
    sunday_lines.append("Sunday Messages (Unfiltered)")
    sunday_lines.append(f"Source: {input_path.as_posix()}")
    sunday_lines.append(f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for d in sorted(sundays_all.keys()):
        sunday_lines.append("")
        sunday_lines.append(format_date_header(d))
        for msg in sundays_all[d]:
            sunday_lines.append("")
            sunday_lines.extend(msg.lines)
    sunday_output_path.parent.mkdir(parents=True, exist_ok=True)
    sunday_output_path.write_text("\n".join(sunday_lines) + "\n", encoding="utf-8")

    # Filter: Sunday + attendance report.
    sunday_reports: list[LineMessage] = [m for m in messages if m.dow == "Sun" and is_attendance_report(m)]

    # Write "Sunday recap" output (attendance recap messages only; duplicates allowed).
    recap_by_date: dict[dt.date, list[LineMessage]] = {}
    for m in sunday_reports:
        recap_by_date.setdefault(m.date, []).append(m)
    for d, msgs in list(recap_by_date.items()):
        recap_by_date[d] = sorted(msgs, key=parse_time_sort_key)

    recap_lines: list[str] = []
    recap_lines.append("Sunday Attendance Recap Messages (Filtered; Duplicates Allowed)")
    recap_lines.append(f"Source: {input_path.as_posix()}")
    recap_lines.append(f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for d in sorted(recap_by_date.keys()):
        recap_lines.append("")
        recap_lines.append(format_date_header(d))
        for msg in recap_by_date[d]:
            recap_lines.append("")
            recap_lines.extend(msg.lines)
    sunday_recap_output_path.parent.mkdir(parents=True, exist_ok=True)
    sunday_recap_output_path.write_text("\n".join(recap_lines) + "\n", encoding="utf-8")

    # Group by date+location and pick only the latest recap per location.
    by_date_loc: dict[tuple[dt.date, str], list[LineMessage]] = {}
    for m in sunday_reports:
        for loc in detect_locations(m):
            by_date_loc.setdefault((m.date, loc), []).append(m)

    # Zhongli tends to have repeated copy/paste resends; collapse consecutive duplicates
    # so the per-day counts are easier to review.
    for (date, loc), msgs in list(by_date_loc.items()):
        if loc != "Zhongli":
            continue
        by_date_loc[(date, loc)] = _dedupe_consecutive_same_content(msgs)

    chosen_by_date_loc: dict[tuple[dt.date, str], LineMessage] = {}
    by_date: dict[dt.date, list[LineMessage]] = {}
    for (date, loc), msgs in by_date_loc.items():
        if not msgs:
            continue
        chosen = choose_latest_for_location(msgs, loc)
        chosen_by_date_loc[(date, loc)] = chosen
        existing = by_date.setdefault(date, [])
        if chosen not in existing:
            existing.append(chosen)

    # Keep output stable: Zhongli first, then Taipei.
    for d, msgs in list(by_date.items()):
        by_date[d] = sorted(msgs, key=lambda m: ("Zhongli" not in detect_locations(m), parse_time_sort_key(m)))

    # Expectation set: Sundays within each year present in the export.
    # This lets us identify "missing chat" Sundays (no date header) as well.
    if not all_dates:
        print("No dated headers found in export.")
        return 1

    dates_by_year: dict[int, list[dt.date]] = {}
    for d in all_dates:
        dates_by_year.setdefault(d.year, []).append(d)

    expected_sundays: list[dt.date] = []
    for year, dates in dates_by_year.items():
        expected_sundays.extend(sunday_range(min(dates), max(dates)))
    expected_sundays = sorted(set(expected_sundays))

    missing_chat_sundays = [d for d in expected_sundays if d not in all_dates]

    # Coverage check (based on the latest-per-location messages we actually write).
    coverage: dict[dt.date, dict[str, bool]] = {}
    for d in expected_sundays:
        coverage[d] = {
            "Zhongli": (d, "Zhongli") in chosen_by_date_loc,
            "Taipei": (d, "Taipei") in chosen_by_date_loc,
        }

    missing_zhongli = [d for d in expected_sundays if not coverage[d]["Zhongli"]]
    missing_taipei = [d for d in expected_sundays if not coverage[d]["Taipei"]]

    # Write report summary CSV (missing counts + revisi flags).
    results_dir.mkdir(parents=True, exist_ok=True)
    report_csv = results_dir / "filtered-chat-report.csv"

    def row_for_date(d: dt.date) -> dict[str, object]:
        tpe_msg = chosen_by_date_loc.get((d, "Taipei"))
        zl_msg = chosen_by_date_loc.get((d, "Zhongli"))

        def cell(msg: LineMessage | None) -> tuple[str, str]:
            if msg is None:
                return "MISSING", ""
            reporter = _reporter_name(msg)
            if _has_revisi(msg):
                return "Revisi", reporter
            return "1", reporter

        tpe_cell, tpe_reporter = cell(tpe_msg)
        zl_cell, zl_reporter = cell(zl_msg)
        return {
            "Date": d.isoformat(),
            "Taipei": tpe_cell,
            "Zhongli": zl_cell,
            "TPE-Reporter": tpe_reporter,
            "ZL-Reporter": zl_reporter,
        }

    fieldnames = ["Date", "Taipei", "Zhongli", "TPE-Reporter", "ZL-Reporter"]

    with report_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for d in expected_sundays:
            w.writerow(row_for_date(d))

    # Write output.
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_lines: list[str] = []
    out_lines.append("Filtered Sunday Attendance Reports (Adult count OR Total-only)")
    out_lines.append(f"Source: {input_path.as_posix()}")
    out_lines.append(f"Generated: {generated_at}")

    for d in sorted(by_date.keys()):
        out_lines.append("")
        out_lines.append(format_date_header(d))
        for msg in by_date[d]:
            out_lines.append("")
            out_lines.extend(msg.lines)

    out_lines.append("")
    out_lines.append("=" * 60)
    out_lines.append("Coverage Check (per Sunday)")
    out_lines.append("=" * 60)

    for d in expected_sundays:
        rep_count = len(by_date.get(d, []))
        z_ok = "OK" if coverage[d]["Zhongli"] else "MISSING"
        t_ok = "OK" if coverage[d]["Taipei"] else "MISSING"
        out_lines.append(f"{format_date_header(d)} | reports found: {rep_count} | Zhongli: {z_ok} | Taipei: {t_ok}")

    out_lines.append("")
    out_lines.append("Missing summary")
    out_lines.append("-" * 60)
    out_lines.append(f"Sundays missing from chat export (no date header): {len(missing_chat_sundays)}")
    out_lines.append(f"Missing Zhongli Sundays: {len(missing_zhongli)}")
    out_lines.append(f"Missing Taipei Sundays: {len(missing_taipei)}")
    out_lines.append("")
    out_lines.append(f"Report CSV: {report_csv.as_posix()}")

    output_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    kept_count = sum(len(v) for v in by_date.values())
    print(f"Wrote: {sunday_output_path}")
    print(f"Wrote: {sunday_recap_output_path}")
    print(f"Wrote: {output_path}")
    print(f"Wrote: {report_csv}")
    print(f"Sunday report messages matched (pre-dedupe): {len(sunday_reports)}")
    print(f"Sunday recaps written (post-dedupe): {kept_count}")
    print(f"Sundays in range: {len(expected_sundays)}")
    print(f"Missing Zhongli Sundays: {len(missing_zhongli)}")
    print(f"Missing Taipei Sundays: {len(missing_taipei)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
