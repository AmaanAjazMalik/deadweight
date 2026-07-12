#!/usr/bin/env python3
"""
Deadweight — a local desktop storage triage tool.

Auto-detects the disks/volumes attached to this machine and scans them
for size hogs and likely-junk files, sorted into Applications / Videos /
Images / Documents / Archives / Audio / Other.

Run it directly:  python deadweight_app.py
Package it as a real double-click app: see README.md next to this file.
"""

import os
import sys
import time
import queue
import platform
import threading
import subprocess
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False

try:
    from send2trash import send2trash
    HAVE_TRASH = True
except ImportError:
    HAVE_TRASH = False

# ----------------------------------------------------------------------
# Category / junk rules  (same logic as the web prototype)
# ----------------------------------------------------------------------

CATEGORIES = {
    "apps":     {"label": "Applications", "color": "#7C8CFF",
                 "exts": {"exe", "msi", "dmg", "app", "apk", "deb", "rpm", "pkg", "appimage", "bat", "sh"}},
    "videos":   {"label": "Videos", "color": "#F0637A",
                 "exts": {"mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v", "mpg", "mpeg"}},
    "images":   {"label": "Images", "color": "#3DC6C0",
                 "exts": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "heic", "tiff", "tif", "raw"}},
    "docs":     {"label": "Documents", "color": "#F2A94D",
                 "exts": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "odt", "pages", "key", "numbers"}},
    "archives": {"label": "Archives", "color": "#B18CF0",
                 "exts": {"zip", "rar", "7z", "tar", "gz", "bz2", "iso"}},
    "audio":    {"label": "Audio", "color": "#56C596",
                 "exts": {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"}},
    "other":    {"label": "Other", "color": "#6B7280", "exts": set()},
}

JUNK_EXTS = {"tmp", "temp", "log", "bak", "old", "cache", "dmp", "crdownload", "part", "download"}
JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", ".localized"}
JUNK_DIRS = {"node_modules", "__pycache__", ".git", ".cache", "cache", "temp", "tmp",
             "$recycle.bin", ".trash", "trash"}

# filesystem types to skip when auto-detecting "real" local disks
SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2", "overlay",
    "squashfs", "autofs", "mqueue", "debugfs", "tracefs", "securityfs", "pstore",
    "bpf", "configfs", "fusectl", "hugetlbfs", "binfmt_misc", "efivarfs", "rpc_pipefs",
}


def ext_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot > 0 else ""


def category_of(extension: str) -> str:
    for key, info in CATEGORIES.items():
        if extension in info["exts"]:
            return key
    return "other"


def fmt_bytes(n: float) -> str:
    if n == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.2f} {units[i]}" if i and n < 10 else f"{n:.1f} {units[i]}" if i else f"{int(n)} {units[i]}"


