#!/usr/bin/python3
from __future__ import annotations
import re

import argparse
import base64
import contextlib
import datetime as dt
import getpass
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from core import (
    APP_ID,
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
    install_native_manifests,
    launch_browser_extensions_page,
    looks_like_placeholder_filename,
    probe_ssh_endpoint,
    resolve_remote_file,
    safe_filename,
    unique_path,
)


from web_server import WebUIServer


STATUS_LABELS = {
    "active": "Downloading",
    "waiting": "Queued",
    "paused": "Paused",
    "complete": "Complete",
    "error": "Error",
    "removed": "Removed",
    "scheduled": "Scheduled",
}


def choose_folder(parent: Gtk.Window, entry: Gtk.Entry) -> None:
    # Use GTK's in-process chooser instead of FileChooserNative.  On some
    # GNOME/Ubuntu setups the native chooser is routed through the desktop
    # portal/Nautilus and SELECT_FOLDER can fail with "Operation was cancelled".
    # Gtk.FileChooserDialog avoids that portal round-trip and is much more
    # reliable for choosing a local download directory.
    chooser = Gtk.FileChooserDialog(
        title="Choose download folder",
        transient_for=parent,
        modal=True,
        action=Gtk.FileChooserAction.SELECT_FOLDER,
    )
    chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
    chooser.add_button("Select", Gtk.ResponseType.ACCEPT)
    chooser.set_default_response(Gtk.ResponseType.ACCEPT)

    current = Path(entry.get_text().strip() or str(Path.home() / "Downloads")).expanduser()
    if current.exists():
        with contextlib.suppress(Exception):
            chooser.set_current_folder(Gio.File.new_for_path(str(current)))

    def on_response(dialog, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                path = file.get_path()
                if path:
                    entry.set_text(path)
        dialog.destroy()

    chooser.connect("response", on_response)
    chooser.present()


class DownloadObject(GObject.Object):
    def __init__(self, row: Any):
        super().__init__()
        self.db_id = int(row["id"])
        self.gid = row["gid"] or ""
        self.file_name = row["file_name"]
        self.url = row["url"]
        self.save_dir = row["save_dir"]
        self.category = row["category"]
        self.description = row["description"]
        self.queue_name = row["queue_name"]
        self.status = row["status"]
        self.total = int(row["total_length"] or 0)
        self.completed = int(row["completed_length"] or 0)
        self.speed = int(row["download_speed"] or 0)
        self.error_message = row["error_message"] or ""
        self.start_time = row["start_time"] or ""
        self.added_at = row["added_at"] or ""

    @property
    def size_text(self) -> str:
        return format_bytes(self.total) if self.total else ""

    @property
    def status_text(self) -> str:
        label = STATUS_LABELS.get(self.status, self.status.title())
        if self.status == "active":
            return label
        if self.status == "error" and self.error_message:
            return f"Error: {self.error_message}"
        if self.status == "scheduled" and self.start_time:
            return f"Scheduled: {self.start_time.replace('T', ' ')}"
        return label

    @property
    def added_at_text(self) -> str:
        if not self.added_at:
            return ""
        with contextlib.suppress(ValueError, TypeError):
            value = dt.datetime.fromisoformat(str(self.added_at))
            return value.strftime("%Y-%m-%d %H:%M")
        return str(self.added_at).replace("T", " ")

    @property
    def progress_text(self) -> str:
        if self.status == "complete":
            return "100.0%"
        if self.total <= 0:
            return ""
        percent = min(100.0, max(0.0, (self.completed / self.total) * 100.0))
        return f"{percent:.1f}%"

    @property
    def eta_text(self) -> str:
        if self.status == "complete":
            return "0s"
        if self.status != "active":
            return ""
        if self.total <= 0 or self.completed >= self.total or self.speed <= 0:
            return "Calculating…"

        eta = format_eta(self.total, self.completed, self.speed)
        if eta:
            return eta

        remaining = max(0, self.total - self.completed)
        seconds = max(1, (remaining + self.speed - 1) // self.speed)
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {sec}s"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h {minutes}m"
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"

    @property
    def speed_text(self) -> str:
        return format_speed(self.speed)


class AddDownloadDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, initial: dict[str, Any], on_accept):
        super().__init__(title="Download File Info", transient_for=parent, modal=True)
        self.set_default_size(760, 410)
        self.parent_window = parent
        self.on_accept = on_accept
        self.cancel_button = self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.later_button = self.add_button("Download Later", Gtk.ResponseType.APPLY)
        self.start_button = self.add_button("Start Download", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.initial = dict(initial)
        self.filename_user_edited = False
        self._updating_filename = False
        self._resolved_filename_confident = bool(
            initial.get("filename")
            and not looks_like_placeholder_filename(
                str(initial.get("filename") or ""),
                str(initial.get("url") or ""),
            )
        )
        self._resolve_token = 0
        self._resolve_source_id = 0
        self._filename_base = ""

        area = self.get_content_area()
        area.set_spacing(10)
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        area.append(body)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10, hexpand=True)
        body.append(grid)

        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview.set_size_request(125, -1)
        preview.set_halign(Gtk.Align.CENTER)
        preview.set_valign(Gtk.Align.CENTER)
        self.file_info_icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        self.file_info_icon.set_pixel_size(64)
        self.file_info_size = Gtk.Label(label="—", xalign=0.5)
        self.file_info_name = Gtk.Label(label="", xalign=0.5, wrap=True)
        self.file_info_name.set_max_width_chars(18)
        preview.append(self.file_info_icon)
        preview.append(self.file_info_size)
        preview.append(self.file_info_name)
        body.append(preview)

        self.url_entry = Gtk.Entry(hexpand=True)
        self.url_entry.set_text(initial.get("url", ""))
        self.file_entry = Gtk.Entry(hexpand=True)
        filename = initial.get("filename") or safe_filename(initial.get("url", ""))
        self.file_entry.set_text(filename)
        self._filename_base = filename
        self.file_entry.connect("changed", self._on_filename_changed)
        self.file_entry.connect("notify::has-focus", self._on_filename_focus_changed)
        self.url_entry.connect("changed", self._on_url_changed)
        self.dir_entry = Gtk.Entry(hexpand=True)
        self.dir_entry.set_text(initial.get("save_dir", str(Path.home() / "Downloads")))
        self.dir_entry.connect("changed", self._on_directory_changed)
        self.dir_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, hexpand=True)
        self.dir_box.append(self.dir_entry)
        self.dir_browse = Gtk.Button(label="Browse…")
        self.dir_browse.connect("clicked", lambda *_: choose_folder(self, self.dir_entry))
        self.dir_box.append(self.dir_browse)
        self.categories = [
            "General", "Compressed", "Documents", "Music",
            "Programs", "Video", "Images",
        ]
        self.category_combo = Gtk.DropDown.new_from_strings(self.categories)
        detected = initial.get("category") or category_for_filename(filename)
        with contextlib.suppress(ValueError):
            self.category_combo.set_selected(self.categories.index(detected))

        self.remember_category_path = Gtk.CheckButton()
        self.remember_category_path.set_active(True)
        self.category_combo.connect("notify::selected", self._on_category_changed)

        self.description_entry = Gtk.Entry(hexpand=True)
        self.description_entry.set_text(initial.get("description", ""))

        # Real date/time picker for scheduled downloads.  A Gtk.Calendar is
        # used for the date and spin buttons for hour/minute, so the user no
        # longer has to type a YYYY-MM-DD HH:MM string by hand.
        self.schedule_time: dt.datetime | None = None
        self.schedule_button = Gtk.MenuButton(label="Choose date & time…", hexpand=True)
        self.schedule_button.set_halign(Gtk.Align.FILL)
        self.schedule_popover = Gtk.Popover()
        picker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        picker_box.set_margin_top(12)
        picker_box.set_margin_bottom(12)
        picker_box.set_margin_start(12)
        picker_box.set_margin_end(12)

        self.schedule_calendar = Gtk.Calendar()
        picker_box.append(self.schedule_calendar)

        time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        time_row.append(Gtk.Label(label="Time", xalign=0, hexpand=True))
        self.schedule_hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.schedule_hour.set_width_chars(2)
        self.schedule_hour.set_numeric(True)
        self.schedule_minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        self.schedule_minute.set_width_chars(2)
        self.schedule_minute.set_numeric(True)
        time_row.append(self.schedule_hour)
        time_row.append(Gtk.Label(label=":"))
        time_row.append(self.schedule_minute)
        picker_box.append(time_row)

        picker_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        picker_actions.set_halign(Gtk.Align.END)
        clear_schedule = Gtk.Button(label="Clear")
        set_schedule = Gtk.Button(label="Set")
        set_schedule.add_css_class("suggested-action")
        clear_schedule.connect("clicked", self._clear_schedule)
        set_schedule.connect("clicked", self._set_schedule)
        picker_actions.append(clear_schedule)
        picker_actions.append(set_schedule)
        picker_box.append(picker_actions)
        self.schedule_popover.set_child(picker_box)
        self.schedule_button.set_popover(self.schedule_popover)
        self.schedule_button.connect("notify::active", self._schedule_popover_opened)

        initial_start = initial.get("start_time")
        if initial_start:
            with contextlib.suppress(ValueError, TypeError):
                parsed = dt.datetime.fromisoformat(str(initial_start))
                self.schedule_time = parsed.replace(second=0, microsecond=0)
                self._sync_schedule_controls(self.schedule_time)
                self._update_schedule_label()

        labels = ["URL", "Category", "Save as", "Folder", "Description", "Schedule"]
        widgets = [
            self.url_entry,
            self.category_combo,
            self.file_entry,
            self.dir_box,
            self.description_entry,
            self.schedule_button,
        ]
        for idx, (label, widget) in enumerate(zip(labels, widgets)):
            lab = Gtk.Label(label=label, xalign=1)
            grid.attach(lab, 0, idx, 1, 1)
            grid.attach(widget, 1, idx, 1, 1)

        self._update_remember_path_label()
        grid.attach(self.remember_category_path, 1, len(labels), 1, 1)

        note = Gtk.Label(
            label="Authenticated downloads can receive the current page referrer and cookies from the browser extension.",
            wrap=True,
            xalign=0,
        )
        note.add_css_class("dim-label")
        grid.attach(note, 1, len(labels) + 1, 1, 1)

        self.resolve_label = Gtk.Label(
            label="",
            wrap=True,
            xalign=0,
        )
        self.resolve_label.add_css_class("dim-label")
        grid.attach(self.resolve_label, 1, len(labels) + 2, 1, 1)

        self.connect("response", self._on_response)
        self._update_file_info_panel(filename, 0)
        self._load_remembered_category_path()
        self._refresh_unique_filename()
        if self.url_entry.get_text().strip():
            self._schedule_resolve(delay_ms=50)
        else:
            self._prefill_url_from_clipboard()

    def _category_name(self) -> str:
        index = int(self.category_combo.get_selected())
        if 0 <= index < len(self.categories):
            return self.categories[index]
        return "General"

    def _category_dir_key(self, category: str | None = None) -> str:
        return f"download.category_dir::{category or self._category_name()}"

    def _update_remember_path_label(self) -> None:
        self.remember_category_path.set_label(
            f"Remember this path for {self._category_name()} category"
        )

    def _load_remembered_category_path(self) -> None:
        remembered = str(
            self.parent_window.settings.get(self._category_dir_key(), "") or ""
        ).strip()
        if remembered:
            self.dir_entry.set_text(remembered)

    def _on_category_changed(self, *_args) -> None:
        self._update_remember_path_label()
        if self.remember_category_path.get_active():
            self._load_remembered_category_path()

    def _update_file_info_panel(self, filename: str, total_length: int) -> None:
        category = category_for_filename(filename or "")
        icon_names = {
            "Compressed": "package-x-generic-symbolic",
            "Documents": "x-office-document-symbolic",
            "Music": "audio-x-generic-symbolic",
            "Programs": "application-x-executable-symbolic",
            "Video": "video-x-generic-symbolic",
            "Images": "image-x-generic-symbolic",
            "General": "text-x-generic-symbolic",
        }
        self.file_info_icon.set_from_icon_name(
            icon_names.get(category, "text-x-generic-symbolic")
        )
        self.file_info_size.set_text(
            format_bytes(total_length) if total_length > 0 else "Resolving…"
        )
        self.file_info_name.set_text(filename or "")

    def _prefill_url_from_clipboard(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.read_text_async(None, self._clipboard_text_ready)

    def _clipboard_text_ready(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except Exception:
            return
        if self.url_entry.get_text().strip():
            return

        candidate = str(text or "").strip()
        if not candidate or any(ch.isspace() for ch in candidate):
            return
        try:
            parsed = urllib.parse.urlsplit(candidate)
        except Exception:
            return
        if parsed.scheme.lower() not in {"http", "https", "ftp"} or not parsed.netloc:
            return

        self.url_entry.set_text(candidate)
        self._schedule_resolve(delay_ms=25)

    def _unique_filename_for(self, directory: str, filename: str) -> str:
        filename = str(filename or "").strip()
        if not filename:
            return ""
        target_dir = Path(directory or str(Path.home() / "Downloads")).expanduser()
        return unique_path(target_dir, filename).name

    def _refresh_unique_filename(self) -> None:
        base = str(self._filename_base or self.file_entry.get_text() or "").strip()
        if not base:
            return
        directory = self.dir_entry.get_text().strip()
        suggested = self._unique_filename_for(directory, base)
        if not suggested or suggested == self.file_entry.get_text():
            return
        self._updating_filename = True
        try:
            self.file_entry.set_text(suggested)
        finally:
            self._updating_filename = False

    def _on_directory_changed(self, _entry: Gtk.Entry) -> None:
        self._refresh_unique_filename()

    def _on_filename_focus_changed(self, entry: Gtk.Entry, _param) -> None:
        if not bool(entry.get_property("has-focus")):
            self._refresh_unique_filename()

    def _on_filename_changed(self, entry: Gtk.Entry) -> None:
        if not self._updating_filename:
            self._filename_base = entry.get_text().strip()
            self.filename_user_edited = True
            self._resolved_filename_confident = True

    def _on_url_changed(self, _entry: Gtk.Entry) -> None:
        self.filename_user_edited = False
        self._resolved_filename_confident = False
        self._schedule_resolve(delay_ms=500)

    def _schedule_resolve(self, delay_ms: int = 500) -> None:
        self._resolve_token += 1
        token = self._resolve_token
        if self._resolve_source_id:
            with contextlib.suppress(Exception):
                GLib.source_remove(self._resolve_source_id)
            self._resolve_source_id = 0

        url = self.url_entry.get_text().strip()
        if not url:
            self.resolve_label.set_text("")
            self.later_button.set_sensitive(True)
            self.start_button.set_sensitive(True)
            return

        self.resolve_label.set_text("Resolving file information…")
        self.later_button.set_sensitive(False)
        self.start_button.set_sensitive(False)

        def begin() -> bool:
            self._resolve_source_id = 0
            if token != self._resolve_token:
                return GLib.SOURCE_REMOVE
            current_url = self.url_entry.get_text().strip()
            headers = dict(self.initial.get("headers", {}) or {})
            source_page = str(self.initial.get("source_page", "") or "")

            def worker() -> None:
                info = resolve_remote_file(
                    current_url,
                    headers=headers,
                    source_page=source_page,
                )
                GLib.idle_add(self._apply_resolved_info, token, current_url, info)

            threading.Thread(target=worker, daemon=True).start()
            return GLib.SOURCE_REMOVE

        self._resolve_source_id = GLib.timeout_add(delay_ms, begin)

    def _apply_resolved_info(self, token: int, requested_url: str, info: Any) -> bool:
        if token != self._resolve_token:
            return GLib.SOURCE_REMOVE
        if requested_url != self.url_entry.get_text().strip():
            return GLib.SOURCE_REMOVE

        if info.filename and not self.filename_user_edited:
            self._filename_base = info.filename
            self._resolved_filename_confident = bool(info.filename_confident)
            self._refresh_unique_filename()
            detected = category_for_filename(info.filename)
            with contextlib.suppress(ValueError):
                self.category_combo.set_selected(self.categories.index(detected))

        self._update_file_info_panel(
            self.file_entry.get_text().strip() or info.filename,
            int(info.total_length or 0),
        )

        details: list[str] = []
        if info.total_length:
            details.append(format_bytes(info.total_length))
        if info.content_type:
            details.append(info.content_type)

        if info.filename_confident:
            prefix = "File information resolved"
        elif info.error:
            prefix = "Server metadata unavailable; aria2 will resolve the final filename"
        else:
            prefix = "Waiting for the final filename from aria2"

        self.resolve_label.set_text(
            prefix + (f" — {' · '.join(details)}" if details else "")
        )
        self.later_button.set_sensitive(True)
        self.start_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _default_schedule_time() -> dt.datetime:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        return now + dt.timedelta(hours=1)

    def _sync_schedule_controls(self, value: dt.datetime) -> None:
        with contextlib.suppress(Exception):
            calendar_date = GLib.DateTime.new_local(
                value.year, value.month, value.day, 0, 0, 0.0
            )
            self.schedule_calendar.select_day(calendar_date)
        self.schedule_hour.set_value(value.hour)
        self.schedule_minute.set_value(value.minute)

    def _schedule_popover_opened(self, button: Gtk.MenuButton, _param) -> None:
        if not button.get_active():
            return
        value = self.schedule_time or self._default_schedule_time()
        self._sync_schedule_controls(value)

    def _set_schedule(self, *_args) -> None:
        selected = self.schedule_calendar.get_date()
        self.schedule_time = dt.datetime(
            selected.get_year(),
            selected.get_month(),
            selected.get_day_of_month(),
            self.schedule_hour.get_value_as_int(),
            self.schedule_minute.get_value_as_int(),
        )
        self._update_schedule_label()
        self.schedule_popover.popdown()

    def _clear_schedule(self, *_args) -> None:
        self.schedule_time = None
        self._update_schedule_label()
        self.schedule_popover.popdown()

    def _update_schedule_label(self) -> None:
        if self.schedule_time is None:
            self.schedule_button.set_label("Choose date & time…")
        else:
            self.schedule_button.set_label(self.schedule_time.strftime("%Y-%m-%d   %H:%M"))

    def _on_response(self, _dialog, response: int) -> None:
        if response not in {Gtk.ResponseType.OK, Gtk.ResponseType.APPLY}:
            self.destroy()
            return
        url = self.url_entry.get_text().strip()
        filename = self.file_entry.get_text().strip()
        directory = self.dir_entry.get_text().strip()
        if not url or not filename or not directory:
            return
        filename = self._unique_filename_for(directory, filename)
        category = self.categories[self.category_combo.get_selected()]
        if self.remember_category_path.get_active():
            self.parent_window.settings.set(
                self._category_dir_key(category),
                directory,
            )

        start_time = None
        if response == Gtk.ResponseType.APPLY and self.schedule_time is not None:
            start_time = self.schedule_time.isoformat(timespec="seconds")

        payload = {
            "url": url,
            "filename": filename,
            "save_dir": directory,
            "category": category,
            "description": self.description_entry.get_text().strip(),
            "start_time": start_time,
            "headers": self.initial.get("headers", {}),
            "source_page": self.initial.get("source_page", ""),
            "file_name_locked": bool(
                self.filename_user_edited or self._resolved_filename_confident
            ),
            "start_now": response == Gtk.ResponseType.OK,
        }
        self.on_accept(payload)
        self.destroy()


class LinkSelectionDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, links: list[dict[str, str]], download_dir: str, on_accept):
        super().__init__(title="Select links to download", transient_for=parent, modal=True)
        self.set_default_size(900, 660)
        # Keep a generous batch size. 200+ links are intentionally supported.
        self.links = links[:2000]
        self.on_accept = on_accept
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Add to Queue", Gtk.ResponseType.APPLY)
        self.add_button("Start Selected", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        area = self.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(12)
        area.set_margin_bottom(12)
        area.set_margin_start(12)
        area.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        select_all = Gtk.Button(label="Select all")
        clear = Gtk.Button(label="Clear")
        self.filter_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Filter links")
        top.append(select_all)
        top.append(clear)
        top.append(self.filter_entry)
        area.append(top)

        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_box.append(Gtk.Label(label="Download folder", xalign=0))
        self.dir_entry = Gtk.Entry(hexpand=True, text=download_dir)
        folder_box.append(self.dir_entry)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", lambda *_: choose_folder(self, self.dir_entry))
        folder_box.append(browse)
        area.append(folder_box)

        self.count_label = Gtk.Label(xalign=0)
        self.count_label.add_css_class("dim-label")
        area.append(self.count_label)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll.set_child(self.list_box)
        area.append(scroll)
        self.checks: list[tuple[Gtk.CheckButton, dict[str, str], Gtk.ListBoxRow]] = []
        for link in self.links:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(6)
            box.set_margin_end(6)
            check = Gtk.CheckButton(active=True)
            check.connect("toggled", lambda *_: self._update_count())
            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            title = Gtk.Label(label=link.get("text") or safe_filename(link.get("url", "")), xalign=0, ellipsize=3)
            url = Gtk.Label(label=link.get("url", ""), xalign=0, ellipsize=3)
            url.add_css_class("dim-label")
            text_box.append(title)
            text_box.append(url)
            box.append(check)
            box.append(text_box)
            row.set_child(box)
            self.list_box.append(row)
            self.checks.append((check, link, row))
        select_all.connect("clicked", self._select_all)
        clear.connect("clicked", self._clear_all)
        self.filter_entry.connect("search-changed", self._filter)
        self.connect("response", self._response)
        self._update_count()

    def _select_all(self, *_args) -> None:
        for check, _, row in self.checks:
            if row.get_visible():
                check.set_active(True)
        self._update_count()

    def _clear_all(self, *_args) -> None:
        for check, _, row in self.checks:
            if row.get_visible():
                check.set_active(False)
        self._update_count()

    def _update_count(self) -> None:
        selected = sum(1 for check, _, row in self.checks if row.get_visible() and check.get_active())
        visible = sum(1 for _, _, row in self.checks if row.get_visible())
        self.count_label.set_text(f"{selected} selected of {visible} visible • {len(self.checks)} total links")

    def _filter(self, entry: Gtk.SearchEntry) -> None:
        needle = entry.get_text().lower().strip()
        for _check, link, row in self.checks:
            haystack = f"{link.get('text','')} {link.get('url','')}".lower()
            row.set_visible(not needle or needle in haystack)
        self._update_count()

    def _response(self, _dialog, response: int) -> None:
        if response in {Gtk.ResponseType.OK, Gtk.ResponseType.APPLY}:
            chosen = [link for check, link, row in self.checks if row.get_visible() and check.get_active()]
            directory = self.dir_entry.get_text().strip()
            if not chosen or not directory:
                return
            self.on_accept(chosen, directory, response == Gtk.ResponseType.OK)
        self.destroy()


class ConfirmDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, title: str, message: str, on_confirm):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.on_confirm = on_confirm
        self.set_default_size(460, 180)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        delete_button = self.add_button("Delete", Gtk.ResponseType.OK)
        with contextlib.suppress(Exception):
            delete_button.add_css_class("destructive-action")
        self.set_default_response(Gtk.ResponseType.CANCEL)

        area = self.get_content_area()
        area.set_spacing(12)
        area.set_margin_top(18)
        area.set_margin_bottom(18)
        area.set_margin_start(18)
        area.set_margin_end(18)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.START)
        row.append(icon)
        label = Gtk.Label(label=message, xalign=0, wrap=True, hexpand=True)
        row.append(label)
        area.append(row)
        self.connect("response", self._response)

    def _response(self, _dialog, response: int) -> None:
        if response == Gtk.ResponseType.OK:
            self.on_confirm()
        self.destroy()


class OptionsDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, settings: Settings, on_save):
        super().__init__(title="Options", transient_for=parent, modal=True)
        self.set_default_size(560, 500)
        self.settings = settings
        self.on_save = on_save
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        area = self.get_content_area()
        area.set_spacing(12)
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        area.append(grid)
        self.dir_entry = Gtk.Entry(hexpand=True, text=str(settings.get("download_dir")))
        self.dir_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, hexpand=True)
        self.dir_box.append(self.dir_entry)
        self.dir_browse = Gtk.Button(label="Browse…")
        self.dir_browse.connect("clicked", lambda *_: choose_folder(self, self.dir_entry))
        self.dir_box.append(self.dir_browse)
        self.concurrent = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.concurrent.set_value(int(settings.get("max_concurrent", 5)))
        self.concurrent.set_tooltip_text("Number of files that may download at the same time. Remaining files stay queued.")
        self.connections = Gtk.SpinButton.new_with_range(1, 16, 1)
        self.connections.set_value(int(settings.get("connections", 16)))
        self.speed = Gtk.Entry(hexpand=True, text=str(settings.get("speed_limit", "0")))
        self.prompt = Gtk.Switch(active=bool(settings.get("browser_prompt", True)))
        self.prompt.set_halign(Gtk.Align.START)
        self.prompt.set_valign(Gtk.Align.CENTER)
        self.prompt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.prompt_box.set_halign(Gtk.Align.START)
        self.prompt_box.append(self.prompt)

        self.complete_dialog = Gtk.Switch(
            active=bool(settings.get("show_download_complete_dialog", True))
        )
        self.complete_dialog.set_halign(Gtk.Align.START)
        self.complete_dialog.set_valign(Gtk.Align.CENTER)
        self.complete_dialog_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.complete_dialog_box.set_halign(Gtk.Align.START)
        self.complete_dialog_box.append(self.complete_dialog)

        self.web_enabled = Gtk.Switch(
            active=bool(settings.get("web_enabled", False))
        )
        self.web_enabled.set_halign(Gtk.Align.START)
        self.web_enabled.set_valign(Gtk.Align.CENTER)
        self.web_enabled_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.web_enabled_box.set_halign(Gtk.Align.START)
        self.web_enabled_box.append(self.web_enabled)

        self.web_port = Gtk.SpinButton.new_with_range(1024, 65535, 1)
        self.web_port.set_value(
            int(settings.get("web_port", 8600) or 8600)
        )
        self.web_port.set_tooltip_text(
            "The Web UI listens on all network interfaces. "
            "Use it only on a trusted LAN or VPN."
        )
        self.web_port.set_sensitive(self.web_enabled.get_active())

        def web_enabled_changed(*_args) -> None:
            self.web_port.set_sensitive(self.web_enabled.get_active())

        self.web_enabled.connect("notify::active", web_enabled_changed)

        rows = [
            ("Default download folder", self.dir_box),
            ("Simultaneous downloads", self.concurrent),
            ("Connections per download", self.connections),
            ("Global speed limit (e.g. 2M, 0=unlimited)", self.speed),
            ("Show file-info dialog for browser downloads", self.prompt_box),
            ("Show download-complete dialog", self.complete_dialog_box),
            ("Enable Web UI (trusted LAN / VPN)", self.web_enabled_box),
            ("Web UI port", self.web_port),
        ]
        for idx, (label, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, idx, 1, 1)
            grid.attach(widget, 1, idx, 1, 1)
        self.connect("response", self._response)

    def _response(self, _dialog, response: int) -> None:
        if response == Gtk.ResponseType.OK:
            values = {
                "download_dir": self.dir_entry.get_text().strip(),
                "max_concurrent": int(self.concurrent.get_value()),
                "connections": int(self.connections.get_value()),
                "speed_limit": self.speed.get_text().strip() or "0",
                "browser_prompt": self.prompt.get_active(),
                "show_download_complete_dialog": self.complete_dialog.get_active(),
                "web_enabled": self.web_enabled.get_active(),
                "web_port": int(self.web_port.get_value()),
            }
            self.on_save(values)
        self.destroy()


class RemoteDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, settings: Settings):
        super().__init__(title="Remote", transient_for=parent, modal=True)
        self.parent_window = parent
        self.settings = settings
        self.owner_user = getpass.getuser()
        self.set_default_size(650, 520)

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.save_button = self.add_button(
            "Create / Update User",
            Gtk.ResponseType.OK,
        )
        self.save_button.set_sensitive(False)

        area = self.get_content_area()
        area.set_spacing(13)
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)

        intro = Gtk.Label(
            label=(
                "Create a dedicated Linux account for UDM Remote. "
                "SSH authentication protects Remote access, and the account "
                "is restricted to UDM download commands."
            ),
            xalign=0,
            wrap=True,
        )
        intro.add_css_class("dim-label")
        area.append(intro)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        area.append(grid)

        self.port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_spin.set_value(
            int(settings.get("remote_port", 8347) or 8347)
        )

        self.user_entry = Gtk.Entry(
            hexpand=True,
            text=str(settings.get("remote_user", "usrudm") or "usrudm"),
        )
        self.user_entry.set_placeholder_text("usrudm")

        self.password_entry = Gtk.Entry(hexpand=True)
        self.password_entry.set_visibility(False)
        self.password_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password_entry.set_placeholder_text("At least 8 characters")

        self.confirm_entry = Gtk.Entry(hexpand=True)
        self.confirm_entry.set_visibility(False)
        self.confirm_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.confirm_entry.set_placeholder_text("Repeat password")

        rows = [
            ("External port", self.port_spin),
            ("Remote user", self.user_entry),
            ("Password", self.password_entry),
            ("Confirm password", self.confirm_entry),
        ]

        for index, (caption, widget) in enumerate(rows):
            grid.attach(
                Gtk.Label(label=caption, xalign=0),
                0, index, 1, 1,
            )
            grid.attach(widget, 1, index, 1, 1)

        owner = Gtk.Label(
            label=(
                f"Downloads received through this Remote account are added "
                f"to the UDM profile of: {self.owner_user}"
            ),
            xalign=0,
            wrap=True,
        )
        owner.add_css_class("dim-label")
        area.append(owner)

        password_note = Gtk.Label(
            label=(
                "The password is not saved in UDM settings. "
                "Linux stores the account password hash in its normal system "
                "password database."
            ),
            xalign=0,
            wrap=True,
        )
        password_note.add_css_class("dim-label")
        area.append(password_note)

        self.router_note = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.router_note.add_css_class("dim-label")
        area.append(self.router_note)

        self.examples = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.examples.add_css_class("dim-label")
        area.append(self.examples)

        self.status = Gtk.Label(
            label="Enter a username and matching password to create Remote access.",
            xalign=0,
            wrap=True,
        )
        area.append(self.status)

        self.user_entry.connect("changed", self._changed)
        self.password_entry.connect("changed", self._changed)
        self.confirm_entry.connect("changed", self._changed)
        self.port_spin.connect("value-changed", self._changed)
        self.connect("response", self._response)

        self._changed()

    def _changed(self, *_args) -> None:
        username = self.user_entry.get_text().strip()
        password = self.password_entry.get_text()
        confirm = self.confirm_entry.get_text()
        port = int(self.port_spin.get_value())

        valid_user = bool(
            re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username)
        )
        valid_password = (
            len(password) >= 8
            and password == confirm
        )

        self.save_button.set_sensitive(
            valid_user and valid_password
        )

        self.router_note.set_text(
            "Router / firewall:\n"
            f"Forward TCP {port} on your router to this computer's LAN IP, "
            "destination/internal port 22. Allow OpenSSH Server through the "
            "computer firewall."
        )

        shown_user = username or "usrudm"
        self.examples.set_text(
            "From a computer with UDM installed:\n"
            f'udownload remote "https://example.com/file.iso" '
            f'--server PUBLIC_IP --user {shown_user} --now\n\n'
            "From any computer with an SSH client:\n"
            f'ssh -p {port} {shown_user}@PUBLIC_IP '
            f'\'udownload add "https://example.com/file.iso" --now\''
        )

        if password and password != confirm:
            self.status.set_text("Passwords do not match.")
        elif password and len(password) < 8:
            self.status.set_text("Password must be at least 8 characters.")
        elif username and not valid_user:
            self.status.set_text(
                "Username: lowercase letters, numbers, _ or - only."
            )
        else:
            self.status.set_text(
                "The first setup may request administrator authorization."
            )

    def _response(self, _dialog, response: int) -> None:
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return

        if not self.save_button.get_sensitive():
            return

        self._configure_remote_user()

    def _configure_remote_user(self) -> None:
        helper = Path(__file__).with_name("remote_admin.py")
        if not helper.is_file():
            self.status.set_text(
                f"Remote administration helper not found: {helper}"
            )
            return

        username = self.user_entry.get_text().strip()
        password = self.password_entry.get_text()
        port = int(self.port_spin.get_value())

        payload = {
            "username": username,
            "password": password,
            "owner": self.owner_user,
            "external_port": port,
        }

        self.save_button.set_sensitive(False)
        self.status.set_text(
            "Configuring Remote access... "
            "Administrator authorization may be requested."
        )

        def worker() -> None:
            command = [
                "/usr/bin/pkexec",
                "/usr/bin/python3",
                str(helper),
            ]

            try:
                result = subprocess.run(
                    command,
                    input=json.dumps(payload),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                message = ""
                ok = False

                for line in reversed(result.stdout.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ok = bool(response.get("ok"))
                    message = str(response.get("message", "") or "")
                    break

                if not message:
                    if result.returncode == 126:
                        message = "Administrator authorization was cancelled."
                    else:
                        message = (
                            result.stderr.strip()
                            or result.stdout.strip()
                            or f"Remote setup failed with code {result.returncode}"
                        )

                GLib.idle_add(
                    self._configure_done,
                    ok and result.returncode == 0,
                    message,
                    username,
                    port,
                )
            except Exception as exc:
                GLib.idle_add(
                    self._configure_done,
                    False,
                    str(exc),
                    username,
                    port,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _configure_done(
        self,
        ok: bool,
        message: str,
        username: str,
        port: int,
    ) -> bool:
        if ok:
            self.settings.set("remote_user", username)
            self.settings.set("remote_port", port)
            self.password_entry.set_text("")
            self.confirm_entry.set_text("")
            self.status.set_text(message)
        else:
            self.status.set_text(message)
            self._changed()

        return GLib.SOURCE_REMOVE

class BrowserIntegrationDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Browser Integration", transient_for=parent, modal=True)
        self.parent_window = parent
        self.set_default_size(650, 420)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        area = self.get_content_area()
        area.set_spacing(12)
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        label = Gtk.Label(
            label=(
                "Chrome / Chromium:\n"
                "1. Open chrome://extensions and enable Developer mode.\n"
                "2. Choose Load unpacked.\n"
                "3. Select /usr/share/udownload/browser/chrome\n\n"
                "Firefox:\n"
                "Open about:debugging → This Firefox → Load Temporary Add-on and select "
                "/usr/share/udownload/browser/firefox/manifest.json. Standard Firefox requires Mozilla signing for permanent installation."
            ),
            xalign=0,
            wrap=True,
        )
        area.append(label)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chrome = Gtk.Button(label="Open Chrome extensions")
        firefox = Gtk.Button(label="Open Firefox debugging")
        folder = Gtk.Button(label="Open extension folder")
        repair = Gtk.Button(label="Repair native host")
        chrome.connect("clicked", lambda *_: launch_browser_extensions_page("chrome"))
        firefox.connect("clicked", lambda *_: launch_browser_extensions_page("firefox"))
        folder.connect("clicked", lambda *_: subprocess.Popen(["xdg-open", "/usr/share/udownload/browser"]))
        repair.connect("clicked", self._repair_native_host)
        buttons.append(chrome)
        buttons.append(firefox)
        buttons.append(folder)
        buttons.append(repair)
        area.append(buttons)
        self.connect("response", lambda *_: self.destroy())

    def _repair_native_host(self, *_args) -> None:
        try:
            paths = install_native_manifests()
            self.parent_window.status_label.set_text(
                f"Browser native host repaired ({len(paths)} manifests written)"
            )
        except Exception as exc:
            self.parent_window.status_label.set_text(
                f"Could not repair browser native host: {exc}"
            )


class DownloadCompleteDialog(Gtk.Dialog):
    def __init__(self, parent: "MainWindow", item: DownloadObject):
        super().__init__(
            title="Download complete",
            transient_for=parent,
            modal=False,
        )
        self.parent_window = parent
        self.item = item
        self._changing_checkbox = False
        self.set_default_size(610, 310)

        area = self.get_content_area()
        area.set_spacing(10)
        area.set_margin_top(14)
        area.set_margin_bottom(14)
        area.set_margin_start(14)
        area.set_margin_end(14)

        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
        icon.set_pixel_size(42)
        heading.append(icon)

        title_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=3,
            hexpand=True,
        )
        done = Gtk.Label(label="Download complete", xalign=0)
        done.add_css_class("title-3")
        title_box.append(done)

        final_bytes = item.completed or item.total
        size = Gtk.Label(
            label=f"Downloaded {format_bytes(final_bytes)}"
            + (f" ({final_bytes} Bytes)" if final_bytes else ""),
            xalign=0,
        )
        size.add_css_class("dim-label")
        title_box.append(size)
        heading.append(title_box)

        self.drag_button = Gtk.Button()
        self.drag_button.set_tooltip_text(
            "Drag the completed file to a folder or another application"
        )
        drag_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        drag_icon = Gtk.Image.new_from_icon_name("document-send-symbolic")
        drag_icon.set_pixel_size(28)
        drag_box.append(drag_icon)
        drag_box.append(Gtk.Label(label="Drag"))
        self.drag_button.set_child(drag_box)
        heading.append(self.drag_button)

        self.drag_source = Gtk.DragSource()
        self.drag_source.set_actions(
            Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        )
        self.drag_source.connect("prepare", self._prepare_drag)
        self.drag_source.connect("drag-end", self._drag_end)
        self.drag_button.add_controller(self.drag_source)

        area.append(heading)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        area.append(grid)

        self.address_entry = Gtk.Entry(hexpand=True)
        self.address_entry.set_text(item.url)
        self.address_entry.set_editable(False)

        self.path_entry = Gtk.Entry(hexpand=True)
        self.path_entry.set_text(str(parent._path_for_item(item)))
        self.path_entry.set_editable(False)

        for index, (label_text, widget) in enumerate([
            ("Address", self.address_entry),
            ("The file saved as", self.path_entry),
        ]):
            grid.attach(Gtk.Label(label=label_text, xalign=0), 0, index, 1, 1)
            grid.attach(widget, 1, index, 1, 1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        open_button = Gtk.Button(label="Open")
        open_with_button = Gtk.Button(label="Open with…")
        folder_button = Gtk.Button(label="Open folder")
        close_button = Gtk.Button(label="Close")

        path = parent._path_for_item(item)
        open_button.set_sensitive(path.exists())
        open_with_button.set_sensitive(path.exists())
        folder_button.set_sensitive(Path(item.save_dir).expanduser().exists())

        open_button.connect("clicked", lambda *_: parent._open_item(item))
        open_with_button.connect("clicked", lambda *_: parent._open_with_item(item))
        folder_button.connect("clicked", lambda *_: parent._open_item_folder(item))
        close_button.connect("clicked", lambda *_: self.destroy())

        actions.append(open_button)
        actions.append(open_with_button)
        actions.append(folder_button)
        actions.append(Gtk.Box(hexpand=True))
        actions.append(close_button)
        area.append(actions)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.dont_show = Gtk.CheckButton(
            label="Don't show this dialog again"
        )
        self.dont_show.connect("toggled", self._dont_show_toggled)
        footer.append(self.dont_show)
        area.append(footer)

    def _prepare_drag(
        self,
        _source: Gtk.DragSource,
        _x: float,
        _y: float,
    ):
        path = self.parent_window._path_for_item(self.item)
        if not path.exists():
            self.parent_window.status_label.set_text(
                f"File not found: {path}"
            )
            return None

        uri = Gio.File.new_for_path(str(path)).get_uri()
        payload = (uri + "\r\n").encode("utf-8")
        return Gdk.ContentProvider.new_for_bytes(
            "text/uri-list",
            GLib.Bytes.new(payload),
        )

    def _drag_end(
        self,
        _source: Gtk.DragSource,
        _drag,
        delete_data: bool,
    ) -> None:
        if not delete_data:
            return

        path = self.parent_window._path_for_item(self.item)
        if path.exists():
            try:
                path.unlink()
            except Exception as exc:
                self.parent_window.status_label.set_text(
                    f"Could not finish file move: {exc}"
                )
                return

        self.parent_window.status_label.set_text(
            f"File moved by drag-and-drop: {self.item.file_name}"
        )
        self.path_entry.set_text("The file has been moved.")

    def _dont_show_toggled(self, check: Gtk.CheckButton) -> None:
        if self._changing_checkbox or not check.get_active():
            return

        confirm = Gtk.Dialog(
            title="Disable download-complete dialog?",
            transient_for=self,
            modal=True,
        )
        confirm.add_button("Cancel", Gtk.ResponseType.CANCEL)
        disable = confirm.add_button("Disable", Gtk.ResponseType.OK)
        with contextlib.suppress(Exception):
            disable.add_css_class("destructive-action")
        confirm.set_default_response(Gtk.ResponseType.CANCEL)

        box = confirm.get_content_area()
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(
            Gtk.Label(
                label=(
                    "Stop showing the Download complete dialog?\n\n"
                    "You can turn it back on later from Options → "
                    "Show download-complete dialog."
                ),
                xalign=0,
                wrap=True,
            )
        )

        def response(dialog: Gtk.Dialog, value: int) -> None:
            if value == Gtk.ResponseType.OK:
                self.parent_window.settings.set(
                    "show_download_complete_dialog",
                    False,
                )
                self.parent_window.status_label.set_text(
                    "Download-complete dialog disabled; it can be restored from Options"
                )
            else:
                self._changing_checkbox = True
                try:
                    self.dont_show.set_active(False)
                finally:
                    self._changing_checkbox = False
            dialog.destroy()

        confirm.connect("response", response)
        confirm.present()


class DownloadProgressDialog(Gtk.Dialog):
    def __init__(self, parent: "MainWindow", download_id: int):
        row = parent.db.get(download_id)
        title = str(row["file_name"]) if row else "Download"
        super().__init__(
            title=f"Downloading - {title}",
            transient_for=parent,
            modal=False,
        )
        self.parent_window = parent
        self.download_id = int(download_id)
        self._source_id = 0
        self._completion_handled = False
        self._action_busy = False
        self._closed = False
        self.set_default_size(620, 430)

        self.details_button = self.add_button(
            "Show details",
            Gtk.ResponseType.NONE,
        )
        self.minimize_button = self.add_button(
            "Minimize",
            Gtk.ResponseType.NONE,
        )
        self.pause_button = self.add_button(
            "Pause",
            Gtk.ResponseType.NONE,
        )
        self.cancel_button = self.add_button(
            "Cancel",
            Gtk.ResponseType.NONE,
        )

        self.details_button.connect("clicked", self._show_details)
        self.minimize_button.connect("clicked", self._minimize_to_list)
        self.pause_button.connect("clicked", self._pause_or_resume)
        self.cancel_button.connect("clicked", self._cancel_download)

        self.connect("response", self._on_dialog_response)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)

        area = self.get_content_area()
        area.set_spacing(10)
        area.set_margin_top(12)
        area.set_margin_bottom(12)
        area.set_margin_start(12)
        area.set_margin_end(12)

        self.notebook = Gtk.Notebook()
        area.append(self.notebook)

        status_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        status_tab.set_margin_top(10)
        status_tab.set_margin_bottom(10)
        status_tab.set_margin_start(10)
        status_tab.set_margin_end(10)

        self.status_grid = Gtk.Grid(column_spacing=12, row_spacing=7)
        status_tab.append(self.status_grid)

        self.value_labels: dict[str, Gtk.Label] = {}
        fields = [
            ("URL", "url"),
            ("Status", "status"),
            ("File size", "size"),
            ("Downloaded", "downloaded"),
            ("Transfer rate", "speed"),
            ("Time left", "eta"),
            ("Resume capability", "resume"),
        ]
        for index, (caption, key) in enumerate(fields):
            left = Gtk.Label(label=caption, xalign=0)
            left.add_css_class("dim-label")
            right = Gtk.Label(label="", xalign=0, hexpand=True, selectable=True)
            right.set_ellipsize(3)
            self.status_grid.attach(left, 0, index, 1, 1)
            self.status_grid.attach(right, 1, index, 1, 1)
            self.value_labels[key] = right

        self.progress = Gtk.ProgressBar(show_text=True)
        status_tab.append(self.progress)

        status_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_actions.set_visible(False)
        status_tab.append(status_actions)

        self.notebook.append_page(status_tab, Gtk.Label(label="Download status"))

        limiter_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        limiter_tab.set_margin_top(16)
        limiter_tab.set_margin_bottom(16)
        limiter_tab.set_margin_start(16)
        limiter_tab.set_margin_end(16)
        self.limit_enabled = Gtk.CheckButton(
            label="Limit transfer rate for this download"
        )
        limiter_tab.append(self.limit_enabled)
        limiter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        limiter_row.append(Gtk.Label(label="Limit"))
        self.limit_kib = Gtk.SpinButton.new_with_range(1, 1024 * 1024, 1)
        self.limit_kib.set_value(1024)
        limiter_row.append(self.limit_kib)
        limiter_row.append(Gtk.Label(label="KiB/s"))
        limiter_tab.append(limiter_row)
        apply_limit = Gtk.Button(label="Apply speed limit")
        apply_limit.set_halign(Gtk.Align.START)
        apply_limit.connect("clicked", self._apply_speed_limit)
        limiter_tab.append(apply_limit)
        self.notebook.append_page(limiter_tab, Gtk.Label(label="Speed limiter"))

        completion_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        completion_tab.set_margin_top(16)
        completion_tab.set_margin_bottom(16)
        completion_tab.set_margin_start(16)
        completion_tab.set_margin_end(16)
        self.open_file_done = Gtk.CheckButton(label="Open file when done")
        self.open_folder_done = Gtk.CheckButton(label="Open folder when done")
        self.close_window_done = Gtk.CheckButton(
            label="Close this progress window when done"
        )
        completion_tab.append(self.open_file_done)
        completion_tab.append(self.open_folder_done)
        completion_tab.append(self.close_window_done)
        self.notebook.append_page(
            completion_tab,
            Gtk.Label(label="Options on completion"),
        )

        self._refresh()
        self._source_id = GLib.timeout_add(750, self._refresh)

    def _row_and_item(self):
        row = self.parent_window.db.get(self.download_id)
        if row is None:
            return None, None
        return row, DownloadObject(row)

    def _refresh(self) -> bool:
        row, item = self._row_and_item()
        if row is None or item is None:
            self.destroy()
            return GLib.SOURCE_REMOVE

        self.set_title(f"{item.progress_text or '0.0%'} {item.file_name}")
        self.value_labels["url"].set_text(item.url)
        self.value_labels["status"].set_text(item.status_text)
        self.value_labels["size"].set_text(item.size_text or "Unknown")
        self.value_labels["downloaded"].set_text(
            f"{format_bytes(item.completed)}"
            + (f"  ({item.progress_text})" if item.progress_text else "")
        )
        self.value_labels["speed"].set_text(item.speed_text or "0 B/s")
        self.value_labels["eta"].set_text(item.eta_text or "Calculating…")
        self.value_labels["resume"].set_text(
            "Yes" if item.status in {"active", "paused", "waiting"} else "—"
        )

        fraction = 0.0
        if item.total > 0:
            fraction = min(1.0, max(0.0, item.completed / item.total))
        elif item.status == "complete":
            fraction = 1.0
        self.progress.set_fraction(fraction)
        self.progress.set_text(item.progress_text or "Resolving…")

        if item.status == "active" or (item.status == "waiting" and item.gid):
            self.pause_button.set_label("Pause")
            self.pause_button.set_sensitive(not self._action_busy)
        elif item.status == "paused" or (item.status == "waiting" and not item.gid):
            self.pause_button.set_label("Resume")
            self.pause_button.set_sensitive(not self._action_busy)
        elif item.status == "complete":
            self.pause_button.set_label("Open")
            self.pause_button.set_sensitive(True)
            self.cancel_button.set_label("Close")
            self._handle_completion(item)
        else:
            self.pause_button.set_sensitive(False)

        return GLib.SOURCE_CONTINUE

    def _pause_or_resume(self, *_args) -> None:
        if self._action_busy:
            return

        _row, item = self._row_and_item()
        if item is None:
            return
        if item.status == "complete":
            self.parent_window._open_item(item)
            return

        if not item.gid:
            self._action_busy = True
            self.pause_button.set_sensitive(False)
            try:
                self.parent_window._start_db_download(item.db_id)
            finally:
                self._action_busy = False
            GLib.timeout_add(150, self._refresh_once)
            return

        self._action_busy = True
        self.pause_button.set_sensitive(False)
        threading.Thread(
            target=self._toggle_transfer_worker,
            args=(item.gid,),
            daemon=True,
        ).start()

    def _toggle_transfer_worker(self, gid: str) -> None:
        error = ""
        action = ""
        snapshot: dict = {}
        hard_stopped = False

        try:
            snapshot = self.parent_window.aria.tell_status(gid)
            live_status = str(snapshot.get("status", "") or "")

            if live_status in {"active", "waiting", "paused"}:
                self.parent_window.aria.hard_stop(gid)
                hard_stopped = True
                action = "paused"
            elif live_status == "complete":
                action = "complete"
            elif live_status == "removed":
                hard_stopped = True
                action = "paused"
            else:
                raise Aria2Error(
                    f"Download cannot be paused/resumed while status is "
                    f"{live_status or 'unknown'}"
                )
        except Exception as exc:
            error = str(exc)

        GLib.idle_add(
            self._toggle_transfer_done,
            action,
            error,
            gid,
            snapshot,
            hard_stopped,
        )

    def _toggle_transfer_done(
        self,
        action: str,
        error: str,
        gid: str,
        snapshot: dict,
        hard_stopped: bool,
    ) -> bool:
        self._action_busy = False

        if hard_stopped and action == "paused":
            self.parent_window._commit_hard_pause(
                self.download_id,
                gid,
                snapshot,
            )
        elif snapshot:
            with contextlib.suppress(Exception):
                self.parent_window.db.update_state(gid, snapshot)

        self.parent_window.load_rows()

        if error:
            self.parent_window.status_label.set_text(
                f"Could not change download state: {error}"
            )
        elif action == "paused":
            self.parent_window.status_label.set_text(
                "Download paused (hard stop)"
            )
        elif action == "resumed":
            self.parent_window.status_label.set_text("Download resumed")

        if not self._closed:
            self._refresh()
        return GLib.SOURCE_REMOVE

    def _refresh_once(self) -> bool:
        if not self._closed:
            self._refresh()
        return GLib.SOURCE_REMOVE

    def _minimize_to_list(self, *_args) -> None:
        self.destroy()

    def _on_dialog_response(self, _dialog, response_id: int) -> None:
        if response_id in {
            Gtk.ResponseType.CLOSE,
            Gtk.ResponseType.CANCEL,
            Gtk.ResponseType.DELETE_EVENT,
        }:
            self.destroy()

    def _on_close_request(self, *_args) -> bool:
        self.destroy()
        return True

    def _cancel_download(self, *_args) -> None:
        _row, item = self._row_and_item()
        if item is None:
            self.destroy()
            return

        if not item.gid:
            self.destroy()
            return

        download_id = item.db_id
        gid = item.gid
        self.destroy()

        def worker() -> None:
            error = ""
            snapshot: dict = {}
            hard_stopped = False

            try:
                snapshot = self.parent_window.aria.tell_status(gid)
                live_status = str(snapshot.get("status", "") or "")

                if live_status == "complete":
                    pass
                elif live_status in {"active", "waiting", "paused", "removed"}:
                    if live_status != "removed":
                        self.parent_window.aria.hard_stop(gid)
                    hard_stopped = True
                else:
                    raise Aria2Error(
                        f"Download cannot be cancelled while status is "
                        f"{live_status or 'unknown'}"
                    )
            except Exception as exc:
                error = str(exc)

            GLib.idle_add(
                self.parent_window._progress_cancel_done,
                download_id,
                gid,
                snapshot,
                hard_stopped,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _show_details(self, *_args) -> None:
        _row, item = self._row_and_item()
        if item is not None:
            self.parent_window.show_properties(item)

    def _apply_speed_limit(self, *_args) -> None:
        _row, item = self._row_and_item()
        if item is None or not item.gid:
            return
        value = (
            f"{self.limit_kib.get_value_as_int()}K"
            if self.limit_enabled.get_active()
            else "0"
        )
        try:
            self.parent_window.aria.call(
                "changeOption",
                [item.gid, {"max-download-limit": value}],
            )
            self.parent_window.status_label.set_text(
                f"Per-download speed limit updated for {item.file_name}"
            )
        except Exception as exc:
            self.parent_window.status_label.set_text(
                f"Could not change speed limit: {exc}"
            )

    def _handle_completion(self, item: DownloadObject) -> None:
        if self._completion_handled:
            return
        self._completion_handled = True
        if self.open_file_done.get_active():
            self.parent_window._open_item(item)
        if self.open_folder_done.get_active():
            self.parent_window._open_item_folder(item)
        GLib.idle_add(lambda: (self.destroy(), GLib.SOURCE_REMOVE)[1])

    def _on_destroy(self, *_args) -> None:
        self._closed = True
        if self._source_id:
            with contextlib.suppress(Exception):
                GLib.source_remove(self._source_id)
            self._source_id = 0


class SchedulerDialog(Gtk.Dialog):
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def __init__(self, parent: "MainWindow"):
        super().__init__(title="Scheduler", transient_for=parent, modal=True)
        self.parent_window = parent
        self.db = parent.db
        self.settings = parent.settings
        self.current_queue = "Main download queue"
        self._loading = False
        self.set_default_size(900, 650)

        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.connect("response", lambda *_: self.destroy())

        area = self.get_content_area()
        area.set_spacing(10)
        area.set_margin_top(12)
        area.set_margin_bottom(12)
        area.set_margin_start(12)
        area.set_margin_end(12)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        area.append(body)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(220, -1)
        left.append(Gtk.Label(label="Queues", xalign=0))
        self.queue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.queue_list.add_css_class("boxed-list")
        self.queue_list.connect("row-selected", self._queue_selected)
        queue_scroll = Gtk.ScrolledWindow(vexpand=True)
        queue_scroll.set_child(self.queue_list)
        left.append(queue_scroll)

        queue_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        new_queue = Gtk.Button(label="New queue")
        delete_queue = Gtk.Button(label="Delete")
        new_queue.connect("clicked", self._new_queue)
        delete_queue.connect("clicked", self._delete_queue)
        queue_actions.append(new_queue)
        queue_actions.append(delete_queue)
        left.append(queue_actions)
        body.append(left)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        self.queue_title = Gtk.Label(label=self.current_queue, xalign=0)
        self.queue_title.add_css_class("title-3")
        right.append(self.queue_title)

        self.notebook = Gtk.Notebook()
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        right.append(self.notebook)
        body.append(right)

        self._build_schedule_tab()
        self._build_files_tab()

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        start_now = Gtk.Button(label="Start now")
        stop_now = Gtk.Button(label="Stop")
        apply_button = Gtk.Button(label="Apply")
        start_now.add_css_class("suggested-action")
        start_now.connect("clicked", self._start_now)
        stop_now.connect("clicked", self._stop_now)
        apply_button.connect("clicked", self._apply)
        footer.append(start_now)
        footer.append(stop_now)
        footer.append(apply_button)
        area.append(footer)

        self._reload_queues()
        self._select_queue(self.current_queue)

    def _build_schedule_tab(self) -> None:
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab.set_margin_top(12)
        tab.set_margin_bottom(12)
        tab.set_margin_start(12)
        tab.set_margin_end(12)

        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.mode_once = Gtk.CheckButton(label="One-time downloading")
        self.mode_daily = Gtk.CheckButton(label="Daily")
        self.mode_daily.set_group(self.mode_once)
        mode_row.append(self.mode_once)
        mode_row.append(self.mode_daily)
        tab.append(mode_row)

        self.start_on_startup = Gtk.CheckButton(label="Start queue on UDownload startup")
        tab.append(self.start_on_startup)

        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.start_enabled = Gtk.CheckButton(label="Start queue at")
        self.start_date = Gtk.Entry(width_chars=12)
        self.start_date.set_placeholder_text("YYYY-MM-DD")
        self.start_hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.start_minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        self.start_hour.set_width_chars(2)
        self.start_minute.set_width_chars(2)
        start_box.append(self.start_enabled)
        start_box.append(self.start_date)
        start_box.append(self.start_hour)
        start_box.append(Gtk.Label(label=":"))
        start_box.append(self.start_minute)
        tab.append(start_box)

        weekday_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        weekday_box.append(Gtk.Label(label="Days", xalign=0))
        self.weekday_checks: list[Gtk.CheckButton] = []
        for day in self.WEEKDAYS:
            check = Gtk.CheckButton(label=day[:3])
            self.weekday_checks.append(check)
            weekday_box.append(check)
        tab.append(weekday_box)

        stop_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.stop_enabled = Gtk.CheckButton(label="Stop queue at")
        self.stop_hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.stop_minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        self.stop_hour.set_width_chars(2)
        self.stop_minute.set_width_chars(2)
        stop_box.append(self.stop_enabled)
        stop_box.append(self.stop_hour)
        stop_box.append(Gtk.Label(label=":"))
        stop_box.append(self.stop_minute)
        tab.append(stop_box)

        retry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.retry_enabled = Gtk.CheckButton(label="Number of retries for failed downloads")
        self.retry_count = Gtk.SpinButton.new_with_range(0, 100, 1)
        self.retry_count.set_value(3)
        retry_box.append(self.retry_enabled)
        retry_box.append(self.retry_count)
        tab.append(retry_box)

        open_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.open_when_done = Gtk.CheckButton(label="Open the following file when queue completes")
        self.open_path = Gtk.Entry(hexpand=True)
        browse_open = Gtk.Button(label="Browse…")
        browse_open.connect("clicked", self._browse_completion_file)
        open_box.append(self.open_when_done)
        open_box.append(self.open_path)
        open_box.append(browse_open)
        tab.append(open_box)

        self.exit_when_done = Gtk.CheckButton(label="Exit UDownload when queue completes")
        self.shutdown_when_done = Gtk.CheckButton(label="Turn off computer when queue completes")
        tab.append(self.exit_when_done)
        tab.append(self.shutdown_when_done)

        note = Gtk.Label(
            label=(
                "The computer shutdown option uses the normal system power-off action "
                "and may require desktop authorization."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        tab.append(note)

        self.notebook.append_page(tab, Gtk.Label(label="Schedule"))

    def _build_files_tab(self) -> None:
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab.set_margin_top(12)
        tab.set_margin_bottom(12)
        tab.set_margin_start(12)
        tab.set_margin_end(12)

        simultaneous = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        simultaneous.append(Gtk.Label(label="Download", xalign=0))
        self.max_concurrent = Gtk.SpinButton.new_with_range(1, 20, 1)
        simultaneous.append(self.max_concurrent)
        simultaneous.append(Gtk.Label(label="files at the same time"))
        tab.append(simultaneous)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        for text, width in [("File Name", 34), ("Size", 12), ("Status", 14), ("Time left", 12)]:
            label = Gtk.Label(label=text, xalign=0, width_chars=width)
            label.add_css_class("heading")
            header.append(label)
        tab.append(header)

        self.files_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        files_scroll = Gtk.ScrolledWindow(vexpand=True)
        files_scroll.set_child(self.files_list)
        tab.append(files_scroll)

        move_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        up_button = Gtk.Button(icon_name="go-up-symbolic")
        down_button = Gtk.Button(icon_name="go-down-symbolic")
        remove_button = Gtk.Button(icon_name="edit-delete-symbolic")
        up_button.set_tooltip_text("Move up")
        down_button.set_tooltip_text("Move down")
        remove_button.set_tooltip_text("Remove from this queue")
        up_button.connect("clicked", lambda *_: self._move_selected_file(-1))
        down_button.connect("clicked", lambda *_: self._move_selected_file(1))
        remove_button.connect("clicked", self._remove_selected_from_queue)
        move_actions.append(up_button)
        move_actions.append(down_button)
        move_actions.append(remove_button)
        tab.append(move_actions)

        self.notebook.append_page(tab, Gtk.Label(label="Files in queue"))

    def _scheduler_key(self, queue_name: str) -> str:
        return f"scheduler.queue::{queue_name}"

    def _default_config(self) -> dict[str, Any]:
        now = dt.datetime.now()
        return {
            "mode": "once",
            "start_on_startup": False,
            "start_enabled": False,
            "start_date": now.date().isoformat(),
            "start_hour": now.hour,
            "start_minute": now.minute,
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "stop_enabled": False,
            "stop_hour": 0,
            "stop_minute": 0,
            "retry_enabled": False,
            "retry_count": 3,
            "open_when_done": False,
            "open_path": "",
            "exit_when_done": False,
            "shutdown_when_done": False,
            "max_concurrent": int(self.settings.get("max_concurrent", 5)),
            "order": [],
            "run_active": False,
            "completion_done": False,
            "last_start_key": "",
            "last_stop_key": "",
            "retry_attempts": {},
        }

    def _config(self) -> dict[str, Any]:
        value = self.settings.get(self._scheduler_key(self.current_queue), {})
        config = self._default_config()
        if isinstance(value, dict):
            config.update(value)
        return config

    def _clear_listbox(self, listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def _reload_queues(self) -> None:
        self._clear_listbox(self.queue_list)
        rows = list(self.db.conn.execute(
            "SELECT name FROM queues WHERE enabled=1 ORDER BY sort_order ASC, name ASC"
        ))
        if not rows:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO queues(name,enabled,sort_order) VALUES('Main download queue',1,0)"
            )
            self.db.conn.commit()
            rows = list(self.db.conn.execute(
                "SELECT name FROM queues WHERE enabled=1 ORDER BY sort_order ASC, name ASC"
            ))

        for row in rows:
            name = str(row["name"])
            item = Gtk.ListBoxRow()
            item.queue_name = name
            item.set_child(Gtk.Label(label=name, xalign=0))
            self.queue_list.append(item)

    def _select_queue(self, name: str) -> None:
        child = self.queue_list.get_first_child()
        while child is not None:
            if getattr(child, "queue_name", "") == name:
                self.queue_list.select_row(child)
                return
            child = child.get_next_sibling()
        first = self.queue_list.get_row_at_index(0)
        if first:
            self.queue_list.select_row(first)

    def _queue_selected(self, _listbox, row) -> None:
        if row is None:
            return
        self.current_queue = str(getattr(row, "queue_name", "Main download queue"))
        self.queue_title.set_text(self.current_queue)
        self._load_config()
        self._refresh_files()

    def _load_config(self) -> None:
        self._loading = True
        try:
            config = self._config()
            if config.get("mode") == "daily":
                self.mode_daily.set_active(True)
            else:
                self.mode_once.set_active(True)

            self.start_on_startup.set_active(bool(config.get("start_on_startup", False)))
            self.start_enabled.set_active(bool(config.get("start_enabled", False)))
            self.start_date.set_text(str(config.get("start_date", dt.date.today().isoformat())))
            self.start_hour.set_value(int(config.get("start_hour", 0)))
            self.start_minute.set_value(int(config.get("start_minute", 0)))

            enabled_days = {int(value) for value in config.get("weekdays", [])}
            for index, check in enumerate(self.weekday_checks):
                check.set_active(index in enabled_days)

            self.stop_enabled.set_active(bool(config.get("stop_enabled", False)))
            self.stop_hour.set_value(int(config.get("stop_hour", 0)))
            self.stop_minute.set_value(int(config.get("stop_minute", 0)))
            self.retry_enabled.set_active(bool(config.get("retry_enabled", False)))
            self.retry_count.set_value(int(config.get("retry_count", 3)))
            self.open_when_done.set_active(bool(config.get("open_when_done", False)))
            self.open_path.set_text(str(config.get("open_path", "")))
            self.exit_when_done.set_active(bool(config.get("exit_when_done", False)))
            self.shutdown_when_done.set_active(bool(config.get("shutdown_when_done", False)))
            self.max_concurrent.set_value(int(config.get("max_concurrent", 5)))
        finally:
            self._loading = False

    def _save_config(self) -> dict[str, Any]:
        config = self._config()
        config.update({
            "mode": "daily" if self.mode_daily.get_active() else "once",
            "start_on_startup": self.start_on_startup.get_active(),
            "start_enabled": self.start_enabled.get_active(),
            "start_date": self.start_date.get_text().strip() or dt.date.today().isoformat(),
            "start_hour": self.start_hour.get_value_as_int(),
            "start_minute": self.start_minute.get_value_as_int(),
            "weekdays": [
                index for index, check in enumerate(self.weekday_checks)
                if check.get_active()
            ],
            "stop_enabled": self.stop_enabled.get_active(),
            "stop_hour": self.stop_hour.get_value_as_int(),
            "stop_minute": self.stop_minute.get_value_as_int(),
            "retry_enabled": self.retry_enabled.get_active(),
            "retry_count": self.retry_count.get_value_as_int(),
            "open_when_done": self.open_when_done.get_active(),
            "open_path": self.open_path.get_text().strip(),
            "exit_when_done": self.exit_when_done.get_active(),
            "shutdown_when_done": self.shutdown_when_done.get_active(),
            "max_concurrent": self.max_concurrent.get_value_as_int(),
        })
        self.settings.set(self._scheduler_key(self.current_queue), config)
        return config

    def _apply(self, *_args) -> None:
        self._save_config()
        self.parent_window.status_label.set_text(
            f"Scheduler settings saved for {self.current_queue}"
        )

    def _start_now(self, *_args) -> None:
        self._save_config()
        self.parent_window.start_scheduler_queue(self.current_queue)

    def _stop_now(self, *_args) -> None:
        self.parent_window.stop_scheduler_queue(self.current_queue)

    def _browse_completion_file(self, *_args) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Choose file to open when queue completes",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Select", Gtk.ResponseType.ACCEPT)

        def response(dialog, value: int) -> None:
            if value == Gtk.ResponseType.ACCEPT:
                file = dialog.get_file()
                path = file.get_path() if file else None
                if path:
                    self.open_path.set_text(path)
            dialog.destroy()

        chooser.connect("response", response)
        chooser.present()

    def _refresh_files(self) -> None:
        self._clear_listbox(self.files_list)
        config = self._config()
        rows = list(self.db.conn.execute(
            "SELECT * FROM downloads WHERE queue_name=? AND status!='complete' ORDER BY id ASC",
            (self.current_queue,),
        ))

        order = [int(value) for value in config.get("order", []) if str(value).isdigit()]
        positions = {download_id: index for index, download_id in enumerate(order)}
        rows.sort(key=lambda row: (positions.get(int(row["id"]), 10**9), int(row["id"])))

        for row in rows:
            item = DownloadObject(row)
            list_row = Gtk.ListBoxRow()
            list_row.download_id = item.db_id
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            name = Gtk.Label(label=item.file_name, xalign=0, width_chars=34, ellipsize=3)
            size = Gtk.Label(label=item.size_text, xalign=0, width_chars=12)
            status = Gtk.Label(label=item.status_text, xalign=0, width_chars=14)
            eta = Gtk.Label(label=item.eta_text, xalign=0, width_chars=12)
            box.append(name)
            box.append(size)
            box.append(status)
            box.append(eta)
            list_row.set_child(box)
            self.files_list.append(list_row)

    def _current_file_ids(self) -> list[int]:
        ids: list[int] = []
        row = self.files_list.get_first_child()
        while row is not None:
            download_id = getattr(row, "download_id", None)
            if download_id is not None:
                ids.append(int(download_id))
            row = row.get_next_sibling()
        return ids

    def _move_selected_file(self, direction: int) -> None:
        selected = self.files_list.get_selected_row()
        if selected is None:
            return
        ids = self._current_file_ids()
        download_id = int(getattr(selected, "download_id"))
        try:
            index = ids.index(download_id)
        except ValueError:
            return
        target = index + direction
        if target < 0 or target >= len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        config = self._config()
        config["order"] = ids
        self.settings.set(self._scheduler_key(self.current_queue), config)
        self._refresh_files()
        row = self.files_list.get_row_at_index(target)
        if row:
            self.files_list.select_row(row)

    def _remove_selected_from_queue(self, *_args) -> None:
        selected = self.files_list.get_selected_row()
        if selected is None:
            return
        if self.current_queue == "Main download queue":
            self.parent_window.status_label.set_text(
                "Items in the main queue cannot be removed from all queues"
            )
            return
        download_id = int(getattr(selected, "download_id"))
        self.db.conn.execute(
            "UPDATE downloads SET queue_name='Main download queue' WHERE id=?",
            (download_id,),
        )
        self.db.conn.commit()
        self._refresh_files()
        self.parent_window.load_rows()

    def _new_queue(self, *_args) -> None:
        dialog = Gtk.Dialog(title="New queue", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Create", Gtk.ResponseType.OK)
        entry = Gtk.Entry(hexpand=True, placeholder_text="Queue name")
        area = dialog.get_content_area()
        area.set_margin_top(14)
        area.set_margin_bottom(14)
        area.set_margin_start(14)
        area.set_margin_end(14)
        area.append(entry)

        def response(_dialog, value: int) -> None:
            if value == Gtk.ResponseType.OK:
                name = entry.get_text().strip()
                if name:
                    try:
                        sort_order = int(self.db.conn.execute(
                            "SELECT COALESCE(MAX(sort_order),0)+1 FROM queues"
                        ).fetchone()[0])
                        self.db.conn.execute(
                            "INSERT INTO queues(name,enabled,sort_order) VALUES(?,1,?)",
                            (name, sort_order),
                        )
                        self.db.conn.commit()
                        self._reload_queues()
                        self._select_queue(name)
                    except Exception as exc:
                        self.parent_window.status_label.set_text(
                            f"Could not create queue: {exc}"
                        )
            dialog.destroy()

        dialog.connect("response", response)
        dialog.present()

    def _delete_queue(self, *_args) -> None:
        if self.current_queue == "Main download queue":
            self.parent_window.status_label.set_text("The main download queue cannot be deleted")
            return
        name = self.current_queue
        self.db.conn.execute(
            "UPDATE downloads SET queue_name='Main download queue' WHERE queue_name=?",
            (name,),
        )
        self.db.conn.execute("DELETE FROM queues WHERE name=?", (name,))
        self.db.conn.commit()
        self._reload_queues()
        self._select_queue("Main download queue")
        self.parent_window.load_rows()


class DownloadPropertiesDialog(Gtk.Dialog):
    def __init__(self, parent: "MainWindow", item: DownloadObject, row: Any):
        super().__init__(title="File Properties", transient_for=parent, modal=True)
        self.parent_window = parent
        self.item = item
        self.row = row
        self.set_default_size(760, 560)
        self.add_button("OK", Gtk.ResponseType.CLOSE)
        self.set_default_response(Gtk.ResponseType.CLOSE)

        area = self.get_content_area()
        area.set_spacing(12)
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        icon.set_pixel_size(40)
        title_row.append(icon)
        title_label = Gtk.Label(label=item.file_name, xalign=0, hexpand=True)
        title_label.add_css_class("title-3")
        title_label.set_ellipsize(3)
        title_row.append(title_label)
        area.append(title_row)

        grid = Gtk.Grid(column_spacing=12, row_spacing=9)
        area.append(grid)

        path = Path(item.save_dir) / item.file_name
        mime_type, _encoding = mimetypes.guess_type(item.file_name)
        mime_text = mime_type or item.category or "Unknown"
        size_text = format_bytes(item.total) if item.total else "Unknown"
        if item.total:
            size_text += f" ({item.total} bytes)"

        save_to_text = (
            str(path)
            if path.exists() or item.status != "complete"
            else "The file has been moved."
        )

        values = [
            ("Type", mime_text),
            ("Status", item.status_text),
            ("Size", size_text),
            ("Progress", item.progress_text or "—"),
            ("Time left", item.eta_text or "—"),
            ("Transfer rate", item.speed_text or "—"),
            ("Save to", save_to_text),
            ("Address", item.url),
            ("Description", item.description or ""),
            ("Source page / Referrer", str(row["source_page"] or "")),
            ("Date added", item.added_at_text),
            ("Queue", item.queue_name),
        ]
        if item.error_message:
            values.append(("Error", item.error_message))

        for index, (label_text, value) in enumerate(values):
            label = Gtk.Label(label=label_text, xalign=1)
            label.set_valign(Gtk.Align.CENTER)
            entry = Gtk.Entry(hexpand=True)
            entry.set_text(str(value or ""))
            entry.set_editable(False)
            entry.set_can_focus(True)
            grid.attach(label, 0, index, 1, 1)
            grid.attach(entry, 1, index, 1, 1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        open_button = Gtk.Button(label="Open")
        open_button.set_sensitive(path.exists())
        open_button.connect("clicked", lambda *_: parent._open_item(item))
        folder_button = Gtk.Button(label="Open Folder")
        folder_button.connect("clicked", lambda *_: parent._open_item_folder(item))
        move_button = Gtk.Button(label="Move / Rename…")
        move_button.set_sensitive(item.status == "complete" and path.exists())
        move_button.connect("clicked", lambda *_: parent._move_rename_item(item, self))
        actions.append(open_button)
        actions.append(folder_button)
        actions.append(move_button)
        area.append(actions)

        self.connect("response", lambda *_: self.destroy())


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title=APP_NAME)
        self.db = Database()
        self.settings = Settings(self.db)
        self.aria = Aria2Client()
        self.web_server: WebUIServer | None = None
        self._web_settings_snapshot: tuple[bool, int] | None = None
        self.current_category = str(self.settings.get("current_category", "All Downloads"))
        self.search_text = ""
        self.refresh_busy = False
        self.scroll_hold_until = 0.0
        self.column_width_save_source = 0
        self._restoring_layout = False
        self._scheduler_startup_pending = True
        self._progress_windows: dict[int, Gtk.Window] = {}
        self._completion_dialog_ids: set[int] = set()
        self.set_default_size(int(self.settings.get("window_width", 1180)), int(self.settings.get("window_height", 720)))
        self.set_icon_name("udownload")
        install_native_manifests()
        self._build_ui()
        self.connect("close-request", self._on_close)
        GLib.idle_add(self._start_engine)
        GLib.idle_add(self._sync_web_server)
        GLib.timeout_add_seconds(2, self._watch_web_settings)
        GLib.timeout_add_seconds(1, self.refresh)
        GLib.timeout_add_seconds(20, self.run_scheduler)

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        header = Gtk.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title.append(Gtk.Label(label="Ubuntu Download Manager", xalign=0))
        subtitle = Gtk.Label(label="Fast segmented downloads and browser integration", xalign=0)
        subtitle.add_css_class("dim-label")
        title.append(subtitle)
        header.set_title_widget(title)
        self.search = Gtk.SearchEntry(placeholder_text="Search downloads", width_chars=25)
        self.search.connect("search-changed", self._on_search)
        header.pack_end(self.search)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Import Links from TXT…", "win.import-links")
        menu.append("Export Unfinished Links…", "win.export-links")
        menu.append("Browser Integration", "win.browser")
        menu.append("Remote", "win.remote")
        menu.append("Open Downloads Folder", "win.open-downloads")
        menu.append("About", "win.about")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)
        # Use the GTK header bar as the actual window title bar. Appending a
        # HeaderBar inside the content creates two sets of window controls on
        # some Ubuntu desktop/window-manager combinations.
        self.set_titlebar(header)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.add_css_class("toolbar")
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(4)
        toolbar.set_margin_bottom(4)
        actions = [
            ("Add URL", "list-add-symbolic", self.add_url_dialog),
            ("Resume", "media-playback-start-symbolic", self.resume_selected),
            ("Stop", "media-playback-pause-symbolic", self.pause_selected),
            ("Stop All", "media-playback-stop-symbolic", self.pause_all),
            ("Delete", "user-trash-symbolic", self.delete_selected),
            ("Delete Completed", "edit-clear-all-symbolic", self.delete_completed),
            ("Options", "emblem-system-symbolic", self.options_dialog),
            ("Remote", "network-wired-symbolic", lambda: RemoteDialog(self, self.settings).present()),
            ("Scheduler", "alarm-symbolic", self.show_scheduled),
            ("Start Queue", "view-list-symbolic", self.resume_all),
            ("Stop Queue", "process-stop-symbolic", self.pause_all),
        ]
        for label, icon, callback in actions:
            button = Gtk.Button()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=label))
            button.set_child(box)
            button.set_tooltip_text(label)
            button.connect("clicked", lambda _b, cb=callback: cb())
            toolbar.append(button)
        root.append(toolbar)
        root.append(Gtk.Separator())

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.paned = paned
        paned.set_position(int(self.settings.get("sidebar_position", 220)))
        paned.set_wide_handle(True)
        root.append(paned)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar_box.set_size_request(210, -1)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        categories = [
            ("All Downloads", "folder-download-symbolic"),
            ("Compressed", "package-x-generic-symbolic"),
            ("Documents", "x-office-document-symbolic"),
            ("Music", "audio-x-generic-symbolic"),
            ("Programs", "application-x-executable-symbolic"),
            ("Video", "video-x-generic-symbolic"),
            ("Images", "image-x-generic-symbolic"),
            ("Unfinished", "media-playback-pause-symbolic"),
            ("Finished", "emblem-ok-symbolic"),
        ]
        for name, icon in categories:
            row = Gtk.ListBoxRow()
            row.category_name = name
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(7)
            box.set_margin_bottom(7)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=name, xalign=0, hexpand=True))
            row.set_child(box)
            self.sidebar.append(row)
        self.sidebar.connect("row-selected", self._category_changed)
        sidebar_scroll = Gtk.ScrolledWindow(vexpand=True)
        sidebar_scroll.set_child(self.sidebar)
        sidebar_box.append(sidebar_scroll)
        queue_label = Gtk.Label(label="Queues", xalign=0)
        queue_label.add_css_class("heading")
        queue_label.set_margin_start(12)
        sidebar_box.append(queue_label)
        queue_row = Gtk.Button(label="Main download queue")
        queue_row.connect("clicked", lambda *_: self._set_category("All Downloads"))
        sidebar_box.append(queue_row)
        paned.set_start_child(sidebar_box)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.store = Gio.ListStore.new(DownloadObject)
        # ColumnView exposes a combined sorter.  We attach it to a
        # SortListModel so clicking a sortable header immediately reorders the
        # visible rows without changing the database/model order.
        self.selection = Gtk.SingleSelection.new(self.store)
        self.column_view = Gtk.ColumnView.new(self.selection)
        self.column_view.set_hexpand(True)
        self.column_view.set_vexpand(True)
        self.column_view.set_show_row_separators(True)
        self.column_view.connect("activate", self._activate_row)

        self.columns_by_key: dict[str, Gtk.ColumnViewColumn] = {}
        saved_widths = self.settings.get("column_widths", {}) or {}
        columns = [
            ("File Name", "name", lambda item: item.file_name, 340, True, self._sort_name),
            ("Size", "size", lambda item: item.size_text, 110, False, self._sort_size),
            ("Status", "status", lambda item: item.status_text, 140, True, self._sort_status),
            ("Progress", "progress", lambda item: item.progress_text, 90, False, None),
            ("Time left", "eta", lambda item: item.eta_text, 110, False, None),
            ("Transfer rate", "speed", lambda item: item.speed_text, 120, False, None),
            ("Description", "description", lambda item: item.description, 220, True, None),
            ("Date Added", "date_added", lambda item: item.added_at_text, 155, False, self._sort_date_added),
        ]
        self._restoring_layout = True
        for title, key, getter, width, expand, sort_func in columns:
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._column_setup)
            factory.connect("bind", self._column_bind, getter)
            column = Gtk.ColumnViewColumn.new(title, factory)
            with contextlib.suppress(Exception):
                column.set_id(key)
            column.set_resizable(True)
            saved_width = saved_widths.get(key, width) if isinstance(saved_widths, dict) else width
            try:
                saved_width = max(55, int(saved_width))
            except (TypeError, ValueError):
                saved_width = width
            column.set_fixed_width(saved_width)
            column.set_expand(expand)
            if sort_func is not None:
                column.set_sorter(Gtk.CustomSorter.new(sort_func, None))
            column.connect("notify::fixed-width", self._on_column_width_changed)
            self.column_view.append_column(column)
            self.columns_by_key[key] = column
        self._restoring_layout = False

        self.view_sorter = self.column_view.get_sorter()
        self.sort_model = Gtk.SortListModel.new(self.store, self.view_sorter)
        self.selection = Gtk.SingleSelection.new(self.sort_model)
        self.column_view.set_model(self.selection)
        self.view_sorter.connect("changed", self._on_sort_changed)

        saved_sort_key = str(self.settings.get("sort_column", "date_added") or "date_added")
        saved_sort_order = str(self.settings.get("sort_order", "descending") or "descending")
        saved_sort_column = self.columns_by_key.get(saved_sort_key)
        if saved_sort_column is not None and saved_sort_column.get_sorter() is not None:
            direction = (
                Gtk.SortType.DESCENDING
                if saved_sort_order == "descending"
                else Gtk.SortType.ASCENDING
            )
            self.column_view.sort_by_column(saved_sort_column, direction)
        self.download_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.download_scroll.set_child(self.column_view)
        self.download_scroll.get_vadjustment().connect("value-changed", self._on_download_scroll)
        content.append(self.download_scroll)
        self.status_label = Gtk.Label(label="Ready", xalign=0)
        self.status_label.set_margin_start(8)
        self.status_label.set_margin_end(8)
        self.status_label.set_margin_top(4)
        self.status_label.set_margin_bottom(4)
        content.append(Gtk.Separator())
        content.append(self.status_label)
        paned.set_end_child(content)

        self._add_actions()
        startup_row = None
        for idx in range(len(categories)):
            candidate = self.sidebar.get_row_at_index(idx)
            if candidate and getattr(candidate, "category_name", "") == self.current_category:
                startup_row = candidate
                break
        self.sidebar.select_row(startup_row or self.sidebar.get_row_at_index(0))

    def _add_actions(self) -> None:
        actions = {
            "import-links": self.import_links_from_txt,
            "export-links": self.export_unfinished_links,
            "browser": lambda *_: BrowserIntegrationDialog(self).present(),
            "remote": lambda *_: RemoteDialog(self, self.settings).present(),
            "open-downloads": lambda *_: subprocess.Popen(["xdg-open", str(self.settings.get("download_dir"))]),
            "about": self._about,
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _about(self, *_args) -> None:
        about = Adw.AboutWindow(
            transient_for=self,
            application_name=APP_NAME,
            application_icon="udownload",
            version=APP_VERSION,
            developer_name="امیرحسین آقاجانی",
            developers=["امیرحسین آقاجانی <aghajani@dr.com>"],
            comments="A native Ubuntu download manager with segmented downloads, queues, scheduling and browser integration.",
            website="https://amirhossein.dev",
            copyright="© 2026 امیرحسین آقاجانی",
            license="GNU General Public License v3.0 or later (GPL-3.0-or-later)",
        )
        about.present()

    def _column_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        cell.set_hexpand(True)
        cell.set_vexpand(True)
        cell.set_halign(Gtk.Align.FILL)
        cell.set_valign(Gtk.Align.FILL)

        label = Gtk.Label(xalign=0, ellipsize=3, hexpand=True)
        label.set_halign(Gtk.Align.FILL)
        label.set_valign(Gtk.Align.CENTER)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(7)
        label.set_margin_bottom(7)
        cell.append(label)

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect("pressed", self._on_row_right_click, list_item)
        cell.add_controller(right_click)
        list_item.set_child(cell)

    @staticmethod
    def _column_bind(_factory, list_item: Gtk.ListItem, getter) -> None:
        item = list_item.get_item()
        cell = list_item.get_child()
        label = cell.get_first_child() if cell is not None else None
        if not isinstance(label, Gtk.Label):
            return
        value = str(getter(item) or "")
        label.set_text(value)
        label.set_tooltip_text(value)

    @staticmethod
    def _compare(left: Any, right: Any) -> Gtk.Ordering:
        if left < right:
            return Gtk.Ordering.SMALLER
        if left > right:
            return Gtk.Ordering.LARGER
        return Gtk.Ordering.EQUAL

    def _sort_name(self, a: DownloadObject, b: DownloadObject, _data=None) -> Gtk.Ordering:
        return self._compare(a.file_name.casefold(), b.file_name.casefold())

    def _sort_size(self, a: DownloadObject, b: DownloadObject, _data=None) -> Gtk.Ordering:
        result = self._compare(a.total, b.total)
        if result == Gtk.Ordering.EQUAL:
            return self._compare(a.file_name.casefold(), b.file_name.casefold())
        return result

    def _sort_status(self, a: DownloadObject, b: DownloadObject, _data=None) -> Gtk.Ordering:
        left = STATUS_LABELS.get(a.status, a.status).casefold()
        right = STATUS_LABELS.get(b.status, b.status).casefold()
        result = self._compare(left, right)
        if result == Gtk.Ordering.EQUAL:
            return self._compare(a.file_name.casefold(), b.file_name.casefold())
        return result

    def _sort_date_added(self, a: DownloadObject, b: DownloadObject, _data=None) -> Gtk.Ordering:
        # added_at is ISO-8601, so lexical order is chronological.
        result = self._compare(a.added_at, b.added_at)
        if result == Gtk.Ordering.EQUAL:
            return self._compare(a.db_id, b.db_id)
        return result

    def _on_sort_changed(self, sorter, *_args) -> None:
        with contextlib.suppress(Exception):
            column = sorter.get_primary_sort_column()
            if column is None:
                return
            key = column.get_id() or ""
            if not key:
                for candidate_key, candidate_column in self.columns_by_key.items():
                    if candidate_column == column:
                        key = candidate_key
                        break
            if not key:
                return
            order = sorter.get_primary_sort_order()
            order_name = "descending" if order == Gtk.SortType.DESCENDING else "ascending"
            self.settings.set("sort_column", key)
            self.settings.set("sort_order", order_name)

    def _on_column_width_changed(self, *_args) -> None:
        if self._restoring_layout:
            return
        if self.column_width_save_source:
            with contextlib.suppress(Exception):
                GLib.source_remove(self.column_width_save_source)
        self.column_width_save_source = GLib.timeout_add(350, self._save_column_widths)

    def _save_column_widths(self) -> bool:
        widths = {
            key: max(55, int(column.get_fixed_width()))
            for key, column in self.columns_by_key.items()
        }
        self.settings.set("column_widths", widths)
        self.column_width_save_source = 0
        return GLib.SOURCE_REMOVE

    def _select_db_id(self, db_id: int | None) -> None:
        if db_id is None:
            return
        for idx in range(self.sort_model.get_n_items()):
            item = self.sort_model.get_item(idx)
            if item and item.db_id == db_id:
                self.selection.set_selected(idx)
                return

    def _start_engine(self) -> bool:
        if self.aria.ensure_running():
            self.status_label.set_text("Download engine connected")
            with contextlib.suppress(Exception):
                self.aria.set_global_options(self.settings)
            self.refresh()
        else:
            self.status_label.set_text("Could not start aria2 download engine")
        return GLib.SOURCE_REMOVE

    def _on_close(self, _window) -> bool:
        width, height = self.get_width(), self.get_height()
        if width > 0 and height > 0:
            self.settings.set("window_width", width)
            self.settings.set("window_height", height)
        if hasattr(self, "paned"):
            self.settings.set("sidebar_position", int(self.paned.get_position()))
        if hasattr(self, "columns_by_key"):
            self._save_column_widths()
        self.settings.set("current_category", self.current_category)
        self._stop_web_server()
        return False

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self.search_text = entry.get_text().strip()
        self.load_rows(preserve_view=False)

    def _category_changed(self, _listbox, row) -> None:
        if row:
            self.current_category = row.category_name
            self.settings.set("current_category", self.current_category)
            self.load_rows(preserve_view=False)

    def _set_category(self, category: str) -> None:
        self.current_category = category
        self.settings.set("current_category", self.current_category)
        self.load_rows(preserve_view=False)

    def _selected(self) -> DownloadObject | None:
        item = self.selection.get_selected_item()
        return item if isinstance(item, DownloadObject) else None

    def _activate_row(self, _view, position: int) -> None:
        item = self.sort_model.get_item(position)
        if not isinstance(item, DownloadObject):
            return

        if item.status in {"active", "paused", "waiting"}:
            self._show_download_progress_after_start(item.db_id)
            return

        self.show_properties(item)

    @staticmethod
    def _path_for_item(item: DownloadObject) -> Path:
        return Path(item.save_dir).expanduser() / item.file_name

    def _open_item(self, item: DownloadObject) -> None:
        path = self._path_for_item(item)
        if not path.exists():
            self.status_label.set_text(f"File not found: {path}")
            return
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _open_item_folder(self, item: DownloadObject) -> None:
        directory = Path(item.save_dir).expanduser()
        if not directory.exists():
            self.status_label.set_text(f"Folder not found: {directory}")
            return
        subprocess.Popen(["xdg-open", str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _open_with_item(self, item: DownloadObject) -> None:
        path = self._path_for_item(item)
        if not path.exists():
            self.status_label.set_text(f"File not found: {path}")
            return
        content_type, _uncertain = Gio.content_type_guess(str(path), None)
        apps = [app for app in Gio.AppInfo.get_all_for_type(content_type) if app.should_show()]
        if not apps:
            self.status_label.set_text("No application is registered for this file type")
            return

        dialog = Gtk.Dialog(title="Open With", transient_for=self, modal=True)
        dialog.set_default_size(460, 420)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(14)
        area.set_margin_bottom(14)
        area.set_margin_start(14)
        area.set_margin_end(14)
        area.append(Gtk.Label(label=f"Open {item.file_name} with:", xalign=0))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        apps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll.set_child(apps_box)
        area.append(scroll)
        file = Gio.File.new_for_path(str(path))

        for app in apps:
            button = Gtk.Button(label=app.get_display_name())
            button.set_halign(Gtk.Align.FILL)
            def launch(_button, chosen=app) -> None:
                try:
                    chosen.launch([file], None)
                    dialog.destroy()
                except Exception as exc:
                    self.status_label.set_text(f"Could not open file: {exc}")
            button.connect("clicked", launch)
            apps_box.append(button)

        dialog.connect("response", lambda *_: dialog.destroy())
        dialog.present()

    def _move_rename_item(self, item: DownloadObject, properties_dialog=None) -> None:
        source = self._path_for_item(item)
        if item.status != "complete" or not source.exists():
            self.status_label.set_text("Move / Rename is available for completed files")
            return

        chooser = Gtk.FileChooserDialog(
            title="Move / Rename",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Move", Gtk.ResponseType.ACCEPT)
        chooser.set_default_response(Gtk.ResponseType.ACCEPT)
        chooser.set_current_name(item.file_name)
        with contextlib.suppress(Exception):
            chooser.set_current_folder(Gio.File.new_for_path(str(source.parent)))

        def on_response(dialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                file = dialog.get_file()
                target_path = file.get_path() if file else None
                if target_path:
                    target = Path(target_path).expanduser()
                    if target != source:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            target = unique_path(target.parent, target.name)
                        try:
                            shutil.move(str(source), str(target))
                            self.db.conn.execute(
                                "UPDATE downloads SET file_name=?,save_dir=?,file_name_locked=1 WHERE id=?",
                                (target.name, str(target.parent), item.db_id),
                            )
                            self.db.conn.commit()
                            self.load_rows()
                            self.status_label.set_text(f"Moved to: {target}")
                            if properties_dialog is not None:
                                properties_dialog.destroy()
                        except Exception as exc:
                            self.status_label.set_text(f"Could not move file: {exc}")
            dialog.destroy()

        chooser.connect("response", on_response)
        chooser.present()

    def _redownload_item(self, item: DownloadObject) -> None:
        row = self.db.get(item.db_id)
        if row is None:
            return
        headers: dict[str, str] = {}
        with contextlib.suppress(Exception):
            headers = json.loads(row["headers_json"] or "{}")
        self.add_url_dialog({
            "url": item.url,
            "filename": item.file_name,
            "save_dir": item.save_dir,
            "category": item.category,
            "description": item.description,
            "headers": headers,
            "source_page": row["source_page"] or "",
        })

    def _resume_item(self, item: DownloadObject) -> None:
        self._select_db_id(item.db_id)
        self.resume_selected()

    def _pause_item(self, item: DownloadObject) -> None:
        if item.gid:
            self._run_selected_transfer_command(
                item.db_id,
                item.gid,
                "pause",
                show_progress=False,
            )

    def _remove_item(self, item: DownloadObject) -> None:
        self._select_db_id(item.db_id)
        self.delete_selected()

    def _add_item_to_main_queue(self, item: DownloadObject) -> None:
        self.db.conn.execute(
            "UPDATE downloads SET queue_name='Main download queue' WHERE id=?",
            (item.db_id,),
        )
        self.db.conn.commit()
        self.load_rows()
        self.status_label.set_text(f"Added to Main download queue: {item.file_name}")

    def show_properties(self, item: DownloadObject | None = None) -> None:
        item = item or self._selected()
        if not item:
            return
        row = self.db.get(item.db_id)
        if row is None:
            return
        DownloadPropertiesDialog(self, item, row).present()

    def _context_button(self, label: str, callback, sensitive: bool = True) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_has_frame(False)
        button.set_halign(Gtk.Align.FILL)
        button.set_sensitive(sensitive)
        button.connect("clicked", callback)
        return button

    def _on_row_right_click(self, gesture, _n_press: int, x: float, y: float, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        if not isinstance(item, DownloadObject):
            return
        position = list_item.get_position()
        if position >= 0:
            self.selection.set_selected(position)

        path = self._path_for_item(item)
        file_exists = path.exists()
        is_active = item.status == "active"
        can_resume = item.status in {"paused", "waiting"}
        can_move = item.status == "complete" and file_exists

        popover = Gtk.Popover()
        popover.set_autohide(True)
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        menu_box.set_margin_top(5)
        menu_box.set_margin_bottom(5)
        menu_box.set_margin_start(5)
        menu_box.set_margin_end(5)

        def add(label: str, action, sensitive: bool = True) -> None:
            def run(_button) -> None:
                popover.popdown()
                action()
            menu_box.append(self._context_button(label, run, sensitive))

        def separator() -> None:
            menu_box.append(Gtk.Separator())

        add("Open", lambda: self._open_item(item), file_exists)
        add("Open with…", lambda: self._open_with_item(item), file_exists)
        add("Open Folder", lambda: self._open_item_folder(item), Path(item.save_dir).exists())
        add("Move / Rename…", lambda: self._move_rename_item(item), can_move)
        add("Redownload…", lambda: self._redownload_item(item))
        separator()
        add("Resume Download", lambda: self._resume_item(item), can_resume)
        add("Stop Download", lambda: self._pause_item(item), is_active)
        separator()
        add("Add to Main download queue", lambda: self._add_item_to_main_queue(item))
        add("Remove", lambda: self._remove_item(item))
        separator()
        add("Properties", lambda: self.show_properties(item))

        popover.set_child(menu_box)

        # Keep the popover attached to a stable widget. Active rows are
        # replaced during live refreshes, but the ColumnView itself persists.
        widget = gesture.get_widget()
        anchor = self.column_view
        popover.set_parent(anchor)

        rect = Gdk.Rectangle()
        translated = False
        with contextlib.suppress(Exception):
            translated, bounds = widget.compute_bounds(anchor)
            if translated:
                rect.x = int(bounds.get_x())
                rect.y = int(bounds.get_y())
                rect.width = max(1, int(bounds.get_width()))
                rect.height = max(1, int(bounds.get_height()))

        if not translated:
            rect.x = 0
            rect.y = 0
            rect.width = 1
            rect.height = 1

        popover.set_pointing_to(rect)

        def cleanup(_popover) -> None:
            with contextlib.suppress(Exception):
                _popover.unparent()

        popover.connect("closed", cleanup)
        popover.popup()


    def _on_download_scroll(self, _adjustment) -> None:
        # While the user is actively scrolling, do not mutate the visible
        # model.  Even a correctly restored adjustment can cause a tiny visual
        # snap if GTK relayout happens between wheel/touchpad events.
        self.scroll_hold_until = time.monotonic() + 0.8

    @staticmethod
    def _row_signature(item: DownloadObject) -> tuple[Any, ...]:
        return (
            item.db_id, item.gid, item.file_name, item.url, item.save_dir,
            item.category, item.description, item.queue_name, item.status,
            item.total, item.completed, item.speed, item.error_message,
            item.start_time, item.added_at,
        )

    @staticmethod
    def _db_row_signature(row: Any) -> tuple[Any, ...]:
        return (
            int(row["id"]), row["gid"] or "", row["file_name"], row["url"],
            row["save_dir"], row["category"], row["description"],
            row["queue_name"], row["status"], int(row["total_length"] or 0),
            int(row["completed_length"] or 0), int(row["download_speed"] or 0),
            row["error_message"] or "", row["start_time"] or "",
            row["added_at"] or "",
        )

    def refresh(self) -> bool:
        if self.refresh_busy:
            return GLib.SOURCE_CONTINUE
        self.refresh_busy = True
        try:
            completed_ids: list[int] = []
            if self.aria.ping():
                for state in self.aria.tell_all():
                    gid = str(state.get("gid", "") or "")
                    before = self.db.get_by_gid(gid) if gid else None
                    was_complete = bool(
                        before is not None and str(before["status"]) == "complete"
                    )
                    self.db.update_state(gid, state)
                    if (
                        gid
                        and str(state.get("status", "")) == "complete"
                        and not was_complete
                    ):
                        after = self.db.get_by_gid(gid)
                        if after is not None:
                            completed_ids.append(int(after["id"]))
            self.load_rows(skip_if_scrolling=True)
            for download_id in completed_ids:
                GLib.idle_add(
                    self._show_download_complete_for_id,
                    download_id,
                )
        except Exception as exc:
            self.status_label.set_text(f"Engine: {exc}")
        finally:
            self.refresh_busy = False
        return GLib.SOURCE_CONTINUE

    def load_rows(self, preserve_view: bool = True, skip_if_scrolling: bool = False) -> None:
        rows = self.db.rows(self.current_category, self.search_text)
        active = sum(1 for row in rows if row["status"] == "active")
        total_speed = sum(int(row["download_speed"] or 0) for row in rows)
        self.status_label.set_text(
            f"{len(rows)} items   |   {active} active   |   {format_speed(total_speed) or '0 B/s'}"
        )

        # A periodic engine refresh must never fight with wheel/touchpad
        # scrolling.  Database state is already current; the visual rows can
        # safely wait until the user has stopped interacting for a moment.
        if skip_if_scrolling and time.monotonic() < self.scroll_hold_until:
            return

        selected = self._selected() if preserve_view else None
        selected_id = selected.db_id if selected else None
        current_ids = [self.store.get_item(i).db_id for i in range(self.store.get_n_items())]
        new_ids = [int(row["id"]) for row in rows]

        if current_ids == new_ids:
            # Normal 2-second refresh: keep the model structure intact and
            # replace only rows whose displayed data actually changed.  This
            # avoids the remove_all()/rebuild relayout that caused the subtle
            # scroll jump.
            changed = False
            for idx, row in enumerate(rows):
                item = self.store.get_item(idx)
                if self._row_signature(item) != self._db_row_signature(row):
                    self.store.splice(idx, 1, [DownloadObject(row)])
                    changed = True
            if changed:
                self._select_db_id(selected_id)
            return

        adjustment = self.download_scroll.get_vadjustment() if hasattr(self, "download_scroll") else None
        scroll_value = adjustment.get_value() if preserve_view and adjustment else 0.0

        # Structural changes (new/deleted rows or a filter membership change)
        # still require a rebuild, but these are no longer part of ordinary
        # status refreshes.  Preserve the selected DB id and viewport.
        self.store.remove_all()
        for row in rows:
            self.store.append(DownloadObject(row))
        self._select_db_id(selected_id)

        if preserve_view and adjustment:
            def restore_scroll() -> bool:
                upper = max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())
                adjustment.set_value(min(max(scroll_value, adjustment.get_lower()), upper))
                return GLib.SOURCE_REMOVE
            GLib.idle_add(restore_scroll, priority=GLib.PRIORITY_LOW)

    def add_url_dialog(self, initial: dict[str, Any] | None = None) -> None:
        data = dict(initial or {})
        data.setdefault("save_dir", str(self.settings.get("download_dir")))
        AddDownloadDialog(self, data, self.add_download).present()

    def add_download(self, data: dict[str, Any]) -> None:
        directory = Path(data["save_dir"]).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        download_id = self.db.add_download(
            url=data["url"],
            file_name=data["filename"],
            save_dir=str(directory),
            category=data["category"],
            description=data.get("description", ""),
            start_time=data.get("start_time"),
            headers=data.get("headers", {}),
            source_page=data.get("source_page", ""),
            file_name_locked=bool(data.get("file_name_locked", False)),
        )
        if data.get("start_now") and not data.get("start_time"):
            self._start_db_download(download_id)
            GLib.timeout_add(
                200,
                self._show_download_progress_after_start,
                download_id,
            )
        self.load_rows()

    def _show_download_progress_after_start(self, download_id: int) -> bool:
        row = self.db.get(download_id)
        if row is None:
            return GLib.SOURCE_REMOVE

        existing = self._progress_windows.get(download_id)
        if existing is not None:
            with contextlib.suppress(Exception):
                existing.present()
            return GLib.SOURCE_REMOVE

        dialog = DownloadProgressDialog(self, download_id)
        self._progress_windows[download_id] = dialog

        def cleanup(*_args) -> None:
            self._progress_windows.pop(download_id, None)

        dialog.connect("destroy", cleanup)
        dialog.present()
        return GLib.SOURCE_REMOVE

    def _show_download_complete_for_id(self, download_id: int) -> bool:
        if not bool(self.settings.get("show_download_complete_dialog", True)):
            return GLib.SOURCE_REMOVE
        if download_id in self._completion_dialog_ids:
            return GLib.SOURCE_REMOVE

        row = self.db.get(download_id)
        if row is None or str(row["status"]) != "complete":
            return GLib.SOURCE_REMOVE

        item = DownloadObject(row)
        dialog = DownloadCompleteDialog(self, item)
        self._completion_dialog_ids.add(download_id)

        def cleanup(*_args) -> None:
            self._completion_dialog_ids.discard(download_id)

        dialog.connect("destroy", cleanup)
        dialog.present()
        return GLib.SOURCE_REMOVE

    def _start_db_download(self, download_id: int) -> None:
        row = self.db.get(download_id)
        if not row:
            return
        try:
            if not self.aria.ensure_running():
                raise Aria2Error("Engine unavailable")
            gid = self.aria.add_uri(row, self.settings)
            self.db.set_gid(download_id, gid)
            self.status_label.set_text(f"Added: {row['file_name']}")
        except Exception as exc:
            self.db.conn.execute("UPDATE downloads SET status='error',error_message=? WHERE id=?", (str(exc), download_id))
            self.db.conn.commit()
            self.status_label.set_text(f"Could not add download: {exc}")

    def _scheduler_key(self, queue_name: str) -> str:
        return f"scheduler.queue::{queue_name}"

    def _scheduler_config(self, queue_name: str) -> dict[str, Any]:
        value = self.settings.get(self._scheduler_key(queue_name), {})
        return dict(value) if isinstance(value, dict) else {}

    def _save_scheduler_config(self, queue_name: str, config: dict[str, Any]) -> None:
        self.settings.set(self._scheduler_key(queue_name), config)

    def _queue_rows(self, queue_name: str) -> list[Any]:
        rows = list(self.db.conn.execute(
            "SELECT * FROM downloads WHERE queue_name=? AND status!='complete' ORDER BY id ASC",
            (queue_name,),
        ))
        config = self._scheduler_config(queue_name)
        order = [int(value) for value in config.get("order", []) if str(value).isdigit()]
        positions = {download_id: index for index, download_id in enumerate(order)}
        rows.sort(key=lambda row: (positions.get(int(row["id"]), 10**9), int(row["id"])))
        return rows

    def start_scheduler_queue(self, queue_name: str) -> None:
        config = self._scheduler_config(queue_name)
        max_concurrent = max(
            1,
            int(config.get("max_concurrent", self.settings.get("max_concurrent", 5))),
        )

        try:
            if not self.aria.ensure_running():
                raise Aria2Error("Engine unavailable")
            self.aria.call("changeGlobalOption", [{
                "max-concurrent-downloads": str(max_concurrent),
            }])
        except Exception as exc:
            self.status_label.set_text(f"Could not start scheduler queue: {exc}")
            return

        rows = self._queue_rows(queue_name)
        started = 0
        for row in rows:
            status = str(row["status"] or "")
            gid = str(row["gid"] or "")

            if gid and status == "paused":
                with contextlib.suppress(Exception):
                    self.aria.resume(gid)
                started += 1
                continue

            if gid and status in {"active", "waiting"}:
                continue

            if status == "error":
                if gid:
                    with contextlib.suppress(Exception):
                        self.aria.remove(gid)
                self.db.conn.execute(
                    "UPDATE downloads SET gid=NULL,status='waiting',error_message='',start_time=NULL WHERE id=?",
                    (int(row["id"]),),
                )
                self.db.conn.commit()
            elif status == "scheduled":
                self.db.conn.execute(
                    "UPDATE downloads SET status='waiting',start_time=NULL WHERE id=?",
                    (int(row["id"]),),
                )
                self.db.conn.commit()

            fresh = self.db.get(int(row["id"]))
            if fresh and not fresh["gid"] and str(fresh["status"]) in {"waiting", "error", "scheduled"}:
                self._start_db_download(int(row["id"]))
                started += 1

        config["run_active"] = True
        config["completion_done"] = False
        config["retry_attempts"] = {}
        self._save_scheduler_config(queue_name, config)
        self.status_label.set_text(
            f"Scheduler started {queue_name}: {started} item(s) submitted"
        )

    def stop_scheduler_queue(self, queue_name: str) -> None:
        stopped = 0
        for row in self._queue_rows(queue_name):
            gid = str(row["gid"] or "")
            if gid and str(row["status"]) in {"active", "waiting"}:
                with contextlib.suppress(Exception):
                    self.aria.pause(gid)
                    stopped += 1
        config = self._scheduler_config(queue_name)
        config["run_active"] = False
        self._save_scheduler_config(queue_name, config)
        self.status_label.set_text(
            f"Scheduler stopped {queue_name}: {stopped} item(s) paused"
        )

    def _scheduler_start_due(self, config: dict[str, Any], now: dt.datetime) -> bool:
        if not bool(config.get("start_enabled", False)):
            return False

        hour = int(config.get("start_hour", 0))
        minute = int(config.get("start_minute", 0))
        scheduled_time = dt.time(hour=hour, minute=minute)
        last_key = str(config.get("last_start_key", ""))

        if str(config.get("mode", "once")) == "daily":
            weekdays = {int(value) for value in config.get("weekdays", [])}
            if now.weekday() not in weekdays:
                return False
            key = now.date().isoformat()
            return now.time() >= scheduled_time and last_key != key

        try:
            date_value = dt.date.fromisoformat(str(config.get("start_date", "")))
        except ValueError:
            return False
        scheduled = dt.datetime.combine(date_value, scheduled_time)
        key = scheduled.isoformat(timespec="minutes")
        return now >= scheduled and last_key != key

    def _scheduler_stop_due(self, config: dict[str, Any], now: dt.datetime) -> bool:
        if not bool(config.get("stop_enabled", False)):
            return False
        if not bool(config.get("run_active", False)):
            return False
        hour = int(config.get("stop_hour", 0))
        minute = int(config.get("stop_minute", 0))
        stop_time = dt.time(hour=hour, minute=minute)
        key = now.date().isoformat()
        return (
            now.time() >= stop_time
            and str(config.get("last_stop_key", "")) != key
        )

    def _retry_scheduler_errors(self, queue_name: str, config: dict[str, Any]) -> None:
        if not bool(config.get("retry_enabled", False)):
            return
        limit = max(0, int(config.get("retry_count", 0)))
        if limit <= 0:
            return

        attempts = dict(config.get("retry_attempts", {}) or {})
        changed = False
        error_rows = list(self.db.conn.execute(
            "SELECT * FROM downloads WHERE queue_name=? AND status='error' ORDER BY id ASC",
            (queue_name,),
        ))

        for row in error_rows:
            key = str(int(row["id"]))
            count = int(attempts.get(key, 0))
            if count >= limit:
                continue
            gid = str(row["gid"] or "")
            if gid:
                with contextlib.suppress(Exception):
                    self.aria.remove(gid)
            self.db.conn.execute(
                "UPDATE downloads SET gid=NULL,status='waiting',error_message='',start_time=NULL WHERE id=?",
                (int(row["id"]),),
            )
            self.db.conn.commit()
            attempts[key] = count + 1
            changed = True
            self._start_db_download(int(row["id"]))

        if changed:
            config["retry_attempts"] = attempts
            self._save_scheduler_config(queue_name, config)

    def _scheduler_completion_actions(
        self,
        queue_name: str,
        config: dict[str, Any],
    ) -> None:
        if not bool(config.get("run_active", False)):
            return
        if bool(config.get("completion_done", False)):
            return

        unfinished = int(self.db.conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE queue_name=? AND status!='complete'",
            (queue_name,),
        ).fetchone()[0])
        if unfinished > 0:
            return

        config["run_active"] = False
        config["completion_done"] = True
        self._save_scheduler_config(queue_name, config)

        if bool(config.get("open_when_done", False)):
            target = Path(str(config.get("open_path", "") or "")).expanduser()
            if target.exists():
                subprocess.Popen(
                    ["xdg-open", str(target)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

        if bool(config.get("shutdown_when_done", False)):
            subprocess.Popen(
                ["systemctl", "poweroff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        if bool(config.get("exit_when_done", False)):
            GLib.idle_add(lambda: (self.close(), GLib.SOURCE_REMOVE)[1])

    def run_scheduler(self) -> bool:
        for row in self.db.scheduled_due():
            self._start_db_download(int(row["id"]))

        now = dt.datetime.now()
        queue_rows = list(self.db.conn.execute(
            "SELECT name FROM queues WHERE enabled=1 ORDER BY sort_order ASC, name ASC"
        ))

        for queue_row in queue_rows:
            queue_name = str(queue_row["name"])
            config = self._scheduler_config(queue_name)
            if not config:
                continue

            if (
                self._scheduler_startup_pending
                and bool(config.get("start_on_startup", False))
            ):
                self.start_scheduler_queue(queue_name)
                config = self._scheduler_config(queue_name)

            if self._scheduler_start_due(config, now):
                self.start_scheduler_queue(queue_name)
                config = self._scheduler_config(queue_name)
                if str(config.get("mode", "once")) == "daily":
                    config["last_start_key"] = now.date().isoformat()
                else:
                    try:
                        date_value = dt.date.fromisoformat(
                            str(config.get("start_date", ""))
                        )
                        time_value = dt.time(
                            int(config.get("start_hour", 0)),
                            int(config.get("start_minute", 0)),
                        )
                        config["last_start_key"] = dt.datetime.combine(
                            date_value,
                            time_value,
                        ).isoformat(timespec="minutes")
                    except ValueError:
                        config["last_start_key"] = now.isoformat(timespec="minutes")
                self._save_scheduler_config(queue_name, config)

            config = self._scheduler_config(queue_name)
            if self._scheduler_stop_due(config, now):
                self.stop_scheduler_queue(queue_name)
                config = self._scheduler_config(queue_name)
                config["last_stop_key"] = now.date().isoformat()
                self._save_scheduler_config(queue_name, config)

            config = self._scheduler_config(queue_name)
            if bool(config.get("run_active", False)):
                self._retry_scheduler_errors(queue_name, config)
                config = self._scheduler_config(queue_name)
                self._scheduler_completion_actions(queue_name, config)

        self._scheduler_startup_pending = False
        return GLib.SOURCE_CONTINUE

    def _commit_hard_pause(
        self,
        download_id: int,
        gid: str,
        snapshot: dict,
    ) -> None:
        # All SQLite writes happen here on GTK's main thread.
        if snapshot:
            with contextlib.suppress(Exception):
                self.db.update_state(gid, snapshot)

        row = self.db.get(download_id)
        if row is None:
            return

        current_gid = str(row["gid"] or "")
        # Never let an old worker callback erase a newer Resume GID.
        if current_gid and current_gid != gid:
            return

        self.db.conn.execute(
            "UPDATE downloads SET "
            "gid=NULL,status='paused',download_speed=0," 
            "file_name_locked=1,error_message='' WHERE id=?",
            (download_id,),
        )
        self.db.conn.commit()

    def _progress_cancel_done(
        self,
        download_id: int,
        gid: str,
        snapshot: dict,
        hard_stopped: bool,
        error: str,
    ) -> bool:
        if hard_stopped:
            self._commit_hard_pause(download_id, gid, snapshot)
        elif snapshot:
            with contextlib.suppress(Exception):
                self.db.update_state(gid, snapshot)

        self.load_rows()

        if error:
            self.status_label.set_text(f"Could not cancel download: {error}")
        elif hard_stopped:
            self.status_label.set_text(
                "Download cancelled and kept resumable"
            )
        else:
            self.status_label.set_text("Download already completed")

        return GLib.SOURCE_REMOVE

    def pause_selected(self) -> None:
        item = self._selected()
        if not item or not item.gid:
            return
        self._run_selected_transfer_command(
            item.db_id,
            item.gid,
            "pause",
            show_progress=False,
        )

    def resume_selected(self) -> None:
        item = self._selected()
        if not item:
            return

        if not item.gid:
            self._start_db_download(item.db_id)
            GLib.timeout_add(
                150,
                self._show_download_progress_after_start,
                item.db_id,
            )
            return

        self._run_selected_transfer_command(
            item.db_id,
            item.gid,
            "resume",
            show_progress=True,
        )

    def _run_selected_transfer_command(
        self,
        download_id: int,
        gid: str,
        command: str,
        show_progress: bool,
    ) -> None:
        self.status_label.set_text(
            "Pausing download…" if command == "pause" else "Resuming download…"
        )

        def worker() -> None:
            error = ""
            snapshot: dict = {}
            hard_stopped = False

            try:
                snapshot = self.aria.tell_status(gid)
                live_status = str(snapshot.get("status", "") or "")

                if command == "pause":
                    if live_status in {"active", "waiting", "paused"}:
                        self.aria.hard_stop(gid)
                        hard_stopped = True
                    elif live_status == "removed":
                        hard_stopped = True
                    elif live_status != "complete":
                        raise Aria2Error(
                            f"Download cannot be paused while status is "
                            f"{live_status or 'unknown'}"
                        )
                else:
                    if live_status == "paused":
                        self.aria.resume(gid)
                        threading.Event().wait(0.15)
                        snapshot = self.aria.tell_status(gid)
                    elif live_status not in {"active", "waiting", "complete"}:
                        raise Aria2Error(
                            f"Download cannot be resumed while status is "
                            f"{live_status or 'unknown'}"
                        )
            except Exception as exc:
                error = str(exc)

            GLib.idle_add(
                self._selected_transfer_command_done,
                download_id,
                gid,
                command,
                show_progress,
                snapshot,
                hard_stopped,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _selected_transfer_command_done(
        self,
        download_id: int,
        gid: str,
        command: str,
        show_progress: bool,
        snapshot: dict,
        hard_stopped: bool,
        error: str,
    ) -> bool:
        if hard_stopped and command == "pause":
            self._commit_hard_pause(download_id, gid, snapshot)
        elif snapshot:
            with contextlib.suppress(Exception):
                self.db.update_state(gid, snapshot)

        self.load_rows()

        if error:
            self.status_label.set_text(
                f"Could not {command} download: {error}"
            )
            return GLib.SOURCE_REMOVE

        self.status_label.set_text(
            "Download paused (hard stop)"
            if command == "pause"
            else "Download resumed"
        )

        if show_progress:
            self._show_download_progress_after_start(download_id)

        return GLib.SOURCE_REMOVE

    def pause_all(self) -> None:
        with contextlib.suppress(Exception):
            self.aria.pause_all()
        self.refresh()

    def resume_all(self) -> None:
        # Resume items already known by aria2, then explicitly start DB-only
        # queued items. Nothing enters aria2 until the user presses Start Queue
        # (or Start Download / Start Selected).
        with contextlib.suppress(Exception):
            self.aria.resume_all()

        queued_ids = [
            int(row["id"])
            for row in self.db.rows("All Downloads")
            if row["status"] == "waiting" and not row["gid"]
        ]
        if not queued_ids:
            self.refresh()
            return

        pending = iter(queued_ids)
        def start_next() -> bool:
            try:
                download_id = next(pending)
            except StopIteration:
                self.load_rows()
                self.status_label.set_text(f"Started/queued {len(queued_ids)} queued downloads")
                return GLib.SOURCE_REMOVE
            self._start_db_download(download_id)
            return GLib.SOURCE_CONTINUE
        GLib.idle_add(start_next, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def delete_selected(self) -> None:
        item = self._selected()
        if not item:
            return

        def do_delete() -> None:
            if item.gid:
                with contextlib.suppress(Exception):
                    self.aria.remove(item.gid)
            self.db.remove(item.db_id)
            self.load_rows()
            self.status_label.set_text(f"Removed from list: {item.file_name}")

        ConfirmDialog(
            self,
            "Confirm delete",
            f"Remove ‘{item.file_name}’ from the download list?\n\nThe downloaded file on disk, if any, will not be deleted.",
            do_delete,
        ).present()

    def delete_completed(self) -> None:
        count_row = self.db.conn.execute("SELECT COUNT(*) FROM downloads WHERE status='complete'").fetchone()
        count = int(count_row[0] or 0) if count_row else 0
        if count <= 0:
            self.status_label.set_text("No completed downloads to delete")
            return

        def do_delete_completed() -> None:
            self.db.clear_completed()
            self.load_rows()
            self.status_label.set_text(f"Removed {count} completed downloads from the list")

        ConfirmDialog(
            self,
            "Confirm delete completed",
            f"Remove {count} completed download{'s' if count != 1 else ''} from the list?\n\nDownloaded files on disk will not be deleted.",
            do_delete_completed,
        ).present()

    def import_links_from_txt(self, *_args) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Import links from text file",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Import", Gtk.ResponseType.ACCEPT)
        chooser.set_default_response(Gtk.ResponseType.ACCEPT)
        text_filter = Gtk.FileFilter()
        text_filter.set_name("Text files")
        text_filter.add_pattern("*.txt")
        chooser.add_filter(text_filter)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        chooser.add_filter(all_filter)

        def on_response(dialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                file = dialog.get_file()
                path = file.get_path() if file else None
                if path:
                    self._import_links_file(Path(path))
            dialog.destroy()

        chooser.connect("response", on_response)
        chooser.present()

    def _import_links_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as exc:
            self.status_label.set_text(f"Could not read text file: {exc}")
            return

        # Accept normal Notepad-style files with one URL per line, while also
        # finding multiple URLs on the same line. Preserve input order.
        candidates = re.findall(r"(?:https?|ftp)://[^\s<>\"']+", text, flags=re.IGNORECASE)
        urls: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            url = raw.rstrip(".,;:!?)]}")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        if not urls:
            self.status_label.set_text("No http/https/ftp links found in the selected text file")
            return

        directory = Path(str(self.settings.get("download_dir"))).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        added = 0
        for url in urls:
            filename = safe_filename(url)
            self.db.add_download(
                url=url,
                file_name=filename,
                save_dir=str(directory),
                category=category_for_filename(filename),
            )
            added += 1
        self.load_rows()
        self.status_label.set_text(f"Imported {added} links to the queue — nothing started")

    def export_unfinished_links(self, *_args) -> None:
        rows = list(self.db.conn.execute(
            "SELECT url FROM downloads WHERE status!='complete' ORDER BY id ASC"
        ))
        urls: list[str] = []
        seen: set[str] = set()
        for row in rows:
            url = str(row["url"] or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        if not urls:
            self.status_label.set_text("There are no unfinished links to export")
            return

        chooser = Gtk.FileChooserDialog(
            title="Export unfinished links",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
        chooser.add_button("Export", Gtk.ResponseType.ACCEPT)
        chooser.set_default_response(Gtk.ResponseType.ACCEPT)
        chooser.set_current_name(f"udownload-unfinished-links-{dt.datetime.now():%Y%m%d-%H%M}.txt")
        text_filter = Gtk.FileFilter()
        text_filter.set_name("Text files")
        text_filter.add_pattern("*.txt")
        chooser.add_filter(text_filter)

        def on_response(dialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                file = dialog.get_file()
                path = file.get_path() if file else None
                if path:
                    target = Path(path)
                    if target.suffix.lower() != ".txt":
                        target = target.with_suffix(".txt")
                    try:
                        target.write_text("\n".join(urls) + "\n", encoding="utf-8")
                        self.status_label.set_text(f"Exported {len(urls)} unfinished links to {target.name}")
                    except Exception as exc:
                        self.status_label.set_text(f"Could not export links: {exc}")
            dialog.destroy()

        chooser.connect("response", on_response)
        chooser.show()

    def options_dialog(self) -> None:
        OptionsDialog(self, self.settings, self.save_options).present()

    def save_options(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.settings.set(key, value)
        with contextlib.suppress(Exception):
            self.aria.set_global_options(self.settings)
        self.status_label.set_text("Options saved")
        self._sync_web_server()

    def _stop_web_server(self) -> None:
        server = self.web_server
        self.web_server = None
        if server is not None:
            with contextlib.suppress(Exception):
                server.stop()

    def _watch_web_settings(self) -> bool:
        enabled = bool(self.settings.get("web_enabled", False))
        try:
            port = int(self.settings.get("web_port", 8600) or 8600)
        except (TypeError, ValueError):
            port = 8600

        state = (enabled, port)
        if state != self._web_settings_snapshot:
            self._sync_web_server()
        return GLib.SOURCE_CONTINUE

    def _sync_web_server(self) -> bool:
        enabled = bool(self.settings.get("web_enabled", False))
        try:
            port = int(self.settings.get("web_port", 8600) or 8600)
        except (TypeError, ValueError):
            port = 8600

        if not 1024 <= port <= 65535:
            port = 8600
            self.settings.set("web_port", port)

        self._web_settings_snapshot = (enabled, port)

        if not enabled:
            was_running = self.web_server is not None
            self._stop_web_server()
            if was_running:
                self.status_label.set_text("Web UI disabled")
            return GLib.SOURCE_REMOVE

        if (
            self.web_server is not None
            and self.web_server.running
            and self.web_server.port == port
        ):
            self.status_label.set_text(
                f"Web UI active: {self.web_server.url}"
            )
            return GLib.SOURCE_REMOVE

        self._stop_web_server()
        server = WebUIServer(port=port)
        try:
            url = server.start()
        except Exception as exc:
            self.status_label.set_text(
                f"Could not start Web UI on port {port}: {exc}"
            )
            return GLib.SOURCE_REMOVE

        self.web_server = server
        self.status_label.set_text(f"Web UI active: {url}")
        return GLib.SOURCE_REMOVE

    def show_scheduled(self) -> None:
        SchedulerDialog(self).present()

    def handle_browser_message(self, message: dict[str, Any]) -> None:
        self.present()
        action = message.get("action")
        if action == "ping":
            return
        if action in {"add_url", "download"}:
            url = message.get("url", "")
            if not url:
                return
            headers = dict(message.get("headers", {}) or {})
            cookies = message.get("cookies", "")
            if cookies:
                headers["Cookie"] = cookies
            user_agent = str(message.get("userAgent", "") or "").strip()
            if user_agent and not any(key.casefold() == "user-agent" for key in headers):
                headers["User-Agent"] = user_agent
            browser_filename = str(message.get("filename", "") or "").strip()
            initial_filename = browser_filename or safe_filename(url)
            initial = {
                "url": url,
                "filename": initial_filename,
                "filename_locked": bool(
                    browser_filename
                    and not looks_like_placeholder_filename(browser_filename, url)
                ),
                "save_dir": str(self.settings.get("download_dir")),
                "headers": headers,
                "source_page": message.get("pageUrl") or message.get("referrer", ""),
                "description": message.get("description", ""),
            }
            if self.settings.get("browser_prompt", True):
                self.add_url_dialog(initial)
            else:
                initial["category"] = category_for_filename(initial["filename"])
                initial["file_name_locked"] = bool(initial.get("filename_locked", False))
                initial["start_now"] = False
                self.add_download(initial)
                self.status_label.set_text("Added to queue — press Resume or Start Queue to download")
        elif action in {"select_links", "download_all", "download_selected"}:
            links = message.get("links", [])
            if isinstance(links, list) and links:
                LinkSelectionDialog(
                    self,
                    links,
                    str(self.settings.get("download_dir")),
                    self._add_selected_links,
                ).present()

    def _add_selected_links(self, links: list[dict[str, str]], save_dir: str, start_now: bool) -> None:
        directory = Path(save_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        download_ids: list[int] = []
        for link in links:
            url = link.get("url", "")
            if not url:
                continue
            filename = safe_filename(url)
            download_id = self.db.add_download(
                url=url,
                file_name=filename,
                save_dir=str(directory),
                category=category_for_filename(filename),
                description=link.get("text", ""),
                headers={},
                source_page=link.get("pageUrl", ""),
            )
            download_ids.append(download_id)

        self.load_rows()
        if not start_now:
            self.status_label.set_text(f"{len(download_ids)} downloads added to queue — nothing started")
            return

        # Queue every selected URL in aria2. aria2 itself enforces the configured
        # simultaneous-download limit, so hundreds of links can be added safely.
        pending = iter(download_ids)
        def start_next() -> bool:
            try:
                download_id = next(pending)
            except StopIteration:
                self.load_rows()
                self.status_label.set_text(f"Started/queued {len(download_ids)} downloads")
                return GLib.SOURCE_REMOVE
            self._start_db_download(download_id)
            return GLib.SOURCE_CONTINUE
        GLib.idle_add(start_next, priority=GLib.PRIORITY_DEFAULT_IDLE)


def _cli_url(positional: str | None, link: str | None) -> str:
    value = str(positional or link or "").strip()
    if not value:
        raise ValueError("A download URL is required")

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https", "ftp"}:
        raise ValueError("URL must start with http://, https:// or ftp://")
    return value


def _cli_schedule(value: str | None) -> str | None:
    if not value:
        return None

    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(
            "--at must look like '2026-08-13 22:30'"
        ) from exc

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)

    parsed = parsed.replace(second=0, microsecond=0)
    if parsed <= dt.datetime.now():
        raise ValueError("--at must be in the future")

    return parsed.isoformat(timespec="seconds")


def _cli_resolve_filename(
    url: str,
    timeout: float = 20.0,
) -> tuple[str, bool]:
    """Resolve a usable filename before inserting a CLI download."""
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

        if candidate and not looks_like_placeholder_filename(
            candidate,
            final_url,
        ):
            return candidate, bool(info.filename_confident)

        if fallback and not looks_like_placeholder_filename(
            fallback,
            url,
        ):
            return fallback, True

        if time.monotonic() >= deadline:
            break

        time.sleep(0.75)

    detail = (
        f" Last resolver error: {last_error}"
        if last_error
        else ""
    )
    raise ValueError(
        "Could not determine the remote filename within "
        f"{int(timeout)} seconds."
        + detail
    )

def _cli_add_local(
    url: str,
    path: str | None,
    start_now: bool,
    at: str | None,
) -> int:
    db = Database()
    try:
        settings = Settings(db)
        directory = Path(
            path or str(settings.get("download_dir"))
        ).expanduser()
        directory.mkdir(parents=True, exist_ok=True)

        print("Resolving file information...", flush=True)
        filename, filename_locked = _cli_resolve_filename(url)
        print(f"Resolved filename: {filename}", flush=True)
        start_time = _cli_schedule(at)

        download_id = db.add_download(
            url=url,
            file_name=filename,
            save_dir=str(directory),
            category=category_for_filename(filename),
            start_time=start_time,
            file_name_locked=filename_locked,
        )

        if start_now:
            aria = Aria2Client()
            if not aria.ensure_running():
                db.conn.execute(
                    "UPDATE downloads "
                    "SET status='error',error_message=? "
                    "WHERE id=?",
                    ("aria2 engine unavailable", download_id),
                )
                db.conn.commit()
                print("Could not start aria2", file=sys.stderr)
                return 4

            row = db.get(download_id)
            if row is None:
                print("Download record disappeared", file=sys.stderr)
                return 5

            try:
                gid = aria.add_uri(row, settings)
                db.set_gid(download_id, gid)
            except Exception as exc:
                db.conn.execute(
                    "UPDATE downloads "
                    "SET status='error',error_message=? "
                    "WHERE id=?",
                    (str(exc), download_id),
                )
                db.conn.commit()
                print(
                    f"Could not start download: {exc}",
                    file=sys.stderr,
                )
                return 4

            print(f"Started download #{download_id}: {filename}")
            return 0

        if start_time:
            print(
                f"Scheduled download #{download_id} "
                f"for {start_time}: {filename}"
            )
        else:
            print(f"Added to queue #{download_id}: {filename}")
        return 0
    finally:
        db.conn.close()


def _cli_remote(
    url: str,
    server: str,
    port: int,
    user: str,
    key: str,
    path: str | None,
    start_now: bool,
    at: str | None,
) -> int:
    server = str(server or "").strip()
    if not server:
        print("--server is required", file=sys.stderr)
        return 2

    if not 1 <= int(port) <= 65535:
        print("--port must be between 1 and 65535", file=sys.stderr)
        return 2

    ok, detail = probe_ssh_endpoint(server, int(port), timeout=2.5)
    if not ok:
        print(detail, file=sys.stderr)
        return 3

    ssh = shutil.which("ssh")
    if not ssh:
        print(
            "ssh was not found. Install openssh-client first.",
            file=sys.stderr,
        )
        return 3

    normalized_at = _cli_schedule(at)
    target = f"{user}@{server}" if user else server

    remote_args = ["udownload", "add", url]
    if start_now:
        remote_args.append("--now")
    if normalized_at:
        remote_args.extend(["--at", normalized_at])
    if path:
        remote_args.extend(["--path", path])

    remote_command = " ".join(
        shlex.quote(part) for part in remote_args
    )

    command = [
        ssh,
        "-p",
        str(port),
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]

    if key:
        command.extend([
            "-i",
            str(Path(key).expanduser()),
            "-o",
            "IdentitiesOnly=yes",
        ])
    else:
        # Let OpenSSH read the password directly from the terminal.
        # OpenSSH disables terminal echo while the password is typed,
        # so the password never appears in command history or argv.
        command.extend([
            "-o",
            "PreferredAuthentications=keyboard-interactive,password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=3",
        ])

    command.extend([target, remote_command])

    print(f"Remote SSH: {target}:{port}")
    result = subprocess.run(command, check=False)
    return int(result.returncode)



def _cli_web(action: str, port: int | None = None) -> int:
    db = Database()
    try:
        settings = Settings(db)

        try:
            current_port = int(settings.get("web_port", 8600) or 8600)
        except (TypeError, ValueError):
            current_port = 8600

        if action == "status":
            enabled = bool(settings.get("web_enabled", False))
            print(f"Web UI: {'enabled' if enabled else 'disabled'}")
            print(f"Port: {current_port}")
            if enabled:
                print(f"URL: http://SERVER_IP:{current_port}/")
            return 0

        if action == "enable":
            if port is not None:
                if not 1024 <= int(port) <= 65535:
                    print(
                        "--port must be between 1024 and 65535",
                        file=sys.stderr,
                    )
                    return 2
                current_port = int(port)
                settings.set("web_port", current_port)

            settings.set("web_enabled", True)
            print("Web UI enabled")
            print(f"Port: {current_port}")
            print(f"URL: http://SERVER_IP:{current_port}/")
            print(
                "If UDM is running, the change is applied within a few seconds; "
                "otherwise it applies on the next UDM start."
            )
            return 0

        if action == "disable":
            settings.set("web_enabled", False)
            print("Web UI disabled")
            print(
                "If UDM is running, the listener stops within a few seconds; "
                "otherwise the setting is saved for the next UDM start."
            )
            return 0

        print(f"Unknown Web UI action: {action}", file=sys.stderr)
        return 2
    finally:
        db.conn.close()


def _handle_headless_cli(argv: list[str]) -> int | None:
    if len(argv) < 2 or argv[1] not in {"add", "remote", "web"}:
        return None

    parser = argparse.ArgumentParser(
        prog="udownload",
        description="UDM command-line download control",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    add_parser = subparsers.add_parser(
        "add",
        help="Add a link on this machine",
    )
    add_parser.add_argument("url", nargs="?")
    add_parser.add_argument("--link", dest="link")
    add_parser.add_argument("--path")
    add_mode = add_parser.add_mutually_exclusive_group()
    add_mode.add_argument(
        "--now",
        action="store_true",
        help="Start immediately",
    )
    add_mode.add_argument(
        "--at",
        help="Schedule, e.g. '2026-08-13 22:30'",
    )

    remote_parser = subparsers.add_parser(
        "remote",
        help="Add a link to another UDM machine over SSH",
    )
    remote_parser.add_argument("url", nargs="?")
    remote_parser.add_argument("--link", dest="link")
    remote_parser.add_argument("--server", required=True)
    remote_parser.add_argument(
        "--port",
        type=int,
        default=8347,
        help="SSH/NAT port (default: 8347)",
    )
    remote_parser.add_argument(
        "--user",
        required=True,
        help="Remote UDM SSH username",
    )
    remote_parser.add_argument("--key", default="")
    remote_parser.add_argument("--path")
    remote_mode = remote_parser.add_mutually_exclusive_group()
    remote_mode.add_argument(
        "--now",
        action="store_true",
        help="Start immediately on the remote machine",
    )
    remote_mode.add_argument(
        "--at",
        help="Schedule on the remote machine",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="Control the UDM Web UI",
    )
    web_commands = web_parser.add_subparsers(
        dest="web_action",
        required=True,
    )
    web_enable = web_commands.add_parser(
        "enable",
        help="Enable the Web UI",
    )
    web_enable.add_argument(
        "--port",
        type=int,
        help="Set the Web UI port while enabling it",
    )
    web_commands.add_parser(
        "disable",
        help="Disable the Web UI",
    )
    web_commands.add_parser(
        "status",
        help="Show the saved Web UI state and port",
    )

    args = parser.parse_args(argv[1:])
    try:
        if args.command == "web":
            return _cli_web(
                action=args.web_action,
                port=getattr(args, "port", None),
            )

        url = _cli_url(args.url, args.link)
        if args.command == "add":
            return _cli_add_local(
                url=url,
                path=args.path,
                start_now=bool(args.now),
                at=args.at,
            )

        return _cli_remote(
            url=url,
            server=args.server,
            port=args.port,
            user=args.user,
            key=args.key,
            path=args.path,
            start_now=bool(args.now),
            at=args.at,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

class UDMApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window: MainWindow | None = None

    def do_activate(self) -> None:
        if not self.window:
            self.window = MainWindow(self)
        self.window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        args = [arg.decode() if isinstance(arg, bytes) else arg for arg in command_line.get_arguments()]
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--browser-message-b64")
        parser.add_argument("--add-url")
        parser.add_argument("--self-test", action="store_true")
        parser.add_argument("--browser-integration", action="store_true")
        parsed, _unknown = parser.parse_known_args(args[1:])
        if parsed.self_test:
            print("UDownload self-test: OK")
            return 0
        self.activate()
        if parsed.browser_message_b64:
            try:
                raw = base64.urlsafe_b64decode(parsed.browser_message_b64.encode()).decode()
                message = json.loads(raw)
                GLib.idle_add(self.window.handle_browser_message, message)
            except Exception as exc:
                print(f"Invalid browser message: {exc}", file=sys.stderr)
                return 2
        elif parsed.add_url:
            GLib.idle_add(self.window.handle_browser_message, {"action": "add_url", "url": parsed.add_url})
        elif parsed.browser_integration:
            GLib.idle_add(lambda: (BrowserIntegrationDialog(self.window).present(), False)[1])
        return 0


def main() -> int:
    if "--version" in sys.argv:
        print(f"UDM {APP_VERSION}")
        return 0

    cli_result = _handle_headless_cli(sys.argv)
    if cli_result is not None:
        return cli_result

    if "--self-test" in sys.argv:
        db = Database(Path("/tmp/udownload-self-test.db"))
        assert category_for_filename("movie.mkv") == "Video"
        assert category_for_filename("archive.rar") == "Compressed"
        assert safe_filename("https://example.com/files/test%20file.zip") == "test file.zip"
        assert format_eta(4 * 1024**3, 0, 8 * 1024**2) == "8m 32s"
        duplicate_dir = Path("/tmp/udownload-duplicate-name-test")
        duplicate_dir.mkdir(parents=True, exist_ok=True)
        try:
            (duplicate_dir / "file.iso").touch()
            (duplicate_dir / "file (1).iso").touch()
            assert unique_path(duplicate_dir, "file.iso").name == "file (2).iso"
        finally:
            (duplicate_dir / "file.iso").unlink(missing_ok=True)
            (duplicate_dir / "file (1).iso").unlink(missing_ok=True)
            duplicate_dir.rmdir()
        assert looks_like_placeholder_filename(
            "download", "https://example.com/download?id=123"
        )
        from core import filename_from_content_disposition
        assert filename_from_content_disposition(
            "attachment; filename*=UTF-8''Ubuntu%2026.04.iso"
        ) == "Ubuntu 26.04.iso"
        db.conn.close()
        Path("/tmp/udownload-self-test.db").unlink(missing_ok=True)
        print("UDownload self-test: OK")
        return 0
    Adw.init()
    app = UDMApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
