"""FastAPI proxy occupying supervisor's `backend` slot on :8001.

The real dashboard is a Flask app served by `waitress` on :3000. Emergent's
ingress routes any URL prefixed with `/api` to :8001, but the Flask app has
many `/api/*` endpoints (e.g. `/api/nifty_positions`, `/api-monitor`) that
must not be shadowed by this stub. To make them reachable via the preview
URL, this FastAPI process transparently reverse-proxies everything to the
Flask app on `localhost:3000`.
"""
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

FLASK_URL = "http://localhost:3000"

app = FastAPI(title="ui-trading-system backend proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "note": "proxy to Flask :3000"}


_client = httpx.AsyncClient(base_url=FLASK_URL, timeout=60.0, follow_redirects=False)


# Catch-all reverse proxy. Any request not matched by the routes above is
# forwarded to the Flask backend on :3000 with the original method, headers,
# body and query string.
@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(full_path: str, request: Request):
    # Strip hop-by-hop headers that must not be forwarded.
    _hop = {
        "host", "content-length", "connection", "keep-alive",
        "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade",
    }
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _hop}

    body = await request.body()
    url = "/" + full_path
    if request.url.query:
        url = f"{url}?{request.url.query}"

    upstream = await _client.request(
        method=request.method,
        url=url,
        headers=fwd_headers,
        content=body,
    )

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in {"content-encoding", "transfer-encoding", "content-length", "connection"}
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
