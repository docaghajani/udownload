# UDM 1.2.1

UDM 1.2.1 is a maintenance release that fixes an intermittent SQLite concurrency
error in the authenticated Web UI introduced in 1.2.0.

## Fixed

- Web UI authentication-setting reads now use the existing
  `WebDownloadService` re-entrant lock.
- Concurrent `ThreadingHTTPServer` request threads no longer access the
  service-owned SQLite connection outside the Web UI service synchronization
  boundary.
- Fixes intermittent:

```text
sqlite3.InterfaceError: bad parameter or other API misuse
```

- Adds a concurrent authenticated Web UI stress test with 480 requests.

## Authentication behavior retained

The Web UI authentication design from 1.2.0 is unchanged:

- username/password configured in **UDM → Options**
- salted `PBKDF2-HMAC-SHA256` password hashes
- in-memory authenticated sessions
- `HttpOnly`, `SameSite=Strict` session cookies
- session expiry
- logout
- failed-login rate limiting
- CLI credential management

## Web UI CLI

```bash
udownload web auth --user admin
udownload web status
udownload web enable --port 8600
udownload web disable
```

## Validation performed for 1.2.1

- Python compilation checks
- built-in UDM self-test
- Web UI self-test
- JavaScript syntax checks when Node.js is available
- AppStream XML validation
- authenticated Web UI concurrency stress test
- 16 concurrent workers
- 30 authenticated requests per worker
- 480 authenticated requests total
- `git diff --check`
- staged diff validation

## Documentation

See:

- `README.md`
- `docs/WEB_UI.md`
- `docs/RELEASE_1.2.1.md`
