# Test Credentials

## App access
Gatekeeper is BYPASSED — no login. Landing on `/` redirects to `/creds` if no token, else `/home`.

## Kite Connect (Zerodha) — persisted
- API key:      79m7qb0mj6bzh9f8
- API secret:   w6ax073pxxa4trviwbwcsouebthrdnuy
- Access token: ytgmRcgtR61qCN5ZPYaHIGv4SXAz7ha5 (in kite_session_tokens table)
- Kite profile: Aniket Damodhar Deotale (HV7316) — probe passes.

## Trading mode
- Current: PAPER (dry-run, `[safety] live_trading = false`)
- Paper capital: ₹500,000 (`[paper_trading] capital = 500000`)
- Toggle via /creds page.

## Update creds
Open /creds. Blank fields keep old values. `request_token` auto-exchanges.
