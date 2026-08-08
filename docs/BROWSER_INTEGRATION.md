# UDownload Browser Integration

UDownload includes browser integration sources for Google Chrome, Chromium and Mozilla Firefox.

Before installing the browser extension, install and run UDownload at least once. This creates the Native Messaging configuration required by the browsers.

## Google Chrome / Chromium

1. Install UDownload.
2. Start UDownload at least once.
3. Open:

   chrome://extensions/

4. Enable **Developer mode**.
5. Click **Load unpacked**.
6. Select:

   /usr/share/udownload/browser/chrome

The UDownload extension will then be able to send downloads and links to the desktop application.

## Mozilla Firefox

1. Install UDownload.
2. Start UDownload at least once.
3. Open:

   about:debugging#/runtime/this-firefox

4. Click **Load Temporary Add-on**.
5. Select:

   /usr/share/udownload/browser/firefox/manifest.json

This development installation remains active until Firefox is restarted.

For permanent installation on normal Firefox releases, the UDownload Firefox extension will be distributed as a Mozilla-signed XPI.

## Installed browser files

Chrome / Chromium:

    /usr/share/udownload/browser/chrome/

Firefox:

    /usr/share/udownload/browser/firefox/

Native Messaging host:

    com.ideveloper.udownload.native
