# UDM 1.0.19

UDM 1.0.19 introduces an optional browser-based **Web UI** for controlling the
same download queue and aria2 transfer engine used by the native GTK4 desktop
application.

This release also adds complete command-line control for enabling, disabling,
inspecting and configuring the Web UI.

## Highlights

### Web UI

- Optional browser-based control panel for UDM.
- Enable or disable it from **UDM → Options**.
- Configurable listening port; the default is **8600**.
- Open it from another trusted device with:

```text
http://SERVER_IP:8600/
```

- Add a URL and start it immediately.
- Add a URL to the queue without starting it.
- Schedule downloads.
- Search and filter the shared download list.
- View live progress, status, transfer rate and ETA.
- Pause, resume, start and remove queue entries.
- Uses the same UDM database and aria2 engine as the desktop application.
- Keeps the existing UDM scheduler as the single scheduler owner.

### Web UI command line

The Web UI can now be controlled without opening the Options dialog:

```bash
# Show saved Web UI status and port
udownload web status

# Enable with the currently saved/default port
udownload web enable

# Enable and set a port
udownload web enable --port 8600

# Disable
udownload web disable
```

When the desktop application is already running, Web UI setting changes are
detected and applied within a few seconds. If UDM is not running, the setting is
saved and takes effect the next time UDM starts.

### Security

The Web UI in UDM 1.0.19 is intended for a **trusted LAN or VPN**.

This release does **not** include a public-facing Web UI login screen. Do not
forward the Web UI port directly from the public Internet. For Internet access,
use a trusted VPN/private-network layer.

The existing SSH-based UDM Remote feature remains the recommended mechanism for
restricted remote download commands over SSH.

## Existing capabilities retained

UDM 1.0.19 keeps the functionality introduced in previous releases, including:

- aria2-powered segmented downloads
- pause, resume and cancel
- resumable partial downloads
- queue management
- one-time and daily scheduling
- download history and categories
- local CLI download control
- SSH-based Remote downloads
- restricted Remote users
- SSH key and password authentication
- Chrome, Chromium and Firefox Native Messaging integration
- live progress dialogs
- download-complete workflow
- drag completed files to other applications
- filename resolution before CLI/Remote insertion
- duplicate aria2 GID/path protection

## Local CLI examples

Add a URL to the queue:

```bash
udownload add "https://example.com/file.iso"
```

Start immediately:

```bash
udownload add "https://example.com/file.iso" --now
```

Schedule:

```bash
udownload add "https://example.com/file.iso" --at "2026-08-15 22:30"
```

## SSH Remote example

```bash
udownload remote "https://example.com/file.iso" \
  --server 23.53.215.63 \
  --user usrudm \
  --now
```

## Validation performed for 1.0.19

- Python compilation checks
- built-in UDM self-test
- Web UI self-test
- isolated Web UI CLI enable/status/disable tests
- invalid Web UI port validation
- JavaScript syntax check
- `git diff --check`

## Documentation

The README and Web UI documentation include:

- UDM 1.0.19 overview
- Web UI setup
- configurable port instructions
- browser access example
- CLI enable/disable/status commands
- trusted LAN/VPN security guidance

See:

- `README.md`
- `docs/WEB_UI.md`

## Source checkout

```bash
git clone --branch v1.0.19 --depth 1 \
  https://github.com/docaghajani/udownload.git \
  udownload-1.0.19
```

Then:

```bash
cd udownload-1.0.19
python3 src/web_server.py --self-test
python3 src/udownload.py --self-test
python3 src/udownload.py --version
```

Expected version:

```text
UDM 1.0.19
```

Thank you to everyone testing and improving UDM.
