"""Browser Web Push notification channel.

Sends OS-level push notifications to all registered browser subscriptions using
pywebpush and VAPID keys. Stale subscriptions (HTTP 410 Gone) are automatically
removed from the database.
"""

import configparser
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "configfile.ini",
)


def _load_vapid_config() -> dict[str, str]:
    """Read VAPID keys and claims from configfile.ini [notifications].

    Returns:
        Dict with keys: private_key, public_key, claims_sub. Any value may be
        an empty string if not configured.
    """
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    return {
        "private_key": cfg.get("notifications", "vapid_private_key", fallback="").strip(),
        "public_key":  cfg.get("notifications", "vapid_public_key",  fallback="").strip(),
        "claims_sub":  cfg.get("notifications", "vapid_claims_sub",  fallback="mailto:admin@example.com").strip(),
    }


def get_vapid_public_key() -> str:
    """Return the VAPID public key for serving to the browser.

    Returns:
        Base64url-encoded VAPID public key string, or empty string if not configured.
    """
    return _load_vapid_config()["public_key"]


def push_to_all(title: str, body: str, notification_id: int) -> None:
    """Send a Web Push to every subscription stored in the database.

    Subscriptions that return HTTP 410 (Gone) are automatically deleted.
    All other errors are logged but never raised — a push failure must never
    break the notification dispatch flow.

    Args:
        title: Notification title shown in the OS notification.
        body: Notification body text.
        notification_id: DB row id included in the push payload so the service
            worker can correlate with the in-app notification.
    """
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed — skipping web push. Run: pip install pywebpush")
        return

    from notifications.database import get_all_push_subscriptions, delete_push_subscription

    vapid = _load_vapid_config()
    if not vapid["private_key"] or not vapid["public_key"]:
        logger.warning("VAPID keys not configured — skipping web push")
        return

    subscriptions = get_all_push_subscriptions()
    if not subscriptions:
        logger.debug("No push subscriptions registered — skipping web push")
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "notification_id": notification_id,
    })

    for sub in subscriptions:
        subscription_info: dict[str, Any] = {
            "endpoint": sub["endpoint"],
            "keys": {
                "p256dh": sub["p256dh"],
                "auth":   sub["auth"],
            },
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid["private_key"],
                vapid_claims={"sub": vapid["claims_sub"]},
            )
            logger.debug("Web push sent to endpoint %s…", sub["endpoint"][:40])
        except WebPushException as exc:
            if exc.response is not None and exc.response.status_code == 410:
                logger.info(
                    "Web push: subscription expired (410 Gone), removing endpoint %s…",
                    sub["endpoint"][:40],
                )
                delete_push_subscription(sub["endpoint"])
            else:
                logger.error(
                    "Web push failed for endpoint %s…: %s",
                    sub["endpoint"][:40],
                    exc,
                )
        except Exception as exc:
            logger.error("Web push unexpected error for endpoint %s…: %s", sub["endpoint"][:40], exc)
