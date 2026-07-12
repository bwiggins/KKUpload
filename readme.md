# KKUpload

## Overview

KKUpload is a Windows desktop application for bulk uploading local media into Karakeep while preserving a folder-based organization.

The goal is to make collecting and organizing large media libraries significantly faster than using the Karakeep web interface alone.

This project is designed around the author's personal workflow but should remain useful for anyone maintaining a large self-hosted Karakeep archive.

---

# Design Goals

- Minimize repetitive manual work.
- Keep the workflow transparent and easy to verify.
- Never move or delete a local file until a successful upload has been confirmed.
- Produce detailed logging of every operation.
- Make batch imports reliable and repeatable.
- Keep Version 1 intentionally small and focused.

---

# Version 1 Features

## Folder Selection

When the user presses **Upload**, display a dialog allowing selection of:

- Upload Folder
- Completed Folder
- Error Folder

Each folder field should support:

- Manual typing
- Paste
- Browse button
- Recently-used folder history

The Upload dialog closes only after validation succeeds.

---

## Folder Validation

Upload Folder:

- Must already exist.
- If missing, display an error and do not begin processing.

Completed Folder:

- Automatically create if missing.

Error Folder:

- Automatically create if missing.

Prevent invalid configurations such as:

- duplicate folder selections
- Completed or Error folders located inside the Upload folder

---

## Folder Processing

The Upload folder is scanned recursively.

Folder hierarchy is preserved.

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