"""Jobiba webhook signature, as documented at jobiba.com/developers#webhooks.

X-Jobiba-Signature: "sha256=" + hex(HMAC-SHA256(secret, "{timestamp}.{body}"))
X-Jobiba-Timestamp: unix seconds. A signature older than 5 minutes is refused.
"""
import hashlib
import hmac

TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 256 * 1024


def sign(secret, timestamp, body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    message = str(int(timestamp)).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret, timestamp, body, signature, now):
    """True only for a fresh timestamp and a matching signature."""
    if not secret or not signature:
        return False
    try:
        stamp = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    if abs(int(now) - stamp) > TOLERANCE_SECONDS:
        return False
    given = str(signature).strip()
    if given.startswith("sha256="):
        given = given[7:]
    expected = sign(secret, stamp, body)
    return hmac.compare_digest(expected, given.lower())
