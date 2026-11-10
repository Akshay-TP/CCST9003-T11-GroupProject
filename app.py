"""DiskTrace: a local, metadata-only storage explorer for CCST9003 T11."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
LIMIT = 20_000
MAX_SECONDS = 15
APP_NAMES = {"orbitbrowser": "Orbit Browser", "chatter": "Chatter", "codepad": "CodePad"}


def linked(path: Path) -> bool:
    """Treat Windows junctions and other reparse points as links too."""
    s = path.lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, "st_file_attributes", 0) & 0x400)


def classify(relative: Path) -> dict:
    parts = [p.casefold() for p in relative.parts]
    name = parts[-1]
    suffix = relative.suffix.casefold()
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        kind = "Database"
    elif suffix in {".json", ".ini", ".cfg", ".toml", ".yaml", ".yml"} or any(p in {"settings", "config", "preferences"} for p in parts[:-1]):
        kind = "Settings"
    elif suffix in {".docx", ".pdf", ".txt", ".pptx", ".xlsx", ".jpg", ".png", ".mp4"}:
        kind = "Personal files"
    elif "cache" in parts[:-1] or "code cache" in parts[:-1] or "gpucache" in parts[:-1]:
        kind = "Cache"
    elif "logs" in parts[:-1] or suffix == ".log":
        kind = "Logs"
    elif "temp" in parts[:-1] or "tmp" in parts[:-1] or suffix in {".tmp", ".temp"}:
        kind = "Temporary"
    else:
        kind = "Other"

    app, confidence, evidence = "Unknown or shared", "Unknown", "No application-specific folder found."
    for part, label in APP_NAMES.items():
        if part in parts[:-1]:
            app, confidence = label, "Inferred"
            evidence = f"Path contains the {part} folder; this does not prove ownership."
            break
    if confidence == "Unknown":
        for marker in ("local", "roaming"):
            # Infer only from an explicit AppData/Local|Roaming/<name> sequence.
            for i in range(1, len(parts) - 2):
                if parts[i - 1] == "appdata" and parts[i] == marker:
                    app = relative.parts[i + 1]
                    confidence = "Inferred"
                    evidence = f"Located under AppData/{marker}/{app}; publisher folders may contain several apps."
                    break
    if any(p in {"shared", "common"} for p in parts[:-1]):
        app, confidence, evidence = "Unknown or shared", "Unknown", "Shared folder; no single application assigned."
    return dict(category=kind, app=app, confidence=confidence, evidence=evidence)


class Store:
    def __init__(self, data_dir: Path):
        self.data = data_dir.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.demo = self.data / "demo"
        self.vault = self.data / "quarantine"
        self.vault.mkdir(exist_ok=True)
        self.db = self.data / "history.sqlite3"
        self.lock = threading.RLock()
        self.current = None
        self.events = []
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS moves (id TEXT PRIMARY KEY, relative TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL)")
        self.seed()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def seed(self):
        # Never regenerate moved files on a restart.
        if self.demo.exists():
            return
        self.demo.mkdir()
        fixtures = [
            ("AppData/Local/OrbitBrowser/Cache/video-cache.bin", 10_485_760, 95),
            ("AppData/Local/OrbitBrowser/Cache/page-cache.bin", 4_194_304, 61),
            ("AppData/Local/OrbitBrowser/Cache/current-session.bin", 1_048_576, 0),
            ("AppData/Local/OrbitBrowser/settings.json", 1_024, 120),
            ("AppData/Local/OrbitBrowser/history.sqlite", 2_097_152, 2),
            ("AppData/Roaming/Chatter/Cache/thumbnails.bin", 6_291_456, 48),
            ("AppData/Roaming/Chatter/Logs/previous-session.log", 524_288, 73),
            ("AppData/Roaming/Chatter/messages.db", 3_145_728, 0),
            ("AppData/Local/CodePad/Temp/old-build.tmp", 2_097_152, 42),
            ("AppData/Local/CodePad/preferences.json", 2_048, 7),
            ("Documents/assignment-notes.txt", 8_192, 150),
            ("Shared/Cache/shared-data.bin", 1_048_576, 90),
        ]
        for rel, size, days in fixtures:
            p = self.demo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("wb") as f:
                f.write(b"0" * size)
            stamp = time.time() - days * 86400
            os.utime(p, (stamp, stamp))

    def scan(self, root: Path, mode: str) -> dict:
        with self.lock:
            root = root.expanduser()
            if not root.is_absolute():
                raise ValueError("Enter an absolute folder path.")
            if not root.is_dir() or any(linked(p) for p in [root, *root.parents]):
                raise ValueError("Choose an existing folder without symbolic links or junctions in its path.")
            root = root.resolve()
            previous = self.current
            rows, skipped, truncated = [], 0, False
            stack, seen, started = [root], set(), time.monotonic()
            while stack:
                if len(rows) >= LIMIT or time.monotonic() - started > MAX_SECONDS:
                    truncated = True
                    break
                folder = stack.pop()
                try:
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            if len(rows) >= LIMIT or time.monotonic() - started > MAX_SECONDS:
                                truncated = True
                                break
                            try:
                                path = Path(entry.path)
                                # Never descend into our manifests/vault on a real scan.
                                if mode == "real" and (path == self.data or self.data in path.parents):
                                    continue
                                if linked(path):
                                    skipped += 1
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(path)
                                    continue
                                s = entry.stat(follow_symlinks=False)
                                if not stat.S_ISREG(s.st_mode):
                                    skipped += 1
                                    continue
                                identity = (s.st_dev, s.st_ino)
                                if s.st_ino and identity in seen:
                                    skipped += 1
                                    continue
                                if s.st_ino:
                                    seen.add(identity)
                                rel = path.relative_to(root)
                                info = classify(rel)
                                days = max(0, int((time.time() - s.st_mtime) / 86400))
                                eligible = info["category"] in {"Cache", "Logs", "Temporary"} and days >= 30 and info["confidence"] == "Inferred"
                                row = dict(id=hashlib.sha256(str(rel).encode()).hexdigest()[:24], path=str(rel), size=s.st_size,
                                           modified=s.st_mtime, days=days, candidate=eligible,
                                           actionable=eligible and mode == "demo", **info)
                                row["explanation"] = self.explain(row, mode)
                                rows.append(row)
                            except OSError:
                                skipped += 1
                except OSError:
                    skipped += 1
            rows.sort(key=lambda x: x["size"], reverse=True)
            result = dict(mode=mode, root=str(root), files=rows, skipped=skipped, truncated=truncated,
                          total=sum(r["size"] for r in rows), candidates=sum(r["size"] for r in rows if r["candidate"]),
                          scannedAt=time.time(), elapsed=round(time.monotonic() - started, 2))
            if previous and previous["root"] == str(root) and not truncated and not previous["truncated"] and not skipped and not previous["skipped"]:
                old = {r["path"]: r for r in previous["files"]}
                new = {r["path"]: r for r in rows}
                for rel in new.keys() - old.keys():
                    self.event("Created", rel)
                for rel in old.keys() - new.keys():
                    self.event("Removed", rel)
                for rel in new.keys() & old.keys():
                    if (new[rel]["size"], new[rel]["modified"]) != (old[rel]["size"], old[rel]["modified"]):
                        self.event("Changed", rel)
            elif not previous or previous["root"] != str(root):
                self.events = []
            self.current = result
            return self.state()

    @staticmethod
    def explain(row, mode):
        age = f"Last modified {row['days']} days ago. Modification time does not tell us when it was last read."
        if row["candidate"]:
            impact = {"Cache": "The app may need to download or rebuild this cache.", "Logs": "Moving this log removes diagnostic history from the app.", "Temporary": "Temporary files may still be needed by unfinished work."}[row["category"]]
            return f"{row['category']} inferred from the path or extension. {age} {impact} " + ("Fictional demo file; you can quarantine and restore it." if mode == "demo" else "Review only; this prototype cannot move real files.")
        if row["category"] in {"Settings", "Database", "Personal files"}:
            return f"Keep this file. It may contain preferences, application records, or personal work. {age}"
        return f"Keep for manual review. Recent changes, uncertain ownership, or an unknown file type prevent a cleanup suggestion. {age}"

    def event(self, kind, path):
        self.events.insert(0, dict(kind=kind, path=path, time=time.time()))
        self.events = self.events[:60]

    def state(self):
        with self.lock, self.connect() as db:
            moved = [dict(id=r[0], path=r[1], size=r[2], time=r[3]) for r in db.execute("SELECT id,relative,size,created FROM moves WHERE status='stored' ORDER BY created DESC")]
            return dict(scan=self.current, quarantine=moved, events=self.events)

    def safe_demo_path(self, relative):
        p = self.demo / relative
        if not p.is_relative_to(self.demo) or ".." in Path(relative).parts:
            raise ValueError("Invalid demo path.")
        for ancestor in [p, *p.parents]:
            if ancestor.exists() and linked(ancestor):
                raise ValueError("Links and junctions cannot be moved.")
        if not p.resolve().is_relative_to(self.demo.resolve()):
            raise ValueError("Path leaves the demo folder.")
        return p

    @staticmethod
    def digest(p):
        with p.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    def quarantine(self, file_id, confirmed):
        with self.lock:
            if confirmed is not True or not self.current or self.current["mode"] != "demo":
                raise ValueError("Only confirmed demo files can be quarantined.")
            row = next((r for r in self.current["files"] if r["id"] == file_id), None)
            if not row or not row["actionable"]:
                raise ValueError("This file is protected or is no longer available.")
            src = self.safe_demo_path(row["path"])
            s = src.stat()
            if s.st_size != row["size"] or s.st_mtime != row["modified"] or s.st_nlink != 1:
                raise ValueError("File changed since scanning or has multiple links. Scan again.")
            key, digest = secrets.token_hex(16), self.digest(src)
            # Persist intent before rename; recover pending records on restart.
            with self.connect() as db:
                db.execute("INSERT INTO moves VALUES (?,?,?,?,?,?)", (key, row["path"], row["size"], digest, time.time(), "pending"))
            src.rename(self.vault / key)
            with self.connect() as db:
                db.execute("UPDATE moves SET status='stored' WHERE id=?", (key,))
            return self.scan(self.demo, "demo")

    def recover(self):
        with self.connect() as db:
            for key, status in db.execute("SELECT id,status FROM moves WHERE status IN ('pending','restoring')").fetchall():
                if status == "pending":
                    new_status = "stored" if (self.vault / key).is_file() else "cancelled"
                else:
                    new_status = "stored" if (self.vault / key).is_file() else "restored"
                db.execute("UPDATE moves SET status=? WHERE id=?", (new_status, key))

    def restore(self, key, confirmed):
        with self.lock, self.connect() as db:
            if confirmed is not True:
                raise ValueError("Confirm the restore first.")
            record = db.execute("SELECT relative,digest FROM moves WHERE id=? AND status='stored'", (key,)).fetchone()
            if not record:
                raise ValueError("Quarantined file not found.")
            dest = self.safe_demo_path(record[0])
            src = self.vault / key
            if dest.exists():
                raise ValueError("A file already exists at the original path. Nothing was overwritten.")
            if linked(src) or self.digest(src) != record[1]:
                raise ValueError("Quarantined file failed the integrity check.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            db.execute("UPDATE moves SET status='restoring' WHERE id=?", (key,))
            db.commit()
            # Windows rename refuses to replace an existing destination.
            if os.name == "nt":
                src.rename(dest)
            else:
                os.link(src, dest)
                src.unlink()
            db.execute("UPDATE moves SET status='restored' WHERE id=?", (key,))
            db.commit()
            return self.scan(self.demo, "demo")


def make_handler(store, token, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not write personal paths to terminal logs.

        def send(self, code, payload, content_type="application/json"):
            data = json.dumps(payload).encode() if content_type == "application/json" else payload
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "Local access only."})
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if self.path in assets:
                name, mime = assets[self.path]
                data = (WEB / name).read_bytes().replace(b"__SESSION_TOKEN__", token.encode())
                return self.send(200, data, mime)
            if self.path == "/api/state" and self.headers.get("X-DiskTrace-Token") == token:
                return self.send(200, store.state())
            self.send(404, {"error": "Not found."})

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            if not self.valid_host() or self.headers.get("X-DiskTrace-Token") != token or (origin and origin not in allowed):
                return self.send(403, {"error": "Invalid local session."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object.")
                if self.path == "/api/scan":
                    mode = data.get("mode")
                    if mode not in {"demo", "real"}:
                        raise ValueError("Select demo or real mode.")
                    if mode == "real" and data.get("consent") is not True:
                        raise ValueError("Consent is required before scanning a folder.")
                    root = store.demo if mode == "demo" else Path(data.get("root", ""))
                    return self.send(200, store.scan(root, mode))
                if self.path == "/api/quarantine":
                    return self.send(200, store.quarantine(data.get("id"), data.get("confirmed")))
                if self.path == "/api/restore":
                    return self.send(200, store.restore(data.get("id"), data.get("confirmed")))
                return self.send(404, {"error": "Not found."})
            except (ValueError, TypeError, OSError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                self.send(500, {"error": "Operation failed. Your files have not been automatically deleted. Restart to recover pending moves."})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=BASE / ".disktrace")
    args = parser.parse_args()
    store = Store(args.data_dir)
    store.recover()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, secrets.token_urlsafe(32), args.port))
    print(f"DiskTrace is running at http://127.0.0.1:{args.port}\nPress Ctrl+C to stop. No administrator permission needed.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
