"""Minimal FastAPI stub occupying supervisor's `backend` slot on :8001.

The real application is a Flask app served by `waitress` on :3000 (see
`/app/frontend/package.json`). Emergent's ingress routes `/api/*` to :8001
and everything else to :3000, so this stub only needs a couple of health
endpoints under `/api` for the platform's smoke checks.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="ui-trading-system backend stub")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "note": "real app is the Flask dashboard on :3000"}


@app.get("/api")
async def root():
    return {"message": "See the Flask dashboard at /"}
