# Project: Apple Shared Album to Google Photos Sync

## Context & Environment
- **Host:** macOS Sequoia (Advanced Security/Permissions).
- **Source:** Apple Photos `Photos.sqlite` (Shared Albums are "second-class" assets).
- **Local Storage:** `~/Pictures/Photos Library.photoslibrary/resources/cloudsharing/data/`.
- **Target:** Google Photos API (Daily quotas & Batching limits apply).
- **Python:** 3.12 (Stable) inside a Virtual Environment (`~/photos-sync/venv`).
- **Core Libraries:** `osxphotos` (for Apple metadata/export) and `google-api-python-client`.

## Technical Architecture
1. **Database Logic:** Use `osxphotos.PhotosDB()` to query `album.shared` attributes.
2. **Resume Support:** A local SQLite database (`sync_state.db`) tracks `photo_uuid` + `album_title` to prevent duplicates.
3. **Atomic Operations:** Mark as "Synced" only AFTER the Google API returns a 200 OK and successfully adds the item to the album.
4. **Export Strategy:** Export photos one-by-one with `download_missing=True` to force iCloud downloads, then delete local copies immediately after upload to save disk space.

## Current Script Version
The script supports `--dry-run` and uses the Google Photos 2-step upload process (Upload Bytes -> Get Token -> Batch Create Media Item).

## Outstanding Goals
- Ensure "Search for existing album" logic is robust.
- Handle potential iCloud download failures gracefully.
- Add a progress indicator for long-running syncs.
