<p align="center">
  <img src=".github/assets/udownload-hero.svg" alt="Ubuntu Download Manager" width="100%">
</p>

<p align="center">
  <a href="https://github.com/docaghajani/udownload/releases/latest"><img src="https://img.shields.io/github/v/release/docaghajani/udownload?style=flat-square&label=release&logo=github" alt="Latest release"></a>
  <a href="COPYING"><img src="https://img.shields.io/github/license/docaghajani/udownload?style=flat-square" alt="License"></a>
  <a href="https://launchpad.net/~iaghajani/+archive/ubuntu/udownload"><img src="https://img.shields.io/badge/Ubuntu-PPA-E95420?style=flat-square&logo=ubuntu&logoColor=white" alt="Ubuntu PPA"></a>
  <img src="https://img.shields.io/badge/GTK-4-4A86CF?style=flat-square&logo=gnome&logoColor=white" alt="GTK4">
  <img src="https://img.shields.io/badge/libadwaita-native-3584E4?style=flat-square" alt="libadwaita">
  <img src="https://img.shields.io/badge/engine-aria2-2EA44F?style=flat-square" alt="aria2">
</p>

<p align="center">
  <strong>Fast transfers · Native Linux UX · Reliable transfer control · Browser integration</strong>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-highlights">Highlights</a> •
  <a href="#-browser-integration">Browser Integration</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-packaging-status">Packaging</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

## Why UDM?

**UDM** is a native GTK4/libadwaita download manager for Linux powered by the
high-performance **aria2** transfer engine.

It is designed around a simple idea: advanced download management should feel like
a first-class Linux desktop experience — not a web page wrapped in a window.

<table>
<tr>
<td width="33%" valign="top">

### ⚡ Fast
Segmented and concurrent transfers powered by aria2.

</td>
<td width="33%" valign="top">

### 🧭 Native
GTK4 + libadwaita UI designed for the Linux desktop.

</td>
<td width="33%" valign="top">

### 🔗 Integrated
Chrome, Chromium and Firefox integration through Native Messaging.

</td>
</tr>
<tr>
<td width="33%" valign="top">

### ⏯ Reliable
Pause, resume and cancel behavior designed around live aria2 state.

</td>
<td width="33%" valign="top">

### 🕒 Organized
Queues, scheduling, categories, history and batch URL workflows.

</td>
<td width="33%" valign="top">

### 🧩 Open
Open-source, GPL-3.0-or-later and packaged for Debian/Ubuntu workflows.

</td>
</tr>
</table>

---

## ✨ Highlights

<table>
<tr>
<td valign="top" width="50%">

### Transfer control

- Segmented downloads
- Multiple concurrent downloads
- Pause / Resume / Cancel
- Resume existing partial files
- Duplicate aria2 GID protection
- Live progress dialog
- Download-complete workflow

</td>
<td valign="top" width="50%">

### Power-user workflow

- Download queues
- Scheduling
- Batch URL handling
- Import / export links
- Download history
- Categories
- Persistent UI preferences

</td>
</tr>
<tr>
<td valign="top" width="50%">

### Desktop integration

- Native GTK4 UI
- libadwaita UX
- Open file
- Open with
- Open folder
- Drag completed files to other apps

</td>
<td valign="top" width="50%">

### Browser integration

- Google Chrome
- Chromium
- Mozilla Firefox
- Native Messaging bridge
- Browser-to-desktop download handoff
- Native-host repair support

</td>
</tr>
</table>

---

## 🚀 Quick Start

### Ubuntu — PPA

```bash
sudo add-apt-repository ppa:iaghajani/udownload
sudo apt update
sudo apt install udownload
```

Launch the application:

```bash
udownload
```

Run the built-in self-test:

```bash
udownload --self-test
```

