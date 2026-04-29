"""Export Sunday attendance reports from LINE export to CSV.

Outputs 2 CSV files per year:
    - <yyyy>-ZL.csv (Zhongli)
    - <yyyy>-TPE.csv (Taipei)

For each year, the CSV includes a row for every Sunday between the first and
last date present in the LINE export for that year.

If a Sunday is missing a report for the location, the row will have
Status=Missing (immediately after Date).

Input: data/LINE chat report.txt (LINE export)

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

from .filter_line_reports import LineMessage, detect_location, parse_line_export


TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(AM|PM)\t")


@dataclass(frozen=True)
class ExtractedCounts:
    total: int | None = None
    adult: int | None = None
    college: int | None = None
    adult_plus_college: int | None = None
    youth: int | None = None
    kids: int | None = None
    online: int | None = None
    zoom: int | None = None

    def score(self) -> int:
        return sum(
            v is not None
            for v in (
                self.total,
                self.adult,
                self.college,
                self.adult_plus_college,
                self.youth,
                self.kids,
                self.online,
                self.zoom,
            )
        )


def parse_time_sort_key(msg: LineMessage) -> int:
    if not msg.lines:
        return -1
    m = TIME_RE.match(msg.lines[0])
    if not m:
        return -1
    hh, mm, ampm = m.groups()
    hour = int(hh) % 12
    if ampm.upper() == "PM":
        hour += 12
    return hour * 60 + int(mm)


def _first_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


# Regex to detect IFGF location section headers inside combined messages.
# Handles "IFGF ZHONGLI", "IFGF TAIPEI", "IFGF TPE", and the common typo
# "FGF TAIPEI" (missing leading I).
_IFGF_SECTION_RE = re.compile(r"\bI?FGF\s+(ZHONGLI|ZL|TAIPEI|TPE)\b", re.IGNORECASE)


def _parse_value_for_token(line: str, token: str) -> int | None:
    """Return the integer count associated with a keyword token on a single line."""
    v = _first_int(rf"\b{token}\b\s*[:=]?\s*(\d+)\b", line)
    if v is not None:
        return v
    return _first_int(rf"(\d+)\D*\b{token}\b", line)


def _parse_value_for_regex(line: str, token_regex: str) -> int | None:
    v = _first_int(rf"\b(?:{token_regex})\b\s*[:=]?\s*(\d+)\b", line)
    if v is not None:
        return v
    return _first_int(rf"(\d+)\D*\b(?:{token_regex})\b", line)


def extract_counts_from_lines(body_lines: list[str]) -> ExtractedCounts:
    """Extract attendance counts from a list of body lines (no LineMessage needed)."""

    total: int | None = None
    adult: int | None = None
    college: int | None = None
    adult_plus_college: int | None = None
    youth: int | None = None
    kids: int | None = None
    online: int | None = None
    zoom: int | None = None

    for raw in body_lines:
        line = raw.strip().strip('"')
        if not line:
            continue
        cf = line.casefold()

        # Combined line: "Adult + college 29" (avoid double-filling Adult/College)
        if "adult" in cf and "college" in cf and "+" in cf:
            if adult_plus_college is None:
                adult_plus_college = _first_int(r"(\d+)", line)
            continue

        if total is None and re.search(r"\b(total|ttl)\b", cf):
            total = _parse_value_for_token(line, "total") or _parse_value_for_token(line, "ttl")

        # Parse each token independently; it's common to have multiple tokens
        # on the same line (e.g., "college 16 dan adult 9").
        if adult is None and re.search(r"\badult\b", cf):
            adult = _parse_value_for_token(line, "adult")
        if college is None and re.search(r"\bcollege\b", cf):
            college = _parse_value_for_token(line, "college")
        if youth is None and (re.search(r"\byouth\b", cf) or re.search(r"\bty\b", cf)):
            youth = _parse_value_for_token(line, "youth") or _parse_value_for_token(line, "ty")
        if kids is None and re.search(r"\b(kids?|kid|child|children)\b", cf):
            kids = (
                _parse_value_for_token(line, "kids")
                or _parse_value_for_token(line, "kid")
                or _parse_value_for_regex(line, r"child(?:ren)?")
            )
        if online is None and re.search(r"\bonline\b", cf):
            online = _parse_value_for_token(line, "online")
        if zoom is None and re.search(r"\bzoom\b", cf):
            zoom = _parse_value_for_token(line, "zoom")

    # Shorthand fallback: numbers without labels, typically on a line that includes
    # the location, e.g. "Zhongli 43/4/1" or "Taipei 50 1 4" or "TPE 50 1 4".
    if (
        total is None
        and adult is None
        and youth is None
        and kids is None
        and college is None
        and adult_plus_college is None
    ):
        for raw in body_lines:
            line = raw.strip().strip('"')
            if not line:
                continue
            cf = line.casefold()
            if not re.search(r"\b(zhongli|taipei|tpe)\b", cf):
                continue

            nums = [int(x) for x in re.findall(r"\b\d{1,3}\b", line)]
            if len(nums) >= 4:
                adult, college, youth, kids = nums[0], nums[1], nums[2], nums[3]
                break
            if len(nums) >= 3:
                adult, youth, kids = nums[0], nums[1], nums[2]
                break
            if len(nums) == 1:
                total = nums[0]
                break

    return ExtractedCounts(
        total=total,
        adult=adult,
        college=college,
        adult_plus_college=adult_plus_college,
        youth=youth,
        kids=kids,
        online=online,
        zoom=zoom,
    )


def extract_counts(msg: LineMessage) -> ExtractedCounts:
    return extract_counts_from_lines(msg.body_lines())


def split_combined_locations(msg: LineMessage) -> list[tuple[str, list[str]]]:
    """Detect and split messages that contain both a Zhongli and a Taipei section.

    Many reporters post a single message like:
        IFGF ZHONGLI [date]
        Adult : N  College : N  ...
        IFGF TAIPEI [date]
        Adult : N  College : N  ...

    Returns a list of (canonical_location, body_lines_for_that_section) pairs,
    or an empty list if this is not a combined message.
    """
    body = msg.body_lines()
    sections: list[tuple[str, int]] = []  # (location, start_line_index)
    seen_locs: set[str] = set()

    for i, raw in enumerate(body):
        line = raw.strip().strip('"')
        m = _IFGF_SECTION_RE.search(line)
        if m:
            tok = m.group(1).upper()
            loc = "Taipei" if tok in ("TAIPEI", "TPE") else "Zhongli"
            if loc not in seen_locs:
                sections.append((loc, i))
                seen_locs.add(loc)

    if len(sections) < 2:
        return []

    result: list[tuple[str, list[str]]] = []
    for idx, (loc, start) in enumerate(sections):
        end = sections[idx + 1][1] if idx + 1 < len(sections) else len(body)
        result.append((loc, body[start:end]))
    return result


def sunday_list_between(start: dt.date, end: dt.date) -> list[dt.date]:
    if start > end:
        return []

    # Find first Sunday on/after start.
    days_ahead = (6 - start.weekday()) % 7  # Monday=0 ... Sunday=6
    first = start + dt.timedelta(days=days_ahead)

    out: list[dt.date] = []
    cur = first
    while cur <= end:
        out.append(cur)
        cur += dt.timedelta(days=7)
    return out


def quarter_of(date: dt.date) -> int:
    return (date.month - 1) // 3 + 1


def safe_location(raw: str | None) -> str | None:
    if raw is None:
        return None
    raw_cf = raw.casefold()
    has_z = re.search(r"\bzhongli\b", raw_cf) is not None
    has_t = re.search(r"\b(taipei|tpe)\b", raw_cf) is not None
    # If both appear, it's ambiguous; do not auto-assign.
    if has_z and has_t:
        return None
    if has_z:
        return "Zhongli"
    if has_t:
        return "Taipei"
    return None


def detect_location_loose(msg: LineMessage) -> str | None:
    """Best-effort location detection.

    Uses the shared detect_location() (which looks at early body lines) and also
    falls back to searching the raw message text.
    """

    loc = safe_location(detect_location(msg))
    if loc:
        return loc

    # IMPORTANT: search only the message body, not sender name.
    text = "\n".join(msg.body_lines()).casefold()
    has_z = re.search(r"\bzhongli\b", text) is not None
    has_t = re.search(r"\b(taipei|tpe)\b", text) is not None
    if has_z and has_t:
        return None
    if has_z:
        return "Zhongli"
    if has_t:
        return "Taipei"
    return None


def choose_best(messages: list[LineMessage]) -> LineMessage:
    # Prefer the latest time; tie-breaker by the most extracted fields.
    scored = []
    for m in messages:
        counts = extract_counts(m)
        scored.append((parse_time_sort_key(m), counts.score(), m))
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[-1][2]


def looks_like_report(counts: ExtractedCounts) -> bool:
    """Heuristic: accept as a report if we have enough structured signal.

    Some Sundays are reported as only "Total ... Kids ... Online ...".
    Others include Adult/College/Youth/Kids.
    """

    key_present = counts.total is not None or counts.adult is not None or counts.adult_plus_college is not None or counts.college is not None
    if not key_present:
        return False

    score_core = sum(
        v is not None
        for v in (
            counts.total,
            counts.adult,
            counts.college,
            counts.adult_plus_college,
            counts.youth,
            counts.kids,
        )
    )
    score_extras = sum(v is not None for v in (counts.online, counts.zoom))

    # Accept total-only reports (some weeks are reported as just "Total N").
    if counts.total is not None and (score_core + score_extras) >= 1:
        return True

    return (score_core + score_extras) >= 2


def write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_list = list(rows)
    fieldnames = [
        "Date",
        "Status",
        "Inferred",
        "Adult",
        "College",
        "Youth/TY",
        "Kids",
        "Online",
        "Total",
        "SourceTime",
        "Reporter",
    ]

    def as_int(value: object) -> int:
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        text = str(value).strip()
        return int(text) if text.isdigit() else 0

    def normalize_row(r: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {k: r.get(k, "") for k in fieldnames}

        # Normalize numeric recap fields to 0 when empty.
        adult_v = r.get("Adult")
        college_v = r.get("College")
        youth_v = r.get("Youth/TY")
        kids_v = r.get("Kids")
        online_v = r.get("Online")

        out["Adult"] = as_int(adult_v) if adult_v in (None, "") else adult_v
        out["College"] = as_int(college_v) if college_v in (None, "") else college_v
        out["Youth/TY"] = as_int(youth_v) if youth_v in (None, "") else youth_v
        out["Kids"] = as_int(kids_v) if kids_v in (None, "") else kids_v
        out["Online"] = as_int(online_v) if online_v in (None, "") else online_v

        total_v = r.get("Total")
        if total_v in (None, ""):
            total_calc = (
                as_int(out["Adult"])
                + as_int(out["College"])
                + as_int(out["Youth/TY"])
                + as_int(out["Kids"])
                + as_int(out["Online"])
            )
            out["Total"] = total_calc
        else:
            out["Total"] = as_int(total_v)

        # Reporter rename (backward-compatible if old key is present).
        if not out.get("Reporter") and r.get("Sender"):
            out["Reporter"] = r.get("Sender", "")

        return out
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows_list:
            writer.writerow(normalize_row(r))


def location_code(loc: str) -> str:
    if loc == "Taipei":
        return "TPE"
    if loc == "Zhongli":
        return "ZL"
    raise ValueError(f"Unknown location: {loc!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export LINE Sunday attendance reports to CSV")
    parser.add_argument(
        "--input",
        default="LINE chat report.txt",
        help="Path to LINE export txt",
    )
    parser.add_argument(
        "--outdir",
        default="results",
        help="Output directory for generated annual CSV files",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)

    raw_lines = input_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    messages, all_dates = parse_line_export(raw_lines)

    # Consider all Sunday messages. Some valid reports have counts but omit the
    # location keyword; we can infer them when the other location is present.
    by_date_loc: dict[tuple[dt.date, str], list[LineMessage]] = {}
    # Combined messages split into per-location sections: (msg, section_lines, score)
    by_date_loc_split: dict[tuple[dt.date, str], list[tuple[LineMessage, list[str], int]]] = {}
    candidates_by_date_loc: dict[tuple[dt.date, str], list[LineMessage]] = {}
    unknown_reports_by_date: dict[dt.date, list[LineMessage]] = {}

    for m in messages:
        if m.dow != "Sun":
            continue

        # First try to split combined messages (e.g. "IFGF ZHONGLI ... IFGF TAIPEI ...").
        splits = split_combined_locations(m)
        if splits:
            for loc, section_lines in splits:
                counts = extract_counts_from_lines(section_lines)
                is_reportish = looks_like_report(counts)
                candidates_by_date_loc.setdefault((m.date, loc), []).append(m)
                if is_reportish:
                    by_date_loc_split.setdefault((m.date, loc), []).append(
                        (m, section_lines, parse_time_sort_key(m) * 100 + counts.score())
                    )
            continue

        loc = detect_location_loose(m)  # may be None
        counts = extract_counts(m)
        is_reportish = looks_like_report(counts)

        if loc in ("Zhongli", "Taipei"):
            candidates_by_date_loc.setdefault((m.date, loc), []).append(m)
            if is_reportish:
                by_date_loc.setdefault((m.date, loc), []).append(m)
        else:
            if is_reportish:
                unknown_reports_by_date.setdefault(m.date, []).append(m)

    years = sorted({d.year for d in all_dates})
    if not years:
        print("No dated headers found in export.")
        return 1

    generated_files: list[Path] = []
    diagnostics_lines: list[str] = []

    for year in years:
        # Only include Sundays that actually exist in the export. This prevents
        # marking weeks as Missing when the chat log has no entries for that day.
        sundays = sorted(d for d in all_dates if d.year == year and d.weekday() == 6)
        if not sundays:
            continue

        for loc in ("Zhongli", "Taipei"):
            rows: list[dict[str, object]] = []

            def combined_online(counts: ExtractedCounts) -> int | None:
                vals = [v for v in (counts.online, counts.zoom) if v is not None]
                return sum(vals) if vals else None

            for d in sundays:
                key = (d, loc)
                msgs = by_date_loc.get(key, [])

                if not msgs:
                    # Try split combined messages first (highest fidelity fallback).
                    split_items = by_date_loc_split.get(key, [])
                    if split_items:
                        best = max(split_items, key=lambda t: t[2])
                        chosen_m, chosen_lines, _ = best
                        counts = extract_counts_from_lines(chosen_lines)

                        sender = ""
                        if chosen_m.lines:
                            parts = chosen_m.lines[0].split("\t")
                            if len(parts) >= 2:
                                sender = parts[1]
                        source_time = chosen_m.lines[0].split("\t")[0] if chosen_m.lines else ""

                        adult_val: object = counts.adult
                        college_val: object = counts.college
                        if counts.adult_plus_college is not None and counts.adult is None and counts.college is None:
                            adult_val = counts.adult_plus_college
                            college_val = "Combined with Adult"

                        rows.append(
                            {
                                "Date": d.isoformat(),
                                "Adult": adult_val,
                                "College": college_val,
                                "Youth/TY": counts.youth,
                                "Kids": counts.kids,
                                "Total": counts.total,
                                "Online": combined_online(counts),
                                "Status": "OK",
                                "Inferred": "",
                                "SourceTime": source_time,
                                "Reporter": sender,
                            }
                        )
                        continue

                    # Try inference: if there is an unlabeled report on this date and the
                    # other location already has a report, treat the unlabeled one as this
                    # location.
                    other_loc = "Taipei" if loc == "Zhongli" else "Zhongli"
                    other_has = bool(by_date_loc.get((d, other_loc), [])) or bool(by_date_loc_split.get((d, other_loc), []))
                    unknowns = unknown_reports_by_date.get(d, [])
                    if other_has and unknowns:
                        inferred_msg = choose_best(unknowns)
                        counts = extract_counts(inferred_msg)

                        sender = ""
                        if inferred_msg.lines:
                            parts = inferred_msg.lines[0].split("\t")
                            if len(parts) >= 2:
                                sender = parts[1]

                        source_time = ""
                        if inferred_msg.lines:
                            source_time = inferred_msg.lines[0].split("\t")[0]

                        adult_val: object = counts.adult
                        college_val: object = counts.college
                        if counts.adult_plus_college is not None and counts.adult is None and counts.college is None:
                            adult_val = counts.adult_plus_college
                            college_val = "Combined with Adult"

                        rows.append(
                            {
                                "Date": d.isoformat(),
                                "Adult": adult_val,
                                "College": college_val,
                                "Youth/TY": counts.youth,
                                "Kids": counts.kids,
                                "Total": counts.total,
                                "Online": combined_online(counts),
                                "Status": "OK",
                                "Inferred": "Yes",
                                "SourceTime": source_time,
                                "Reporter": sender,
                            }
                        )

                        diagnostics_lines.append(
                            f"{d.isoformat()} {loc}: inferred from unlabeled report at {source_time} ({sender})"
                        )

                        # Remove the inferred message so it can't be used twice.
                        unknowns.remove(inferred_msg)
                        if not unknowns:
                            unknown_reports_by_date.pop(d, None)

                        continue

                    rows.append(
                        {
                            "Date": d.isoformat(),
                            "Status": "Missing",
                            "Inferred": "",
                        }
                    )

                    # Diagnostics: if there were location-mentioning candidates but we
                    # couldn't parse them into a report, record them.
                    cands = candidates_by_date_loc.get(key, [])
                    if cands:
                        diagnostics_lines.append(f"{d.isoformat()} {loc}: found {len(cands)} candidate message(s) but none parsed as a report")
                        for cm in sorted(cands, key=parse_time_sort_key)[-3:]:
                            preview = cm.raw_text.replace("\n", " ")
                            if len(preview) > 220:
                                preview = preview[:217] + "..."
                            diagnostics_lines.append(f"  - {preview}")

                    continue

                chosen = choose_best(msgs)
                counts = extract_counts(chosen)

                sender = ""
                if chosen.lines:
                    parts = chosen.lines[0].split("\t")
                    if len(parts) >= 2:
                        sender = parts[1]

                source_time = ""
                if chosen.lines:
                    # Keep the raw time token (e.g. 10:55AM)
                    source_time = chosen.lines[0].split("\t")[0]

                rows.append(
                    {
                        "Date": d.isoformat(),
                        "Adult": (counts.adult_plus_college if (counts.adult_plus_college is not None and counts.adult is None and counts.college is None) else counts.adult),
                        "College": (
                            "Combined with Adult"
                            if (counts.adult_plus_college is not None and counts.adult is None and counts.college is None)
                            else counts.college
                        ),
                        "Youth/TY": counts.youth,
                        "Kids": counts.kids,
                        "Total": counts.total,
                        "Online": combined_online(counts),
                        "Status": "OK",
                        "Inferred": "",
                        "SourceTime": source_time,
                        "Reporter": sender,
                    }
                )

            out_path = outdir / f"Absen-{year}-{location_code(loc)}.csv"
            write_csv(out_path, rows)
            generated_files.append(out_path)

    # Write diagnostics file (helps spot alternative formats that still exist).
    diag_path = outdir / "missing_diagnostics.txt"
    diag_header = [
        "Diagnostics for Remaining Missing Sundays",
        f"Source: {input_path.as_posix()}",
        f"Generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "Only includes dates where messages mentioned the location but were not parsed.",
        "",
    ]
    diag_path.write_text("\n".join(diag_header + diagnostics_lines) + "\n", encoding="utf-8")

    print(f"Wrote {len(generated_files)} CSV files under: {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
