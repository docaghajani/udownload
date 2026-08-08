#!/usr/bin/python3
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

APP_ID = "com.ideveloper.UDownload"
APP_NAME = "Ubuntu Download Manager"
CONFIG_DIR = Path.home() / ".config" / "udownload"
DATA_DIR = Path.home() / ".local" / "share" / "udownload"
LEGACY_CONFIG_DIR = Path.home() / ".config" / "udm"
LEGACY_DATA_DIR = Path.home() / ".local" / "share" / "udm"
DB_PATH = DATA_DIR / "downloads.db"
SECRET_PATH = CONFIG_DIR / "rpc-secret"
ARIA2_PORT = 16801
ARIA2_ENDPOINT = f"http://127.0.0.1:{ARIA2_PORT}/jsonrpc"

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "Compressed": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso"},
    "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "csv", "odt", "ods", "epub"},
    "Music": {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma"},
    "Programs": {"deb", "rpm", "appimage", "exe", "msi", "apk", "dmg", "pkg", "bin", "run"},
    "Video": {"mp4", "mkv", "avi", "mov", "webm", "wmv", "m4v", "ts", "mpeg", "mpg"},
    "Images": {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tif", "tiff", "heic"},
}


def ensure_dirs() -> None:
    # Preserve settings and download history from pre-udownload builds.
    if not CONFIG_DIR.exists() and LEGACY_CONFIG_DIR.exists():
        import shutil
        shutil.copytree(LEGACY_CONFIG_DIR, CONFIG_DIR, dirs_exist_ok=True)
    if not DATA_DIR.exists() and LEGACY_DATA_DIR.exists():
        import shutil
        shutil.copytree(LEGACY_DATA_DIR, DATA_DIR, dirs_exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def format_bytes(value: int | float | str | None) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        num = 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if num < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(num)} {unit}"
            if num >= 100:
                return f"{num:.0f} {unit}"
            if num >= 10:
                return f"{num:.1f} {unit}"
            return f"{num:.2f} {unit}"
        num /= 1024
    return "0 B"


def format_speed(value: int | float | str | None) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        num = 0
    return "" if num <= 0 else f"{format_bytes(num)}/s"


def format_eta(total: int, completed: int, speed: int) -> str:
    if speed <= 0 or total <= completed:
        return ""
    seconds = max(0, (total - completed) // speed)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def safe_filename(url: str, fallback: str = "download") -> str:
    parsed = urllib.parse.urlsplit(url)
    name = Path(urllib.parse.unquote(parsed.path)).name.strip()
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", name)
    return name or fallback


def category_for_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    for category, extensions in CATEGORY_EXTENSIONS.items():
        if ext in extensions:
            return category
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        if guessed.startswith("video/"):
            return "Video"
        if guessed.startswith("audio/"):
            return "Music"
        if guessed.startswith("image/"):
            return "Images"
        if guessed.startswith("text/"):
            return "Documents"
    return "General"


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for idx in range(1, 10000):
        alternative = directory / f"{stem} ({idx}){suffix}"
        if not alternative.exists():
            return alternative
    digest = hashlib.sha1(f"{filename}-{time.time_ns()}".encode()).hexdigest()[:8]
    return directory / f"{stem}-{digest}{suffix}"


class Settings:
    DEFAULTS: dict[str, Any] = {
        "download_dir": str(Path.home() / "Downloads"),
        "max_concurrent": 5,
        "connections": 16,
        "speed_limit": "0",
        "show_completed_notification": True,
        "confirm_delete": True,
        "browser_prompt": True,
        "auto_start_aria2": True,
        "window_width": 1180,
        "window_height": 720,
        "sidebar_position": 220,
        "current_category": "All Downloads",
        "sort_column": "date_added",
        "sort_order": "descending",
        "column_widths": {},
    }

    def __init__(self, db: "Database") -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        fallback = self.DEFAULTS.get(key, default)
        row = self.db.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return fallback
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def set(self, key: str, value: Any) -> None:
        self.db.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.db.conn.commit()


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        ensure_dirs()
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gid TEXT UNIQUE,
                url TEXT NOT NULL,
                file_name TEXT NOT NULL,
                save_dir TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'General',
                description TEXT NOT NULL DEFAULT '',
                queue_name TEXT NOT NULL DEFAULT 'Main download queue',
                status TEXT NOT NULL DEFAULT 'scheduled',
                total_length INTEGER NOT NULL DEFAULT 0,
                completed_length INTEGER NOT NULL DEFAULT 0,
                download_speed INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL,
                start_time TEXT,
                completed_at TEXT,
                headers_json TEXT NOT NULL DEFAULT '{}',
                source_page TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
            CREATE INDEX IF NOT EXISTS idx_downloads_category ON downloads(category);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queues (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO queues(name,enabled,sort_order) VALUES('Main download queue',1,0);
            """
        )
        self.conn.commit()

    def add_download(
        self,
        url: str,
        file_name: str,
        save_dir: str,
        category: str,
        description: str = "",
        queue_name: str = "Main download queue",
        start_time: str | None = None,
        headers: dict[str, str] | None = None,
        source_page: str = "",
    ) -> int:
        status = "scheduled" if start_time else "waiting"
        cur = self.conn.execute(
            """
            INSERT INTO downloads(url,file_name,save_dir,category,description,queue_name,status,added_at,start_time,headers_json,source_page)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                url,
                file_name,
                save_dir,
                category,
                description,
                queue_name,
                status,
                dt.datetime.now().isoformat(timespec="seconds"),
                start_time,
                json.dumps(headers or {}, ensure_ascii=False),
                source_page,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_gid(self, download_id: int, gid: str) -> None:
        self.conn.execute("UPDATE downloads SET gid=?,status='waiting' WHERE id=?", (gid, download_id))
        self.conn.commit()

    def update_state(self, gid: str, state: dict[str, Any]) -> None:
        status = state.get("status", "unknown")
        error = state.get("errorMessage", "")
        completed_at = dt.datetime.now().isoformat(timespec="seconds") if status == "complete" else None
        self.conn.execute(
            """
            UPDATE downloads SET status=?,total_length=?,completed_length=?,download_speed=?,error_message=?,
                completed_at=COALESCE(completed_at,?) WHERE gid=?
            """,
            (
                status,
                int(state.get("totalLength", 0) or 0),
                int(state.get("completedLength", 0) or 0),
                int(state.get("downloadSpeed", 0) or 0),
                error,
                completed_at,
                gid,
            ),
        )
        self.conn.commit()

    def rows(self, category: str = "All Downloads", search: str = "") -> list[sqlite3.Row]:
        conditions: list[str] = []
        args: list[Any] = []
        if category == "Finished":
            conditions.append("status='complete'")
        elif category == "Unfinished":
            conditions.append("status!='complete'")
        elif category not in {"All Downloads", "Queues"}:
            conditions.append("category=?")
            args.append(category)
        if search:
            conditions.append("(file_name LIKE ? OR url LIKE ? OR description LIKE ?)")
            pattern = f"%{search}%"
            args.extend([pattern, pattern, pattern])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return list(self.conn.execute(f"SELECT * FROM downloads{where} ORDER BY id DESC", args))

    def scheduled_due(self) -> list[sqlite3.Row]:
        now = dt.datetime.now().isoformat(timespec="seconds")
        return list(
            self.conn.execute(
                "SELECT * FROM downloads WHERE status='scheduled' AND start_time IS NOT NULL AND start_time<=?",
                (now,),
            )
        )

    def get(self, download_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()

    def get_by_gid(self, gid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM downloads WHERE gid=?", (gid,)).fetchone()

    def remove(self, download_id: int) -> None:
        self.conn.execute("DELETE FROM downloads WHERE id=?", (download_id,))
        self.conn.commit()

    def clear_completed(self) -> None:
        self.conn.execute("DELETE FROM downloads WHERE status='complete'")
        self.conn.commit()


class Aria2Error(RuntimeError):
    pass


class Aria2Client:
    def __init__(self) -> None:
        ensure_dirs()
        self.secret = self._read_secret()
        self.request_id = 0

    def _read_secret(self) -> str:
        if SECRET_PATH.exists():
            return SECRET_PATH.read_text().strip()
        return ""

    def reload_secret(self) -> None:
        self.secret = self._read_secret()

    def ensure_running(self) -> bool:
        if self.ping():
            return True
        with contextlib.suppress(Exception):
            subprocess.run(["systemctl", "--user", "daemon-reload"], timeout=5, check=False)
            subprocess.run(["systemctl", "--user", "start", "udownload-aria2.service"], timeout=8, check=False)
        for _ in range(20):
            time.sleep(0.2)
            self.reload_secret()
            if self.ping():
                return True
        return False

    def call(self, method: str, params: list[Any] | None = None, timeout: float = 2.5) -> Any:
        self.request_id += 1
        actual_params: list[Any] = []
        if self.secret:
            actual_params.append(f"token:{self.secret}")
        actual_params.extend(params or [])
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": str(self.request_id), "method": f"aria2.{method}", "params": actual_params}
        ).encode()
        req = urllib.request.Request(ARIA2_ENDPOINT, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = json.loads(response.read().decode())
        except Exception as exc:
            raise Aria2Error(str(exc)) from exc
        if "error" in body:
            raise Aria2Error(body["error"].get("message", "aria2 error"))
        return body.get("result")

    def ping(self) -> bool:
        try:
            self.call("getVersion", timeout=0.5)
            return True
        except Exception:
            return False

    def add_uri(self, row: sqlite3.Row, settings: Settings) -> str:
        headers = json.loads(row["headers_json"] or "{}")
        options: dict[str, str] = {
            "dir": row["save_dir"],
            "out": row["file_name"],
            "continue": "true",
            "split": str(settings.get("connections", 16)),
            "max-connection-per-server": str(settings.get("connections", 16)),
            "min-split-size": "1M",
            "auto-file-renaming": "true",
            "allow-overwrite": "false",
            "summary-interval": "1",
        }
        if row["source_page"]:
            options["referer"] = row["source_page"]
        if headers:
            options["header"] = [f"{key}: {value}" for key, value in headers.items() if value]
        return str(self.call("addUri", [[row["url"]], options]))

    def tell_all(self) -> list[dict[str, Any]]:
        keys = [
            "gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorMessage", "files"
        ]
        result: list[dict[str, Any]] = []
        result.extend(self.call("tellActive", [keys]) or [])
        result.extend(self.call("tellWaiting", [0, 1000, keys]) or [])
        result.extend(self.call("tellStopped", [0, 1000, keys]) or [])
        return result

    def pause(self, gid: str) -> None:
        self.call("forcePause", [gid])

    def resume(self, gid: str) -> None:
        self.call("unpause", [gid])

    def remove(self, gid: str) -> None:
        try:
            self.call("forceRemove", [gid])
        except Aria2Error:
            with contextlib.suppress(Exception):
                self.call("removeDownloadResult", [gid])

    def pause_all(self) -> None:
        self.call("forcePauseAll")

    def resume_all(self) -> None:
        self.call("unpauseAll")

    def set_global_options(self, settings: Settings) -> None:
        options = {
            "max-concurrent-downloads": str(settings.get("max_concurrent", 5)),
            "max-overall-download-limit": str(settings.get("speed_limit", "0")),
        }
        self.call("changeGlobalOption", [options])


def install_native_manifests() -> list[Path]:
    chrome_id = "fnindjfclfmejhmeilmnmmgfbegecnkf"
    executable = "/usr/lib/udownload/native_host.py"
    manifests: list[tuple[Path, dict[str, Any]]] = []
    chrome_manifest = {
        "name": "com.ideveloper.udownload.native",
        "description": "Ubuntu Download Manager browser bridge",
        "path": executable,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{chrome_id}/"],
    }
    firefox_manifest = {
        "name": "com.ideveloper.udownload.native",
        "description": "Ubuntu Download Manager browser bridge",
        "path": executable,
        "type": "stdio",
        "allowed_extensions": ["udownload@ideveloper.local"],
    }
    chrome_dirs = [
        Path.home() / ".config/google-chrome/NativeMessagingHosts",
        Path.home() / ".config/chromium/NativeMessagingHosts",
        Path.home() / ".config/BraveSoftware/Brave-Browser/NativeMessagingHosts",
        Path.home() / ".config/microsoft-edge/NativeMessagingHosts",
        Path.home() / ".config/vivaldi/NativeMessagingHosts",
    ]
    for directory in chrome_dirs:
        manifests.append((directory / "com.ideveloper.udownload.native.json", chrome_manifest))
    manifests.append((Path.home() / ".mozilla/native-messaging-hosts/com.ideveloper.udownload.native.json", firefox_manifest))
    written: list[Path] = []
    for path, data in manifests:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        written.append(path)
    return written


def launch_browser_extensions_page(browser: str) -> None:
    targets = {
        "chrome": "chrome://extensions/",
        "chromium": "chrome://extensions/",
        "firefox": "about:debugging#/runtime/this-firefox",
    }
    url = targets.get(browser, targets["chrome"])
    candidates = {
        "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "chromium": ["chromium", "chromium-browser", "google-chrome"],
        "firefox": ["firefox"],
    }[browser]
    for command in candidates:
        if subprocess.run(["sh", "-lc", f"command -v {command}"], capture_output=True).returncode == 0:
            subprocess.Popen([command, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