> Available Ubuntu series and current PPA builds are listed on
> [Launchpad](https://launchpad.net/~iaghajani/+archive/ubuntu/udownload).

---

## 🌐 Browser Integration

UDM can receive download requests directly from supported browsers through
a Native Messaging bridge.

| Browser | Integration |
|---|---|
| Google Chrome | ✅ Supported |
| Chromium | ✅ Supported |
| Mozilla Firefox | ✅ Supported |

Native Messaging host:

```text
com.ideveloper.udownload.native
```

Full setup guide:

**[Read the Browser Integration Guide →](docs/BROWSER_INTEGRATION.md)**

---

## 🧠 Architecture

```text
┌──────────────────────────────────────────────────────────┐
│                Chrome · Chromium · Firefox               │
└──────────────────────────┬───────────────────────────────┘
                           │ Native Messaging
                           ▼
┌──────────────────────────────────────────────────────────┐
│                         UDM                        │
│                 GTK4 + libadwaita desktop UI             │
│                                                          │
│  Queue · Scheduler · History · Categories · Dialogs      │
└──────────────────────────┬───────────────────────────────┘
                           │ aria2 RPC
                           ▼
┌──────────────────────────────────────────────────────────┐
│                           aria2                          │
│              High-performance transfer engine            │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
                        Downloads
```

The UI and transfer engine are intentionally separated. aria2 handles network
transfers while UDM manages desktop UX, state synchronization, scheduling,
browser communication and file actions.

---

## 🔐 Transfer Reliability

UDM adds transfer-state handling around aria2 to make desktop controls
predictable during real-world use.

- Pause and Cancel operate against the live transfer state.
- Partial data remains resumable.
- Resume continues the existing file instead of creating unnecessary duplicates.
- Duplicate aria2 GIDs targeting the same destination are detected and stopped.
- Transfer RPC work stays away from the GTK main thread so the UI remains responsive.

---

## 📦 Packaging Status

| Target | Status |
|---|---|
| Ubuntu PPA | 🟢 Available |
| Ubuntu 24.04 LTS (Noble) | 🟢 PPA build available |
| Ubuntu development series | 🟢 PPA build available |
| Ubuntu Universe | 🟠 Sponsorship requested |
| Debian | 🟠 Debian Mentors package available |

### Ubuntu

- **PPA:** [ppa:iaghajani/udownload](https://launchpad.net/~iaghajani/+archive/ubuntu/udownload)
- **Packaging request:** [Launchpad Bug #2163282](https://bugs.launchpad.net/ubuntu/+bug/2163282)

### Debian

- **Debian Mentors:** [mentors.debian.net/package/udownload](https://mentors.debian.net/package/udownload/)

---

## 🧪 Release & Packaging Checks

```text
✓ Python compilation checks
✓ Built-in UDM self-test
✓ desktop-file validation
✓ AppStream metadata validation
✓ Debian Lintian
✓ Clean Debian unstable sbuild
✓ Ubuntu Launchpad build farm
✓ APT installation from the public Launchpad PPA
```

---

## 🛠 Technology

<table>
<tr>
<td><strong>Desktop UI</strong></td><td>GTK4</td>
<td><strong>Linux UX</strong></td><td>libadwaita</td>
</tr>
<tr>
<td><strong>Transfer engine</strong></td><td>aria2</td>
<td><strong>Application</strong></td><td>Python 3</td>
</tr>
<tr>
<td><strong>Browser bridge</strong></td><td>Native Messaging</td>
<td><strong>Packaging</strong></td><td>Debian / Ubuntu</td>
</tr>
<tr>
<td><strong>License</strong></td><td>GPL-3.0-or-later</td>
<td><strong>App ID</strong></td><td><code>com.ideveloper.UDownload</code></td>
</tr>
</table>

---

## 📁 Repository Layout

```text
udownload/
├── bin/        Application launchers
├── browser/    Chrome / Chromium / Firefox integration
├── data/       Desktop files, icon and AppStream metadata
├── docs/       Documentation
├── src/        Application source
└── systemd/    Service integration
```

---

## 🤝 Contributing

Bug reports, feature requests, documentation improvements and code contributions
are welcome.

**[Open an Issue →](https://github.com/docaghajani/udownload/issues)**

```bash
git clone https://github.com/docaghajani/udownload.git
cd udownload
git checkout -b feature/my-improvement
```

Keep changes focused and submit them through a Pull Request.

---

## 📄 License

UDM is free and open-source software licensed under the
**GNU General Public License v3.0 or later**.

See [COPYING](COPYING) for the complete license text.

---

## 👨‍💻 Author

**AmirHossein Aghajani**
GitHub: [@docaghajani](https://github.com/docaghajani)
Email: `aghajani@dr.com`

---

<p align="center">
  <strong>UDM — Download better on Linux.</strong>
</p>

<p align="center">
  <a href="https://github.com/docaghajani/udownload/releases/latest">Latest Release</a> •
  <a href="https://github.com/docaghajani/udownload/issues">Report a Bug</a> •
  <a href="https://launchpad.net/~iaghajani/+archive/ubuntu/udownload">Ubuntu PPA</a>
</p>

<p align="center">
  ⭐ If UDM is useful to you, consider starring the repository.
</p>
