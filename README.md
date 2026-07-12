# Deadweight — local storage triage app

A real desktop app (not a web page): it opens, **automatically detects the
disks/volumes attached to your machine**, and immediately starts scanning
the one your home folder lives on — no folder picker, no manual step.
You can switch to any other detected disk from the dropdown at any time.

Everything runs locally in this one process. Nothing is uploaded anywhere.

## What it does

- **Auto-detects disks** at launch (uses `psutil`; falls back to OS-native
  detection if `psutil` isn't installed).
- **Scans automatically** — starts on the disk containing your home folder
  the moment the app opens.
- **Overview tab** — a segmented bar shows how space splits across
  Applications / Videos / Images / Documents / Archives / Audio.
- **Largest files tab** — everything over a threshold you drag (default 100 MB).
- **Likely useless tab** — temp/cache files, system clutter (`Thumbs.db`,
  `.DS_Store`), files inside `node_modules`/`.git`/cache folders, empty files,
  old installers, and probable duplicates (same name + size).
- **Browse by type tab** — drill into just one category.
- **Move selected to Trash** — if `send2trash` is installed, you can select
  rows and send them to your system Trash (recoverable, not permanent
  deletion) right from the app, with a confirmation dialog showing count
  and total size first.

## Run it

You need Python 3.9+ installed.

```bash
pip install -r requirements.txt
python deadweight_app.py
```

That's it — a window opens and the scan starts by itself.

If you skip installing `send2trash`, the app still works fully for
scanning/browsing/exporting; it just won't show the delete button (so it
never has to guess about permanently removing something).

## Turn it into a real double-click app (no terminal needed)

This packages the script into a native executable for whichever OS you run
the command on. You have to run this on the actual OS you want the app for —
a Mac build has to be built on a Mac, a Windows build on Windows, etc.

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name Deadweight deadweight_app.py
```

This produces:
- **Windows:** `dist\Deadweight.exe` — double-click to run, no console window.
- **macOS:** `dist/Deadweight.app` — drag into Applications.
- **Linux:** `dist/Deadweight` — a standalone binary; make it executable
  (`chmod +x dist/Deadweight`) and double-click or run it.

## A couple of honest notes

- **Full-disk scans on Windows/macOS system drives** will skip a lot of
  protected system folders you don't have permission to read — that's
  normal and the app just moves past them.
- **Nothing is auto-deleted.** Trash moves only happen when you select
  rows and confirm. Permanent deletion is never offered — the Trash is
  always recoverable if you change your mind.



<img width="1447" height="886" alt="Screenshot 2026-07-12 174157" src="https://github.com/user-attachments/assets/5a268b24-848d-47c8-9716-bba21e4c46a9" />
