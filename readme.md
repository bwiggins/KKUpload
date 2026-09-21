# KKUpload

KKUpload is a Windows desktop application for bulk uploading local media to a
self-hosted [Karakeep](https://karakeep.app/) server. It recreates folder
hierarchies as Karakeep lists, applies review tags, and moves each local file
only after Karakeep confirms that processing succeeded.

## Features

- Recursively scans an upload folder and preserves its hierarchy as nested lists.
- Supports images and common video formats.
- Adds an `!!-TAGGING-!!` review tag to imported bookmarks.
- Moves successful and failed files into separate folders while preserving paths.
- Offers a dry run before upload, progress and ETA reporting, pause, and stop.
- Resizes oversized images to configurable limits before retrying an upload.
- Detects exact duplicate assets and maintains Karakeep duplicate groups.
- Can replicate metadata across duplicates or aggressively cull duplicates when
  explicitly enabled.
- Writes a detailed automatic log for each session.

## Requirements

- Windows 10 or later
- Python 3.11 or later
- A reachable Karakeep server and API key

## Windows Shortcut

To create a local Windows shortcut with the KKUpload icon, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Create-KKUploadShortcut.ps1
```

This creates `KKUpload.lnk` in the project folder. You can pin that shortcut to Start or copy it wherever you prefer.

---


## Setup

Clone the repository, create a virtual environment, and install the dependencies:

```powershell
git clone https://github.com/bwiggins/KKUpload.git
cd KKUpload
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

In KKUpload, open **File > Connection Settings**, enter your Karakeep server URL
and API key, and test the connection. The API key is stored through the Windows
credential manager rather than in the repository or preferences file.

To create a local Windows shortcut with the KKUpload icon, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Create-KKUploadShortcut.ps1
```

The generated `KKUpload.lnk` is local-only and ignored by Git.

## Using KKUpload

Choose **Configure Upload** and select an upload folder, completed folder, and
error folder. KKUpload validates that the locations do not overlap in unsafe
ways and creates the destination folders when necessary.

Files directly inside the upload folder go into the selected base list. Files
inside subfolders go into corresponding nested Karakeep lists. After a file is
uploaded and organized successfully, KKUpload moves it to the completed folder.
Failures are moved to the error folder so the rest of the batch can continue.

Run a dry run first when working with a new folder layout. Stopping an active
upload can leave a server upload completed before the matching local move, so a
later retry may create a duplicate.

## Duplicate Maintenance

**Check Duplicates** hashes Karakeep assets and groups exact matches with
`POTENTIAL_DUPLICATE` and numbered `PD:` tags. Some scans can take a long time
on large libraries.

Metadata replication and aggressive duplicate clearing modify server data.
Review the options and your backups before enabling them. Aggressive clearing
can delete duplicate Karakeep bookmarks and should be treated as destructive.

## Local Data and Privacy

KKUpload keeps machine-specific data outside the repository in:

```text
%LOCALAPPDATA%\KKUpload\
  preferences.json
  timing_stats.json
  logs\
```

The folder is private to the local Windows profile and is not tracked by Git.
If `preferences.json` is missing, KKUpload creates it with defaults. Existing
repo-local preferences from older versions are migrated automatically.

Logs may contain local file paths, Karakeep URLs, bookmark details, and error
responses. Review them before sharing. The server URL is stored with Windows
application settings, and the API key is stored with the system keyring.

## Development

Run the test suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Runtime files, virtual environments, build output, and generated shortcuts are
excluded through `.gitignore`.

## License

No license has been selected yet. Until a license is added, copyright law
reserves reuse, modification, and redistribution rights to the author.

Example:

Upload/

    Import Inbox/
        image.jpg

    Favorite Models/
        Rosemarie Orwin/
            image2.jpg

becomes nested Karakeep lists using the same hierarchy.

Files located directly inside the Upload folder are placed into the root **Import Inbox** list.

Files inside subfolders belong only to their corresponding deepest list.

---

## Tags

Every imported bookmark automatically receives:

!!-TAGGING-!!

This tag represents items that still require manual review and organization.

---

## Successful Uploads

Only after Karakeep confirms successful upload and organization:

- move the file into the Completed folder
- preserve the original folder structure
- remove empty source folders where appropriate

---

## Failed Uploads

If processing fails:

- move the file into the Error folder
- preserve folder hierarchy
- continue processing the remainder of the batch

---

## Logging

Display a continuously scrolling console log showing every significant operation.

Examples include:

- folders discovered
- folders created
- list creation
- uploads
- tagging
- completed moves
- failures
- summary statistics

The log should remain available for review after processing finishes.

---

## Progress

Display:

- overall progress bar
- current file
- successful uploads
- failed uploads
- remaining files

---

## Stop

During processing the Upload button becomes disabled.

A Stop button becomes available.

Stop should terminate the batch without undoing already completed uploads.

---

# Future Ideas

Potential future features include:

- video support
- duplicate detection
- drag-and-drop
- watched folders
- export utility
- richer metadata support
- command-line mode
- resumable interrupted uploads

These ideas should not delay Version 1.

---

# Development Philosophy

Prefer simple, understandable code over clever code.

Implement only features that solve a real workflow problem.

Every feature should be motivated by actual use rather than speculation.

Small, working improvements are preferred over large unfinished systems.

---

# License

KKUpload is available under the [MIT License](LICENSE).

Copyright (c) 2026 Brad Wiggins

