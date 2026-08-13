#!/usr/bin/python3
from __future__ import annotations

import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GROUP = "udownload-remote"
LIBEXEC_DIR = Path("/usr/local/libexec")
REMOTE_COMMAND_DST = LIBEXEC_DIR / "udownload-remote-command"
SSHD_DROPIN = Path("/etc/ssh/sshd_config.d/90-udownload-remote.conf")
SUDOERS_DROPIN = Path("/etc/sudoers.d/udownload-remote")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def emit(ok: bool, message: str) -> int:
    print(
        json.dumps(
            {"ok": bool(ok), "message": str(message)},
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(value))


def self_test() -> int:
    good = ["usrudm", "udm_user", "u1", "_udm"]
    bad = ["", "Root", "user name", "-udm", "a" * 40]

    if not all(valid_username(value) for value in good):
        return emit(False, "username validator rejected a valid value")
    if any(valid_username(value) for value in bad):
        return emit(False, "username validator accepted an invalid value")

    source = Path(__file__).with_name("remote_command.py")
    if not source.is_file():
        return emit(False, f"missing {source}")

    result = run(
        [sys.executable, str(source), "--self-test"],
        check=False,
    )
    if result.returncode != 0:
        return emit(False, result.stderr or result.stdout)

    return emit(True, "UDM remote-admin self-test: OK")


def require_binary(path_or_name: str, install_hint: str) -> str:
    if "/" in path_or_name:
        path = Path(path_or_name)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    else:
        resolved = shutil.which(path_or_name)
        if resolved:
            return resolved

    raise RuntimeError(install_hint)


def ensure_group() -> None:
    try:
        grp.getgrnam(GROUP)
    except KeyError:
        run(["/usr/sbin/groupadd", "--system", GROUP])


def ensure_remote_user(username: str, password: str) -> None:
    ensure_group()

    try:
        entry = pwd.getpwnam(username)
        groups = {
            group.gr_name
            for group in grp.getgrall()
            if username in group.gr_mem
        }
        primary = grp.getgrgid(entry.pw_gid).gr_name
        groups.add(primary)

        if GROUP not in groups:
            raise RuntimeError(
                f"Linux user '{username}' already exists and is not a UDM "
                "Remote user. Choose another username."
            )
    except KeyError:
        run(
            [
                "/usr/sbin/useradd",
                "--create-home",
                "--shell",
                "/bin/bash",
                "--groups",
                GROUP,
                username,
            ]
        )

    run(
        ["/usr/sbin/usermod", "--append", "--groups", GROUP, username]
    )

    run(
        ["/usr/sbin/chpasswd"],
        input_text=f"{username}:{password}\n",
    )


def install_remote_command() -> None:
    source = Path(__file__).with_name("remote_command.py")
    if not source.is_file():
        raise RuntimeError(f"Missing remote command source: {source}")

    LIBEXEC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, REMOTE_COMMAND_DST)
    os.chown(REMOTE_COMMAND_DST, 0, 0)
    os.chmod(REMOTE_COMMAND_DST, 0o755)

    result = run(
        [sys.executable, str(REMOTE_COMMAND_DST), "--self-test"],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Installed remote command failed self-test: "
            + (result.stderr or result.stdout)
        )


def write_atomic(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=f".{path.name}.",
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)

    try:
        os.chown(temporary, 0, 0)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_sudo(owner: str) -> None:
    content = (
        f'Defaults:%{GROUP} env_keep += "SSH_ORIGINAL_COMMAND"\n'
        f'%{GROUP} ALL=({owner}) NOPASSWD: {REMOTE_COMMAND_DST}\n'
    )

    write_atomic(SUDOERS_DROPIN, content, 0o440)

    visudo = require_binary(
        "/usr/sbin/visudo",
        "sudo/visudo is required for UDM Remote",
    )
    result = run([visudo, "-cf", str(SUDOERS_DROPIN)], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Invalid sudoers configuration: "
            + (result.stderr or result.stdout)
        )


def configure_sshd(owner: str) -> None:
    sshd = require_binary(
        "/usr/sbin/sshd",
        "OpenSSH Server is not installed. Install it with: "
        "sudo apt install openssh-server",
    )
    require_binary(
        "/usr/bin/sudo",
        "sudo is required for UDM Remote",
    )

    content = (
        f"Match Group {GROUP}\n"
        "    PasswordAuthentication yes\n"
        "    KbdInteractiveAuthentication yes\n"
        "    PubkeyAuthentication yes\n"
        "    AllowTcpForwarding no\n"
        "    AllowAgentForwarding no\n"
        "    X11Forwarding no\n"
        "    PermitTunnel no\n"
        "    PermitTTY no\n"
        f"    ForceCommand /usr/bin/sudo -n -H -u {owner} "
        f"{REMOTE_COMMAND_DST}\n"
        "\n"
        "Match all\n"
    )

    write_atomic(SSHD_DROPIN, content, 0o644)

    result = run([sshd, "-t"], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "sshd configuration check failed: "
            + (result.stderr or result.stdout)
        )

    systemctl = require_binary(
        "/usr/bin/systemctl",
        "systemd/systemctl is required for UDM Remote",
    )

    start = run(
        [systemctl, "enable", "--now", "ssh.service"],
        check=False,
    )
    if start.returncode != 0:
        fallback = run(
            [systemctl, "enable", "--now", "sshd.service"],
            check=False,
        )
        if fallback.returncode != 0:
            raise RuntimeError(
                "Could not start OpenSSH Server: "
                + (fallback.stderr or start.stderr)
            )

    reload_result = run(
        [systemctl, "reload", "ssh.service"],
        check=False,
    )
    if reload_result.returncode != 0:
        run(
            [systemctl, "reload", "sshd.service"],
            check=False,
        )


def configure(payload: dict) -> int:
    if os.geteuid() != 0:
        return emit(False, "Administrator privileges are required")

    username = str(payload.get("username", "") or "").strip()
    password = str(payload.get("password", "") or "")
    owner = str(payload.get("owner", "") or "").strip()

    if not valid_username(username):
        return emit(
            False,
            "Username must use lowercase letters, numbers, _ or - "
            "and start with a letter or _",
        )

    if len(password) < 8:
        return emit(False, "Password must be at least 8 characters")

    if not valid_username(owner):
        return emit(False, "Invalid desktop owner username")

    try:
        pwd.getpwnam(owner)
    except KeyError:
        return emit(False, f"Desktop owner '{owner}' does not exist")

    if owner == "root":
        return emit(False, "UDM Remote cannot target the root account")

    try:
        install_remote_command()
        configure_sudo(owner)
        configure_sshd(owner)
        ensure_remote_user(username, password)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return emit(False, detail)
    except Exception as exc:
        return emit(False, str(exc))

    return emit(
        True,
        f"Remote user '{username}' is ready. "
        "Password is stored by Linux as a system password hash, not by UDM.",
    )


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as exc:
        return emit(False, f"Invalid configuration request: {exc}")

    return configure(payload)


if __name__ == "__main__":
    raise SystemExit(main())
