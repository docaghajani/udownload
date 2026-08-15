# UDM Web UI

UDM 1.0.19 adds an optional Web UI for controlling the same download queue and aria2 engine used by the desktop application.

## Enable

Open:

```text
UDM → Options
```

Enable:

```text
Enable Web UI (trusted LAN / VPN)
```

Choose the listening port (default `8600`) and save.

Then open the server from another device on the same trusted network:

```text
http://SERVER_IP:8600/
```

## Command line

The Web UI can also be enabled, disabled and inspected from the terminal:

```bash
udownload web status
udownload web enable
udownload web enable --port 8600
udownload web disable
```

If the desktop application is already running, it detects the changed setting and
starts, stops or restarts the Web UI within a few seconds. Otherwise the setting is
saved and takes effect on the next UDM start.

## Features

- Add a URL and start it immediately.
- Add a URL to the queue.
- Schedule downloads.
- View live progress, speed, status and ETA.
- Pause, resume and start downloads.
- Remove downloads from the UDM list.
- Search and filter the shared UDM queue.

The Web UI uses the same UDM database and aria2 engine as the desktop app.

## Security

The 1.0.19 Web UI is intended for a **trusted LAN or VPN**.

Do **not** forward the Web UI port directly from the public Internet. This release does not provide a public-facing login screen.

If remote access over the Internet is needed, use a VPN or another trusted private network layer instead of exposing the Web UI port directly.

## Disable

Open UDM Options, turn off `Enable Web UI (trusted LAN / VPN)`, and save.
