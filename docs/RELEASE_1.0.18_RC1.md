# UDM 1.0.18 RC1

UDM 1.0.18 RC1 introduces command-line downloads and secure SSH-based Remote control,
plus a set of UI and download-list improvements.

## Remote control

- Added a **Remote** configuration dialog.
- Added dedicated Remote users for authenticated SSH access.
- Remote users are restricted to UDM download commands instead of receiving a normal shell.
- The Remote password is handled by Linux and is not stored in the UDM settings database.
- Disabled SSH TCP forwarding, agent forwarding, X11, tunnels and TTY for the restricted Remote group.
- Default external Remote port is **8347**.
- Router/NAT setup uses `8347 → target LAN IP:22`.
- Remote commands can be sent from another UDM installation.
- Remote commands can also be sent directly with OpenSSH.
- Windows OpenSSH and PuTTY/Plink examples are documented.
- Password entry is handled by the SSH client with terminal echo disabled.
- SSH private-key authentication remains supported.

## Command line

New local CLI commands:

```bash
udownload add "https://example.com/file.iso"
udownload add "https://example.com/file.iso" --now
udownload add "https://example.com/file.iso" --path "$HOME/Downloads"
udownload add "https://example.com/file.iso" --at "2026-08-13 22:30"
```

New Remote command:

```bash
udownload remote "https://example.com/file.iso" \
  --server 23.53.215.63 \
  --user usrudm \
  --now
```

The Remote port defaults to `8347`.

## Filename resolution

CLI additions now resolve a usable filename before inserting the download into the
database. If neither the server nor URL provides a usable filename, UDM fails instead
of creating a generic `download` entry.

## Main-window interaction

- Active download rows continue refreshing while a context menu is open.
- Download-list refresh interval is reduced for more responsive live information.
- Added a visible Remote action in the main toolbar and application menu.

## Version

Application, AppStream metadata, browser integration manifests and native host are
updated to version **1.0.18**.

## Testing

This is an **RC1 / pre-release** intended for testing before the final 1.0.18 release.
Please report regressions or Remote/CLI issues through GitHub Issues.
