#!/usr/bin/python3
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
from email.message import Message
from email.utils import collapse_rfc2231_value
import json
import mimetypes
import os
import re
import shlex
import socket
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
APP_VERSION = "1.0.18"
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


def probe_ssh_endpoint(
    host: str,
    port: int,
    timeout: float = 2.0,
) -> tuple[bool, str]:
    host = str(host or "").strip()

    if not host:
        return False, "Server address is required"

    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "Port must be a number"

    if not 1 <= port <= 65535:
        return False, "Port must be between 1 and 65535"

    try:
        with socket.create_connection(
            (host, port),
            timeout=timeout,
        ) as sock:
            sock.settimeout(timeout)
            banner = sock.recv(192).decode(
                "utf-8",
                errors="replace",
            ).strip()
    except Exception as exc:
        return False, f"Cannot connect to {host}:{port}: {exc}"

    if banner.startswith("SSH-"):
        return True, banner

    if banner:
        return (
            False,
            f"Port {port} is open, but it is not SSH ({banner[:80]})",
        )

    return (
        False,
        f"Port {port} accepted a connection but sent no SSH banner",
    )

def safe_filename(url: str, fallback: str = "download") -> str:
    parsed = urllib.parse.urlsplit(url)
    name = Path(urllib.parse.unquote(parsed.path)).name.strip()
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", name)
    return name or fallback


def sanitize_filename(name: str) -> str:
    value = str(name or "").strip()
    value = Path(value.replace("\\", "/")).name.strip()
    value = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", value)
    return value.strip(" .")


def filename_from_content_disposition(value: str) -> str:
    if not value:
        return ""

    # Prefer RFC 5987 / RFC 6266 filename*= over the legacy filename= value.
    match = re.search(r"(?:^|;)\s*filename\*\s*=\s*([^;]+)", value, flags=re.IGNORECASE)
    if match:
        raw = match.group(1).strip().strip('"')
        try:
            if "''" in raw:
                charset, encoded = raw.split("''", 1)
                decoded = urllib.parse.unquote(
                    encoded,
                    encoding=(charset or "utf-8"),
                    errors="replace",
                )
            else:
                decoded = urllib.parse.unquote(raw)
            cleaned = sanitize_filename(decoded)
            if cleaned:
                return cleaned
        except Exception:
            pass

    try:
        message = Message()
        message["content-disposition"] = value
        filename = message.get_param("filename", header="content-disposition")
        if isinstance(filename, tuple):
            filename = collapse_rfc2231_value(filename)
        cleaned = sanitize_filename(str(filename or ""))
        if cleaned:
            return cleaned
    except Exception:
        pass
    return ""


def looks_like_placeholder_filename(filename: str, url: str = "") -> bool:
    cleaned = sanitize_filename(filename)
    if not cleaned:
        return True
    if Path(cleaned).suffix:
        return False
    generic = {
        "download", "file", "get", "attachment", "index", "fetch",
        "downloadfile", "download_file",
    }
    if cleaned.casefold() in generic:
        return True
    if url and cleaned == safe_filename(url):
        return True
    # Automatically inferred extensionless names are not reliable enough to
    # force as aria2's output filename.
    return True


@dataclass(slots=True)
class RemoteFileInfo:
    final_url: str
    filename: str = ""
    total_length: int = 0
    content_type: str = ""
    filename_source: str = ""
    error: str = ""

    @property
    def filename_confident(self) -> bool:
        if not self.filename:
            return False
        if self.filename_source == "content-disposition":
            return True
        return self.filename_source == "url" and not looks_like_placeholder_filename(
            self.filename, self.final_url
        )


def _remote_total_length(headers: Any) -> int:
    content_range = str(headers.get("Content-Range", "") or "")
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _remote_info_from_response(response: Any) -> RemoteFileInfo:
    final_url = str(response.geturl() or "")
    headers = response.headers
    disposition_name = filename_from_content_disposition(
        str(headers.get("Content-Disposition", "") or "")
    )
    if disposition_name:
        filename = disposition_name
        source = "content-disposition"
    else:
        filename = sanitize_filename(safe_filename(final_url, fallback=""))
        source = "url" if filename else ""

    return RemoteFileInfo(
        final_url=final_url,
        filename=filename,
        total_length=_remote_total_length(headers),
        content_type=str(headers.get("Content-Type", "") or "").split(";", 1)[0].strip(),
        filename_source=source,
    )


