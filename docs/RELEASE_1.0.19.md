# UDM 1.0.19

Release date: 2026-08-15

## Highlights

- Add an optional browser-based Web UI.
- Add `Enable Web UI (trusted LAN / VPN)` to UDM Options.
- Add a configurable Web UI listening port, defaulting to `8600`.
- Open the Web UI from another trusted LAN/VPN device using `http://SERVER_IP:PORT/`.
- Share the existing UDM download database and aria2 transfer engine between desktop and web views.
- Add live download progress, speed, ETA and status updates in the browser.
- Add browser controls for add, queue, schedule, pause, resume, start and remove.
- Keep scheduled-download ownership with the main UDM scheduler to avoid a duplicate scheduler loop.
- Add Web UI self-test coverage.

## Security

The Web UI in 1.0.19 is designed for trusted LAN/VPN use. Do not expose its port directly to the public Internet; this release does not include a public-facing login screen.

## Existing features retained

UDM 1.0.19 retains the local CLI, SSH Remote control, browser Native Messaging integration, scheduler, queues, download progress dialogs and aria2-backed transfer handling from 1.0.18.
