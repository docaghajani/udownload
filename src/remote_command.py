#!/usr/bin/python3
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path


def fail(message: str, code: int = 2) -> int:
    print(f"UDM Remote: {message}", file=sys.stderr)
    return code


def find_udownload() -> str | None:
    for candidate in (
        "/usr/local/bin/udownload",
        "/usr/bin/udownload",
    ):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("udownload")


def parse_original_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid command quoting: {exc}") from exc

    if len(parts) < 2:
        raise ValueError(
            'Allowed command: udownload add "URL" [--now | --at TIME] [--path PATH]'
        )

    binary = Path(parts[0]).name
    if binary not in {"udownload", "udm"} or parts[1] != "add":
        raise ValueError(
            "This SSH account is restricted to the UDM 'add' command"
        )

    args = parts[2:]
    output: list[str] = ["add"]
    url: str | None = None
    index = 0
    mode_seen = False

    while index < len(args):
        value = args[index]

        if value == "--now":
            if mode_seen:
                raise ValueError("--now and --at cannot be used together")
            mode_seen = True
            output.append("--now")
            index += 1
            continue

        if value in {"--at", "--path", "--link"}:
            if index + 1 >= len(args):
                raise ValueError(f"{value} requires a value")
            option_value = args[index + 1]

            if value == "--at":
                if mode_seen:
                    raise ValueError("--now and --at cannot be used together")
                mode_seen = True

            if value == "--link":
                if url is not None:
                    raise ValueError("Use either a positional URL or --link, not both")
                url = option_value
            else:
                output.extend([value, option_value])

            index += 2
            continue

        if value.startswith("-"):
            raise ValueError(f"Unsupported option: {value}")

        if url is not None:
            raise ValueError("Only one download URL is allowed")
        url = value
        index += 1

    if not url:
        raise ValueError("A download URL is required")

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https", "ftp"}:
        raise ValueError("URL must start with http://, https:// or ftp://")

    return ["add", url] + output[1:]


def self_test() -> int:
    tests = [
        (
            'udownload add "https://example.com/a.iso" --now',
            ["add", "https://example.com/a.iso", "--now"],
        ),
        (
            'udownload add --link "https://example.com/a.iso" --path "/home/a/Downloads"',
            ["add", "https://example.com/a.iso", "--path", "/home/a/Downloads"],
        ),
    ]

    for command, expected in tests:
        actual = parse_original_command(command)
        if actual != expected:
            print(f"FAILED: {command}\n{actual!r} != {expected!r}", file=sys.stderr)
            return 1

    print("UDM remote-command self-test: OK")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    original = str(os.environ.get("SSH_ORIGINAL_COMMAND", "") or "").strip()
    if not original:
        return fail(
            'This is a restricted UDM Remote account.\n'
            'Example: udownload add "https://example.com/file.iso" --now'
        )

    try:
        args = parse_original_command(original)
    except ValueError as exc:
        return fail(str(exc))

    binary = find_udownload()
    if not binary:
        return fail("udownload is not installed on the target machine", 127)

    result = subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
