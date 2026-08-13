# UDM 1.0.18

UDM 1.0.18 adds command-line download control, secure SSH-based Remote downloads,
improved filename resolution, and smoother live list interaction.

## Remote downloads

UDM can now receive download commands from another computer over SSH.

- Added a **Remote** setup dialog.
- Added dedicated Remote usernames and passwords.
- Remote accounts are restricted to UDM download commands instead of receiving a normal interactive shell.
- Remote passwords use the normal Linux account password mechanism and are not stored in the UDM settings database.
- SSH TCP forwarding, agent forwarding, X11, tunnels and TTY are disabled for the restricted Remote group.
- Default external Remote port is **8347**.
- Typical router/NAT setup is `TCP 8347 → target LAN IP:22`.
- Commands can be sent from another UDM installation.
- Commands can also be sent directly with OpenSSH.
- Windows OpenSSH and PuTTY/Plink are supported.
- Password input is handled by SSH with terminal echo disabled.
- SSH private-key authentication is supported.

Example from another UDM installation:

```bash
udownload remote "https://example.com/file.iso" \
  --server 23.53.215.63 \
  --user usrudm \
  --now
```

Example using OpenSSH directly:

```bash
ssh -p 8347 usrudm@23.53.215.63 \
  'udownload add "https://example.com/file.iso" --now'
```

## Command line

Downloads can now be added without opening the main GUI.

Queue a download:

```bash
udownload add "https://example.com/file.iso"
```

Start immediately:

```bash
udownload add "https://example.com/file.iso" --now
```

Choose a destination:

```bash
udownload add "https://example.com/file.iso" \
  --path "$HOME/Downloads/ISO" \
  --now
```

Schedule a download:

```bash
udownload add "https://example.com/file.iso" \
  --at "2026-08-13 22:30"
```

## Filename resolution

CLI and Remote additions now wait for a usable filename before inserting the
download into the database. UDM no longer silently creates a generic `download`
entry when the file name cannot be resolved.

## Main window

- Active download rows continue updating while their context menu remains open.
- The main list refreshes more frequently for smoother live status information.
- Remote is available from the toolbar and application menu.

## Version metadata

Application, AppStream metadata, browser integration manifests and native host
metadata are updated to **1.0.18**.

## Documentation

The README now includes:

- local CLI examples
- Remote server setup
- router/NAT instructions
- UDM-to-UDM Remote commands
- direct OpenSSH commands
- Windows OpenSSH examples
- PuTTY/Plink examples
- password and SSH-key authentication details

Thank you to everyone who tested the 1.0.18 release candidate.
