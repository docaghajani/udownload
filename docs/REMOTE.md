# UDM Remote authentication

UDM Remote supports two client methods with the same restricted SSH account.

## 1. UDM installed on the client

The target server user is required. Port 8347 is the default:

    udownload remote "https://example.com/file.iso" \
      --server 23.53.215.63 \
      --user usrudm \
      --now

When `--key` is not supplied, UDM launches the system OpenSSH client in
password/keyboard-interactive mode. OpenSSH asks for the password directly in
the terminal and does not echo the typed password.

To use a private key instead:

    udownload remote "https://example.com/file.iso" \
      --server 23.53.215.63 \
      --user usrudm \
      --key ~/.ssh/id_ed25519 \
      --now

## 2. Any SSH client

Linux/macOS/OpenSSH:

    ssh -p 8347 usrudm@23.53.215.63 \
      'udownload add "https://example.com/file.iso" --now'

Windows Command Prompt with OpenSSH:

    ssh -p 8347 usrudm@23.53.215.63 "udownload add \"https://example.com/file.iso\" --now"

PuTTY/Plink:

    plink.exe -P 8347 -l usrudm 23.53.215.63 "udownload add \"https://example.com/file.iso\" --now"

The SSH client asks for the password without displaying it.

## Server-side user setup

Open UDM > Remote and create a dedicated username/password.

UDM asks for administrator authorization and then:

- creates a Linux account in the `udownload-remote` group
- stores the password through the normal Linux system password mechanism
- restricts SSH sessions to UDM `add` commands only
- disables TCP forwarding, agent forwarding, X11, tunnels and TTY for this group
- routes accepted download commands into the desktop owner's UDM profile
- enables and validates OpenSSH Server configuration

The Remote password is never stored in the UDM settings database.

## Router

Default external Remote port: `8347`.

Configure:

    Router TCP 8347 -> target computer LAN IP : 22

If you choose another external port in the Remote dialog, forward that port to
internal SSH port 22.
