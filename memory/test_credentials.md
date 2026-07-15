# Test Credentials

## App access (gatekeeper BYPASSED)
The Flask gatekeeper login has been bypassed. Every session is auto-authenticated.
Landing URL redirects to `/creds` if no Kite token is stored, otherwise to `/home`.

## Kite Connect (Zerodha) - already saved
- API key:      79m7qb0mj6bzh9f8
- API secret:   w6ax073pxxa4trviwbwcsouebthrdnuy
- Access token: ytgmRcgtR61qCN5ZPYaHIGv4SXAz7ha5   (persisted in instruments.db → kite_session_tokens)
- Kite profile verified: Aniket Damodhar Deotale (HV7316)

## Update creds any time
Open `/creds` in browser. Blank fields keep old values. `request_token` field auto-exchanges to a fresh access token.

## Safety
`[safety] live_trading = false` in /app/configfile.ini — DRY-RUN mode, no real orders sent.
