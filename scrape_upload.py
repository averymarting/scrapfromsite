#!/usr/bin/env python3
"""
scrape_upload.py

Scrapes all <img> src values inside elements with class "item-post" from a
given page URL, auto-scrolling to trigger lazy-loaded / infinite-scroll
content. Scrolling stops once 8 consecutive scrolls produce no new images.
Only .jpg images are kept. Downloaded images are then uploaded to a Google
Drive folder (created if it doesn't already exist).

Usage:
    python scrape_upload.py --url "https://example.com/page" --folder-name "MyFolder"

Env vars (used by the GitHub Actions workflow, but work locally too):
    PAGE_URL           -> same as --url
    DRIVE_FOLDER_NAME  -> same as --folder-name
    GDRIVE_CREDENTIALS_JSON -> combined OAuth credentials JSON (token, refresh_token,
                               client_id, client_secret, etc.) generated once via
                               generate_refresh_token.py
    GDRIVE_PARENT_ID   -> (optional) Drive folder ID to create the new folder under
                          (any folder in your own Drive, since auth is your own OAuth account)
    MAX_IDLE_SCROLLS   -> (optional) override the default of 8
"""

import argparse
import io
import os
import re
import sys
import time
import hashlib
import json
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
ITEM_SELECTOR = ".item-post img"
DOWNLOAD_DIR = Path("downloaded_images")

# Default target sheet (from the link you shared). Can be overridden with
# the SPREADSHEET_ID env var / --spreadsheet-id flag.
DEFAULT_SPREADSHEET_ID = "1OQns3xUPeTQslsw0FaD-a85DAM0Sc_L6BnaGDMqGPmY"
DEFAULT_SHEET_TAB = "Sheet1"