def fmt_date(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "—"


# ----------------------------------------------------------------------
# Automatic disk / volume detection
# ----------------------------------------------------------------------

def detect_drives():
    """Return a list of dicts: {label, path, total, used, free} for real local disks."""
    drives = []

    if HAVE_PSUTIL:
        for part in psutil.disk_partitions(all=False):
            if part.fstype.lower() in SKIP_FSTYPES:
                continue
            if platform.system() == "Linux" and not (
                part.mountpoint == "/" or part.mountpoint.startswith("/home")
                or part.mountpoint.startswith("/media") or part.mountpoint.startswith("/mnt")
                or part.mountpoint.startswith("/run/media")
            ):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            drives.append({
                "label": f"{part.device} ({part.mountpoint})",
                "path": part.mountpoint,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            })
    else:
        # Fallback with no third-party deps
        system = platform.system()
        if system == "Windows":
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = f"{chr(65 + i)}:\\"
                    drive_type = ctypes.windll.kernel32.GetDriveTypeW(letter)
                    if drive_type == 3:  # DRIVE_FIXED
                        drives.append({"label": letter, "path": letter, "total": 0, "used": 0, "free": 0})
        elif system == "Darwin":
            drives.append({"label": "Macintosh HD (/)", "path": "/", "total": 0, "used": 0, "free": 0})
            vol_dir = "/Volumes"
            if os.path.isdir(vol_dir):
                for name in os.listdir(vol_dir):
                    drives.append({"label": name, "path": os.path.join(vol_dir, name), "total": 0, "used": 0, "free": 0})
        else:
            drives.append({"label": "Root (/)", "path": "/", "total": 0, "used": 0, "free": 0})

    if not drives:
        home = os.path.expanduser("~")
        drives.append({"label": f"Home ({home})", "path": home, "total": 0, "used": 0, "free": 0})

    return drives


def default_drive_index(drives):
    """Prefer the drive that contains the user's home directory."""
    home = os.path.expanduser("~")
    for i, d in enumerate(drives):
        if home.startswith(d["path"]) and d["path"] != os.sep:
            return i
    for i, d in enumerate(drives):
        if d["path"] == home:
            return i
    return 0


# ----------------------------------------------------------------------
# Scanning (runs on a background thread)
# ----------------------------------------------------------------------

class FileEntry:
    __slots__ = ("name", "path", "size", "mtime", "ext", "cat", "junk", "dup_of")

    def __init__(self, name, path, size, mtime):
        self.name = name
        self.path = path
        self.size = size
        self.mtime = mtime
        self.ext = ext_of(name)
        self.cat = category_of(self.ext)
        self.junk = None
        self.dup_of = 0


def junk_reason(entry: FileEntry) -> str:
    lower_name = entry.name.lower()
    lower_path = entry.path.lower()

    if entry.size == 0:
        return "Empty file (0 bytes)"
    if lower_name in JUNK_NAMES:
        return "System-generated clutter file"
    if entry.ext in JUNK_EXTS:
        return f"Temporary / cache file (.{entry.ext})"
    parts = lower_path.replace("\\", "/").split("/")
    for d in JUNK_DIRS:
        if d in parts:
            return f'Sits inside a "{d}" folder — usually regenerable'
    age_days = (time.time() - entry.mtime) / 86400
    if entry.ext in {"exe", "dmg", "msi", "pkg"} and entry.size > 50 * 1024 * 1024 and age_days > 180:
        return f"Old installer (~{int(age_days / 30)} mo old) — probably already installed"
    return None


def scan_directory(root_path, progress_cb, stop_flag):
    """Walk root_path, yielding FileEntry objects. progress_cb(count, current_dir) called periodically."""
    entries = []
    count = 0
    last_report = 0

    def on_error(err):
        pass  # permission errors etc: just skip that branch

    for dirpath, dirnames, filenames in os.walk(root_path, onerror=on_error, followlinks=False):
        if stop_flag.is_set():
            break
        # skip obviously virtual / system dirs that blow up scan time for little value
        dirnames[:] = [d for d in dirnames if d not in {"/proc", "/sys", "/dev"}]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.lstat(fpath)
                if not os.path.isfile(fpath):
                    continue
                entries.append(FileEntry(fname, fpath, st.st_size, st.st_mtime))
                count += 1
            except (OSError, PermissionError):
                continue
        if count - last_report > 250:
            last_report = count
            progress_cb(count, dirpath)

    progress_cb(count, "Finalizing…")

    # duplicate detection: same name + same (non-zero) size
    groups = {}
    for e in entries:
        key = (e.name.lower(), e.size)
        groups.setdefault(key, []).append(e)
    for group in groups.values():
        if len(group) > 1 and group[0].size > 0:
            for e in group:
                e.dup_of = len(group) - 1

    for e in entries:
        if e.dup_of:
            plural = "s" if e.dup_of > 1 else ""
            e.junk = f"Duplicate — same name & size as {e.dup_of} other file{plural}"
        else:
            e.junk = junk_reason(e)

    return entries


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------

BG = "#10131A"
PANEL = "#171B24"
PANEL2 = "#1D2230"
BORDER = "#2A3040"
TEXT = "#E9EBF1"
TEXT_MUTED = "#8A93A6"
TEXT_DIM = "#5C6478"
AMBER = "#F2A94D"
RED = "#E8604C"
GREEN = "#4FD1A5"
ACCENT = "#7C8CFF"


class DeadweightApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Deadweight — Local Storage Triage")
        self.geometry("1180x760")
        self.configure(bg=BG)
        self.minsize(900, 600)

        self.drives = detect_drives()
        self.entries = []
        self.scan_thread = None
        self.stop_flag = threading.Event()
        self.progress_queue = queue.Queue()
        self.large_threshold_mb = tk.IntVar(value=100)

        self._build_style()
        self._build_layout()
        self.after(100, self._poll_queue)

        # Automatically start scanning as soon as the app opens
        self.after(200, lambda: self.start_scan(self.drives[self.default_idx]["path"]))

    # ---------------- styling ----------------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Helvetica", 10))
        style.configure("Muted.TLabel", background=BG, foreground=TEXT_MUTED, font=("Helvetica", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Helvetica", 18, "bold"))
        style.configure("Stat.TLabel", background=PANEL, foreground=TEXT, font=("Helvetica", 16, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL2, foreground=TEXT_MUTED, padding=(14, 8), font=("Helvetica", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", TEXT)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=26, borderwidth=0, font=("Menlo", 10))
        style.configure("Treeview.Heading", background=PANEL2, foreground=TEXT_DIM, font=("Helvetica", 9, "bold"))
        style.map("Treeview", background=[("selected", "#2C3350")])
        style.configure("TButton", background=PANEL2, foreground=TEXT, borderwidth=1, font=("Helvetica", 9, "bold"))
        style.configure("Accent.TButton", background=ACCENT, foreground="#0B0D12")
        style.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL2)

    def _build_layout(self):
        self.default_idx = default_drive_index(self.drives)

        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", padx=18, pady=(16, 8))

        left = ttk.Frame(header, style="TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="Deadweight", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Automatically detects your local disks and scans for size hogs and junk.",
                  style="Muted.TLabel").pack(anchor="w")

        right = ttk.Frame(header, style="TFrame")
        right.pack(side="right")
        ttk.Label(right, text="Detected disk:", style="Muted.TLabel").grid(row=0, column=0, sticky="e", padx=(0, 6))
        drive_labels = [d["label"] for d in self.drives]
        self.drive_var = tk.StringVar(value=drive_labels[self.default_idx])
        combo = ttk.Combobox(right, textvariable=self.drive_var, values=drive_labels, state="readonly", width=34)
        combo.grid(row=0, column=1)
        combo.bind("<<ComboboxSelected>>", self._on_drive_change)
        self.rescan_btn = ttk.Button(right, text="Rescan", command=self._rescan, style="Accent.TButton")
        self.rescan_btn.grid(row=0, column=2, padx=(8, 0))

        # status / progress
        status_frame = ttk.Frame(self, style="TFrame")
        status_frame.pack(fill="x", padx=18)
        self.status_label = ttk.Label(status_frame, text="Starting scan…", style="Muted.TLabel")
        self.status_label.pack(side="left")
        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=200)
        self.progress.pack(side="right")
        self.progress.start(12)

        # gauge canvas (signature element)
        gauge_frame = ttk.Frame(self, style="Panel.TFrame")
        gauge_frame.pack(fill="x", padx=18, pady=10)
        self.total_label = ttk.Label(gauge_frame, text="—", style="Stat.TLabel", background=PANEL)
        self.total_label.pack(anchor="w", padx=14, pady=(10, 0))
        self.gauge_canvas = tk.Canvas(gauge_frame, height=26, bg=PANEL, highlightthickness=0)
        self.gauge_canvas.pack(fill="x", padx=14, pady=8)
        self.legend_label = tk.Label(gauge_frame, text="", bg=PANEL, fg=TEXT_MUTED, font=("Menlo", 9), justify="left")
        self.legend_label.pack(anchor="w", padx=14, pady=(0, 10))

        # stat cards
        stats_frame = ttk.Frame(self, style="TFrame")
        stats_frame.pack(fill="x", padx=18, pady=(0, 10))
        self.stat_vars = {}
        for i, (key, label) in enumerate([
            ("files", "Files scanned"), ("big", "Over 100 MB"),
            ("junk", "Flagged as junk"), ("reclaim", "Reclaimable junk")
        ]):
            card = ttk.Frame(stats_frame, style="Panel.TFrame")
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            stats_frame.columnconfigure(i, weight=1)
            v = tk.StringVar(value="—")
            self.stat_vars[key] = v
            tk.Label(card, textvariable=v, bg=PANEL, fg=TEXT, font=("Helvetica", 15, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Label(card, text=label.upper(), bg=PANEL, fg=TEXT_DIM, font=("Menlo", 8)).pack(anchor="w", padx=12, pady=(0, 10))

        # notebook tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        self.tab_overview = ttk.Frame(self.notebook, style="TFrame")
        self.tab_large = ttk.Frame(self.notebook, style="TFrame")
        self.tab_junk = ttk.Frame(self.notebook, style="TFrame")
        self.tab_browse = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_overview, text="Overview")
        self.notebook.add(self.tab_large, text="Largest files")
        self.notebook.add(self.tab_junk, text="Likely useless")
        self.notebook.add(self.tab_browse, text="Browse by type")

        self._build_overview_tab()
        self._build_large_tab()
        self._build_junk_tab()
        self._build_browse_tab()

        if not HAVE_TRASH:
            note = ttk.Label(self, text="Tip: run  pip install send2trash  to enable safe delete-to-trash from this app.",
                              style="Muted.TLabel")
            note.pack(pady=(0, 10))

    # ---------------- overview tab ----------------
    def _build_overview_tab(self):
        self.cat_cards_frame = ttk.Frame(self.tab_overview, style="TFrame")
        self.cat_cards_frame.pack(fill="x", padx=4, pady=10)
        self.cat_card_widgets = {}
        for i, key in enumerate(CATEGORIES):
            card = tk.Frame(self.cat_cards_frame, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=0, column=i, sticky="ew", padx=4)
            self.cat_cards_frame.columnconfigure(i, weight=1)
            dot = tk.Label(card, text="●", fg=CATEGORIES[key]["color"], bg=PANEL, font=("Helvetica", 12))
            dot.pack(anchor="w", padx=10, pady=(10, 0))
            name_lbl = tk.Label(card, text=CATEGORIES[key]["label"], bg=PANEL, fg=TEXT_MUTED, font=("Helvetica", 9))
            name_lbl.pack(anchor="w", padx=10)
            size_lbl = tk.Label(card, text="0 B", bg=PANEL, fg=TEXT, font=("Helvetica", 14, "bold"))
            size_lbl.pack(anchor="w", padx=10, pady=(2, 2))
            count_lbl = tk.Label(card, text="0 files", bg=PANEL, fg=TEXT_DIM, font=("Menlo", 8))
            count_lbl.pack(anchor="w", padx=10, pady=(0, 10))
            card.bind("<Button-1>", lambda e, k=key: self._jump_to_category(k))
            for w in (dot, name_lbl, size_lbl, count_lbl):
                w.bind("<Button-1>", lambda e, k=key: self._jump_to_category(k))
            self.cat_card_widgets[key] = {"size": size_lbl, "count": count_lbl}

        hint = ttk.Label(self.tab_overview, text="Click a category to browse its files.", style="Muted.TLabel")
        hint.pack(anchor="w", padx=8, pady=4)

    def _jump_to_category(self, key):
        self.notebook.select(self.tab_browse)
        self.browse_cat_var.set(CATEGORIES[key]["label"])
        self._render_browse()

    # ---------------- large files tab ----------------
    def _build_large_tab(self):
        controls = ttk.Frame(self.tab_large, style="TFrame")
        controls.pack(fill="x", pady=(8, 6))
        ttk.Label(controls, text="Show files over:", style="Muted.TLabel").pack(side="left")
        scale = ttk.Scale(controls, from_=10, to=2000, orient="horizontal", variable=self.large_threshold_mb,
                           command=lambda e: self._on_threshold_change(), length=180)
        scale.pack(side="left", padx=8)
        self.threshold_label = ttk.Label(controls, text="100 MB", style="Muted.TLabel")
        self.threshold_label.pack(side="left", padx=(0, 16))
        self.search_large_var = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.search_large_var, width=30)
        entry.pack(side="left", padx=8)
        entry.insert(0, "")
        self.search_large_var.trace_add("write", lambda *a: self._render_large())
        ttk.Label(controls, text="filter", style="Muted.TLabel").pack(side="left")
        ttk.Button(controls, text="Export list (.txt)", command=lambda: self._export(self._large_rows(), "large-files.txt")).pack(side="right", padx=4)
        if HAVE_TRASH:
            ttk.Button(controls, text="Move selected to Trash", command=lambda: self._delete_selected(self.tree_large)).pack(side="right", padx=4)

        self.tree_large = self._make_tree(self.tab_large, ("name", "path", "type", "size", "modified"))

    def _on_threshold_change(self):
        self.threshold_label.config(text=f"{int(self.large_threshold_mb.get())} MB")
        self._render_large()

    # ---------------- junk tab ----------------
    def _build_junk_tab(self):
        controls = ttk.Frame(self.tab_junk, style="TFrame")
        controls.pack(fill="x", pady=(8, 6))
        self.search_junk_var = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.search_junk_var, width=30)
        entry.pack(side="left", padx=8)
        self.search_junk_var.trace_add("write", lambda *a: self._render_junk())
        ttk.Label(controls, text="filter", style="Muted.TLabel").pack(side="left")
        ttk.Button(controls, text="Export list (.txt)", command=lambda: self._export(self._junk_rows(), "junk-files.txt")).pack(side="right", padx=4)
        if HAVE_TRASH:
            ttk.Button(controls, text="Move selected to Trash", command=lambda: self._delete_selected(self.tree_junk)).pack(side="right", padx=4)

        self.tree_junk = self._make_tree(self.tab_junk, ("name", "path", "reason", "size"))

    # ---------------- browse tab ----------------
    def _build_browse_tab(self):
        controls = ttk.Frame(self.tab_browse, style="TFrame")
        controls.pack(fill="x", pady=(8, 6))
        ttk.Label(controls, text="Category:", style="Muted.TLabel").pack(side="left")
        cat_labels = [CATEGORIES[k]["label"] for k in CATEGORIES]
        self.browse_cat_var = tk.StringVar(value="Videos")
        combo = ttk.Combobox(controls, textvariable=self.browse_cat_var, values=cat_labels, state="readonly", width=16)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_browse())
        self.search_browse_var = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.search_browse_var, width=30)
        entry.pack(side="left", padx=8)
        self.search_browse_var.trace_add("write", lambda *a: self._render_browse())
        ttk.Label(controls, text="filter", style="Muted.TLabel").pack(side="left")
        if HAVE_TRASH:
            ttk.Button(controls, text="Move selected to Trash", command=lambda: self._delete_selected(self.tree_browse)).pack(side="right", padx=4)

        self.tree_browse = self._make_tree(self.tab_browse, ("name", "path", "size", "modified"))

    # ---------------- shared tree helper ----------------
    def _make_tree(self, parent, columns):
        frame = ttk.Frame(parent, style="TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        headers = {"name": "File", "path": "Path", "type": "Type", "size": "Size",
                   "modified": "Modified", "reason": "Why it's flagged"}
        widths = {"name": 220, "path": 340, "type": 110, "size": 100, "modified": 100, "reason": 300}
        for c in columns:
            tree.heading(c, text=headers[c])
            tree.column(c, width=widths[c], anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        return tree

    # ---------------- scanning control ----------------
    def _on_drive_change(self, event=None):
        idx = [d["label"] for d in self.drives].index(self.drive_var.get())
        self.start_scan(self.drives[idx]["path"])

    def _rescan(self):
        idx = [d["label"] for d in self.drives].index(self.drive_var.get())
        self.start_scan(self.drives[idx]["path"])

    def start_scan(self, path):
        if self.scan_thread and self.scan_thread.is_alive():
            self.stop_flag.set()
            self.scan_thread.join(timeout=1)
        self.stop_flag = threading.Event()
        self.entries = []
        self.status_label.config(text=f"Scanning {path}…")
        self.progress.start(12)

        def worker():
            def progress_cb(count, current):
                self.progress_queue.put(("progress", count, current))
            try:
                result = scan_directory(path, progress_cb, self.stop_flag)
                self.progress_queue.put(("done", result, path))
            except Exception as exc:
                self.progress_queue.put(("error", str(exc), path))

        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()

    def _poll_queue(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                if msg[0] == "progress":
                    _, count, current = msg
                    short = current if len(current) < 60 else "…" + current[-57:]
                    self.status_label.config(text=f"Scanning… {count:,} files found  ·  {short}")
                elif msg[0] == "done":
                    _, entries, path = msg
                    self.entries = entries
                    self.progress.stop()
                    self.status_label.config(text=f"Done — scanned {path}")
                    self._render_all()
                elif msg[0] == "error":
                    self.progress.stop()
                    self.status_label.config(text="Scan error")
                    messagebox.showerror("Scan error", msg[1])
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    # ---------------- rendering ----------------
    def _render_all(self):
        total = sum(e.size for e in self.entries)
        by_cat = {k: {"size": 0, "count": 0} for k in CATEGORIES}
        for e in self.entries:
            by_cat[e.cat]["size"] += e.size
            by_cat[e.cat]["count"] += 1

        self.total_label.config(text=f"{fmt_bytes(total)}  across {len(self.entries):,} files")

        # gauge canvas
        self.gauge_canvas.delete("all")
        self.gauge_canvas.update_idletasks()
        width = max(self.gauge_canvas.winfo_width(), 800)
        x = 0
        legend_bits = []
        for key, info in CATEGORIES.items():
            frac = (by_cat[key]["size"] / total) if total else 0
            seg_w = frac * width
            if seg_w > 0:
                self.gauge_canvas.create_rectangle(x, 0, x + seg_w, 26, fill=info["color"], width=0)
                x += seg_w
            if by_cat[key]["size"] > 0:
                legend_bits.append(f"● {info['label']}: {fmt_bytes(by_cat[key]['size'])} ({by_cat[key]['count']})")
        self.legend_label.config(text="    ".join(legend_bits))

        junk_entries = [e for e in self.entries if e.junk]
        junk_size = sum(e.size for e in junk_entries)
        big_count = sum(1 for e in self.entries if e.size > 100 * 1024 * 1024)

        self.stat_vars["files"].set(f"{len(self.entries):,}")
        self.stat_vars["big"].set(f"{big_count:,}")
        self.stat_vars["junk"].set(f"{len(junk_entries):,}")
        self.stat_vars["reclaim"].set(fmt_bytes(junk_size))

        for key in CATEGORIES:
            self.cat_card_widgets[key]["size"].config(text=fmt_bytes(by_cat[key]["size"]))
            self.cat_card_widgets[key]["count"].config(text=f"{by_cat[key]['count']:,} files")

        self._render_large()
        self._render_junk()
        self._render_browse()

    def _large_rows(self):
        threshold = self.large_threshold_mb.get() * 1024 * 1024
        q = self.search_large_var.get().lower() if hasattr(self, "search_large_var") else ""
        rows = [e for e in self.entries if e.size >= threshold]
        if q:
            rows = [e for e in rows if q in e.name.lower() or q in e.path.lower()]
        rows.sort(key=lambda e: e.size, reverse=True)
        return rows[:500]

    def _render_large(self):
        if not hasattr(self, "tree_large"):
            return
        self.tree_large.delete(*self.tree_large.get_children())
        for e in self._large_rows():
            self.tree_large.insert("", "end", iid=e.path, values=(
                e.name, e.path, CATEGORIES[e.cat]["label"], fmt_bytes(e.size), fmt_date(e.mtime)
            ))

    def _junk_rows(self):
        q = self.search_junk_var.get().lower() if hasattr(self, "search_junk_var") else ""
        rows = [e for e in self.entries if e.junk]
        if q:
            rows = [e for e in rows if q in e.name.lower() or q in e.path.lower()]
        rows.sort(key=lambda e: e.size, reverse=True)
        return rows[:500]

    def _render_junk(self):
        if not hasattr(self, "tree_junk"):
            return
        self.tree_junk.delete(*self.tree_junk.get_children())
        for e in self._junk_rows():
            self.tree_junk.insert("", "end", iid=e.path, values=(
                e.name, e.path, e.junk, fmt_bytes(e.size)
            ))

    def _render_browse(self):
        if not hasattr(self, "tree_browse"):
            return
        label_to_key = {v["label"]: k for k, v in CATEGORIES.items()}
        key = label_to_key.get(self.browse_cat_var.get(), "videos")
        q = self.search_browse_var.get().lower() if hasattr(self, "search_browse_var") else ""
        rows = [e for e in self.entries if e.cat == key]
        if q:
            rows = [e for e in rows if q in e.name.lower() or q in e.path.lower()]
        rows.sort(key=lambda e: e.size, reverse=True)
        self.tree_browse.delete(*self.tree_browse.get_children())
        for e in rows[:500]:
            self.tree_browse.insert("", "end", iid=e.path, values=(
                e.name, e.path, fmt_bytes(e.size), fmt_date(e.mtime)
            ))

    # ---------------- export / delete ----------------
    def _export(self, rows, filename):
        out_path = os.path.join(os.path.expanduser("~"), filename)
        with open(out_path, "w") as f:
            for e in rows:
                f.write(f"{e.path}\t{fmt_bytes(e.size)}\n")
        messagebox.showinfo("Exported", f"Saved {len(rows)} entries to:\n{out_path}")

    def _delete_selected(self, tree):
        selected = tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return
        matched = [e for e in self.entries if e.path in selected]
        total_size = sum(e.size for e in matched)
        confirm = messagebox.askyesno(
            "Move to Trash?",
            f"Move {len(matched)} file(s) totaling {fmt_bytes(total_size)} to the Trash?\n\n"
            "You can restore them from your system Trash afterward."
        )
        if not confirm:
            return
        failed = []
        for e in matched:
            try:
                send2trash(e.path)
            except Exception:
                failed.append(e.path)
        self.entries = [e for e in self.entries if e.path not in [m.path for m in matched] or e.path in failed]
        self._render_all()
        if failed:
            messagebox.showwarning("Some files failed", f"{len(failed)} file(s) could not be moved to Trash.")


def main():
    if not HAVE_PSUTIL:
        print("Note: psutil isn't installed — falling back to basic drive detection.\n"
              "Run:  pip install psutil send2trash   for full disk detection and safe delete.")
    app = DeadweightApp()
    app.mainloop()


if __name__ == "__main__":
    main()
