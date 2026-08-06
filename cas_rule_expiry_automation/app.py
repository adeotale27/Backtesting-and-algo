"""CAS Rule Expiry Automation — lightweight admin UI (light theme).

    python -m cas_rule_expiry_automation
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from functools import wraps
from typing import Callable

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

_PKG = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_PKG)
for p in (_ROOT, _PKG):
    if p not in sys.path:
        sys.path.insert(0, p)

from cas_rule_expiry_automation.backtest_ws import run_ws_backtest
from cas_rule_expiry_automation.config import (
    load_config,
    save_kite_credentials,
    save_strategy_settings,
    set_live_trading,
)
from cas_rule_expiry_automation.engine import get_engine
from cas_rule_expiry_automation.expiry_calendar import describe_today, next_expiry_dates
from cas_rule_expiry_automation.kite_client import KiteClient
from cas_rule_expiry_automation.state import get_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cas_rule.app")

app = Flask(
    __name__,
    template_folder=os.path.join(_PKG, "templates"),
    static_folder=os.path.join(_PKG, "static"),
)
app.secret_key = os.environ.get("CAS_RULE_SECRET", "cas-rule-expiry-dev-key")


def _cfg():
    return load_config()


def login_required(fn: Callable):
    @wraps(fn)
    def wrap(*a, **k):
        if not session.get("ok"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return fn(*a, **k)

    return wrap


@app.get("/login")
def login():
    return render_template("login.html")


@app.post("/login")
def login_post():
    cfg = _cfg()
    if (
        request.form.get("username", "").strip() == cfg.admin_username
        and request.form.get("password", "") == cfg.admin_password
    ):
        session["ok"] = True
        session["user"] = cfg.admin_username
        return redirect(url_for("dashboard"))
    return render_template("login.html", error="Invalid credentials"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    cfg = _cfg()
    eng = get_engine()
    return render_template(
        "dashboard.html",
        cfg=cfg,
        day=describe_today(cfg),
        upcoming=next_expiry_dates(cfg, count=6),
        status=eng.status(),
    )


@app.get("/api/status")
@login_required
def api_status():
    return jsonify({"ok": True, **get_engine().status()})


@app.post("/api/credentials")
@login_required
def api_credentials():
    data = request.get_json(silent=True) or {}
    cfg = _cfg()
    key = (data.get("api_key") or cfg.api_key).strip()
    secret = (data.get("api_secret") or cfg.api_secret).strip()
    token = (data.get("access_token") or "").strip()
    req = (data.get("request_token") or "").strip()
    save_kite_credentials(key, secret, token or cfg.access_token)
    out = {"ok": True}
    try:
        cfg = _cfg()
        client = KiteClient(cfg)
        if req:
            out["access_token"] = client.exchange_request_token(req)
        elif token:
            client.connect(token)
        if client.kite or cfg.access_token:
            if not client.kite:
                client.connect()
            out["profile"] = client.profile()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    get_engine().reload_config()
    return jsonify(out)


@app.get("/api/login_url")
@login_required
def api_login_url():
    try:
        return jsonify({"ok": True, "url": KiteClient(_cfg()).login_url()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/settings")
@login_required
def api_settings():
    data = request.get_json(silent=True) or {}
    lots = int(data.get("lots", _cfg().lots))
    ce = int(data.get("ce_otm_steps", _cfg().ce_otm_steps))
    pe = int(data.get("pe_otm_steps", _cfg().pe_otm_steps))
    product = (data.get("product") or _cfg().product).upper()
    save_strategy_settings(lots, ce, pe, product)
    get_engine().reload_config()
    return jsonify({"ok": True, "lots": lots, "ce_otm_steps": ce, "pe_otm_steps": pe, "product": product})


@app.post("/api/activate")
@login_required
def api_activate():
    state = get_store().activate(session.get("user", "admin"))
    eng = get_engine()
    if not eng.running:
        eng.start()
    return jsonify({"ok": True, "state": state})


@app.post("/api/deactivate")
@login_required
def api_deactivate():
    return jsonify({"ok": True, "state": get_store().deactivate(session.get("user", "admin"))})


@app.post("/api/reset")
@login_required
def api_reset():
    return jsonify({"ok": True, "state": get_store().reset_day()})


@app.post("/api/live")
@login_required
def api_live():
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    set_live_trading(enabled)
    get_engine().reload_config()
    return jsonify({"ok": True, "live_trading": enabled})


@app.post("/api/manual_fire")
@login_required
def api_manual_fire():
    data = request.get_json(silent=True) or {}
    index = (data.get("index") or "NIFTY").upper()
    close_price = float(data.get("close_price") or 0)
    if close_price <= 0:
        return jsonify({"ok": False, "error": "close_price required"}), 400
    cfg = _cfg()
    store = get_store()
    try:
        client = KiteClient(cfg)
        client.connect()
        from cas_rule_expiry_automation.strategy_engine import StrategyEngine

        strat = StrategyEngine(client, cfg, store)
        strat.active_indexes = [index]
        try:
            strat.cache.prewarm(client.kite, index, close_price, cfg.ce_otm_steps, cfg.pe_otm_steps)
        except Exception:
            pass
        fills = strat.manual_fire(index, close_price)
        return jsonify({"ok": True, "fills": [f.__dict__ for f in fills], "state": store.snapshot()})
    except Exception as exc:
        store.set_error(str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/backtest")
@login_required
def api_backtest():
    data = request.get_json(silent=True) or {}
    cfg = _cfg()

    def parse(v, default):
        return date.fromisoformat(v) if v else default

    end = parse(data.get("end"), date.today())
    start = parse(data.get("start"), date(end.year, max(1, end.month - 3), 1))
    capital = float(data.get("capital") or cfg.default_capital)

    kite = None
    try:
        if cfg.access_token:
            kite = KiteClient(cfg).connect()
    except Exception as exc:
        logger.warning("backtest without kite: %s", exc)

    result = run_ws_backtest(kite=kite, config=cfg, start=start, end=end, capital=capital)
    return jsonify({"ok": True, "result": result.to_dict()})


def main() -> None:
    cfg = _cfg()
    eng = get_engine()
    eng.start()
    logger.info(
        "CAS Rule Expiry Automation → http://%s:%s (live=%s)",
        cfg.host,
        cfg.port,
        cfg.live_trading,
    )
    try:
        from waitress import serve

        serve(app, host=cfg.host, port=cfg.port, threads=4)
    except ImportError:
        app.run(host=cfg.host, port=cfg.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
