"""
Micro-WMS Inbound Logistics Pipeline
Automated Delivery Challan OCR & Google Sheets Ingestion via Gemini 1.5 Flash.

Pipeline Steps:
1. Monitors local folder 'inbound_challans' every 10 seconds for PDF or image files.
2. Uploads detected files to Gemini 1.5 Flash using `google.generativeai`.
3. Prompts the model to extract: Date, Challan_No, Client_Name, Vehicle_No, Inward_Qty as raw JSON.
4. Appends extracted fields to Google Sheet using `gspread` and `oauth2client`.
5. Moves processed files to 'processed_challans' to prevent duplicate processing.
"""

import os
import sys
import time
import json
import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration & Environment
# ---------------------------------------------------------------------------
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INBOUND_DIR = BASE_DIR / "inbound_challans"
PROCESSED_DIR = BASE_DIR / "processed_challans"

# Supported file formats
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp"}

# Google credentials & config
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", str(BASE_DIR / "service_account.json"))
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Inbound Challans")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Polling frequency
POLL_INTERVAL_SECONDS = 10

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MicroWMS-Inbound")

# ---------------------------------------------------------------------------
# Gemini Extraction Prompt & Setup
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """
You are an expert OCR and document analysis AI for warehouse logistics and inventory management.
Carefully inspect this Delivery Challan document (PDF/Image) and extract the following 5 key fields:

1. Date: The date printed on the challan or receipt (standardize to YYYY-MM-DD if recognizable, otherwise return as printed).
2. Challan_No: The delivery challan number, dispatch note number, or invoice reference number.
3. Client_Name: The name of the client, customer, consignee, or sending vendor.
4. Vehicle_No: The vehicle registration number or transport identifier (e.g., truck or trailer number).
5. Inward_Qty: The total inward or delivered quantity of items/material (numeric value or count with unit if specified).

You MUST return strictly raw, valid, parseable JSON with exact keys as shown below.
Do not wrap your output in markdown code fences (no ```json or ```).
Do not include any explanations, headers, notes, or additional text.

{
  "Date": "string",
  "Challan_No": "string",
  "Client_Name": "string",
  "Vehicle_No": "string",
  "Inward_Qty": "number or string"
}
"""

def init_gemini() -> genai.GenerativeModel:
    """Configures and returns the Gemini 1.5 Flash model instance."""
    api_key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not found. Please set it in your environment or .env file.")
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.1,
        }
    )
    return model

def init_google_sheet():
    """Authorizes and connects to the target Google Sheet via gspread and oauth2client."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logger.error(f"Service account file not found at: {SERVICE_ACCOUNT_FILE}")
        sys.exit(1)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    logger.info(f"Authenticating with Google Sheets using {SERVICE_ACCOUNT_FILE}...")
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)

    try:
        # Connect by spreadsheet name or spreadsheet key
        if len(SPREADSHEET_NAME) > 30 and " " not in SPREADSHEET_NAME and "/" not in SPREADSHEET_NAME:
            spreadsheet = client.open_by_key(SPREADSHEET_NAME)
        else:
            spreadsheet = client.open(SPREADSHEET_NAME)
        sheet = spreadsheet.sheet1
        logger.info(f"Successfully connected to spreadsheet: '{spreadsheet.title}' (Sheet: '{sheet.title}')")

        # Initialize header row if sheet is completely empty
        existing_values = sheet.get_all_values()
        if not existing_values:
            headers = ["Date", "Challan_No", "Client_Name", "Vehicle_No", "Inward_Qty", "File_Name", "Processed_At"]
            sheet.append_row(headers)
            logger.info(f"Initialized Google Sheet with headers: {headers}")

        return sheet
    except gspread.SpreadsheetNotFound:
        logger.error(
            f"Spreadsheet '{SPREADSHEET_NAME}' not found! "
            f"Ensure the sheet exists and is shared with the client_email from {SERVICE_ACCOUNT_FILE}."
        )
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheets: {e}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Document Processing Helpers
# ---------------------------------------------------------------------------
def is_file_ready(file_path: Path) -> bool:
    """Verifies if a file has finished copying and is available for reading."""
    try:
        if file_path.stat().st_size == 0:
            return False
        with open(file_path, "rb"):
            return True
    except (IOError, OSError):
        return False

def clean_json_response(raw_text: str) -> str:
    """Strips Markdown fences or surrounding noise if present."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

