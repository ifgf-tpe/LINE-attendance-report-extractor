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


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter Sunday Adult/Youth/Kids attendance reports from LINE export")
    parser.add_argument(
        "--input",
        default=str(Path("data") / "LINE chat report.txt"),
        help="Path to LINE export txt",
    )
    parser.add_argument(
        "--output",
        default=str(Path("data") / "LINE chat report.filtered.txt"),
        help="Path to write filtered txt",
    )
    parser.add_argument(
        "--missing-report",
        default=str(Path("data") / "weeks-missing-report.txt"),
        help="Path to write simplified missing-weeks report",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    missing_report_path = Path(args.missing_report)

    raw = input_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    messages, all_dates = parse_line_export(raw)

    # Filter: Sunday + attendance report.
    sunday_reports: list[LineMessage] = [m for m in messages if m.dow == "Sun" and is_attendance_report(m)]

    # Group by date.
    by_date: dict[dt.date, list[LineMessage]] = {}
    for m in sunday_reports:
        by_date.setdefault(m.date, []).append(m)

    # Expectation set: only Sundays that actually exist in the export.
    # This matches the usual meaning of “missing report for a given Sunday”
    # without assuming the chat log is continuous across weeks.
    expected_sundays = sorted(d for d in all_dates if d.weekday() == 6)

    # Coverage check.
    coverage: dict[dt.date, dict[str, bool]] = {}
    for d in expected_sundays:
        coverage[d] = {"Zhongli": False, "Taipei": False}
        for msg in by_date.get(d, []):
            for loc in detect_locations(msg):
                coverage[d][loc] = True

    missing_zhongli = [d for d in expected_sundays if not coverage[d]["Zhongli"]]
    missing_taipei = [d for d in expected_sundays if not coverage[d]["Taipei"]]

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

    if missing_zhongli:
        out_lines.append("Sundays missing Zhongli report:")
        out_lines.extend([f"- {format_date_header(d)}" for d in missing_zhongli])
    else:
        out_lines.append("Sundays missing Zhongli report: (none)")

    out_lines.append("")
    if missing_taipei:
        out_lines.append("Sundays missing Taipei report:")
        out_lines.extend([f"- {format_date_header(d)}" for d in missing_taipei])
    else:
        out_lines.append("Sundays missing Taipei report: (none)")

    output_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    # Write simplified missing-weeks report to make manual checking easier.
    # Include a few candidate raw messages from the same Sunday that mention
    # the missing location and include either "Adult" or "Total".
    messages_by_date: dict[dt.date, list[LineMessage]] = {}
    for m in messages:
        if m.dow == "Sun":
            messages_by_date.setdefault(m.date, []).append(m)

    def summarize_candidates(date: dt.date, missing_loc: str) -> list[str]:
        candidates: list[str] = []
        for m in messages_by_date.get(date, []):
            text = "\n".join(m.body_lines()).casefold()
            if re.search(rf"\b{missing_loc.casefold()}\b", text) is None:
                continue
            if re.search(r"\b(adult|total)\b", text) is None:
                continue
            if re.search(r"\b\d{1,3}\b", text) is None:
                continue
            one_line = " ".join(bl.strip().strip('"') for bl in m.body_lines() if bl.strip())
            one_line = re.sub(r"\s+", " ", one_line).strip()
            if one_line:
                candidates.append(one_line[:200])
        # keep only a few to avoid huge file
        return candidates[:5]

    missing_lines: list[str] = []
    missing_lines.append("Weeks Missing Report (Sunday coverage gaps)")
    missing_lines.append(f"Source: {input_path.as_posix()}")
    missing_lines.append(f"Generated: {generated_at}")
    missing_lines.append("")

    missing_lines.append("Summary")
    missing_lines.append("-" * 60)
    missing_lines.append(f"Missing Zhongli Sundays: {len(missing_zhongli)}")
    missing_lines.extend([f"- {format_date_header(d)}" for d in missing_zhongli] or ["- (none)"])
    missing_lines.append("")
    missing_lines.append(f"Missing Taipei Sundays: {len(missing_taipei)}")
    missing_lines.extend([f"- {format_date_header(d)}" for d in missing_taipei] or ["- (none)"])
    missing_lines.append("")
    missing_lines.append("Details")
    missing_lines.append("-" * 60)
    missing_lines.append("")

    any_missing = False
    for d in expected_sundays:
        missing_locs = [loc for loc in ("Zhongli", "Taipei") if not coverage[d][loc]]
        if not missing_locs:
            continue
        any_missing = True
        missing_lines.append(format_date_header(d))
        missing_lines.append(f"Missing: {', '.join(missing_locs)}")
        missing_lines.append(f"Kept reports found: {len(by_date.get(d, []))}")
        for loc in missing_locs:
            cands = summarize_candidates(d, loc)
            if cands:
                missing_lines.append(f"Candidates mentioning {loc} (Adult/Total):")
                missing_lines.extend([f"- {c}" for c in cands])
            else:
                missing_lines.append(f"Candidates mentioning {loc} (Adult/Total): (none)")
        missing_lines.append("")

    if not any_missing:
        missing_lines.append("No missing Sundays detected for Zhongli/Taipei.")

    missing_report_path.write_text("\n".join(missing_lines) + "\n", encoding="utf-8")

    print(f"Wrote: {output_path}")
    print(f"Wrote: {missing_report_path}")
    print(f"Sunday report messages kept: {len(sunday_reports)}")
    print(f"Sundays in range: {len(expected_sundays)}")
    print(f"Missing Zhongli Sundays: {len(missing_zhongli)}")
    print(f"Missing Taipei Sundays: {len(missing_taipei)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
