# DiskTrace

A local storage analyzer for CCST9003 Team T11. Scan folders, see how applications use storage, and review files with clear explanations.

## Run

Requires Python 3.11 or newer. No extra packages needed.

```powershell
python app.py
```

Open [localhost:8765](http://127.0.0.1:8765) in your browser. You can also double-click `start.bat` on Windows. Press Ctrl+C to stop the server.

## Features

- Storage breakdown by application folder.
- Search, filters, sorting and CSV export.
- File categories and explanations in a side panel.
- Folder comparisons showing created, changed and removed files.
- Quarantine and restore for demo files.

## Try it

Click **Scan demo files**, select a file, and read its explanation. Use **Review quarantine** to move a sample file, then restore it from **Quarantine**.

Use **Open folder** to scan your own files. Real folders are read-only. Everything runs locally.

## Built with

Python, SQLite, HTML, CSS and JavaScript.
