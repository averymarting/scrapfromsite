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
    GDRIVE_SA_KEY_JSON -> raw JSON content of a Google service account key
    GDRIVE_PARENT_ID   -> (optional) Drive folder ID to create the new folder under
                          (must be a folder already shared with the service account,
                          or a Shared Drive folder)
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
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
ITEM_SELECTOR = ".item-post img"
DOWNLOAD_DIR = Path("downloaded_images")


def parse_args():
    p = argparse.ArgumentParser(description="Scrape .item-post images and upload to Google Drive")
    p.add_argument("--url", default=os.environ.get("PAGE_URL"), help="Page URL to scrape")
    p.add_argument("--folder-name", default=os.environ.get("DRIVE_FOLDER_NAME"),
                   help="Google Drive folder name to upload images into")
    p.add_argument("--max-idle-scrolls", type=int,
                   default=int(os.environ.get("MAX_IDLE_SCROLLS", "8")),
                   help="Stop after this many consecutive scrolls with no new images (default: 8)")
    p.add_argument("--parent-id", default=os.environ.get("GDRIVE_PARENT_ID"),
                   help="Optional Drive parent folder ID to create the new folder inside")
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


def scrape_images(url: str, max_idle_scrolls: int) -> set:
    """Scroll the page repeatedly, collecting unique .item-post img src values
    that point to .jpg/.jpeg files, until max_idle_scrolls consecutive scrolls
    produce no new images."""
    found = set()
    idle_scrolls = 0
    scroll_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

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

            print(f"[scroll {scroll_count}] total unique jpgs so far: {len(found)} "
                  f"(+{new_this_round} this round, idle streak: {idle_scrolls})")

            if new_this_round == 0:
                idle_scrolls += 1
            else:
                idle_scrolls = 0

            if idle_scrolls >= max_idle_scrolls:
                print(f"No new images found for {max_idle_scrolls} consecutive scrolls. Stopping.")
                break

            # Scroll to bottom to trigger lazy-load / infinite scroll
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scroll_count += 1
            # Wait for network to settle a bit, then a fixed pause for lazy images
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

        browser.close()

    return found


def download_images(urls: set) -> list:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for src in urls:
        try:
            resp = requests.get(src, headers=headers, timeout=30, stream=True)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ! failed to download {src}: {e}")
            continue

        # Derive a stable filename from the URL (falls back to a hash)
        name = os.path.basename(urlparse(src).path)
        if not name.lower().endswith((".jpg", ".jpeg")):
            name = hashlib.sha1(src.encode()).hexdigest() + ".jpg"

        dest = DOWNLOAD_DIR / name
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        saved_paths.append(dest)
        print(f"  saved {dest}")

    return saved_paths


def get_drive_service():
    key_json = os.environ.get("GDRIVE_SA_KEY_JSON")
    if not key_json:
        sys.exit("ERROR: GDRIVE_SA_KEY_JSON env var not set (service account key JSON)")

    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


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


def upload_to_drive(service, folder_id: str, paths: list):
    for path in paths:
        metadata = {"name": path.name, "parents": [folder_id]}
        media = MediaFileUpload(str(path), mimetype="image/jpeg", resumable=True)
        uploaded = service.files().create(
            body=metadata, media_body=media, fields="id", supportsAllDrives=True
        ).execute()
        print(f"  uploaded {path.name} -> Drive file id {uploaded['id']}")


def main():
    args = parse_args()

    print(f"Scraping: {args.url}")
    print(f"Stop condition: {args.max_idle_scrolls} consecutive idle scrolls")
    image_urls = scrape_images(args.url, args.max_idle_scrolls)
    print(f"\nTotal unique .jpg images found: {len(image_urls)}")

    if not image_urls:
        print("No images found — nothing to upload.")
        return

    print("\nDownloading images...")
    saved_paths = download_images(image_urls)

    if not saved_paths:
        print("No images were successfully downloaded — nothing to upload.")
        return

    print(f"\nUploading {len(saved_paths)} images to Google Drive folder '{args.folder_name}'...")
    service = get_drive_service()
    folder_id = find_or_create_folder(service, args.folder_name, args.parent_id)
    upload_to_drive(service, folder_id, saved_paths)

    print("\nDone.")


if __name__ == "__main__":
    main()
