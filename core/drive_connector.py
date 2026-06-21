"""
drive_connector.py
===================
Lists and downloads invoice PDFs/images from a single Google Drive folder,
using a service account (read-only access, scoped to just the files/folders
explicitly shared with it - see setup steps in project notes).

Why a service account and not OAuth: this runs unattended in a polling loop
with nobody present to click through a login screen, and OAuth user tokens
expire/break on password changes in a way that breaks a long-running
background job. A service account key doesn't expire and isn't tied to a
human login.

USAGE:
    from drive_connector import list_new_files, download_file

    new_files = list_new_files(FOLDER_ID, db_path="output/invoices.db")
    for f in new_files:
        local_path = download_file(f["id"], f["name"], dest_dir="invoices")
        # ... hand local_path to pipeline.process_single_pdf(), passing
        #     f["id"] through as drive_file_id so database.py can dedup it
"""

import io
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

sys.path.insert(0, str(Path(__file__).parent))
import database

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Accepted invoice file types. Google Docs/Sheets natives (no direct binary
# download) are intentionally excluded - invoices arrive as PDFs or scanned
# images, never as native Google Workspace files.
ACCEPTED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}


def _get_credentials_path() -> str:
    path = os.environ.get("GDRIVE_KEY_PATH", "gdrive_key.json")
    if not Path(path).exists():
        raise FileNotFoundError(
            f"Service account key not found at '{path}'. Set GDRIVE_KEY_PATH "
            f"or place gdrive_key.json in the project root."
        )
    return path


def _get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        _get_credentials_path(), scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def list_folder_files(folder_id: str) -> list[dict]:
    """Lists all accepted-type files in the given Drive folder (non-recursive
    - matches the 'one folder, all invoices dumped together' setup). Returns
    dicts with id, name, mimeType, modifiedTime."""
    service = _get_drive_service()
    mime_filter = " or ".join(f"mimeType='{m}'" for m in ACCEPTED_MIME_TYPES)
    query = f"'{folder_id}' in parents and ({mime_filter}) and trashed=false"

    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageToken=page_token,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def list_new_files(folder_id: str, db_path: str = None) -> list[dict]:
    """Lists files in the folder that aren't already in the database -
    this is the dedup check the polling loop uses every cycle so the same
    invoice is never re-downloaded/re-extracted (and never re-spends Gemini/
    Groq tokens) once it's been processed."""
    all_files = list_folder_files(folder_id)
    return [f for f in all_files if not database.is_already_processed(f["id"], db_path)]


def download_file(file_id: str, file_name: str, dest_dir: str) -> str:
    """Downloads one file by ID to dest_dir, returns the local path. Existing
    file with the same name is overwritten - file_id (not file_name) is the
    real dedup key, this is just where the bytes land for ocr_engine to read."""
    service = _get_drive_service()
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    local_path = str(Path(dest_dir) / file_name)

    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.close()
    return local_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python drive_connector.py <folder_id> [dest_dir]")
        sys.exit(1)
    folder_id = sys.argv[1]
    dest_dir = sys.argv[2] if len(sys.argv) > 2 else "invoices"

    new_files = list_new_files(folder_id)
    print(f"Found {len(new_files)} new file(s) in folder {folder_id}:")
    for f in new_files:
        print(f"  {f['name']} ({f['mimeType']}, id={f['id']})")