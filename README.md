# Apple Photos to Google Photos Sync

This tool synchronizes albums from your local Apple Photos library (macOS) to Google Photos. It is designed to handle iCloud-optimized libraries by downloading originals on demand and tracking upload state to avoid duplicates.

## 💡 Why Use This?

**The Problem: Apple Shared Albums are not a true backup.**

Many users rely on Apple Shared Albums to store and share memories, but they have significant limitations:
1.  **Reduced Quality**: Photos in Shared Albums are often downscaled (e.g., 2048px on the long edge) and stripped of some metadata.
2.  **Sync Reliability**: Shared Albums can sometimes disappear from specific devices while remaining visible on others.
3.  **No Bulk Export**: Apple provides no native way to bulk export hundreds of shared albums to another service.

**The Use Case:**

This tool was built for a specific scenario: You have hundreds of Shared Albums visible on your Mac (e.g., running macOS Sequoia), but they are missing from your iPhone or other devices due to an iCloud sync issue.

Before upgrading your Mac or resetting your iCloud account—which could risk losing access to these albums entirely—this tool allows you to **programmatically back them up** to Google Photos, preserving your album structure.

## ⚠️ Important Caveats & Limitations

Before setting up, please understand the following:

1.  **macOS Only**: This script relies on `osxphotos` and Apple's PhotoKit/AppleScript frameworks, so it **must run on macOS**.
2.  **One-Way Sync**: This pushes photos from Apple Photos -> Google Photos. It does not sync changes back.
3.  **Google API Restrictions**:
    *   The script can only add photos to albums **created by this script**. It cannot add photos to albums you created manually in the Google Photos UI.
    *   If an album with the same name exists but wasn't created by this script, the script may fail or create a duplicate album.
4.  **iCloud Downloads**: If your photos are stored in iCloud (Optimize Mac Storage), the script will attempt to download them. This requires a stable internet connection and can be slow.
5.  **Permissions**: macOS requires explicit permission for terminal applications to access your Photos library.

---

## 💻 System Compatibility

This tool was developed and tested on:
*   **Hardware**: Apple M4
*   **OS**: macOS Sequoia (15.x)
*   **Dependencies**: `osxphotos` version 0.75.3 or newer.

Older versions of macOS or `osxphotos` may work but have not been verified.

## 🛠️ Setup Requirements

### 1. Environment Setup

Ensure you have Python 3 installed.

```bash
# Install dependencies
pip install -r requirements.txt
```

### 2. Google Cloud Setup (Crucial)

To use the Google Photos API, you must create your own project in the Google Cloud Console.

1.  Go to the Google Cloud Console.
2.  **Create a Project** (e.g., "MyPhotosSync").
3.  **Enable API**:
    *   Go to "APIs & Services" > "Library".
    *   Search for "Photos Library API" and enable it.
4.  **Configure OAuth Consent Screen**:
    *   Go to "APIs & Services" > "OAuth consent screen".
    *   Choose **External** (unless you have a Google Workspace organization).
    *   Fill in required fields (App name, email).
    *   **Test Users**: Add your own Google email address as a test user. This is required while the app is in "Testing" mode.
5.  **Create Credentials**:
    *   Go to "APIs & Services" > "Credentials".
    *   Click "Create Credentials" > "OAuth client ID".
    *   Application type: **Desktop app**.
    *   Download the JSON file, rename it to `client_secret.json`, and place it in this project folder.

---

## 🔐 macOS Permissions

When running this script for the first time, macOS will prompt you to allow access to your Photos library.

**If you see an error like `could not get authorization to access Photos library`:**

1.  Open **System Settings** > **Privacy & Security**.
2.  Click on **Photos**.
3.  Ensure the toggle is **ON** for your terminal application (e.g., `Terminal`, `iTerm`, or `VS Code`).
    *   *Note: If running from VS Code's integrated terminal, you often need to grant permission to VS Code itself.*
4.  If the app isn't listed, try running the script from the default macOS **Terminal.app** once to trigger the prompt.

---

## 🚀 Usage

You can run the script using the provided wrapper or directly via Python.

### Basic Run

Sync the first 5 albums found in your library:

```bash
./run.sh
```

*Note: `run.sh` defaults to `--num 5` unless modified.*

### Command Line Options

```bash
python3 sync_albums.py [OPTIONS]
```

| Option | Description |
| :--- | :--- |
| `--num N` | Sync the first `N` albums found. (Mutually exclusive with `--all`) |
| `--all` | Sync **all** albums found in the library. |
| `--dry-run` | Scan library and check Google Photos, but **do not** upload or create albums. |
| `--force` | Skip the confirmation prompt at startup. |
| `--verbose` | Enable detailed logging (useful for debugging iCloud downloads). |
| `--library PATH` | Explicitly specify path to `.photoslibrary`. Defaults to system library. |

### Examples

**Sync everything:**
```bash
python3 sync_albums.py --all --force
```

**Test run (no changes):**
```bash
python3 sync_albums.py --num 1 --dry-run --verbose
```

---

## ⚙️ How It Works

1.  **Authentication**: On first run, a browser window opens to log in to Google. Credentials are saved to `token.pickle`.
2.  **Scanning**: The script scans your local Apple Photos library for shared albums.
3.  **State Tracking**: It creates a local SQLite database (`sync_state.db`) to track which photo UUIDs have been uploaded to which albums. This prevents re-uploading the same photo multiple times.
4.  **Export & Upload**:
    *   It iterates through photos in an album.
    *   If a photo is missing locally (i.e., in iCloud), it uses `osxphotos` to trigger a download.
    *   Photos are exported to a temporary folder, uploaded to Google, and then deleted locally.
5.  **Cleanup**: Temporary files are removed immediately after upload.

## ❓ Troubleshooting

*   **"Rate Limit Exceeded"**: Google Photos API has quotas. If you hit this, wait a few minutes/hours and try again.
*   **"Database Locked"**: Ensure you don't have the script running in multiple terminals.
*   **Stuck on "Downloading..."**: iCloud downloads can be slow. Check the Photos app to see if it's actively downloading content.
*   **Authentication Errors**: Delete `token.pickle` and run the script again to re-login.

---

*Created by osxphotos-sync automation.*

## 🤖 AI Development Note

This code was largely developed using Google Gemini. Any PRs or improvements may not be able to be easily adapted, but you're more than welcome to fork the code and adjust with your own AI tools.