def log(msg: str):
    """Print immediately, unbuffered, with a timestamp — so GitHub Actions
    logs show live progress instead of appearing stuck."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_args():
    p = argparse.ArgumentParser(description="Scrape .item-post images and upload to Google Drive")
    p.add_argument("--url", default=os.environ.get("PAGE_URL"), help="Page URL to scrape")
    p.add_argument("--folder-name", default=os.environ.get("DRIVE_FOLDER_NAME"),
                   help="Google Drive folder name to upload images into")
    p.add_argument("--max-idle-scrolls", type=int,
                   default=int(os.environ.get("MAX_IDLE_SCROLLS", "8")),
                   help="Stop after this many consecutive scrolls with no new images (default: 8)")
    p.add_argument("--max-images", type=int,
                   default=(int(os.environ["MAX_IMAGES"]) if os.environ.get("MAX_IMAGES") else None),
                   help="Optional cap on total images to scrape/download/upload. "
                        "Leave unset to scrape until 8 idle scrolls instead.")
    p.add_argument("--parent-id", default=os.environ.get("GDRIVE_PARENT_ID"),
                   help="Optional Drive parent folder ID to create the new folder inside")
    p.add_argument("--spreadsheet-id", default=os.environ.get("SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID),
                   help="Google Sheet ID to log uploaded file names into")
    p.add_argument("--sheet-tab", default=os.environ.get("SHEET_TAB", DEFAULT_SHEET_TAB),
                   help="Tab/sheet name inside the spreadsheet to append rows to")
    p.add_argument("--download-concurrency", type=int,
                   default=int(os.environ.get("DOWNLOAD_CONCURRENCY", "10")),
                   help="How many images to download in parallel (default: 10)")
    p.add_argument("--upload-concurrency", type=int,
                   default=int(os.environ.get("UPLOAD_CONCURRENCY", "8")),
                   help="How many images to upload to Drive in parallel (default: 8)")
    p.add_argument("--headless", action="store_true", default=True)
    args = p.parse_args()

    if not args.url:
        sys.exit("ERROR: --url (or PAGE_URL env var) is required")
    if not args.folder_name:
        sys.exit("ERROR: --folder-name (or DRIVE_FOLDER_NAME env var) is required")
    return args


def is_jpg(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".jpg") or path.endswith(".jpeg")


def scrape_images(url: str, max_idle_scrolls: int, max_images: int | None = None) -> set:
    """Scroll the page repeatedly, collecting unique .item-post img src values
    that point to .jpg/.jpeg files.

    Stops when either:
      - max_images is set and that many unique images have been found, or
      - max_idle_scrolls consecutive scrolls produce no new images.
    """
    found = set()
    idle_scrolls = 0
    scroll_count = 0

    log(f"Launching browser and opening: {url}")
    if max_images:
        log(f"Image limit set: will stop as soon as {max_images} images are found.")
    else:
        log(f"No image limit set: will scrape until {max_idle_scrolls} consecutive idle scrolls.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        log("Page loaded. Waiting for initial images to render...")

        # Give the initial batch of lazy images a moment to appear
        page.wait_for_timeout(2000)

        while True:
            # Collect current image srcs
            srcs = page.eval_on_selector_all(
                ITEM_SELECTOR, "els => els.map(e => e.getAttribute('src') || e.src)"
            )
            new_this_round = 0
            for src in srcs:
                if src and is_jpg(src) and src not in found:
                    found.add(src)
                    new_this_round += 1
                    if max_images and len(found) >= max_images:
                        break

            log(f"Scroll #{scroll_count}: {len(found)} unique jpgs found so far "
                f"(+{new_this_round} new this round, idle streak: {idle_scrolls}/{max_idle_scrolls})")

            if max_images and len(found) >= max_images:
                log(f"Reached the requested limit of {max_images} images. Stopping scroll loop.")
                break

            if new_this_round == 0:
                idle_scrolls += 1
            else:
                idle_scrolls = 0

            if idle_scrolls >= max_idle_scrolls:
                log(f"No new images for {max_idle_scrolls} consecutive scrolls. Stopping scroll loop.")
                break

            # Scroll to bottom to trigger lazy-load / infinite scroll
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scroll_count += 1
            log(f"Scrolled to bottom (scroll #{scroll_count}), waiting for new content to load...")
            # Wait for network to settle a bit, then a fixed pause for lazy images
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                log("  (network still busy after 5s, continuing anyway)")
            page.wait_for_timeout(1500)

        browser.close()
        log("Browser closed.")

    # Trim to the exact limit in case the last batch overshot it
    if max_images and len(found) > max_images:
        found = set(list(found)[:max_images])

    return found


_progress_lock = threading.Lock()


def _download_one(src: str, headers: dict, used_names: set, names_lock: threading.Lock,
                   max_retries: int = 3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(src, headers=headers, timeout=30, stream=True)
            resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(1.5 * attempt)  # small backoff before retrying
            else:
                raise last_err

    # Keep the website's own filename (default behavior). Only fall back
    # to a hash-based name if the URL has no usable filename.
    name = os.path.basename(urlparse(src).path)
    if not name.lower().endswith((".jpg", ".jpeg")):
        name = hashlib.sha1(src.encode()).hexdigest() + ".jpg"

    # Avoid overwriting if two different posts happen to share a filename
    with names_lock:
        if name in used_names:
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{hashlib.sha1(src.encode()).hexdigest()[:6]}{ext}"
        used_names.add(name)

    dest = DOWNLOAD_DIR / name
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)

    return dest, src


def download_images(urls: set, concurrency: int = 10) -> list:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    urls = sorted(urls)
    total = len(urls)
    used_names = set()
    names_lock = threading.Lock()
    saved_paths = []
    completed = 0

    log(f"Starting parallel download of {total} images ({concurrency} at a time)...")
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_src = {
            executor.submit(_download_one, src, headers, used_names, names_lock): src
            for src in urls
        }
        for future in as_completed(future_to_src):
            src = future_to_src[future]
            with _progress_lock:
                completed += 1
                current = completed
            try:
                dest, source_url = future.result()
                saved_paths.append((dest, source_url))
                log(f"  [{current}/{total}] saved: {dest.name}")
            except Exception as e:
                log(f"  [{current}/{total}] FAILED to download {src}: {e}")

    log(f"Download finished: {len(saved_paths)}/{total} images saved successfully.")
    return saved_paths


def get_user_credentials() -> UserCredentials:
    raw = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    if not raw:
        sys.exit("ERROR: GDRIVE_CREDENTIALS_JSON env var not set. "
                  "Run generate_refresh_token.py once locally to obtain it.")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: GDRIVE_CREDENTIALS_JSON is not valid JSON: {e}")

    required = ["refresh_token", "client_id", "client_secret"]
    missing = [k for k in required if not info.get(k)]
    if missing:
        sys.exit(f"ERROR: GDRIVE_CREDENTIALS_JSON is missing field(s): {', '.join(missing)}")

    creds = UserCredentials(
        info.get("token"),
        refresh_token=info["refresh_token"],
        token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=info.get("scopes", SCOPES),
    )
    creds.refresh(Request())
    return creds


def get_drive_service():
    creds = get_user_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


_thread_local = threading.local()


def _get_thread_drive_service(creds: UserCredentials):
    """Each worker thread gets its own Drive service instance — googleapiclient
    service objects aren't safe to share across threads."""
    if not hasattr(_thread_local, "service"):
        _thread_local.service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _thread_local.service


