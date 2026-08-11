#!/usr/bin/python3
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import json
import mimetypes
import os
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
    resolve_remote_file,
    safe_filename,
    unique_path,
)


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
        self.set_default_size(620, 390)
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

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        area.append(grid)

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
        self.category_combo = Gtk.DropDown.new_from_strings(
            ["General", "Compressed", "Documents", "Music", "Programs", "Video", "Images"]
        )
        detected = initial.get("category") or category_for_filename(filename)
        categories = ["General", "Compressed", "Documents", "Music", "Programs", "Video", "Images"]
        with contextlib.suppress(ValueError):
            self.category_combo.set_selected(categories.index(detected))
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

        labels = ["URL", "Save as", "Folder", "Category", "Description", "Schedule"]
        widgets = [
            self.url_entry,
            self.file_entry,
            self.dir_box,
            self.category_combo,
            self.description_entry,
            self.schedule_button,
        ]
        for idx, (label, widget) in enumerate(zip(labels, widgets)):
            lab = Gtk.Label(label=label, xalign=1)
            grid.attach(lab, 0, idx, 1, 1)
            grid.attach(widget, 1, idx, 1, 1)

        note = Gtk.Label(
            label="Authenticated downloads can receive the current page referrer and cookies from the browser extension.",
            wrap=True,
            xalign=0,
        )
        note.add_css_class("dim-label")
        grid.attach(note, 1, len(labels), 1, 1)

        self.resolve_label = Gtk.Label(
            label="",
            wrap=True,
            xalign=0,
        )
        self.resolve_label.add_css_class("dim-label")
        grid.attach(self.resolve_label, 1, len(labels) + 1, 1, 1)

        self.connect("response", self._on_response)
        self._refresh_unique_filename()
        if self.url_entry.get_text().strip():
            self._schedule_resolve(delay_ms=50)
        else:
            self._prefill_url_from_clipboard()

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
            categories = [
                "General", "Compressed", "Documents", "Music",
                "Programs", "Video", "Images",
            ]
            detected = category_for_filename(info.filename)
            with contextlib.suppress(ValueError):
                self.category_combo.set_selected(categories.index(detected))

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
        categories = ["General", "Compressed", "Documents", "Music", "Programs", "Video", "Images"]
        category = categories[self.category_combo.get_selected()]
        start_time = None
        if response == Gtk.ResponseType.APPLY:
            chosen = self.schedule_time or self._default_schedule_time()
            start_time = chosen.isoformat(timespec="seconds")
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
        self.set_default_size(560, 420)
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
        rows = [
            ("Default download folder", self.dir_box),
            ("Simultaneous downloads", self.concurrent),
            ("Connections per download", self.connections),
            ("Global speed limit (e.g. 2M, 0=unlimited)", self.speed),
            ("Show file-info dialog for browser downloads", self.prompt_box),
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
            }
            self.on_save(values)
        self.destroy()


class BrowserIntegrationDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Browser Integration", transient_for=parent, modal=True)
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
        chrome.connect("clicked", lambda *_: launch_browser_extensions_page("chrome"))
        firefox.connect("clicked", lambda *_: launch_browser_extensions_page("firefox"))
        folder.connect("clicked", lambda *_: subprocess.Popen(["xdg-open", "/usr/share/udownload/browser"]))
        buttons.append(chrome)
        buttons.append(firefox)
        buttons.append(folder)
        area.append(buttons)
        self.connect("response", lambda *_: self.destroy())


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

        values = [
            ("Type", mime_text),
            ("Status", item.status_text),
            ("Size", size_text),
            ("Progress", item.progress_text or "—"),
            ("Time left", item.eta_text or "—"),
            ("Transfer rate", item.speed_text or "—"),
            ("Save to", str(path)),
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
        self.current_category = str(self.settings.get("current_category", "All Downloads"))
        self.search_text = ""
        self.refresh_busy = False
        self.scroll_hold_until = 0.0
        self.column_width_save_source = 0
        self._restoring_layout = False
        self.set_default_size(int(self.settings.get("window_width", 1180)), int(self.settings.get("window_height", 720)))
        self.set_icon_name("udownload")
        install_native_manifests()
        self._build_ui()
        self.connect("close-request", self._on_close)
        GLib.idle_add(self._start_engine)
        GLib.timeout_add_seconds(2, self.refresh)
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
            version="1.0.10",
            developer_name="امیرحسین آقاجانی",
            developers=["امیرحسین آقاجانی <aghajani@dr.com>"],
            comments="A native Ubuntu download manager with segmented downloads, queues, scheduling and browser integration.",
            website="https://amirhossein.dev",
            copyright="© 2026 امیرحسین آقاجانی",
            license="GNU General Public License v3.0 or later (GPL-3.0-or-later)",
        )
        about.present()

    def _column_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label(xalign=0, ellipsize=3)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(7)
        label.set_margin_bottom(7)
        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect("pressed", self._on_row_right_click, list_item)
        label.add_controller(right_click)
        list_item.set_child(label)

    @staticmethod
    def _column_bind(_factory, list_item: Gtk.ListItem, getter) -> None:
        item = list_item.get_item()
        label = list_item.get_child()
        label.set_text(str(getter(item) or ""))
        label.set_tooltip_text(str(getter(item) or ""))

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
        if isinstance(item, DownloadObject):
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
            with contextlib.suppress(Exception):
                self.aria.pause(item.gid)
        self.refresh()

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
        widget = gesture.get_widget()
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
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
            if self.aria.ping():
                for state in self.aria.tell_all():
                    self.db.update_state(state.get("gid", ""), state)
            self.load_rows(skip_if_scrolling=True)
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
        self.load_rows()

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

    def run_scheduler(self) -> bool:
        for row in self.db.scheduled_due():
            self._start_db_download(int(row["id"]))
        return GLib.SOURCE_CONTINUE

    def pause_selected(self) -> None:
        item = self._selected()
        if item and item.gid:
            with contextlib.suppress(Exception):
                self.aria.pause(item.gid)
            self.refresh()

    def resume_selected(self) -> None:
        item = self._selected()
        if not item:
            return
        if item.gid:
            with contextlib.suppress(Exception):
                self.aria.resume(item.gid)
        else:
            self._start_db_download(item.db_id)
        self.refresh()

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
        import re
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

    def show_scheduled(self) -> None:
        self.current_category = "Unfinished"
        self.load_rows()

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