def extract_challan_data(model: genai.GenerativeModel, file_path: Path) -> Dict[str, Any]:
    """Uploads file to Gemini File API and prompts Gemini 1.5 Flash for structured extraction."""
    logger.info(f"Uploading '{file_path.name}' to Gemini...")
    uploaded_file = genai.upload_file(path=str(file_path))

    try:
        logger.info(f"Prompting Gemini 1.5 Flash for data extraction from '{file_path.name}'...")
        response = model.generate_content([uploaded_file, EXTRACTION_PROMPT])
        cleaned_text = clean_json_response(response.text)
        data = json.loads(cleaned_text)
        return data
    finally:
        # Clean up file on Gemini servers after processing
        try:
            genai.delete_file(uploaded_file.name)
        except Exception:
            pass

def append_to_sheet(sheet, data: Dict[str, Any], filename: str) -> None:
    """Appends the parsed challan data as a new row in Google Sheet."""
    row = [
        str(data.get("Date", "")),
        str(data.get("Challan_No", "")),
        str(data.get("Client_Name", "")),
        str(data.get("Vehicle_No", "")),
        str(data.get("Inward_Qty", "")),
        filename,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ]
    sheet.append_row(row)
    logger.info(f"Appended row to Google Sheet: {row[:5]}")

def move_to_processed(file_path: Path) -> Path:
    """Moves processed file to 'processed_challans', avoiding name collisions."""
    dest = PROCESSED_DIR / file_path.name
    if dest.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = PROCESSED_DIR / f"{file_path.stem}_{timestamp}{file_path.suffix}"
    shutil.move(str(file_path), str(dest))
    logger.info(f"Moved '{file_path.name}' -> '{dest.name}'")
    return dest

def process_file(file_path: Path, model: genai.GenerativeModel, sheet) -> bool:
    """Orchestrates end-to-end processing of a single challan file."""
    logger.info(f"Processing inbound challan: {file_path.name}")
    try:
        extracted = extract_challan_data(model, file_path)
        logger.info(f"Extracted JSON for {file_path.name}: {json.dumps(extracted)}")

        append_to_sheet(sheet, extracted, file_path.name)
        move_to_processed(file_path)
        return True
    except json.JSONDecodeError as jde:
        logger.error(f"JSON parsing error for '{file_path.name}': {jde}")
    except Exception as e:
        logger.error(f"Error processing file '{file_path.name}': {e}", exc_info=True)
    return False

# ---------------------------------------------------------------------------
# Main Monitoring Loop
# ---------------------------------------------------------------------------
def main():
    # Ensure directories exist
    INBOUND_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Initializing Micro-WMS Inbound Logistics Pipeline...")
    model = init_gemini()
    sheet = init_google_sheet()

    logger.info(f"Pipeline started successfully.")
    logger.info(f"Monitoring folder: '{INBOUND_DIR.resolve()}'")
    logger.info(f"Processed destination: '{PROCESSED_DIR.resolve()}'")
    logger.info(f"Polling cadence: Every {POLL_INTERVAL_SECONDS} seconds. Press Ctrl+C to terminate.")

    try:
        while True:
            # Scan inbound directory for supported files
            candidates = [
                f for f in INBOUND_DIR.iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
            ]

            if candidates:
                logger.info(f"Detected {len(candidates)} candidate file(s) in inbound queue.")
                for file_path in candidates:
                    if is_file_ready(file_path):
                        process_file(file_path, model, sheet)
                    else:
                        logger.warning(f"File '{file_path.name}' is currently being written. Waiting for next cycle.")

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("\nTermination signal received (Ctrl+C). Shutting down pipeline gracefully.")
    except Exception as e:
        logger.critical(f"Unexpected fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