def find_or_create_folder(service, folder_name: str, parent_id: str | None) -> str:
    query = (
        f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(
        q=query, fields="files(id, name)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])

    if files:
        folder_id = files[0]["id"]
        print(f"Using existing Drive folder '{folder_name}' ({folder_id})")
        return folder_id

    metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(
        body=metadata, fields="id", supportsAllDrives=True
    ).execute()
    folder_id = folder["id"]
    print(f"Created new Drive folder '{folder_name}' ({folder_id})")
    return folder_id


def _upload_one(creds: UserCredentials, folder_id: str, path: Path, source_url: str,
                 max_retries: int = 3) -> dict:
    service = _get_thread_drive_service(creds)
    metadata = {"name": path.name, "parents": [folder_id]}

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            media = MediaFileUpload(str(path), mimetype="image/jpeg", resumable=True)
            uploaded = service.files().create(
                body=metadata, media_body=media, fields="id, webViewLink",
                supportsAllDrives=True,
            ).execute()
            file_id = uploaded["id"]
            link = uploaded.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
            return {
                "file_name": path.name,
                "source_url": source_url,
                "drive_file_id": file_id,
                "drive_link": link,
            }
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(1.5 * attempt)  # backoff, helps with transient rate-limit errors
            else:
                raise last_err


def upload_to_drive(creds: UserCredentials, folder_id: str, saved_paths: list,
                     concurrency: int = 8) -> list:
    """Uploads each (path, source_url) pair to Drive in parallel. Returns a
    list of dicts with everything needed for the sheet log."""
    total = len(saved_paths)
    results = []
    completed = 0
    log(f"Starting parallel upload of {total} images to Drive folder id {folder_id} "
        f"({concurrency} at a time)...")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_item = {
            executor.submit(_upload_one, creds, folder_id, path, source_url): (path, source_url)
            for path, source_url in saved_paths
        }
        for future in as_completed(future_to_item):
            path, source_url = future_to_item[future]
            with _progress_lock:
                completed += 1
                current = completed
            try:
                result = future.result()
                results.append(result)
                log(f"  [{current}/{total}] uploaded: {result['file_name']} -> {result['drive_link']}")
            except Exception as e:
                log(f"  [{current}/{total}] FAILED to upload {path.name}: {e}")

    log(f"Upload finished: {len(results)}/{total} images uploaded successfully.")
    return results


def log_to_sheet(spreadsheet_id: str, sheet_tab: str, page_url: str,
                  folder_name: str, upload_results: list):
    if not spreadsheet_id:
        log("No spreadsheet ID configured — skipping sheet logging.")
        return
    if not upload_results:
        return

    log(f"Writing {len(upload_results)} file name(s) to Google Sheet ({sheet_tab})...")
    creds = get_user_credentials()
    sheets = build("sheets", "v4", credentials=creds)

    rows = [[r["file_name"]] for r in upload_results]

    try:
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_tab}!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        log(f"Sheet updated: {len(rows)} file name(s) appended to '{sheet_tab}'.")
    except Exception as e:
        log(f"! Failed to write to Google Sheet: {e}")
        log("  (Make sure the spreadsheet ID and tab name are correct, and that it's "
            "accessible to the Google account used to generate GDRIVE_CREDENTIALS_JSON.)")


def main():
    args = parse_args()

    log(f"=== Starting run ===")
    log(f"Page URL: {args.url}")
    log(f"Drive folder name: {args.folder_name}")
    if args.max_images:
        log(f"Image limit: {args.max_images}")
    else:
        log(f"Image limit: none (stop condition = {args.max_idle_scrolls} consecutive idle scrolls)")

    image_urls = scrape_images(args.url, args.max_idle_scrolls, args.max_images)
    log(f"=== Scrape complete: {len(image_urls)} unique .jpg images found ===")

    if not image_urls:
        log("No images found — nothing to download or upload.")
        return

    saved_paths = download_images(image_urls, concurrency=args.download_concurrency)

    if not saved_paths:
        log("No images were successfully downloaded — nothing to upload.")
        return

    creds = get_user_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    folder_id = find_or_create_folder(service, args.folder_name, args.parent_id)
    upload_results = upload_to_drive(creds, folder_id, saved_paths, concurrency=args.upload_concurrency)

    log_to_sheet(args.spreadsheet_id, args.sheet_tab, args.url, args.folder_name, upload_results)

    log("=== Done ===")


if __name__ == "__main__":
    main()
