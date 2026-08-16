# UDM Web UI — 1.2.0

UDM 1.2.0 protects the browser interface with a username and password.

## Configure from UDM Options

Open:

```text
UDM → Options
```

Set:

```text
Enable Web UI: ON
Web UI port: 8600
Web UI username: your username
Web UI password: your password
```

The password is never saved as plaintext. UDM stores a salted PBKDF2-HMAC-SHA256
password hash in the normal UDM settings database.

If a password is already configured, leave the password box empty to keep it.
Entering a new password replaces the existing password hash.

## Sign in

Open:

```text
http://SERVER_IP:8600/
```

The root page always displays the UDM sign-in screen. After a successful sign-in,
UDM creates an in-memory authenticated session and redirects to `/app`.

There is no “remember me” option. The authentication cookie is a browser-session
cookie (`HttpOnly`, `SameSite=Strict`) and server-side sessions are kept only in
memory. Restarting UDM invalidates all Web UI sessions.

A session also expires after inactivity and the Web UI redirects back to the login
screen when authentication is no longer valid.

## Command line

Show current state:

```bash
udownload web status
```

Configure or change Web UI credentials securely:

```bash
udownload web auth --user admin
```

UDM prompts for the password twice without putting the password in shell history or
process arguments.

Enable:

```bash
udownload web enable --port 8600
```

Disable:

```bash
udownload web disable
```

## SQLite request threading — 1.2.1

UDM 1.2.1 routes Web UI authentication-setting reads through the same
`WebDownloadService` lock used by normal Web UI database operations.

This fixes the intermittent:

```text
sqlite3.InterfaceError: bad parameter or other API misuse
```

seen under concurrent authenticated HTTP requests.

## Security notes

- Use a strong password of at least 8 characters.
- Failed sign-in attempts are rate-limited.
- Password verification uses a salted PBKDF2-HMAC-SHA256 hash.
- Sessions use cryptographically random tokens.
- API requests still use same-origin/CSRF checks in addition to authentication.
- The built-in Web UI still uses plain HTTP. For access beyond a trusted LAN/VPN,
  put it behind a trusted VPN or an HTTPS reverse proxy rather than directly
  forwarding the Web UI port to the public Internet.
