"""Flask Blueprint for the notification API.

Routes:
  GET  /notifications/unread          — poll for unread notifications (toast poller)
  GET  /notifications/active          — active notifications for bell icon/dropdown
  GET  /notifications/all             — all notifications split by status (full page API)
  GET  /notifications/page            — full notifications management page (HTML)
  POST /notifications/mark-read       — mark one or all notifications as read (toast seen)
  POST /notifications/update-status   — set status to 'completed' or 'ignored'
  POST /notifications/subscribe       — register a browser push subscription
  DELETE /notifications/unsubscribe   — remove a push subscription
  GET  /notifications/vapid-public-key — serve VAPID public key to frontend
  GET  /notifications/sound           — serve the alert WAV file
  POST /notifications/test            — fire a test notification (dev helper)
"""

import logging
import os
from flask import Blueprint, jsonify, render_template, request, Response, send_file

from notifications import database
from notifications import webpush

_VIBHU_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

notifications_bp = Blueprint(
    "notifications",
    __name__,
    url_prefix="/notifications",
    static_folder="static",
    static_url_path="/notifications/static",
    template_folder="templates",
)


@notifications_bp.after_request
def _set_sw_allowed_header(response: Response) -> Response:
    """Allow the service worker to control the full origin scope."""
    if request.path.endswith("/sw.js"):
        response.headers["Service-Worker-Allowed"] = "/"
    return response


@notifications_bp.route("/unread", methods=["GET"])
def get_unread():
    """Return unread notifications for the toast poller.

    'Unread' means is_read=0 — the toast has not been shown yet.
    This is separate from the active/inactive lifecycle status.

    Returns:
        JSON with 'notifications' list and 'count'.
    """
    notifications = database.get_unread_notifications(limit=50)
    return jsonify({"notifications": notifications, "count": len(notifications)})


@notifications_bp.route("/active", methods=["GET"])
def get_active():
    """Return active notifications for the bell icon and dropdown.

    'Active' means status='active', regardless of whether the toast was shown.
    This is what the bell badge count and dropdown list should display.

    Returns:
        JSON with 'notifications' list and 'count'.
    """
    notifications = database.get_active_notifications(limit=100)
    return jsonify({"notifications": notifications, "count": len(notifications)})


@notifications_bp.route("/all", methods=["GET"])
def get_all():
    """Return all notifications split into active and inactive sections.

    Used by the full notifications page to populate both sections via JS fetch.

    Returns:
        JSON with 'active' and 'inactive' lists plus counts.
    """
    data = database.get_all_notifications(active_limit=200, inactive_limit=200)
    return jsonify({
        "active": data["active"],
        "inactive": data["inactive"],
        "active_count": len(data["active"]),
        "inactive_count": len(data["inactive"]),
    })


@notifications_bp.route("/page", methods=["GET"])
def notifications_page():
    """Render the full notifications management page.

    Returns:
        HTML page with Active and Completed/Ignored sections.
    """
    return render_template("notifications/page.html")


@notifications_bp.route("/mark-read", methods=["POST"])
def mark_read():
    """Mark one or all notifications as read (toast has been shown).

    Request body (JSON):
        {"id": 42}        — mark a single notification
        {"all": true}     — mark every unread notification

    Returns:
        JSON with 'ok': True on success.
    """
    data = request.get_json(silent=True) or {}

    if data.get("all"):
        database.mark_all_read()
        logger.debug("Marked all notifications as read")
        return jsonify({"ok": True, "action": "all_read"})

    notification_id = data.get("id")
    if not notification_id:
        return jsonify({"ok": False, "error": "Provide 'id' or 'all': true"}), 400

    database.mark_read(int(notification_id))
    logger.debug("Marked notification %d as read", notification_id)
    return jsonify({"ok": True, "id": notification_id})


@notifications_bp.route("/update-status", methods=["POST"])
def update_status():
    """Update the lifecycle status of a notification.

    Request body (JSON):
        {"id": 42, "status": "completed"}
        {"id": 42, "status": "ignored"}

    Returns:
        JSON with 'ok': True on success, or error details on failure.
    """
    data = request.get_json(silent=True) or {}
    notification_id = data.get("id")
    new_status = data.get("status", "").strip()

    if not notification_id:
        return jsonify({"ok": False, "error": "Missing 'id'"}), 400

    try:
        database.update_notification_status(int(notification_id), new_status)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    logger.info("Notification %d marked as '%s'", notification_id, new_status)
    return jsonify({"ok": True, "id": notification_id, "status": new_status})


