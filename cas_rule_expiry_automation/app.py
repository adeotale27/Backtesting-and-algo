"""CAS Rule Expiry Automation — lightweight admin UI (light theme).

    python -m cas_rule_expiry_automation
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta
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
        return redirect(url_for("live_page"))
    return render_template("login.html", error="Invalid credentials"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip()
    return (not v) or v.upper().startswith("YOUR_")


def _creds_flags(cfg) -> dict:
    return {
        "has_key": not _is_placeholder(cfg.api_key),
        "has_secret": not _is_placeholder(cfg.api_secret),
        "has_token": bool((cfg.access_token or "").strip()),
    }


def _page_ctx():
    cfg = _cfg()
    eng = get_engine()
    return dict(
        cfg=cfg,
        creds=_creds_flags(cfg),
        day=describe_today(cfg),
        upcoming=next_expiry_dates(cfg, count=6),
        status=eng.status(),
    )


@app.get("/")
@login_required
def live_page():
    return render_template("live.html", **_page_ctx())


@app.get("/backtest")
@login_required
def backtest_page():
    return render_template("backtest.html", **_page_ctx())


# Backward-compatible alias
@app.get("/live")
@login_required
def live_alias():
    return redirect(url_for("live_page"))


@app.get("/dashboard")
@login_required
def dashboard_alias():
    return redirect(url_for("live_page"))


@app.get("/api/status")
@login_required
def api_status():
    return jsonify({"ok": True, **get_engine().status()})


@app.post("/api/credentials")
@login_required
def api_credentials():
    """Update Kite session. Daily path: send only access_token.

    api_key / api_secret are optional — omitted or blank keeps saved values.
    """
    data = request.get_json(silent=True) or {}
    cfg = _cfg()
    raw_key = (data.get("api_key") or "").strip()
    raw_secret = (data.get("api_secret") or "").strip()
    # Keep existing key/secret unless a real new value is provided.
    key = raw_key if raw_key and not raw_key.startswith("•") else cfg.api_key
    secret = (
        raw_secret if raw_secret and not raw_secret.startswith("•") else cfg.api_secret
    )
    token = (data.get("access_token") or "").strip()
    req = (data.get("request_token") or "").strip()
    if not token and not req and raw_key == "" and raw_secret == "":
        return jsonify({"ok": False, "error": "access_token required"}), 400
    save_kite_credentials(key, secret, token or cfg.access_token)
    out = {"ok": True, "creds": _creds_flags(_cfg())}
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
        out["creds"] = _creds_flags(_cfg())
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
    store = get_store()
    if store.is_activated():
        return jsonify(
            {
                "ok": True,
                "unchanged": True,
                "state": store.snapshot(),
                "message": "CAS window already active",
            }
        )
    state = store.activate(session.get("user", "admin"))
    eng = get_engine()
    if not eng.running:
        eng.start()
    return jsonify(
        {
            "ok": True,
            "unchanged": False,
            "state": state,
            "message": "CAS window activated — watching for close print",
        }
    )


@app.post("/api/deactivate")
@login_required
def api_deactivate():
    store = get_store()
    if not store.is_activated():
        return jsonify(
            {
                "ok": True,
                "unchanged": True,
                "state": store.snapshot(),
                "message": "CAS window already inactive",
            }
        )
    state = store.deactivate(session.get("user", "admin"))
    return jsonify(
        {
            "ok": True,
            "unchanged": False,
            "state": state,
            "message": "CAS window deactivated — no sells",
        }
    )


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
    lots = int(data.get("lots") or cfg.lots)

    # Optional real CAS close override (fixes synthetic wrong closes)
    close_overrides = {}
    force_close = data.get("force_close")
    force_index = (data.get("force_index") or "SENSEX").upper()
    if force_close not in (None, "", 0, "0"):
        close_overrides[force_index] = float(force_close)
        # Also bind to each day in range for clarity
        d = start
        while d <= end:
            close_overrides[f"{d.isoformat()}:{force_index}"] = float(force_close)
            d += timedelta(days=1)
    # Allow map form: {"2026-08-06:SENSEX": 78954.76}
    raw_map = data.get("close_overrides") or {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            try:
                close_overrides[str(k)] = float(v)
            except (TypeError, ValueError):
                pass

    kite = None
    kite_error = None
    try:
        if cfg.access_token:
            kite = KiteClient(cfg).connect()
            kite.profile()  # validate token early
    except Exception as exc:
        kite = None
        kite_error = str(exc)
        logger.warning("backtest without kite: %s", exc)

    result = run_ws_backtest(
        kite=kite,
        config=cfg,
        start=start,
        end=end,
        capital=capital,
        close_overrides=close_overrides or None,
        lots=lots,
    )
    out = result.to_dict()
    if kite_error:
        out.setdefault("notes", []).insert(
            0, f"Kite unavailable ({kite_error}). Use Force close or refresh access_token."
        )
    return jsonify({"ok": True, "result": out, "kite_ok": kite is not None})


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

        serve(app, host=cfg.host, port=cfg.port, threads=8)
    except ImportError:
        app.run(host=cfg.host, port=cfg.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
