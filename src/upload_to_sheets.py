import argparse
import csv
from pathlib import Path

import sys

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("Please install required packages first:")
    print("pip install gspread google-auth")
    sys.exit(1)

# Scopes needed for Google Sheets & Drive
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def main() -> int:
    parser = argparse.ArgumentParser(description="Upload CSVs to Google Sheets as new worksheets.")
    parser.add_argument(
        "--credentials", 
        default="credentials.json", 
        help="Path to the Google Service Account JSON key file."
    )
    parser.add_argument(
        "--sheet-url",
        default="https://docs.google.com/spreadsheets/d/1sfFGvt81Vvz69XJKyxVeQr5oz_AVsRBlUWx97OabCZM/edit",
        help="URL of the target Google Sheet."
    )
    parser.add_argument(
        "--input-dir",
        default="results",
        help="Directory containing the Absen-<yyyy>-<TPE/ZL>.csv files."
    )
    args = parser.parse_args()

    cred_path = Path(args.credentials)
    if not cred_path.exists():
        print(f"Error: Credentials file not found at '{cred_path}'.")
        print("Please download a Google Service Account JSON key, rename it to 'credentials.json', and place it in this folder.")
        print("Also make sure to share your Google Sheet with the client_email found inside the JSON file.")
        return 1

    print("Authenticating with Google...")
    creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
    gc = gspread.authorize(creds)

    print(f"Opening Spreadsheet: {args.sheet_url}")
    try:
        spreadsheet = gc.open_by_url(args.sheet_url)
    except Exception as e:
        print(f"Error opening spreadsheet: {e}")
        print("Did you remember to share the sheet with the Service Account email?")
        return 1

    # Find all Absen-YYYY-LOC.csv files in results folder
    input_dir = Path(args.input_dir)
    csv_files = list(input_dir.glob("Absen-*-*.csv"))
    
    if not csv_files:
        print(f"No CSV files matching 'Absen-*-*.csv' found in '{input_dir}'.")
        return 1

    print(f"Found {len(csv_files)} CSV files. Preparing to upload...")

    # Build a map of existing sheets by title for quick lookup
    existing_sheets = {ws.title: ws for ws in spreadsheet.worksheets()}

    def find_header_row(worksheet, csv_header: list[str]) -> int | None:
        """Return the 1-based row index whose columns A-N match csv_header, or None."""
        # Fetch only the first column-count cells from each row to avoid huge reads.
        # We scan up to the first 50 rows to find the matching header row.
        num_cols = len(csv_header)
        col_letter = chr(ord('A') + num_cols - 1)
        values = worksheet.get(f"A1:{col_letter}50")
        for idx, row in enumerate(values, start=1):
            # Pad short rows so comparison is length-safe
            padded = row + [""] * (num_cols - len(row))
            if padded[:num_cols] == csv_header:
                return idx
        return None

    for csv_path in sorted(csv_files):
        sheet_name = csv_path.stem  # e.g., "Absen-2026-TPE"

        # Read CSV data (first row is the header)
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            csv_data = list(reader)

        if not csv_data:
            print(f"  Skipping empty file : {sheet_name}")
            continue

        csv_header = csv_data[0]

        if sheet_name in existing_sheets:
            worksheet = existing_sheets[sheet_name]
            print(f"  Updating sheet      : {sheet_name}")

            data_start_row = find_header_row(worksheet, csv_header)
            if data_start_row is None:
                # Header not found — append after the last non-empty row
                all_values = worksheet.get_all_values()
                last_used = len(all_values)
                data_start_row = last_used + 2  # leave one blank row as separator
                print(f"    Header not found; appending at row {data_start_row}")
            else:
                print(f"    Header matched at row {data_start_row}; overwriting from there")

            # Ensure the sheet is tall enough for the incoming data
            needed_rows = data_start_row + len(csv_data)
            if worksheet.row_count < needed_rows:
                worksheet.resize(rows=needed_rows)

            # Clear from the header row to the current end of the sheet
            last_col_letter = chr(ord('A') + len(csv_header) - 1)
            clear_range = f"A{data_start_row}:{last_col_letter}{worksheet.row_count}"
            worksheet.batch_clear([clear_range])
        else:
            # Sheet does not exist yet — create it (data will start at row 1)
            data_start_row = 1
            num_rows = max(len(csv_data) + 10, 100)
            num_cols = max(len(csv_header) + 2, 20)
            print(f"  Creating new sheet  : {sheet_name}")
            worksheet = spreadsheet.add_worksheet(
                title=sheet_name, rows=str(num_rows), cols=str(num_cols)
            )

        # Write CSV data (including header) starting at the determined row
        start_cell = f"A{data_start_row}"
        worksheet.update(values=csv_data, range_name=start_cell)

        print(f"  -> Wrote {len(csv_data)} rows to '{sheet_name}' starting at row {data_start_row}.")

    print("\nAll files uploaded successfully!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
