"""
LINE Attendance Report Extractor — main entry point.

End-to-end pipeline:
  1. Export attendance CSVs from the LINE chat export
  2. Check CSVs for anomalies and auto-fix where possible
  3. Upload all CSVs to Google Sheets (delete old sheets, recreate fresh)

Usage:
    python main.py
    python main.py --input "LINE chat report.txt" --outdir results --credentials credentials.json
    python main.py --skip-upload        # export + check only, no Sheets upload
    python main.py --skip-check         # skip anomaly check step
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _banner(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  STEP: {title}")
    print("=" * 60)


def step_export(input_path: str, outdir: str) -> None:
    """Parse LINE chat export and write annual CSV files."""
    from src.export_attendance_csv import main as _export_main

    # Temporarily patch sys.argv so the module's argparse picks up our values
    _prev = sys.argv[:]
    sys.argv = ["export_attendance_csv.py", "--input", input_path, "--outdir", outdir]
    try:
        _export_main()
    finally:
        sys.argv = _prev


def step_check(input_path: str, outdir: str) -> None:
    """Detect and auto-fix anomalies in the generated CSVs."""
    from src.check_csv_errors import main as _check_main

    _prev = sys.argv[:]
    sys.argv = ["check_csv_errors.py", "--all", "--fix", "--input", input_path, "--outdir", outdir]
    try:
        _check_main()
    finally:
        sys.argv = _prev


def step_upload(credentials: str, outdir: str, sheet_url: str | None) -> None:
    """Delete old Google Sheets worksheets and upload fresh CSVs."""
    from src.upload_to_sheets import main as _upload_main

    cmd = ["upload_to_sheets.py", "--credentials", credentials, "--input-dir", outdir]
    if sheet_url:
        cmd += ["--sheet-url", sheet_url]

    _prev = sys.argv[:]
    sys.argv = cmd
    try:
        _upload_main()
    finally:
        sys.argv = _prev


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LINE attendance report extractor — full pipeline."
    )
    parser.add_argument(
        "--input",
        default="LINE chat report.txt",
        help="Path to the raw LINE chat export file.",
    )
    parser.add_argument(
        "--outdir",
        default="results",
        help="Directory for generated CSV files (default: results).",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to the Google Service Account JSON key file.",
    )
    parser.add_argument(
        "--sheet-url",
        default=None,
        help="Override the target Google Sheet URL.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip the Google Sheets upload step.",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip the CSV anomaly check/fix step.",
    )
    args = parser.parse_args()

    # ── Step 1: Export ────────────────────────────────────────────────────────
    _banner("Export attendance CSVs")
    step_export(args.input, args.outdir)

    # ── Step 2: Check & fix ───────────────────────────────────────────────────
    if args.skip_check:
        print("\n[Skipped] CSV anomaly check (--skip-check flag set).")
    else:
        _banner("Check & auto-fix CSV anomalies")
        step_check(args.input, args.outdir)

    # ── Step 3: Upload ────────────────────────────────────────────────────────
    if args.skip_upload:
        print("\n[Skipped] Google Sheets upload (--skip-upload flag set).")
    elif not Path(args.credentials).exists():
        print(f"\n[Skipped] Google Sheets upload — credentials file not found: '{args.credentials}'")
        print("          Place credentials.json in the project root, then re-run.")
    else:
        _banner("Upload to Google Sheets")
        step_upload(args.credentials, args.outdir, args.sheet_url)

    print()
    print("=" * 60)
    print("  Pipeline complete.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
