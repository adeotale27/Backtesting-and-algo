# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Instead, use GitHub's **[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**
on this repository ("Security" tab → "Report a vulnerability").

You can expect an acknowledgement within **7 days**. Please include steps to
reproduce and an assessment of impact if you can.

## Scope

This is **self-hosted** software — there is no hosted service, no central
server, and no data collected by the maintainers. The threat model is:

- an attacker reaching your dashboard (protect it: the app binds to
  `127.0.0.1` by default; put a TLS reverse proxy + strong credentials in
  front before exposing it),
- vulnerabilities in the code that could let an authenticated or
  unauthenticated user place/cancel orders, read credentials, or execute
  code.

## Known limitations (by design, documented for self-hosters)

- Dashboard auth is a **single shared username/password**
  (`[gatekeeper]` in `configfile.ini`) — there are no per-user accounts.
- Login lockout and API rate-limit state are **in-memory**: they reset on
  restart and don't synchronize across multiple worker processes. The
  supported deployment is the single-process `waitress` server the app
  starts by default.
- Your Zerodha API credentials and access token live in plaintext files
  (`configfile.ini`, instrument cache DB) on the machine that runs the app.
  Anyone with filesystem access can trade on your account — treat the host
  like you treat your broker password.
