import json
import logging
import time

from odoo import http
from odoo.http import request

from ..services import webhook_signature

_logger = logging.getLogger(__name__)


def _answer(payload, status=200):
    return request.make_response(
        json.dumps(payload), status=status,
        headers=[("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex")],
    )


class JobibaWebhook(http.Controller):
    """Receives Jobiba's signed webhooks.

    Order: size, connection, signature + timestamp (on the raw bytes), JSON,
    duplicate check, durable receipt. A refused request stores nothing from
    its body. Processing is light (no downloads), so it runs before the answer;
    a processing error keeps the event for a person and still answers 200, so
    Jobiba does not send the same event again.
    """

    @http.route("/jobiba/webhook/<string:token>", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    def receive(self, token, **_kwargs):
        http_request = request.httprequest
        length = http_request.content_length or 0
        if length > webhook_signature.MAX_BODY_BYTES:
            return _answer({"error": "too_large"}, 413)
        raw = http_request.get_data(cache=False)
        if len(raw) > webhook_signature.MAX_BODY_BYTES:
            return _answer({"error": "too_large"}, 413)
        if not token or len(token) < 20 or len(token) > 100:
            return _answer({"error": "not_found"}, 404)
        Instance = request.env["jobiba.instance"].sudo().with_context(active_test=False)
        instance = Instance.search([("webhook_token", "=", token)], limit=1)
        if not instance or not instance.active or instance.state == "disconnected":
            # 410 tells Jobiba the address is gone for good: it stops sending.
            return _answer({"error": "gone"}, 410 if instance else 404)
        headers = http_request.headers
        secret = instance.webhook_secret
        valid = webhook_signature.verify(
            secret, headers.get("X-Jobiba-Timestamp"), raw, headers.get("X-Jobiba-Signature"), time.time())
        if not valid:
            instance._log("security", "webhook_refused", "A webhook with a missing, old or wrong signature was refused.", 401)
            return _answer({"error": "invalid_signature"}, 401)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _answer({"error": "invalid_json"}, 400)
        if not isinstance(body, dict):
            return _answer({"error": "invalid_json"}, 400)
        delivery_id = (headers.get("X-Jobiba-Delivery") or str(body.get("id") or "")).strip()[:100]
        event_type = (headers.get("X-Jobiba-Event") or str(body.get("event") or "")).strip()[:60]
        if not delivery_id or not event_type:
            return _answer({"error": "missing_headers"}, 400)
        event, created = request.env["jobiba.webhook.event"]._ingest(instance, delivery_id, event_type, body)
        if not created:
            return _answer({"ok": True, "duplicate": True})
        event._process()
        return _answer({"ok": True})
