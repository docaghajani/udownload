#!/usr/bin/python3
from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys


def read_message() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) != 4:
        return None
    length = struct.unpack("<I", raw)[0]
    if length <= 0 or length > 16 * 1024 * 1024:
        return None
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def send_message(message: dict) -> None:
    encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> int:
    message = read_message()
    if not message:
        send_message({"ok": False, "error": "Invalid native message"})
        return 1
    if message.get("action") == "ping":
        send_message({"ok": True, "version": "1.0.18"})
        return 0
    payload = base64.urlsafe_b64encode(json.dumps(message, ensure_ascii=False).encode()).decode()
    try:
        subprocess.Popen(
            ["/usr/bin/udownload", "--browser-message-b64", payload],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        send_message({"ok": True})
        return 0
    except Exception as exc:
        send_message({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
