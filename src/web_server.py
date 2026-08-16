#!/usr/bin/python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import secrets
import contextlib
import datetime as dt
import json
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core import (
    APP_NAME,
    APP_VERSION,
    Aria2Client,
    Aria2Error,
    Database,
    Settings,
    category_for_filename,
    format_bytes,
    format_eta,
    format_speed,
    looks_like_placeholder_filename,
    resolve_remote_file,
    safe_filename,
)

DEFAULT_PORT = 8600
MAX_BODY_BYTES = 64 * 1024
WEB_PASSWORD_ITERATIONS = 390_000
WEB_SESSION_TTL_SECONDS = 30 * 60
WEB_LOGIN_WINDOW_SECONDS = 60
WEB_LOGIN_MAX_FAILURES = 5


def hash_web_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8:
        raise ValueError("Web UI password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        WEB_PASSWORD_ITERATIONS,
    )
    return (
        "pbkdf2_sha256$"
        f"{WEB_PASSWORD_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )


def verify_web_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds_text, salt_text, digest_text = str(encoded or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        if rounds < 100_000 or rounds > 2_000_000:
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_text.encode("ascii"), validate=True)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            salt,
            rounds,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _web_auth_fingerprint(username: str, password_hash: str) -> str:
    return hashlib.sha256(
        (str(username) + "\0" + str(password_hash)).encode("utf-8")
    ).hexdigest()

CATEGORY_TABS = ["All Downloads", "Unfinished", "Finished"]
STATUS_ALIASES = {
    "waiting": "queued",
    "scheduled": "scheduled",
    "active": "downloading",
    "paused": "paused",
    "complete": "complete",
    "error": "error",
    "removed": "removed",
}
STATIC_CANDIDATES = (
    Path(__file__).resolve().parent / "web",
    Path(__file__).resolve().parent.parent / "web",
    Path("/usr/share/udownload/web"),
)

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _find_static_dir() -> Path:
    for candidate in STATIC_CANDIDATES:
        if (
            (candidate / "index.html").is_file()
            and (candidate / "app.js").is_file()
            and (candidate / "style.css").is_file()
        ):
            return candidate
    raise FileNotFoundError(
        "UDM Web UI files were not found. Looked in: "
        + ", ".join(str(path) for path in STATIC_CANDIDATES)
    )


def _local_ip_hint() -> str:
    with contextlib.suppress(Exception):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return str(probe.getsockname()[0])
    return "127.0.0.1"


def _parse_schedule(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError("Schedule time must look like 2026-08-15 22:30") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    parsed = parsed.replace(second=0, microsecond=0)
    if parsed <= dt.datetime.now():
        raise ValueError("Scheduled time must be in the future")
    return parsed.isoformat(timespec="seconds")


def _resolve_filename(url: str, timeout: float = 20.0) -> tuple[str, bool]:
    fallback = safe_filename(url)
    deadline = time.monotonic() + max(1.0, float(timeout))
    last_error = ""

    while time.monotonic() < deadline:
        remaining = max(1.0, deadline - time.monotonic())
        info = resolve_remote_file(
            url,
            head_timeout=min(4.0, remaining),
            get_timeout=min(7.0, remaining),
        )
        last_error = str(info.error or "").strip()
        candidate = str(info.filename or "").strip()
        final_url = str(info.final_url or url)

        if candidate and not looks_like_placeholder_filename(candidate, final_url):
            return candidate, bool(info.filename_confident)
        if fallback and not looks_like_placeholder_filename(fallback, url):
            return fallback, True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    detail = f" Last resolver error: {last_error}" if last_error else ""
    raise ValueError(f"Could not determine the remote filename in time.{detail}")


class WebDownloadService:
    """Web-only service layer. The GTK process remains the scheduler owner."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.db = Database()
        self.settings = Settings(self.db)
        self.aria = Aria2Client()

    def close(self) -> None:
        with self.lock:
            with contextlib.suppress(Exception):
                self.db.conn.close()

    def web_credentials(self) -> tuple[str, str]:
        # Read Web authentication settings under the same lock used for all
        # other Web UI access to the service-owned SQLite connection.
        with self.lock:
            return (
                str(self.settings.get("web_username", "") or "").strip(),
                str(self.settings.get("web_password_hash", "") or "").strip(),
            )

    def list_downloads(self, category: str, search: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.rows(category=category, search=search)
            return [self._serialize(row) for row in rows]

    def categories(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.rows(category="All Downloads")
            counts: dict[str, int] = {}
            for row in rows:
                name = str(row["category"] or "General")
                counts[name] = counts.get(name, 0) + 1
            return [
                {"name": name, "count": count}
                for name, count in sorted(counts.items())
            ]

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        total = int(row["total_length"] or 0)
        completed = int(row["completed_length"] or 0)
        speed = int(row["download_speed"] or 0)
        status = str(row["status"] or "")
        progress = int((completed / total) * 100) if total > 0 else (
            100 if status == "complete" else 0
        )
        return {
            "id": int(row["id"]),
            "gid": row["gid"] or "",
            "url": row["url"],
            "file_name": row["file_name"],
            "save_dir": row["save_dir"],
            "category": row["category"],
            "status": status,
            "status_label": STATUS_ALIASES.get(status, status or "unknown"),
            "total_length": total,
            "completed_length": completed,
            "download_speed": speed,
            "progress_percent": max(0, min(100, progress)),
            "size_human": format_bytes(total) if total else "",
            "completed_human": format_bytes(completed),
            "speed_human": format_speed(speed),
            "eta_human": format_eta(total, completed, speed),
            "added_at": row["added_at"],
            "start_time": row["start_time"],
            "completed_at": row["completed_at"],
            "error_message": row["error_message"] or "",
        }

    def add_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("A URL is required")
        if urlsplit(url).scheme.lower() not in {"http", "https", "ftp", "ftps"}:
            raise ValueError("Only http(s)/ftp(s) URLs are supported")

        mode = str(payload.get("start") or "queue").strip().lower()
        if mode not in {"queue", "now", "schedule"}:
            raise ValueError("Unknown start mode")

        path = str(payload.get("path") or "").strip()
        at = payload.get("at")
        if mode == "schedule" and not at:
            raise ValueError("Choose a date and time first")

        with self.lock:
            directory = Path(
                path or str(self.settings.get("download_dir"))
            ).expanduser()
            directory.mkdir(parents=True, exist_ok=True)

            filename, filename_locked = _resolve_filename(url)
            start_time = _parse_schedule(at) if mode == "schedule" else None

            download_id = self.db.add_download(
                url=url,
                file_name=filename,
                save_dir=str(directory),
                category=category_for_filename(filename),
                start_time=start_time,
                file_name_locked=filename_locked,
            )

            if mode == "now":
                if not self.aria.ensure_running():
                    self.db.conn.execute(
                        "UPDATE downloads SET status='error', error_message=? WHERE id=?",
                        ("aria2 engine unavailable", download_id),
                    )
                    self.db.conn.commit()
                    raise RuntimeError("Could not start the aria2 engine")
                row = self.db.get(download_id)
                try:
                    gid = self.aria.add_uri(row, self.settings)
                    self.db.set_gid(download_id, gid)
                except Exception as exc:
                    self.db.conn.execute(
                        "UPDATE downloads SET status='error', error_message=? WHERE id=?",
                        (str(exc), download_id),
                    )
                    self.db.conn.commit()
                    raise RuntimeError(f"Could not start download: {exc}") from exc

            row = self.db.get(download_id)
            if row is None:
                raise RuntimeError("Could not read the newly added download")
            return self._serialize(row)

    def _row(self, download_id: int) -> Any:
        row = self.db.get(download_id)
        if row is None:
            raise LookupError("Download not found")
        return row

    def _gid_for(self, download_id: int) -> str:
        row = self._row(download_id)
        gid = str(row["gid"] or "")
        if not gid:
            raise ValueError("Download has not started yet")
        return gid

    def pause(self, download_id: int) -> None:
        with self.lock:
            self.aria.pause(self._gid_for(download_id))

    def resume(self, download_id: int) -> None:
        with self.lock:
            gid = self._gid_for(download_id)
            if not self.aria.ensure_running():
                raise RuntimeError("Could not reach the aria2 engine")
            self.aria.resume(gid)

    def start(self, download_id: int) -> None:
        with self.lock:
            row = self._row(download_id)
            if row["gid"]:
                raise ValueError("Download has already started")
            if str(row["status"] or "") == "complete":
                raise ValueError("Download is already complete")
            if not self.aria.ensure_running():
                raise RuntimeError("Could not start the aria2 engine")
            try:
                gid = self.aria.add_uri(row, self.settings)
                self.db.set_gid(download_id, gid)
            except Exception as exc:
                self.db.conn.execute(
                    "UPDATE downloads SET status='error', error_message=? WHERE id=?",
                    (str(exc), download_id),
                )
                self.db.conn.commit()
                raise RuntimeError(f"Could not start download: {exc}") from exc

    def remove(self, download_id: int) -> None:
        with self.lock:
            row = self._row(download_id)
            gid = str(row["gid"] or "")
            if gid:
                with contextlib.suppress(Aria2Error):
                    self.aria.remove(gid)
            self.db.remove(download_id)

    def clear_completed(self) -> None:
        with self.lock:
            self.db.clear_completed()

    def pause_all(self) -> None:
        with self.lock:
            with contextlib.suppress(Aria2Error):
                self.aria.pause_all()

    def resume_all(self) -> None:
        with self.lock:
            if not self.aria.ensure_running():
                raise RuntimeError("Could not reach the aria2 engine")

            with contextlib.suppress(Aria2Error):
                self.aria.resume_all()

            # Match the desktop "Start Queue" behavior: DB-only queued items
            # have no aria2 GID yet, so hand them to aria2 as well.
            for row in self.db.rows("All Downloads"):
                if str(row["status"] or "") != "waiting" or row["gid"]:
                    continue
                download_id = int(row["id"])
                try:
                    gid = self.aria.add_uri(row, self.settings)
                    self.db.set_gid(download_id, gid)
                except Exception as exc:
                    self.db.conn.execute(
                        "UPDATE downloads SET status='error', error_message=? WHERE id=?",
                        (str(exc), download_id),
                    )
                    self.db.conn.commit()

    def engine_status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "app": APP_NAME,
                "version": APP_VERSION,
                "aria2_running": self.aria.ping(),
            }


def make_handler(
    service: WebDownloadService,
    static_dir: Path,
) -> type[BaseHTTPRequestHandler]:
    sessions: dict[str, dict[str, Any]] = {}
    sessions_lock = threading.RLock()
    failed_logins: dict[str, list[float]] = {}
    failed_lock = threading.RLock()

    def current_credentials() -> tuple[str, str]:
        # ThreadingHTTPServer handles requests concurrently. Do not touch the
        # service-owned SQLite connection outside WebDownloadService.lock.
        return service.web_credentials()

    def current_fingerprint() -> str:
        username, password_hash = current_credentials()
        return _web_auth_fingerprint(username, password_hash)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"UDMWeb/{APP_VERSION}"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def _common_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'none'; "
                "form-action 'self'",
            )

        def _send_json(
            self,
            payload: dict[str, Any],
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for name, value in extra_headers:
                    self.send_header(name, value)
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self._send_json({"ok": False, "error": "Not found"}, status=404)
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                MIME_TYPES.get(path.suffix, "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Cache-Control",
                "no-store" if path.suffix == ".html" else "public, max-age=300",
            )
            self._common_headers()
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self._common_headers()
            self.end_headers()

        def _same_origin_allowed(self) -> bool:
            if self.headers.get("X-UDM-Web", "") != "1":
                return False
            fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return False
            origin = self.headers.get("Origin", "").strip()
            host = self.headers.get("Host", "").strip()
            if origin and host:
                try:
                    if urlsplit(origin).netloc.casefold() != host.casefold():
                        return False
                except Exception:
                    return False
            return True

        def _cookie_session_token(self) -> str:
            raw_cookie = self.headers.get("Cookie", "")
            if not raw_cookie:
                return ""
            try:
                cookie = SimpleCookie()
                cookie.load(raw_cookie)
                morsel = cookie.get("udm_session")
                return str(morsel.value if morsel is not None else "")
            except Exception:
                return ""

        def _authenticated(self, *, touch: bool = True) -> bool:
            token = self._cookie_session_token()
            if not token:
                return False
            now = time.monotonic()
            fingerprint = current_fingerprint()
            with sessions_lock:
                session = sessions.get(token)
                if not session:
                    return False
                if (
                    str(session.get("fingerprint", "")) != fingerprint
                    or now - float(session.get("last_seen", 0.0)) > WEB_SESSION_TTL_SECONDS
                ):
                    sessions.pop(token, None)
                    return False
                if touch:
                    session["last_seen"] = now
                return True

        def _api_allowed(self) -> tuple[bool, int]:
            if not self._same_origin_allowed():
                return False, 403
            if not self._authenticated():
                return False, 401
            return True, 200

        def _client_key(self) -> str:
            try:
                return str(self.client_address[0])
            except Exception:
                return "unknown"

        def _login_rate_limited(self) -> bool:
            now = time.monotonic()
            cutoff = now - WEB_LOGIN_WINDOW_SECONDS
            key = self._client_key()
            with failed_lock:
                values = [v for v in failed_logins.get(key, []) if v >= cutoff]
                failed_logins[key] = values
                return len(values) >= WEB_LOGIN_MAX_FAILURES

        def _record_login_failure(self) -> None:
            now = time.monotonic()
            cutoff = now - WEB_LOGIN_WINDOW_SECONDS
            key = self._client_key()
            with failed_lock:
                values = [v for v in failed_logins.get(key, []) if v >= cutoff]
                values.append(now)
                failed_logins[key] = values

        def _clear_login_failures(self) -> None:
            with failed_lock:
                failed_logins.pop(self._client_key(), None)

        def _body_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ValueError("Invalid request length") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError("Request body is too large")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body must be valid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("Request body must be a JSON object")
            return value

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            route = parts.path

            if route in {"/", "/index.html"}:
                self._send_file(static_dir / "login.html")
                return

            if route in {"/login.js", "/login.css", "/app.js", "/style.css"}:
                self._send_file(static_dir / route.lstrip("/"))
                return

            if route in {"/app", "/app/"}:
                if not self._authenticated():
                    self._redirect("/")
                    return
                self._send_file(static_dir / "index.html")
                return

            if route.startswith("/api/"):
                allowed, status = self._api_allowed()
                if not allowed:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "Authentication required" if status == 401 else "Forbidden",
                        },
                        status=status,
                    )
                    return

                if route == "/api/downloads":
                    category = (query.get("category") or ["All Downloads"])[0]
                    search = (query.get("search") or [""])[0]
                    self._send_json(
                        {
                            "ok": True,
                            "downloads": service.list_downloads(category, search),
                            "categories": service.categories(),
                            "tabs": CATEGORY_TABS,
                        }
                    )
                    return

                if route == "/api/status":
                    username, _password_hash = current_credentials()
                    self._send_json(
                        {
                            "ok": True,
                            **service.engine_status(),
                            "web_user": username,
                        }
                    )
                    return

            self._send_json({"ok": False, "error": "Not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            route = parts.path

            if route == "/auth/login":
                if not self._same_origin_allowed():
                    self._send_json({"ok": False, "error": "Forbidden"}, status=403)
                    return
                if self._login_rate_limited():
                    self._send_json(
                        {"ok": False, "error": "Too many failed sign-in attempts. Try again shortly."},
                        status=429,
                    )
                    return
                try:
                    payload = self._body_json()
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                    return

                supplied_user = str(payload.get("username") or "").strip()
                supplied_password = str(payload.get("password") or "")
                username, password_hash = current_credentials()

                if not username or not password_hash:
                    self._send_json(
                        {"ok": False, "error": "Web UI credentials are not configured in UDM Options."},
                        status=503,
                    )
                    return

                user_ok = hmac.compare_digest(
                    supplied_user.encode("utf-8"),
                    username.encode("utf-8"),
                )
                password_ok = verify_web_password(supplied_password, password_hash)
                if not (user_ok and password_ok):
                    self._record_login_failure()
                    self._send_json(
                        {"ok": False, "error": "Invalid username or password"},
                        status=401,
                    )
                    return

                self._clear_login_failures()
                token = secrets.token_urlsafe(32)
                with sessions_lock:
                    sessions[token] = {
                        "fingerprint": current_fingerprint(),
                        "last_seen": time.monotonic(),
                    }

                self._send_json(
                    {"ok": True, "redirect": "/app"},
                    extra_headers=[
                        (
                            "Set-Cookie",
                            "udm_session=" + token + "; Path=/; HttpOnly; SameSite=Strict",
                        )
                    ],
                )
                return

            if route == "/auth/logout":
                if not self._same_origin_allowed():
                    self._send_json({"ok": False, "error": "Forbidden"}, status=403)
                    return
                token = self._cookie_session_token()
                if token:
                    with sessions_lock:
                        sessions.pop(token, None)
                self._send_json(
                    {"ok": True},
                    extra_headers=[
                        (
                            "Set-Cookie",
                            "udm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                        )
                    ],
                )
                return

            if not route.startswith("/api/"):
                self._send_json({"ok": False, "error": "Not found"}, status=404)
                return

            allowed, status = self._api_allowed()
            if not allowed:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Authentication required" if status == 401 else "Forbidden",
                    },
                    status=status,
                )
                return

            try:
                if route == "/api/downloads":
                    result = service.add_download(self._body_json())
                    self._send_json({"ok": True, "download": result})
                    return
                if route == "/api/clear-completed":
                    service.clear_completed()
                    self._send_json({"ok": True})
                    return
                if route == "/api/pause-all":
                    service.pause_all()
                    self._send_json({"ok": True})
                    return
                if route == "/api/resume-all":
                    service.resume_all()
                    self._send_json({"ok": True})
                    return

                segments = [segment for segment in route.split("/") if segment]
                if len(segments) == 4 and segments[:2] == ["api", "downloads"]:
                    try:
                        download_id = int(segments[2])
                    except ValueError as exc:
                        raise ValueError("Invalid download id") from exc
                    action = segments[3]
                    if action == "pause":
                        service.pause(download_id)
                    elif action == "resume":
                        service.resume(download_id)
                    elif action == "start":
                        service.start(download_id)
                    elif action == "remove":
                        service.remove(download_id)
                    else:
                        self._send_json(
                            {"ok": False, "error": "Unknown action"},
                            status=400,
                        )
                        return
                    self._send_json({"ok": True})
                    return

                self._send_json({"ok": False, "error": "Not found"}, status=404)
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=404)
            except (ValueError, RuntimeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {"ok": False, "error": f"Unexpected error: {exc}"},
                    status=500,
                )

    return Handler


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class WebUIServer:
    def __init__(self, port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> None:
        self.host = str(host)
        self.port = int(port)
        self.httpd: _ReusableThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.service: WebDownloadService | None = None
        self.url = ""

    @property
    def running(self) -> bool:
        return bool(self.httpd is not None and self.thread is not None and self.thread.is_alive())

    def start(self) -> str:
        if self.running:
            return self.url
        if not 1024 <= self.port <= 65535:
            raise ValueError("Web UI port must be between 1024 and 65535")

        static_dir = _find_static_dir()
        service = WebDownloadService()
        handler_cls = make_handler(service, static_dir)

        try:
            httpd = _ReusableThreadingHTTPServer((self.host, self.port), handler_cls)
        except Exception:
            service.close()
            raise

        self.service = service
        self.httpd = httpd
        self.url = f"http://{_local_ip_hint()}:{self.port}/"
        self.thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.25},
            name=f"udm-web-{self.port}",
            daemon=True,
        )
        self.thread.start()
        return self.url

    def stop(self) -> None:
        httpd = self.httpd
        thread = self.thread
        service = self.service
        self.httpd = None
        self.thread = None
        self.service = None
        self.url = ""

        if httpd is not None:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        if thread is not None and thread.is_alive():
            with contextlib.suppress(Exception):
                thread.join(timeout=2.0)
        if service is not None:
            service.close()


def _self_test() -> int:
    static_dir = _find_static_dir()
    required = [
        "index.html", "app.js", "style.css",
        "login.html", "login.js", "login.css",
    ]
    missing = [name for name in required if not (static_dir / name).is_file()]
    if missing:
        raise RuntimeError("Missing Web UI files: " + ", ".join(missing))
    encoded = hash_web_password("TestPassword123!")
    assert verify_web_password("TestPassword123!", encoded)
    assert not verify_web_password("WrongPassword", encoded)
    print("UDM Web UI self-test: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UDM Web UI helper")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    parser.error("This helper is started by the UDM application. Use --self-test for diagnostics.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
