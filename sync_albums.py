import os
import time
import random
import sqlite3
import argparse
import fnmatch
import csv
import sys
import logging
import osxphotos
from collections import Counter
import pickle
import requests
import subprocess
import glob
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request

# --- CONFIG ---
SCOPES = [
    'https://www.googleapis.com/auth/photoslibrary.appendonly',
    'https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata',
    'https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata'
]
DB_PATH = "sync_state.db"

# File logging — terminal output via print() is unchanged
logging.basicConfig(
    filename='log.out',
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

def setup_tracking():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS uploads 
                     (photo_uuid TEXT, album_title TEXT, PRIMARY KEY (photo_uuid, album_title))''')
    return conn

def get_google_service():
    creds = None
    if os.path.exists('token.pickle'):
        try:
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
        except Exception:
            print("⚠️  Corrupt token.pickle found. Discarding.")
            creds = None

    # Check if cached creds have the required scopes
    if creds and hasattr(creds, 'scopes') and creds.scopes:
        if set(creds.scopes) != set(SCOPES):
            print(f"⚠️  Cached token scopes ({creds.scopes}) do not match configuration. Re-authenticating...")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                print("🔄 Refreshing access token...")
                creds.refresh(Request())
            except Exception as e:
                print(f"⚠️  Token refresh failed: {e}. Re-authenticating...")
                creds = None
        
        if not creds:
            print("🔐 Initiating authentication flow...")
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            # Force consent prompt to ensure user sees permissions checkboxes
            creds = flow.run_local_server(port=0, prompt='consent')
            
            # Verify scopes immediately after login
            granted_scopes = getattr(creds, 'scopes', [])
            if granted_scopes and not set(SCOPES).issubset(set(granted_scopes)):
                print(f"❌ WARNING: You did not grant all requested permissions!")
                print(f"   Requested: {SCOPES}")
                print(f"   Granted:   {granted_scopes}")
                print("   The script will likely fail. Please try again and ensure you check ALL boxes.")

        print(f"ℹ️  Active Token Scopes: {creds.scopes if creds.scopes else 'All requested (implicit)'}")
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
            
    return build('photoslibrary', 'v1', credentials=creds, static_discovery=False)

def find_or_create_album(service, title, dry_run):
    """Prevents duplicate albums by searching Google first."""
    if dry_run: return "DRY_RUN_ID"
    
    # Check existing albums
    page_token = None
    while True:
        results = service.albums().list(
            pageSize=50, pageToken=page_token).execute()
        albums = results.get('albums', [])
        for a in albums:
            if a['title'] == title:
                return a['id']
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    # Not found, create it
    print(f"   🆕 Creating new Google Album: {title}")
    new_album = service.albums().create(body={'album': {'title': title}}).execute()
    return new_album.get('id')

def load_external_albums(path):
    """Load Google Photos album titles from a text file, one title per line.

    The Google Photos API only exposes albums created by this script, so a
    full album list (e.g. albums made by Apple's iCloud transfer service)
    must be exported manually from photos.google.com — see README."""
    with open(path, encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]

def match_external(title, external_titles):
    """Return external Google album names that look like copies of an Apple album.

    Matches exact titles and Apple transfer-service naming, which is
    'Copy of <folder path>/<title>' (e.g. 'Copy of iPhoto Events/My Album')."""
    matches = []
    for ext in external_titles:
        base = ext[len("Copy of "):] if ext.startswith("Copy of ") else ext
        if base == title or base.endswith("/" + title):
            matches.append(ext)
    return matches

def upload_photo(service, file_path, album_id):
    """The two-step Google Photos upload process with retry logic."""
    
    def _upload_bytes_with_retry():
        for attempt in range(5):
            try:
                with open(file_path, 'rb') as f:
                    url = 'https://photoslibrary.googleapis.com/v1/uploads'
                    headers = {
                        'Authorization': f'Bearer {service._http.credentials.token}',
                        'Content-Type': 'application/octet-stream',
                        'X-Goog-Upload-Protocol': 'raw',
                    }
                    response = requests.post(url, data=f, headers=headers, timeout=120)
                    response.raise_for_status()
                    return response.text
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.HTTPError) and e.response.status_code in [429, 500, 502, 503, 504]:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    print(f"      ⏳ Upload bytes rate limited. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                elif isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    print(f"      ⏳ Connection issue ({type(e).__name__}). Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    raise e
        return None

    def _create_media_with_retry(upload_token):
        body = {
            'albumId': album_id,
            'newMediaItems': [{'simpleMediaItem': {'uploadToken': upload_token}}]
        }
        for attempt in range(5):
            try:
                return service.mediaItems().batchCreate(body=body).execute()
            except HttpError as e:
                if e.resp.status in [429, 500, 502, 503, 504]:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    print(f"      ⏳ Media creation rate limited. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    raise e
        return None

    # 1. Upload bytes
    try:
        upload_token = _upload_bytes_with_retry()
    except Exception as e:
        print(f"   ⚠️  Upload bytes failed: {e}")
        log.error(f"Upload bytes failed for {file_path}: {e}")
        return None

    if not upload_token: return None

    # 2. Add the token to the specific album
    try:
        result = _create_media_with_retry(upload_token)
    except Exception as e:
        print(f"   ⚠️  Media creation failed: {e}")
        log.error(f"Media creation failed for {file_path}: {e}")
        return False
    
    if not result: return False
    
    # Check if Google confirmed the creation
    status = result.get('newMediaItemResults', [{}])[0].get('status', {})
    return status.get('message') == 'Success'

def download_and_upload_missing(service, conn, album_title, g_id, missing_photos, temp_dir, total_count, start_index, library_path, verbose=False):
    """Batch downloads missing photos via CLI and uploads them."""
    if not missing_photos:
        return

    print(f"   ⬇️  Batch downloading {len(missing_photos)} items from iCloud (this may take time)...")
    
    # Chunking to avoid command line length limits
    chunk_size = 50
    for i in range(0, len(missing_photos), chunk_size):
        chunk = missing_photos[i:i + chunk_size]
        
        cmd = [
            "osxphotos", "export", temp_dir,
            "--download-missing",
            "--use-photokit",
            "--filename", "{uuid}", # Use UUID to easily map back to our objects
            "--retry", "2",
            "--ignore-exportdb",
            "--no-exportdb",
            "--library", library_path
        ]
        if verbose:
            cmd.append("--verbose")
        for p in chunk:
            cmd.extend(["--uuid", p.uuid])
            
        try:
            filenames = [p.filename for p in chunk]
            chunk_label = f"chunk {i//chunk_size + 1} (items {i+1}-{min(i+chunk_size, len(missing_photos))})"
            print(f"      Processing {chunk_label}...")
            print(f"      Downloading: {', '.join(filenames)}")
            log.info(f"Starting iCloud export: {chunk_label} — {len(chunk)} items")

            # Capture output to detect permission errors
            result = subprocess.run(cmd, check=True, timeout=300, capture_output=True, text=True)
            print(result.stdout)
            log.info(f"iCloud export succeeded: {chunk_label}")

        except subprocess.TimeoutExpired:
            skipped = [p.uuid for p in chunk]
            print(f"      ⚠️  CLI batch export timed out.")
            log.error(f"iCloud export timed out for {chunk_label}. Skipped UUIDs: {skipped}")
            continue
        except subprocess.CalledProcessError as e:
            skipped = [p.uuid for p in chunk]
            print(e.stdout)
            print(e.stderr)
            if "could not get authorization" in e.stdout or "could not get authorization" in e.stderr:
                log.error(f"Photos authorization error during export. Skipped UUIDs: {skipped}")
                print(f"\n❌ Critical Error: Missing permissions for Photos library.")
                print(f"   1. Open 'System Settings > Privacy & Security > Photos'.")
                print(f"   2. Enable access for 'Visual Studio Code', 'Terminal', or 'iTerm'.")
                print(f"   3. If your app is missing, try running this script from the macOS 'Terminal' app instead.")
                sys.exit(1)
            log.error(f"iCloud export failed for {chunk_label}. Skipped UUIDs: {skipped}\nstdout: {e.stdout}\nstderr: {e.stderr}")
            print(f"      ⚠️  CLI batch export failed.")
            continue

        # Process the downloaded files
        for j, photo in enumerate(chunk):
            # Find files matching UUID (handles jpg, mov, etc)
            found_files = glob.glob(os.path.join(temp_dir, f"{photo.uuid}.*"))
            
            if not found_files:
                print(f"      ❌ Still missing: {photo.filename}")
                log.warning(f"File not found after iCloud export: {photo.filename} ({photo.uuid})")
                continue

            # Upload the first file found (usually the image)
            # Note: This skips the video part of Live Photos if both exist, consistent with main loop
            current_idx = start_index + i + j
            print(f"      ☁️  Uploading {photo.filename} to Google Photos...")
            if upload_photo(service, found_files[0], g_id):
                conn.execute("INSERT INTO uploads (photo_uuid, album_title) VALUES (?, ?)", (photo.uuid, album_title))
                conn.commit()
                print(f"   ✅ [{current_idx}/{total_count}] {photo.filename} (from iCloud) synced.")
                log.info(f"Synced (iCloud): {photo.filename} ({photo.uuid}) -> {album_title}")
                time.sleep(1)
            else:
                print(f"   ❌ FAILED: {photo.filename}")
                log.error(f"Upload failed (iCloud): {photo.filename} ({photo.uuid}) -> {album_title}")

            for f in found_files:
                try:
                    os.remove(f)
                except OSError as e:
                    log.warning(f"Could not remove temp file {f}: {e}")

def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''examples:
  # List every album (shared and local) with IDs and pending counts
  python3 sync_albums.py --list

  # Sync everything EXCEPT albums whose name begins with "-"
  # (note the "=": required when the pattern itself starts with "-")
  python3 sync_albums.py --all --exclude="-*"

  # Sync ONLY the albums whose name begins with "-"
  python3 sync_albums.py --album="-*"

  # Sync two specific albums by name
  python3 sync_albums.py --album "Summer 2024" --album "Ski Trip"

  # Audit sync status, checking a manually exported Google album list
  # for duplicates (see README for how to create google_albums.txt)
  python3 sync_albums.py --audit --google-albums google_albums.txt

  # Report each Shared Album's owner, size, and date range -- useful for
  # planning a shared-album consolidation (see README)
  python3 sync_albums.py --shared-inventory --csv shared_inventory.csv
''')
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--library", help="Explicitly specify path to Photos library (e.g. /Users/me/Pictures/Photos Library.photoslibrary)")
    parser.add_argument("--albums", choices=["shared", "local", "all"], default="all",
                        help="Which album types to sync: 'shared' (Apple Shared Albums), 'local' (regular albums you created in Photos), or 'all' (both, default)")

    parser.add_argument("--list", action="store_true",
                        help="List matching albums with their IDs and sync status, then exit (does not contact Google)")
    parser.add_argument("--album", action="append", metavar="TITLE", default=[],
                        help="Sync only album(s) whose title matches this exact title or glob pattern (e.g. \"Summer*\"). May be repeated.")
    parser.add_argument("--album-id", action="append", metavar="ID", default=[],
                        help="Sync only the album with this ID (shown by --list). May be repeated.")
    parser.add_argument("--exclude", action="append", metavar="PATTERN", default=[],
                        help="Skip album(s) whose title matches this exact title or glob pattern (e.g. \"-*\"). May be repeated.")
    parser.add_argument("--audit", action="store_true",
                        help="Report each Apple album's sync status against Google Photos and flag likely duplicates, then exit. Uploads nothing.")
    parser.add_argument("--google-albums", metavar="FILE",
                        help="Text file with one Google Photos album title per line, exported manually from photos.google.com (see README). Lets --audit detect duplicate albums the API cannot see, e.g. ones made by Apple's transfer service.")
    parser.add_argument("--shared-inventory", action="store_true",
                        help="Report each Shared Album's owner, size, and date range to help plan a consolidation, then exit. Does not touch Google or upload anything.")
    parser.add_argument("--csv", metavar="FILE",
                        help="Also write --shared-inventory output to this CSV file.")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--num", type=int, help="Number of albums to sync")
    group.add_argument("--all", action="store_true", help="Sync all albums")
    args = parser.parse_args()

    if not (args.list or args.audit or args.shared_inventory or args.album or args.album_id or args.num or args.all):
        parser.error("specify --num N or --all, select albums with --album/--album-id, or use --list/--audit/--shared-inventory")

    print("🚀 Initializing Sync...")
    conn = setup_tracking()

    try:
        if args.library:
            library_path = os.path.expanduser(args.library)
            print(f"   ℹ️  Opening library: {library_path}")
            if not os.path.exists(library_path):
                print(f"❌ Local Library: Path does not exist: {library_path}")
                return
            photosdb = osxphotos.PhotosDB(dbfile=library_path)
        else:
            photosdb = osxphotos.PhotosDB()
            
        # album_info_shared = Apple Shared Albums; album_info = regular user albums
        # (excludes smart albums). Both return AlbumInfo objects with the same interface.
        selected_albums = []
        album_types = {}
        counts = []
        if args.albums in ("shared", "all"):
            shared = photosdb.album_info_shared
            selected_albums.extend(shared)
            album_types.update({a.uuid: "shared" for a in shared})
            counts.append(f"{len(shared)} shared")
        if args.albums in ("local", "all"):
            local = photosdb.album_info
            selected_albums.extend(local)
            album_types.update({a.uuid: "local" for a in local})
            counts.append(f"{len(local)} local")
        print(f"✅ Local Library: Connected ({', '.join(counts)} albums found)")
        print(f"   📂 Path: {photosdb.library_path}")
    except Exception as e:
        print(f"❌ Local Library: Connection Failed ({e})")
        return

    # Albums without a title can't be mapped to a Google album; skip them.
    untitled = [a for a in selected_albums if not a.title]
    if untitled:
        print(f"⚠️  Skipping {len(untitled)} album(s) with no title.")
        selected_albums = [a for a in selected_albums if a.title]

    # Selective sync: narrow to explicitly requested albums.
    # --album accepts exact titles or glob patterns (fnmatch), e.g. --album "-*"
    if args.album or args.album_id:
        wanted_ids = {i.upper() for i in args.album_id}
        filtered = [a for a in selected_albums
                    if any(fnmatch.fnmatchcase(a.title, pat) for pat in args.album)
                    or a.uuid.upper() in wanted_ids]
        matched_ids = {a.uuid.upper() for a in filtered}
        for pat in args.album:
            if not any(fnmatch.fnmatchcase(a.title, pat) for a in selected_albums):
                print(f"⚠️  No album matches title/pattern: {pat}")
        for i in sorted(wanted_ids - matched_ids):
            print(f"⚠️  No album found with ID: {i}")
        if not filtered:
            print("❌ None of the requested albums were found. Use --list to see available albums.")
            return
        selected_albums = filtered
        print(f"🎯 Selective sync: {len(selected_albums)} album(s) selected.")

    # Exclusions apply last, so they win over --album/--album-id matches.
    if args.exclude:
        before = len(selected_albums)
        selected_albums = [a for a in selected_albums
                           if not any(fnmatch.fnmatchcase(a.title, pat) for pat in args.exclude)]
        removed = before - len(selected_albums)
        if removed:
            print(f"🚫 Excluded {removed} album(s) matching: {', '.join(args.exclude)}")
        if not selected_albums:
            print("❌ All albums were excluded. Nothing to do.")
            return

    if args.list:
        cursor = conn.cursor()
        print(f"\n📋 Albums ({len(selected_albums)}):")
        print(f"   {'TYPE':<7} {'ID':<38} {'PHOTOS':>6} {'PENDING':>7}  TITLE")
        for album in sorted(selected_albums, key=lambda a: (album_types[a.uuid], a.title)):
            synced_uuids = {row[0] for row in cursor.execute(
                "SELECT photo_uuid FROM uploads WHERE album_title=?", (album.title,))}
            photos = album.photos
            pending = sum(1 for p in photos if p.uuid not in synced_uuids)
            print(f"   {album_types[album.uuid]:<7} {album.uuid:<38} {len(photos):>6} {pending:>7}  {album.title}")
        print("\nℹ️  Use --album \"TITLE\" or --album-id ID to sync specific albums.")
        return

    if args.shared_inventory:
        shared_albums = [a for a in selected_albums if album_types.get(a.uuid) == "shared"]
        if not shared_albums:
            print("❌ No shared albums matched. Try without --albums local, --album, or --exclude filters.")
            return

        rows = []
        for album in shared_albums:
            photos = album.photos
            dates = [p.date for p in photos if p.date]
            rows.append({
                "title": album.title,
                "owner": album.owner or "",
                "count": len(photos),
                "start_date": min(dates).date().isoformat() if dates else "",
                "end_date": max(dates).date().isoformat() if dates else "",
                "album_created": album.creation_date.date().isoformat() if album.creation_date else "",
            })

        rows.sort(key=lambda r: (r["owner"], -r["count"]))

        print(f"\n📦 Shared Album Inventory ({len(rows)} albums)")
        print(f"   {'OWNER':<20} {'PHOTOS':>7}  {'DATE RANGE':<23} TITLE")
        for r in rows:
            date_range = f"{r['start_date']} to {r['end_date']}" if r["start_date"] else "(no dated photos)"
            owner_label = r["owner"] if r["owner"] else "(blank/unresolved)"
            print(f"   {owner_label:<20} {r['count']:>7}  {date_range:<23} {r['title']}")

        owner_labels = [r["owner"] or "(blank/unresolved)" for r in rows]
        owner_album_counts = Counter(owner_labels)
        total_photos = sum(r["count"] for r in rows)
        print(f"\n📊 {len(rows)} shared albums, {total_photos} total photo references, by owner:")
        for owner, n in owner_album_counts.most_common():
            owner_photos = sum(r["count"] for r, label in zip(rows, owner_labels) if label == owner)
            print(f"      {owner:<20} {n:>4} albums, {owner_photos:>6} photos")

        print("\nℹ️  'owner' blank/unresolved commonly means an album you created yourself, but this")
        print("   isn't verified — check it against one album you're certain you own before relying")
        print("   on this split to decide what's safe to consolidate from your own account.")

        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["title", "owner", "count", "start_date", "end_date", "album_created"])
                writer.writeheader()
                writer.writerows(rows)
            print(f"\n💾 Wrote {len(rows)} rows to {args.csv}")
        return

    if args.audit:
        external = []
        if args.google_albums:
            try:
                external = load_external_albums(args.google_albums)
                print(f"📄 Loaded {len(external)} Google album title(s) from {args.google_albums}")
            except OSError as e:
                print(f"❌ Could not read {args.google_albums}: {e}")
                return

        try:
            service = get_google_service()
            print("✅ Google Photos: Connected")
        except Exception as e:
            print(f"❌ Google Photos: Connection Failed ({e})")
            return

        # The API only returns albums this script created; anything else
        # (manual albums, Apple transfer-service copies) is invisible to it.
        app_albums = {}
        page_token = None
        while True:
            results = service.albums().list(pageSize=50, pageToken=page_token).execute()
            for a in results.get('albums', []):
                app_albums[a['title']] = int(a.get('mediaItemsCount', 0))
            page_token = results.get('nextPageToken')
            if not page_token:
                break

        cursor = conn.cursor()
        print(f"\n🔎 Audit: {len(selected_albums)} Apple album(s) vs Google Photos")
        print(f"   (Google albums created by this script: {len(app_albums)})\n")
        matched_external = set()
        fully_synced = partial = unsynced = 0
        for album in sorted(selected_albums, key=lambda a: a.title):
            synced_uuids = {row[0] for row in cursor.execute(
                "SELECT photo_uuid FROM uploads WHERE album_title=?", (album.title,))}
            pending = sum(1 for p in album.photos if p.uuid not in synced_uuids)
            in_google = album.title in app_albums
            if pending == 0 and in_google:
                status = "✅ synced"
                fully_synced += 1
            elif in_google:
                status = f"🔶 partial, {pending} pending"
                partial += 1
            else:
                status = "⬜ not synced"
                unsynced += 1
            line = f"   {status:<24} {album.title}  [{len(album.photos)} in Apple"
            if in_google:
                line += f", {app_albums[album.title]} in Google"
            line += "]"
            print(line)
            dupes = match_external(album.title, external)
            matched_external.update(dupes)
            for d in dupes:
                if d != album.title:
                    print(f"        ⚠️  likely duplicate album in Google: {d}")

        print(f"\n📊 {fully_synced} synced, {partial} partial, {unsynced} not synced.")

        # Only meaningful when auditing the full selection, not a filtered subset
        if not (args.album or args.album_id or args.exclude):
            apple_titles = {a.title for a in selected_albums}
            orphans = sorted(t for t in app_albums if t not in apple_titles)
            if orphans:
                print(f"\n🪦 Script-created Google album(s) with no matching Apple album (renamed or deleted in Photos?):")
                for t in orphans:
                    print(f"      - {t}")

        if external:
            unmatched = sum(1 for t in external if t not in matched_external)
            print(f"\n📄 External album list: {len(matched_external)} matched an Apple album, {unmatched} did not (manual albums, transfers of albums not in this library, etc).")
        return

    # State tracking and Google album lookup both key on title, so albums sharing
    # a title (e.g. a local album named like a shared one, or duplicates across
    # folders) would merge into a single Google album. Warn so it's not silent.
    seen_titles = {}
    for a in selected_albums:
        seen_titles.setdefault(a.title, []).append(a)
    duplicates = {t: albums for t, albums in seen_titles.items() if len(albums) > 1}
    if duplicates:
        print(f"⚠️  {len(duplicates)} album title(s) appear more than once; their photos will be combined into one Google album each:")
        for t in duplicates:
            print(f"      - {t}")

    try:
        service = get_google_service()
        print("✅ Google Photos: Connected")
    except Exception as e:
        print(f"❌ Google Photos: Connection Failed ({e})")
        return

    print("🔍 Calculating pending uploads...")
    cursor = conn.cursor()
    albums_to_sync = []
    total_pending = 0

    for album in selected_albums:
        synced_uuids = {row[0] for row in cursor.execute("SELECT photo_uuid FROM uploads WHERE album_title=?", (album.title,))}
        photos_to_sync = [p for p in album.photos if p.uuid not in synced_uuids]
        if photos_to_sync:
            albums_to_sync.append((album, photos_to_sync))
            total_pending += len(photos_to_sync)

    print(f"📊 Status: {total_pending} items to upload across {len(albums_to_sync)} albums.")

    if total_pending == 0:
        print("🎉 All synced! Exiting.")
        return

    if args.num:
        albums_to_sync = albums_to_sync[:args.num]
        subset_pending = sum(len(photos) for _, photos in albums_to_sync)
        print(f"⚠️  Limiting sync to first {args.num} albums ({subset_pending} items).")

    if not args.force:
        try:
            input("\nPress Enter to continue (or Ctrl+C to abort)...")
        except KeyboardInterrupt:
            print("\n🚫 Aborted by user.")
            return

    for album, photos_to_sync in albums_to_sync:
        print(f"\n📂 Processing {album.title} ({len(photos_to_sync)} left)")
        
        try:
            g_id = find_or_create_album(service, album.title, args.dry_run)
        except HttpError as e:
            if e.resp.status == 403 and "insufficient authentication scopes" in str(e):
                print("⚠️  Insufficient permissions detected. Deleting 'token.pickle' and re-authenticating...")
                if os.path.exists('token.pickle'):
                    os.remove('token.pickle')
                
                # Re-authenticate and update service
                service = get_google_service()
                print(f"   ℹ️  New Token Scopes: {service._http.credentials.scopes}")
                
                # Retry the operation with new service
                try:
                    g_id = find_or_create_album(service, album.title, args.dry_run)
                except HttpError as e2:
                    print(f"❌ Failed again after re-authentication: {e2}")
                    print("   👉 Note: Ensure your Google Cloud Project is set to 'Testing' and your email is added as a Test User.")
                    print("Please ensure you check ALL boxes in the Google consent screen.")
                    return
            else:
                raise e

        temp_dir = os.path.join(os.getcwd(), "temp")
        os.makedirs(temp_dir, exist_ok=True)
        total_photos = len(photos_to_sync)
        missing_photos = []

        for i, photo in enumerate(photos_to_sync, 1):
            if args.dry_run:
                print(f"   [DRY] [{i}/{total_photos}] Would sync: {photo.filename}")
                continue

            # Export
            print(f"   ⏳ [{i}/{total_photos}] Preparing {photo.filename}...", end='\r', flush=True)
            try:
                exported = photo.export(temp_dir)
            except Exception as e:
                log.warning(f"Local export failed for {photo.filename} ({photo.uuid}): {e}")
                exported = []

            if not exported:
                print(f"   ⚠️  Local missing: {photo.filename}. Queuing for iCloud download.")
                missing_photos.append(photo)
                continue

            # Upload and Verify
            if upload_photo(service, exported[0], g_id):
                conn.execute("INSERT INTO uploads (photo_uuid, album_title) VALUES (?, ?)", (photo.uuid, album.title))
                conn.commit()
                print(f"   ✅ [{i}/{total_photos}] {photo.filename} synced.                    ")
                log.info(f"Synced: {photo.filename} ({photo.uuid}) -> {album.title}")
                time.sleep(1)
            else:
                print(f"   ❌ FAILED: {photo.filename}. Will retry next run.                    ")
                log.error(f"Upload failed: {photo.filename} ({photo.uuid}) -> {album.title}")

            for f in exported:
                try:
                    os.remove(f)
                except OSError as e:
                    log.warning(f"Could not remove temp file {f}: {e}")

        # Process the batch of missing photos
        if missing_photos:
            download_and_upload_missing(service, conn, album.title, g_id, missing_photos, temp_dir, total_photos, total_photos - len(missing_photos) + 1, photosdb.library_path, verbose=args.verbose)

if __name__ == "__main__":
    main()
