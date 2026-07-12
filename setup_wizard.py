"""First-run setup wizard.

Renders a guided web form that writes ``configfile.ini`` so new self-hosters
never have to hand-edit an INI file. The wizard is reachable ONLY while the
config is missing or still carries placeholder values — the moment a complete
config exists, every wizard route turns into a redirect to the login page, so
it adds no attack surface to a configured install.

Live trading defaults to OFF: the form requires two explicit confirmations
(a checkbox plus a typed acknowledgement) before it will write
``[safety] live_trading = true``.
"""

import configparser
import logging
import os
import re
import sys
import tempfile
import threading
import time
from typing import Optional

from flask import Blueprint, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

setup_bp = Blueprint("setup_wizard", __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_BASE_DIR, "configfile.ini")
_EXAMPLE_PATH = os.path.join(_BASE_DIR, "configfile.ini.example")

# Values that mean "the user never filled this in".
_PLACEHOLDER_VALUES = {
    "", "YOUR_API_KEY", "YOUR_API_SECRET", "YOUR_USERNAME", "YOUR_PASSWORD",
    "kite_api_key", "kite_api_secret",
}

# Kite API keys are short lowercase alphanumeric tokens.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")

# Cache the completeness check on the config file's mtime so enforce_auth's
# per-request call never becomes a per-request disk read.
_completeness_cache_lock = threading.Lock()
_completeness_cache: dict[str, object] = {"mtime": None, "complete": None}


def _read_config() -> Optional[configparser.ConfigParser]:
    """Read configfile.ini if present.

    Returns:
        Parsed config, or None when the file is missing/unreadable.
    """
    if not os.path.exists(_CONFIG_PATH):
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(_CONFIG_PATH)
    except configparser.Error as parse_error:
        logging.error("setup_wizard: configfile.ini unparseable: %s", parse_error)
        return None
    return parser


def _value_is_real(parser: configparser.ConfigParser, section: str, key: str) -> bool:
    """Return True when a config value exists and is not a known placeholder."""
    if not parser.has_option(section, key):
        return False
    return parser.get(section, key).strip() not in _PLACEHOLDER_VALUES


def is_config_complete() -> bool:
    """Check whether configfile.ini has real (non-placeholder) credentials.

    Complete means: [kite_login_details] api_key/api_secret and [gatekeeper]
    username/password all exist with non-placeholder values. Result is cached
    on the file's mtime.

    Returns:
        True when the config is usable and the wizard should be locked out.
    """
    try:
        current_mtime: Optional[float] = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        current_mtime = None

    with _completeness_cache_lock:
        if _completeness_cache["mtime"] == current_mtime and _completeness_cache["complete"] is not None:
            return bool(_completeness_cache["complete"])

    parser = _read_config()
    complete = parser is not None and all(
        _value_is_real(parser, section, key)
        for section, key in (
            ("kite_login_details", "api_key"),
            ("kite_login_details", "api_secret"),
            ("gatekeeper", "username"),
            ("gatekeeper", "password"),
        )
    )

    with _completeness_cache_lock:
        _completeness_cache["mtime"] = current_mtime
        _completeness_cache["complete"] = complete
    return complete


def _validate_form(form: dict) -> Optional[str]:
    """Validate wizard form fields.

    Args:
        form: The POSTed form data.

    Returns:
        An error message, or None when everything is valid.
    """
    api_key = form.get("api_key", "").strip()
    api_secret = form.get("api_secret", "").strip()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    password_confirm = form.get("password_confirm", "")

    if not _API_KEY_RE.match(api_key):
        return "Kite API key looks invalid — expected 8-32 alphanumeric characters."
    if not _API_KEY_RE.match(api_secret):
        return "Kite API secret looks invalid — expected 8-32 alphanumeric characters."
    if not re.match(r"^[A-Za-z0-9_.-]{3,32}$", username):
        return "Dashboard username must be 3-32 characters (letters, digits, _ . -)."
    if len(password) < 8:
        return "Dashboard password must be at least 8 characters."
    if password != password_confirm:
        return "Passwords do not match."
    if form.get("live_trading") == "on" and form.get("confirm_live", "").strip().upper() != "I UNDERSTAND":
        return 'To enable live trading you must type "I UNDERSTAND" in the confirmation box.'
    return None


def _write_config(form: dict) -> None:
    """Write configfile.ini from the example template plus form values.

    The gatekeeper password is stored as a Werkzeug hash, never plaintext.
    The file is written atomically (temp file + rename) with 0600 permissions.

    Args:
        form: Validated form data.

    Raises:
        OSError: If the config file cannot be written.
    """
    parser = configparser.ConfigParser()
    if os.path.exists(_EXAMPLE_PATH):
        parser.read(_EXAMPLE_PATH)

    for section in ("kite_login_details", "gatekeeper", "safety", "option_details", "others"):
        if not parser.has_section(section):
            parser.add_section(section)

    parser.set("kite_login_details", "api_key", form.get("api_key", "").strip())
    parser.set("kite_login_details", "api_secret", form.get("api_secret", "").strip())
    parser.set("gatekeeper", "username", form.get("username", "").strip())
    parser.set("gatekeeper", "password", generate_password_hash(form.get("password", "")))

    live_trading_on = (
        form.get("live_trading") == "on"
        and form.get("confirm_live", "").strip().upper() == "I UNDERSTAND"
    )
    parser.set("safety", "live_trading", "true" if live_trading_on else "false")

    temp_fd, temp_path = tempfile.mkstemp(dir=_BASE_DIR, prefix=".configfile_", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w") as temp_file:
            parser.write(temp_file)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, _CONFIG_PATH)
    except OSError:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    with _completeness_cache_lock:
        _completeness_cache["mtime"] = None
        _completeness_cache["complete"] = None
    logging.info("setup_wizard: configfile.ini written (live_trading=%s)", live_trading_on)


def _schedule_self_restart(delay_seconds: float = 1.5) -> None:
    """Restart this process in-place shortly after the response is sent.

    Most config-derived state (the Kite API key/secret, the ``kite`` client
    singleton, safety/dry-run flags, cool-off timers) is read once at import
    time across `common_lib`/`flask_app`, so an in-memory reload would have to
    re-derive all of that scattered state correctly. Since the app runs as a
    single waitress process with no reloader, `os.execv` re-executing the same
    interpreter invocation is simpler and safer: it keeps the same PID (so a
    systemd unit sees no restart event) while re-running every import fresh
    against the newly written configfile.ini.

    Args:
        delay_seconds: How long to wait before restarting, giving the
            "setup complete" response time to reach the browser first.
    """
    def _restart() -> None:
        time.sleep(delay_seconds)
        logging.info("setup_wizard: restarting process in-place to apply new configfile.ini")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart, name="setup-wizard-restart", daemon=True).start()


@setup_bp.route("/setup", methods=["GET", "POST"])
def setup_page():
    """Render and process the first-run configuration form.

    Locked (redirects to login) whenever a complete config already exists.
    """
    if is_config_complete():
        return redirect(url_for("app_login"))

    if request.method == "POST":
        error = _validate_form(request.form)
        if error:
            return render_template("setup_wizard.html", error=error, form=request.form), 400
        try:
            _write_config(request.form)
        except OSError as write_error:
            logging.error("setup_wizard: failed to write configfile.ini: %s", write_error)
            return render_template(
                "setup_wizard.html",
                error=f"Could not write configfile.ini: {write_error}",
                form=request.form,
            ), 500
        _schedule_self_restart()
        return render_template("setup_wizard_done.html")

    return render_template("setup_wizard.html", error=None, form={})