def _merge_remote_info(primary: RemoteFileInfo, fallback: RemoteFileInfo) -> RemoteFileInfo:
    filename = primary.filename or fallback.filename
    filename_source = primary.filename_source or fallback.filename_source

    if fallback.filename_source == "content-disposition" and primary.filename_source != "content-disposition":
        filename = fallback.filename
        filename_source = fallback.filename_source

    return RemoteFileInfo(
        final_url=primary.final_url or fallback.final_url,
        filename=filename,
        total_length=primary.total_length or fallback.total_length,
        content_type=primary.content_type or fallback.content_type,
        filename_source=filename_source,
        error=primary.error or fallback.error,
    )


def resolve_remote_file(
    url: str,
    headers: dict[str, str] | None = None,
    source_page: str = "",
    head_timeout: float = 3.0,
    get_timeout: float = 5.0,
) -> RemoteFileInfo:
    # Resolve redirects and metadata without downloading the whole file.
    # HEAD is tried first. If metadata is incomplete, a GET request with
    # Range: bytes=0-0 is opened and immediately closed after response headers.
    url = str(url or "").strip()
    fallback = RemoteFileInfo(
        final_url=url,
        filename=safe_filename(url),
        filename_source="url",
    )
    if not url:
        fallback.error = "Empty URL"
        return fallback

    request_headers = {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if key and value is not None and str(value) != ""
    }
    if source_page and not any(key.casefold() == "referer" for key in request_headers):
        request_headers["Referer"] = source_page
    if not any(key.casefold() == "accept-encoding" for key in request_headers):
        request_headers["Accept-Encoding"] = "identity"

    head_info: RemoteFileInfo | None = None
    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme in {"http", "https"}:
        try:
            request = urllib.request.Request(url, headers=request_headers, method="HEAD")
            with urllib.request.urlopen(request, timeout=head_timeout) as response:
                head_info = _remote_info_from_response(response)
        except Exception:
            head_info = None

    if (
        head_info
        and head_info.filename_source == "content-disposition"
        and head_info.total_length > 0
    ):
        return _merge_remote_info(head_info, fallback)

    get_headers = dict(request_headers)
    if parsed.scheme in {"http", "https"}:
        get_headers["Range"] = "bytes=0-0"

    try:
        request = urllib.request.Request(url, headers=get_headers, method="GET")
        with urllib.request.urlopen(request, timeout=get_timeout) as response:
            get_info = _remote_info_from_response(response)
        if head_info:
            return _merge_remote_info(get_info, _merge_remote_info(head_info, fallback))
        return _merge_remote_info(get_info, fallback)
    except Exception as exc:
        result = _merge_remote_info(head_info, fallback) if head_info else fallback
        result.error = str(exc)
        return result


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
        "show_download_complete_dialog": True,
        "confirm_delete": True,
        "browser_prompt": True,
        "remote_server": "",
        "remote_port": 8347,
        "remote_user": "",
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
                file_name_locked INTEGER NOT NULL DEFAULT 0,
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
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(downloads)")
        }
        if "file_name_locked" not in columns:
            self.conn.execute(
                "ALTER TABLE downloads "
                "ADD COLUMN file_name_locked INTEGER NOT NULL DEFAULT 0"
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
        file_name_locked: bool = False,
    ) -> int:
        status = "scheduled" if start_time else "waiting"
        cur = self.conn.execute(
            """
            INSERT INTO downloads(
                url,file_name,file_name_locked,save_dir,category,description,
                queue_name,status,added_at,start_time,headers_json,source_page
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                url,
                file_name,
                1 if file_name_locked else 0,
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

        actual_filename: str | None = None
        actual_category: str | None = None
        row = self.get_by_gid(gid)
        if row is not None and not int(row["file_name_locked"] or 0):
            files = state.get("files") or []
            if files and isinstance(files, list):
                first = files[0] or {}
                path = first.get("path", "") if isinstance(first, dict) else ""
                candidate = sanitize_filename(Path(str(path)).name) if path else ""
                if candidate:
                    actual_filename = candidate
                    actual_category = category_for_filename(candidate)

        self.conn.execute(
            """
            UPDATE downloads SET
                status=?,
                total_length=?,
                completed_length=?,
                download_speed=?,
                error_message=?,
                completed_at=COALESCE(completed_at,?),
                file_name=COALESCE(?,file_name),
                category=COALESCE(?,category)
            WHERE gid=?
            """,
            (
                status,
                int(state.get("totalLength", 0) or 0),
                int(state.get("completedLength", 0) or 0),
                int(state.get("downloadSpeed", 0) or 0),
                error,
                completed_at,
                actual_filename,
                actual_category,
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
        options: dict[str, Any] = {
            "dir": row["save_dir"],
            "continue": "true",
            "split": str(settings.get("connections", 16)),
            "max-connection-per-server": str(settings.get("connections", 16)),
            "min-split-size": "1M",
            "auto-file-renaming": "true",
            "allow-overwrite": "false",
            "summary-interval": "1",
        }
        # Only force aria2's output name when the user explicitly chose it or
        # the preflight resolver obtained a trustworthy remote filename.
        if int(row["file_name_locked"] or 0):
            options["out"] = row["file_name"]
            options["auto-file-renaming"] = "false"
            options["always-resume"] = "true"
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

    def tell_status(self, gid: str) -> dict[str, Any]:
        keys = [
            "gid", "status", "totalLength", "completedLength",
            "downloadSpeed", "errorMessage", "files",
        ]
        value = self.call("tellStatus", [gid, keys]) or {}
        return dict(value) if isinstance(value, dict) else {}

    def pause(self, gid: str) -> None:
        self.call("forcePause", [gid])

    def resume(self, gid: str) -> None:
        self.call("unpause", [gid])

    def hard_stop(self, gid: str) -> list[str]:
        primary = self.tell_status(gid)
        target_paths: set[str] = set()

        for file_info in primary.get("files", []) or []:
            if not isinstance(file_info, dict):
                continue
            raw_path = str(file_info.get("path", "") or "").strip()
            if raw_path:
                target_paths.add(
                    str(Path(raw_path).expanduser().resolve(strict=False))
                )

        candidates: set[str] = {gid}
        if target_paths:
            for state in self.tell_all():
                status = str(state.get("status", "") or "")
                if status not in {"active", "waiting", "paused"}:
                    continue

                state_gid = str(state.get("gid", "") or "")
                if not state_gid:
                    continue

                state_paths: set[str] = set()
                for file_info in state.get("files", []) or []:
                    if not isinstance(file_info, dict):
                        continue
                    raw_path = str(file_info.get("path", "") or "").strip()
                    if raw_path:
                        state_paths.add(
                            str(Path(raw_path).expanduser().resolve(strict=False))
                        )

                if target_paths.intersection(state_paths):
                    candidates.add(state_gid)

        stopped: list[str] = []
        errors: list[str] = []

        # Best effort forcePause first so aria2 can persist its control-file
        # state, then forceRemove to terminate all live sockets immediately.
        for candidate in sorted(candidates):
            try:
                with contextlib.suppress(Exception):
                    self.call("forcePause", [candidate])
                self.call("forceRemove", [candidate])
                stopped.append(candidate)
            except Exception as exc:
                try:
                    state = self.tell_status(candidate)
                    if str(state.get("status", "") or "") == "removed":
                        stopped.append(candidate)
                        continue
                except Exception:
                    pass
                errors.append(f"{candidate}: {exc}")

        if errors and not stopped:
            raise Aria2Error("; ".join(errors))

        return stopped

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

    # Chrome/Chromium must execute an actual executable path. Using the .py
    # file directly depends on its mode bits surviving packaging. Keep a tiny
    # per-user executable wrapper and point every native-messaging manifest to
    # it. This also works while running directly from the source tree.
    source_host = Path(__file__).resolve().with_name("native_host.py")
    installed_host = Path("/usr/lib/udownload/native_host.py")
    host_script = installed_host if installed_host.exists() else source_host

    wrapper = DATA_DIR / "native-host"
    wrapper.write_text(
        "#!/bin/sh\n"
        "exec /usr/bin/python3 "
        + shlex.quote(str(host_script))
        + "\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    executable = str(wrapper)

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
