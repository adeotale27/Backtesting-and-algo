# Security

!!! warning "The dashboard controls a brokerage account"
    Anyone who can reach the web UI and log in can place and cancel real
    orders. Treat access to this app like access to your broker account.

## Deployment posture

- The app binds to **`127.0.0.1:5010` only**. This is deliberate: out of
  the box it is unreachable from other machines.
- To access it remotely, put a **TLS-terminating reverse proxy** (nginx,
  Caddy) in front and keep the app itself on localhost. Never change the
  bind address to `0.0.0.0`.
- With HTTPS in front, set `FLASK_COOKIE_SECURE=true` in the environment so
  session cookies are marked `Secure`.

Example Caddy config (automatic HTTPS):

```
trading.example.com {
    reverse_proxy 127.0.0.1:5010
}
```

Example nginx location block:

```nginx
server {
    listen 443 ssl;
    server_name trading.example.com;
    # ssl_certificate ...; ssl_certificate_key ...;
    location / {
        proxy_pass http://127.0.0.1:5010;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Host $host;
    }
}
```

## Built-in protections

- **Session auth** on every route (`[gatekeeper]` credentials, hashed
  password supported); JSON APIs return 401 instead of redirects.
- **Login lockout**: 5 failed attempts per IP → 15-minute lockout.
- **CSRF protection** (Flask-WTF): all state-changing requests require a
  session-bound token; browser JS attaches it automatically via a fetch/XHR
  shim. Cookies are `HttpOnly` + `SameSite=Lax`.
- **Rate limiting** (flask-limiter) on order-affecting endpoints — starts
  at 30/min, stops deliberately generous at 120/min so an emergency stop is
  never blocked.
- **Input validation** on trading parameters (symbol allowlist regex,
  numeric range checks) before anything reaches a filename or subprocess.
- **Dry-run default**: `[safety] live_trading = false` on fresh installs;
  the header badge always shows the current mode.

## Known limitations (accepted trade-offs for self-hosting)

- **Single shared credential.** There are no per-user accounts, roles, or
  audit trails — the gatekeeper login is one username/password for the
  whole dashboard.
- **In-memory lockout and rate-limit state.** Both reset on restart and do
  not synchronize across processes. The supported deployment is the
  single-process `waitress` server that `python flask_app.py` starts.
- **Plaintext credentials on disk.** `configfile.ini` holds your Kite API
  key/secret, and the day's access token is cached in `instruments.db` for
  restart recovery. Anyone with filesystem access can trade on your
  account. Restrict file permissions and disk access accordingly.
- **No CSRF on the OAuth callback.** The Kite OAuth `/login` callback is
  necessarily exempt from session auth; it only exchanges the request token
  Zerodha redirects with.

## Reporting vulnerabilities

Use GitHub's private vulnerability reporting — see
[SECURITY.md](https://github.com/Raahi-Bhushan/ui-trading-system/blob/main/SECURITY.md).
Please don't open public issues for security problems.
