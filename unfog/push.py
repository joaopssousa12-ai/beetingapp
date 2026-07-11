"""Web push notifications — the daily nudge that fights ADHD-app churn.

Entirely optional and self-contained: if the VAPID_* env vars aren't set (or
pywebpush isn't installed), ENABLED is False and every function is a safe no-op,
so the rest of the app never breaks. Generate a key pair once with:

    python -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); \
        import base64; \
        print('PUBLIC', base64.urlsafe_b64encode(v.public_key.public_bytes(\
        __import__('cryptography').hazmat.primitives.serialization.Encoding.X962,\
        __import__('cryptography').hazmat.primitives.serialization.PublicFormat.UncompressedPoint)).decode().rstrip('=')); \
        print('PRIVATE', base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32,'big')).decode().rstrip('='))"

Then set VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY and VAPID_SUBJECT (mailto:you@x.com).
The CLI `vapid --gen` from py-vapid also works. See docs/NOTIFICATIONS.md.
"""
import json
import os

PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:hello@unfog.app").strip()

try:
    from pywebpush import webpush, WebPushException
    _HAS_LIB = True
except Exception:
    _HAS_LIB = False

ENABLED = bool(_HAS_LIB and PUBLIC_KEY and PRIVATE_KEY)


def send(subscription, title, body, url="/app"):
    """Send one push. Returns True on success, False (and never raises) on failure.
    A 404/410 means the subscription is dead — caller should delete it."""
    if not ENABLED:
        return False
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=PRIVATE_KEY,
            vapid_claims={"sub": SUBJECT},
            ttl=86400,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            raise DeadSubscription()
        return False
    except Exception:
        return False


class DeadSubscription(Exception):
    """Raised when a push endpoint is gone (404/410) so the caller can prune it."""