@notifications_bp.route("/ignore-all", methods=["POST"])
def ignore_all():
    """Set status='ignored' on active notifications, optionally filtered by type.

    Request body (JSON, all optional):
        {"types": ["ORDER_EXECUTED", "ORDER_CANCELLED"]}  — tab-scoped ignore
        {}                                                — ignore all active

    Returns:
        JSON with 'ok': True and count of notifications ignored.
    """
    data = request.get_json(silent=True) or {}
    notification_types: list[str] | None = data.get("types") or None
    updated_count = database.mark_all_ignored(notification_types=notification_types)
    logger.info("Ignored %d active notifications (types=%s)", updated_count, notification_types)
    return jsonify({"ok": True, "count": updated_count})


@notifications_bp.route("/delete/<int:notification_id>", methods=["DELETE"])
def delete_notification(notification_id: int):
    """Permanently delete a single notification from the database.

    Returns:
        JSON with 'ok': True on success.
    """
    database.delete_notification(notification_id)
    logger.info("Deleted notification %d", notification_id)
    return jsonify({"ok": True, "id": notification_id})


@notifications_bp.route("/delete-all-inactive", methods=["POST"])
def delete_all_inactive():
    """Permanently delete all completed and ignored notifications.

    Returns:
        JSON with 'ok': True and count of notifications deleted.
    """
    deleted_count = database.delete_all_inactive_notifications()
    logger.info("Deleted all %d inactive notifications", deleted_count)
    return jsonify({"ok": True, "count": deleted_count})


@notifications_bp.route("/subscribe", methods=["POST"])
def subscribe_push():
    """Save a browser push subscription.

    Request body (JSON):
        {
          "endpoint": "https://...",
          "keys": {"p256dh": "...", "auth": "..."}
        }

    Returns:
        JSON with 'ok': True on success.
    """
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "").strip()
    keys = data.get("keys", {})
    p256dh = keys.get("p256dh", "").strip()
    auth = keys.get("auth", "").strip()

    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "Missing endpoint or keys"}), 400

    user_agent = request.headers.get("User-Agent", "")[:256]
    database.save_push_subscription(endpoint, p256dh, auth, user_agent)
    logger.info("Push subscription saved for endpoint %s…", endpoint[:40])
    return jsonify({"ok": True})


@notifications_bp.route("/unsubscribe", methods=["DELETE"])
def unsubscribe_push():
    """Remove a browser push subscription.

    Request body (JSON):
        {"endpoint": "https://..."}

    Returns:
        JSON with 'ok': True on success.
    """
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "").strip()
    if not endpoint:
        return jsonify({"ok": False, "error": "Missing endpoint"}), 400

    database.delete_push_subscription(endpoint)
    logger.info("Push subscription removed for endpoint %s…", endpoint[:40])
    return jsonify({"ok": True})


@notifications_bp.route("/vapid-public-key", methods=["GET"])
def get_vapid_public_key():
    """Serve the VAPID public key for the browser to use in pushManager.subscribe().

    Returns:
        JSON with 'public_key' string (base64url-encoded).
    """
    public_key = webpush.get_vapid_public_key()
    return jsonify({"public_key": public_key})


@notifications_bp.route("/sound", methods=["GET"])
def serve_notification_sound():
    """Serve the notification alert sound file.

    Returns:
        The .wav audio file for use in the in-app toast player.
    """
    sound_path = os.path.join(_VIBHU_DIR, "notification.wav")
    if not os.path.exists(sound_path):
        logger.warning("Notification sound file not found at: %s", sound_path)
        return Response("Sound file not found", status=404)
    return send_file(sound_path, mimetype="audio/wav")


@notifications_bp.route("/test", methods=["POST"])
def send_test_notification():
    """Fire a test notification across all channels (development helper).

    Returns:
        JSON with 'ok': True and the new notification id.
    """
    from notifications.service import dispatch
    notification_id = dispatch(
        notification_type="KITE_LOGIN_CHECK",
        title="Test Notification",
        body="This is a test alert from the notification system.",
        metadata={"source": "manual_test"},
    )
    return jsonify({"ok": True, "notification_id": notification_id})
