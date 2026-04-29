# LINE Attendance Report Extractor

This project extracts, filters, and formats attendance reports from LINE chat exports.

## Generating the CSV Reports

1. Export your LINE chat history to `LINE chat report.txt` in the root folder.
2. Run the extraction script:
```bash
python export_attendance_csv.py
```
This generates the annual `Absen-<yyyy>-<TPE/ZL>.csv` files inside the `results/` folder.

## Uploading to Google Sheets

We provide a Python script, `upload_to_sheets.py`, to automatically upload these generated CSV files into a Google Sheet.

### Required Setup
Before running the upload script, you need to configure a Google Service Account to act on your behalf. Since `credentials.json` contains sensitive access keys, it is ignored by `.gitignore` and **must never be committed to version control**.

### How to Create and Download the `credentials.json` File

1. **Go to the Google Cloud Console:**
   Navigate to the [Google Cloud Console](https://console.cloud.google.com/). Select your Project or create a new one.

2. **Enable Required APIs:**
   Go to **APIs & Services > Library** and enable both the **Google Sheets API** and the **Google Drive API**.

3. **Navigate to Service Accounts:**
   Go to **IAM & Admin > Service Accounts** and click **CREATE SERVICE ACCOUNT**. Fill in the details and create it.

4. **Generate the Key:**
   - In the Service Accounts list, click on the **Email address** of your new Service Account (e.g., `bot-name@your-project-id.iam.gserviceaccount.com`).
   - Copy this email address. We will need it later.
   - Go to the **KEYS** tab.
   - Click the **ADD KEY** dropdown and select **Create new key**.
   - Choose **JSON** as the format and click **Create**.

5. **Save the File in this Repository:**
   - The JSON key file will automatically download to your computer.
   - Move this file to the root directory of this repository (where `upload_to_sheets.py` is located).
   - **Rename the file to `credentials.json`.**

### Share Your Target Google Sheet
Your Google Sheet is private by default. For the bot to be able to upload data into it, you must invite the bot to the document:
1. Open your target Google Sheet in your web browser.
2. Click **Share** (top right corner).
3. Paste the Service Account's email address (from step 4 above) into the "Add people and groups" field.
4. Ensure the role is set to **Editor**, then click **Share**.

### Run the Upload Script
Install the required packages first:
```bash
pip install gspread google-auth
```
Then, execute the script:
```bash
python upload_to_sheets.py
```