# UDM 1.2.0

UDM 1.2.0 adds password-protected Web UI access with configurable credentials,
secure password hashing, authenticated browser sessions, logout, session expiry,
login rate limiting, and CLI credential management.

## Web UI authentication

The browser interface now requires a username and password before access to the
download dashboard or API is granted.

Credentials are configured in:

```text
UDM → Options
```

New settings:

```text
Enable Web UI
Web UI port
Web UI username
Web UI password
```

The Web UI password is **not stored in plaintext**. UDM stores a per-password
salted `PBKDF2-HMAC-SHA256` hash.

## Login behavior

Opening the Web UI root address always displays the sign-in page:

```text
http://SERVER_IP:8600/
```

After successful authentication, the browser is redirected to the protected
`/app` dashboard.

Authenticated sessions:

- use cryptographically random session tokens
- are stored only in UDM server memory
- use an `HttpOnly` browser-session cookie
- use `SameSite=Strict`
- expire after inactivity
- are invalidated when the configured credentials change
- are invalidated when UDM restarts

The Web UI also includes an explicit **Log out** action.

## Login protection

Repeated failed sign-in attempts are rate-limited.

Existing same-origin and custom-header CSRF protections remain active in
addition to username/password authentication.

## Web UI CLI

Credentials can be configured from the terminal without placing the password in
shell history or process arguments:

```bash
udownload web auth --user admin
```

UDM securely prompts for the password and confirmation.

Then manage the Web UI with:

```bash
udownload web status
udownload web enable --port 8600
udownload web disable
```

## Existing Web UI capabilities retained

- Add a URL and start immediately.
- Add a URL to the queue.
- Schedule downloads.
- Search and filter downloads.
- View live progress, speed, ETA and status.
- Pause, resume, start and remove downloads.
- Pause/resume the full queue.
- Clear completed entries.
- Share the same UDM database and aria2 engine as the desktop application.
- Keep the desktop UDM scheduler as the single scheduler owner.

## Security note

The built-in Web UI still serves plain HTTP.

For a trusted LAN or VPN this can be appropriate. For access beyond a trusted
private network, use an HTTPS reverse proxy or VPN rather than directly exposing
the built-in Web UI port to the public Internet.

## Local source commands

Check version:

```bash
cd ~/Projects/udownload
python3 src/udownload.py --version
```

Configure credentials:

```bash
python3 src/udownload.py web auth --user admin
```

Enable the Web UI:

```bash
python3 src/udownload.py web enable --port 8600
```

Run UDM from the source checkout:

```bash
python3 src/udownload.py
```

## Validation performed for 1.2.0

- Python compilation
- UDM built-in self-test
- Web UI self-test
- password hash verification
- JavaScript syntax checks
- Web auth integration test
  - root login page
  - unauthenticated API rejection
  - invalid-password rejection
  - successful login
  - session cookie creation
  - protected `/app`
  - authenticated API access
  - root still showing login
  - logout
  - session invalidation
- AppStream XML validation
- `git diff --check`
- staged diff validation

## Documentation

See:

- `README.md`
- `docs/WEB_UI.md`
- `docs/RELEASE_1.2.0.md`
