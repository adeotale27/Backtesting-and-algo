"""Telegram notification channel.

Sends messages to a configured Telegram chat via the Bot API using only the
standard ``requests`` library — no Telegram SDK required.
"""

import configparser
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT_SECONDS = 10

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "configfile.ini",
)


def _load_telegram_config() -> tuple[str, str]:
    """Read bot_token and chat_id from configfile.ini [notifications].

    Returns:
        Tuple of (bot_token, chat_id). Either may be an empty string if not
        configured, in which case callers should skip the send.
    """
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    bot_token = cfg.get("notifications", "telegram_bot_token", fallback="")
    chat_id = cfg.get("notifications", "telegram_chat_id", fallback="")
    return bot_token.strip(), chat_id.strip()


def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message to the configured Telegram chat.

    Gracefully skips if credentials are not configured and never raises — a
    Telegram failure must never break the notification dispatch flow.

    Args:
        text: Message text. Supports Markdown formatting by default.
        parse_mode: Telegram parse mode ('Markdown' or 'HTML').

    Returns:
        True if the message was delivered successfully, False otherwise.
    """
    bot_token, chat_id = _load_telegram_config()
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured (missing telegram_bot_token / telegram_chat_id) — skipping send")
        return False

    url = _TELEGRAM_API_URL.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        response = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.error("Telegram send_message network error: %s", exc)
        return False

    if response.status_code != 200:
        logger.error(
            "Telegram API returned HTTP %d: %s",
            response.status_code,
            response.text[:200],
        )
        return False

    logger.debug("Telegram message sent successfully to chat_id=%s", chat_id)
    return True


def format_alert(title: str, body: str, severity: str) -> str:
    """Format a generic notification into a Telegram Markdown message string.

    Args:
        title: Short alert heading.
        body: Full alert message body.
        severity: 'info', 'warning', or 'error'.

    Returns:
        Formatted string ready for Telegram sendMessage.
    """
    severity_emoji: dict[str, str] = {
        "info": "ℹ️",
        "warning": "⚠️",
        "error": "🚨",
    }
    emoji = severity_emoji.get(severity, "📢")
    return f"{emoji} *{title}*\n{body}"


def format_order_executed(title: str, body: str) -> str:
    """Format an ORDER_EXECUTED notification for Telegram.

    Args:
        title: Notification title (e.g. 'Order Executed: NIFTY25000CE').
        body: Notification body (e.g. 'SELL 500 @ ₹120.50').

    Returns:
        Formatted Telegram Markdown string.
    """
    return f"✅ *{title}*\n{body}"


def format_order_cancelled(title: str, body: str) -> str:
    """Format an ORDER_CANCELLED notification for Telegram.

    Args:
        title: Notification title.
        body: Notification body.

    Returns:
        Formatted Telegram Markdown string.
    """
    return f"❌ *{title}*\n{body}"


def format_iv_spike(title: str, body: str) -> str:
    """Format an IV_SPIKE_DETECTED notification for Telegram.

    Args:
        title: Notification title (e.g. 'IV Spike: NIFTY25000CE').
        body: Notification body with IV%, premium, target.

    Returns:
        Formatted Telegram Markdown string.
    """
    return f"📈 *{title}*\n{body}"


def format_daily_pnl(title: str, body: str) -> str:
    """Format a DAILY_PNL_SUMMARY notification for Telegram.

    Args:
        title: Notification title.
        body: P&L summary body.

    Returns:
        Formatted Telegram Markdown string.
    """
    return f"📊 *{title}*\n{body}"


def format_duplicate_order_alert(title: str, body: str) -> str:
    """Format a DUPLICATE_ORDER_ALERT notification for Telegram.

    The body is already pre-formatted by the dispatcher with order count,
    price diff %, algo names, and instrument label.

    Args:
        title: Notification title (e.g. 'Duplicate Orders: NIFTY25000CE').
        body: Pre-formatted body (e.g. '2 BUY orders within 1.2% — Wave Extractor + Manual').

    Returns:
        Formatted Telegram Markdown string.
    """
    return f"⚠️ *{title}*\n{body}\n👉 View: /duplicate-orders"


def format_notification(notification_type: str, title: str, body: str, severity: str) -> str:
    """Dispatch formatting to the appropriate formatter based on notification type.

    Falls back to the generic ``format_alert`` for unrecognised types.

    Args:
        notification_type: Key from ROUTING_TABLE (e.g. 'ORDER_EXECUTED').
        title: Notification title.
        body: Notification body.
        severity: Severity level ('info', 'warning', 'error').

    Returns:
        Formatted Telegram Markdown string.
    """
    match notification_type:
        case "ORDER_EXECUTED":
            return format_order_executed(title, body)
        case "ORDER_CANCELLED":
            return format_order_cancelled(title, body)
        case "IV_SPIKE_DETECTED":
            return format_iv_spike(title, body)
        case "DAILY_PNL_SUMMARY":
            return format_daily_pnl(title, body)
        case "DUPLICATE_ORDER_ALERT":
            return format_duplicate_order_alert(title, body)
        case _:
            return format_alert(title, body, severity)
