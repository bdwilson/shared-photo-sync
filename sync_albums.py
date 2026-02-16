import os
import sqlite3
import argparse
import sys
import osxphotos
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

def upload_photo(service, file_path, album_id):
    """The two-step Google Photos upload process."""
    # 1. Upload bytes to get an upload token
    try:
        with open(file_path, 'rb') as f:
            url = 'https://photoslibrary.googleapis.com/v1/uploads'
            headers = {
                'Authorization': f'Bearer {service._http.credentials.token}',
                'Content-Type': 'application/octet-stream',
                'X-Goog-Upload-Protocol': 'raw',
            }
            response = requests.post(url, data=f, headers=headers)
            response.raise_for_status()
            upload_token = response.text
    except Exception as e:
        print(f"   ⚠️  Upload bytes failed: {e}")
        return None
        
    # 2. Add the token to the specific album
    body = {
        'albumId': album_id,
        'newMediaItems': [{'simpleMediaItem': {'uploadToken': upload_token}}]
    }
    try:
        result = service.mediaItems().batchCreate(body=body).execute()
    except Exception as e:
        print(f"   ⚠️  Media creation failed: {e}")
        return False
    
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
            print(f"      Processing chunk {i//chunk_size + 1} (items {i+1}-{min(i+chunk_size, len(missing_photos))})...")
            print(f"      Downloading: {', '.join(filenames)}")
            
            # Capture output to detect permission errors
            result = subprocess.run(cmd, check=True, timeout=300, capture_output=True, text=True)
            print(result.stdout)
            
        except subprocess.TimeoutExpired:
            print(f"      ⚠️  CLI batch export timed out.")
            continue
        except subprocess.CalledProcessError as e:
            print(e.stdout)
            print(e.stderr)
            if "could not get authorization" in e.stdout or "could not get authorization" in e.stderr:
                print(f"\n❌ Critical Error: Missing permissions for Photos library.")
                print(f"   1. Open 'System Settings > Privacy & Security > Photos'.")
                print(f"   2. Enable access for 'Visual Studio Code', 'Terminal', or 'iTerm'.")
                print(f"   3. If your app is missing, try running this script from the macOS 'Terminal' app instead.")
                sys.exit(1)
            print(f"      ⚠️  CLI batch export failed.")
            continue

        # Process the downloaded files
        for j, photo in enumerate(chunk):
            # Find files matching UUID (handles jpg, mov, etc)
            found_files = glob.glob(os.path.join(temp_dir, f"{photo.uuid}.*"))
            
            if not found_files:
                print(f"      ❌ Still missing: {photo.filename}")
                continue
                
            # Upload the first file found (usually the image)
            # Note: This skips the video part of Live Photos if both exist, consistent with main loop
            current_idx = start_index + i + j
            print(f"      ☁️  Uploading {photo.filename} to Google Photos...")
            if upload_photo(service, found_files[0], g_id):
                conn.execute("INSERT INTO uploads (photo_uuid, album_title) VALUES (?, ?)", (photo.uuid, album_title))
                conn.commit()
                print(f"   ✅ [{current_idx}/{total_count}] {photo.filename} (from iCloud) synced.")
            else:
                print(f"   ❌ FAILED: {photo.filename}")

            for f in found_files:
                try:
                    os.remove(f)
                except:
                    pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--library", help="Explicitly specify path to Photos library (e.g. /Users/me/Pictures/Photos Library.photoslibrary)")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--num", type=int, help="Number of albums to sync")
    group.add_argument("--all", action="store_true", help="Sync all albums")
    args = parser.parse_args()

    print("🚀 Initializing Sync...")
    conn = setup_tracking()
    
    try:
        service = get_google_service()
        print("✅ Google Photos: Connected")
    except Exception as e:
        print(f"❌ Google Photos: Connection Failed ({e})")
        return

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
            
        # Use album_info_shared to get AlbumInfo objects directly
        shared_albums = photosdb.album_info_shared
        print(f"✅ Local Library: Connected ({len(shared_albums)} shared albums found)")
        print(f"   📂 Path: {photosdb.library_path}")
    except Exception as e:
        print(f"❌ Local Library: Connection Failed ({e})")
        return
    
    print("🔍 Calculating pending uploads...")
    cursor = conn.cursor()
    albums_to_sync = []
    total_pending = 0

    for album in shared_albums:
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
            try:
                exported = photo.export(temp_dir)
            except Exception:
                exported = []

            if not exported:
                print(f"   ⚠️  Local missing: {photo.filename}. Queuing for iCloud download.")
                missing_photos.append(photo)
                continue
            
            # Upload and Verify
            if upload_photo(service, exported[0], g_id):
                conn.execute("INSERT INTO uploads (photo_uuid, album_title) VALUES (?, ?)", (photo.uuid, album.title))
                conn.commit()
                print(f"   ✅ [{i}/{total_photos}] {photo.filename} synced.")
            else:
                print(f"   ❌ FAILED: {photo.filename}. Will retry next run.")

            for f in exported:
                os.remove(f)

        # Process the batch of missing photos
        if missing_photos:
            download_and_upload_missing(service, conn, album.title, g_id, missing_photos, temp_dir, total_photos, total_photos - len(missing_photos) + 1, photosdb.library_path, verbose=args.verbose)

if __name__ == "__main__":
    main()
