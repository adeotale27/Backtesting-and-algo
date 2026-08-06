"""Flask admin app for the CAS Expiry algo.

Run:
    python -m cas_expiry
    # or
    python -m cas_expiry.app

Binds to 127.0.0.1:5020 by default (see config.ini [server]).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from functools import wraps
from typing import Any, Callable

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# Ensure repo root + this package are importable
_PKG = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_PKG)
for p in (_ROOT, _PKG):
    if p not in sys.path:
        sys.path.insert(0, p)

from cas_expiry.backtest import run_cas_backtest
from cas_expiry.config import (
    load_config,
    save_kite_credentials,
    set_live_trading,
)
from cas_expiry.kite_session import KiteSession
from cas_expiry.runner import get_runner
from cas_expiry.state import get_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cas_expiry.app")

app = Flask(
    __name__,
    template_folder=os.path.join(_PKG, "templates"),
    static_folder=None,
)
app.secret_key = os.environ.get("CAS_SECRET", "cas-expiry-dev-secret-change-me")


def _cfg():
    return load_config()


def login_required(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_ok"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = _cfg()
    error = None
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        if user == cfg.admin_username and pw == cfg.admin_password:
            session["admin_ok"] = True
            session["admin_user"] = user
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid credentials"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    cfg = _cfg()
    store = get_store().snapshot()
    runner = get_runner()
    return render_template(
        "index.html",
        cfg=cfg,
        state=store,
        runner_alive=runner.running,
        live=cfg.live_trading,
    )


@app.route("/api/status")
@login_required
def api_status():
    cfg = _cfg()
    store = get_store().snapshot()
    runner = get_runner()
    profile = None
    try:
        if cfg.access_token:
            profile = KiteSession(cfg).profile()
    except Exception as exc:
        profile = {"error": str(exc)}
    return jsonify(
        {
            "ok": True,
            "state": store,
            "runner_alive": runner.running,
            "live_trading": cfg.live_trading,
            "index": cfg.index,
            "lots": cfg.lots,
            "product": cfg.product,
            "watch_start": cfg.watch_start.isoformat(timespec="seconds"),
            "watch_end": cfg.watch_end.isoformat(timespec="seconds"),
            "poll_interval_ms": cfg.poll_interval_ms,
            "profile": profile,
            "api_key_set": bool(cfg.api_key) and not cfg.api_key.startswith("YOUR_"),
            "token_set": bool(cfg.access_token),
        }
    )


@app.route("/api/credentials", methods=["POST"])
@login_required
def api_credentials():
    data = request.get_json(silent=True) or request.form
    api_key = (data.get("api_key") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()
    access_token = (data.get("access_token") or "").strip()
    request_token = (data.get("request_token") or "").strip()

    cfg = _cfg()
    key = api_key or cfg.api_key
    secret = api_secret or cfg.api_secret
    save_kite_credentials(key, secret, access_token or cfg.access_token)

    result: dict[str, Any] = {"ok": True}
    if request_token:
        try:
            cfg = _cfg()
            session_obj = KiteSession(cfg)
            token = session_obj.exchange_request_token(request_token)
            result["access_token"] = token
            result["profile"] = session_obj.profile()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    elif access_token:
        try:
            cfg = _cfg()
            session_obj = KiteSession(cfg)
            session_obj.connect(access_token)
            result["profile"] = session_obj.profile()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/login_url")
@login_required
def api_login_url():
    try:
        url = KiteSession(_cfg()).login_url()
        return jsonify({"ok": True, "url": url})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/activate", methods=["POST"])
@login_required
def api_activate():
    store = get_store()
    state = store.activate(by=session.get("admin_user", "admin"))
    runner = get_runner()
    if not runner.running:
        runner.start()
    return jsonify({"ok": True, "state": state})


@app.route("/api/deactivate", methods=["POST"])
@login_required
def api_deactivate():
    state = get_store().deactivate(by=session.get("admin_user", "admin"))
    return jsonify({"ok": True, "state": state})


@app.route("/api/reset_day", methods=["POST"])
@login_required
def api_reset_day():
    return jsonify({"ok": True, "state": get_store().reset_day()})


@app.route("/api/live_trading", methods=["POST"])
@login_required
def api_live_trading():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    set_live_trading(enabled)
    # Refresh runner config on next ensure
    runner = get_runner()
    runner._strategy = None  # noqa: SLF001 — force reconnect with new flag
    runner.config = _cfg()
    return jsonify({"ok": True, "live_trading": enabled})


@app.route("/api/manual_fire", methods=["POST"])
@login_required
def api_manual_fire():
    data = request.get_json(silent=True) or {}
    index = (data.get("index") or _cfg().index).upper()
    if index == "BOTH":
        index = "NIFTY"
    close_price = data.get("close_price")
    use_ltp = bool(data.get("use_ltp"))

    cfg = _cfg()
    store = get_store()
    try:
        session_obj = KiteSession(cfg)
        session_obj.connect()
        session_obj.sync_instruments()
        from cas_expiry.strategy import CasExpiryStrategy

        strategy = CasExpiryStrategy(session_obj, cfg, store)
        if use_ltp or close_price is None:
            fills = strategy.force_ltp_fire(index)
        else:
            fills = strategy.manual_fire(index, float(close_price))
        return jsonify(
            {
                "ok": True,
                "fills": [f.__dict__ for f in fills],
                "state": store.snapshot(),
            }
        )
    except Exception as exc:
        logger.exception("manual_fire failed")
        store.set_error(str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/backtest", methods=["POST"])
@login_required
def api_backtest():
    data = request.get_json(silent=True) or {}
    cfg = _cfg()
    index = (data.get("index") or cfg.index or "NIFTY").upper()
    if index == "BOTH":
        index = "NIFTY"
    capital = float(data.get("capital") or cfg.default_capital)
    lots = int(data.get("lots") or cfg.lots)
    assumed_iv = float(data.get("assumed_iv") or cfg.assumed_iv)

    def _parse(d: str | None, default: date) -> date:
        if not d:
            return default
        return date.fromisoformat(d)

    end = _parse(data.get("end"), date.today())
    start = _parse(data.get("start"), date(end.year, max(1, end.month - 3), 1))

    kite = None
    try:
        if cfg.access_token:
            kite = KiteSession(cfg).connect()
    except Exception as exc:
        logger.warning("Backtest without live kite: %s", exc)

    result = run_cas_backtest(
        kite=kite,
        index=index,
        start=start,
        end=end,
        capital=capital,
        lots=lots,
        ce_offset=cfg.ce_offset,
        pe_offset=cfg.pe_offset,
        assumed_iv=assumed_iv,
    )
    return jsonify({"ok": True, "result": result.to_dict()})


def create_app() -> Flask:
    return app


def main() -> None:
    cfg = _cfg()
    runner = get_runner()
    runner.start()
    logger.info(
        "CAS Expiry admin UI on http://%s:%s (live_trading=%s)",
        cfg.host,
        cfg.port,
        cfg.live_trading,
    )
    # Waitress if available, else Flask built-in
    try:
        from waitress import serve

        serve(app, host=cfg.host, port=cfg.port)
    except ImportError:
        app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
