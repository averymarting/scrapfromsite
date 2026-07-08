# item-post JPG Scraper → Google Drive Uploader

## What it does
1. Opens the given page URL in headless Chromium (Playwright).
2. Reads all `<img src>` values inside elements with class `item-post`.
3. Keeps only `.jpg` / `.jpeg` URLs.
4. Scrolls to the bottom repeatedly to trigger lazy-loaded / infinite-scroll
   content. If **8 consecutive scrolls** produce zero new images, it stops
   (configurable via `max_idle_scrolls`).
5. Downloads every unique jpg found.
6. Uploads them all into a Google Drive folder — creating the folder if it
   doesn't already exist — using a service account.

## One-time setup

### 1. Create a Google service account + key
1. Go to Google Cloud Console → IAM & Admin → Service Accounts → Create.
2. Enable the **Google Drive API** for the project.
3. Create a JSON key for the service account and download it.
4. **Share the Drive location** the uploads should land in with the service
   account's email address (looks like
   `something@project-id.iam.gserviceaccount.com`) as an **Editor**.
   - Easiest: share a normal Drive folder with it, and pass that folder's ID
     as `parent_folder_id` when running the workflow.
   - Service accounts have no personal Drive storage of their own, so this
     sharing step is required — otherwise uploads will fail with a storage
     quota error.

### 2. Add the key as a GitHub secret
- Repo → Settings → Secrets and variables → Actions → New repository secret
- Name: `GDRIVE_SA_KEY_JSON`
- Value: paste the **entire contents** of the downloaded JSON key file.

### 3. Add the workflow + script to your repo
Commit these files:
```
scrape_upload.py
requirements.txt
.github/workflows/scrape-images.yml
```

## Running it
Go to your repo → **Actions** tab → **Scrape item-post images and upload to
Google Drive** → **Run workflow**. You'll be prompted for:

- `page_url` — the page to scrape
- `drive_folder_name` — the Drive folder name to save into (created if it
  doesn't exist)
- `max_idle_scrolls` — optional, defaults to 8
- `parent_folder_id` — optional Drive folder ID to nest the new folder
  inside (the one you shared with the service account)

## Running locally
```bash
pip install -r requirements.txt
playwright install chromium

export GDRIVE_SA_KEY_JSON="$(cat service-account-key.json)"
export GDRIVE_PARENT_ID="your-shared-folder-id"   # optional

python scrape_upload.py --url "https://example.com/page" --folder-name "MyFolder"
```

## Notes specific to foodiesposts.com
The sample HTML you shared already matches the selector used
(`.item-post img`), and the CDN pattern (`contentN.foodiesposts.com/upload/...`)
serves plain `.jpg` files, so no extra thumbnail/retry logic is needed here —
that concern only applied to your separate OG-preview-card poster, not this
scraper.
