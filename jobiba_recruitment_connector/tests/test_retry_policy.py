from odoo.tests import BaseCase, tagged

from ..services import retry_policy, webhook_signature
from ..services.jobiba_client import JobibaClient, JobibaConfigError, clean_base_url
from ..models.jobiba_sync_log import redact


@tagged("post_install", "-at_install", "jobiba")
class TestPureRules(BaseCase):
    def test_classify(self):
        self.assertEqual(retry_policy.classify(200), retry_policy.OK)
        self.assertEqual(retry_policy.classify(401), retry_policy.CREDENTIAL)
        self.assertEqual(retry_policy.classify(403), retry_policy.PLAN)
        self.assertEqual(retry_policy.classify(422), retry_policy.VALIDATION)
        self.assertEqual(retry_policy.classify(429), retry_policy.RATE_LIMITED)
        self.assertEqual(retry_policy.classify(503), retry_policy.TRANSIENT)
        self.assertEqual(retry_policy.classify(None), retry_policy.TRANSIENT)
        self.assertEqual(retry_policy.classify(404), retry_policy.REJECTED)

    def test_backoff_grows_and_is_capped(self):
        low = [retry_policy.backoff_seconds(n, rng=lambda: 0.0) for n in range(1, 12)]
        self.assertEqual(low[:3], [48, 96, 192])
        self.assertTrue(all(a <= b for a, b in zip(low, low[1:])))
        self.assertLessEqual(retry_policy.backoff_seconds(40, rng=lambda: 1.0), int(retry_policy.MAX_DELAY_SECONDS * 1.2))

    def test_retry_after_is_never_shortened(self):
        self.assertGreaterEqual(retry_policy.rate_limit_seconds("60", rng=lambda: 0.0), 60)
        self.assertGreaterEqual(retry_policy.rate_limit_seconds(None, rng=lambda: 0.0), retry_policy.DEFAULT_RETRY_AFTER_SECONDS)
        self.assertGreaterEqual(retry_policy.rate_limit_seconds("abc"), retry_policy.DEFAULT_RETRY_AFTER_SECONDS)

    def test_signature(self):
        body, secret, now = b'{"id":"d1","event":"ping"}', "whsec_abcdefghijklmnop", 1_800_000_000
        good = "sha256=" + webhook_signature.sign(secret, now, body)
        self.assertTrue(webhook_signature.verify(secret, str(now), body, good, now + 10))
        self.assertFalse(webhook_signature.verify(secret, str(now), body + b" ", good, now))
        self.assertFalse(webhook_signature.verify("other-secret-value-123", str(now), body, good, now))
        self.assertFalse(webhook_signature.verify(secret, str(now), body, good, now + 301), "stale timestamp")
        self.assertFalse(webhook_signature.verify(secret, "not-a-number", body, good, now))
        self.assertFalse(webhook_signature.verify(secret, str(now), body, "", now))
        self.assertFalse(webhook_signature.verify("", str(now), body, good, now))

    def test_base_url_rules(self):
        self.assertEqual(clean_base_url("https://jobiba.com/"), "https://jobiba.com")
        self.assertEqual(clean_base_url("http://localhost:3200"), "http://localhost:3200")
        for bad in ("http://jobiba.com", "https://user:pw@jobiba.com", "https://jobiba.com/api", "ftp://jobiba.com", "https://"):
            with self.assertRaises(JobibaConfigError):
                clean_base_url(bad)
        with self.assertRaises(JobibaConfigError):
            JobibaClient("https://jobiba.com", "")
        self.assertNotIn("jbk_secret", repr(JobibaClient("https://jobiba.com", "jbk_secret")))

    def test_log_redaction(self):
        text = redact("Bearer jbk_live_abcdefghijklmnopqrstuvwxyz0123456789 for asha@example.com call +91 98765 43210")
        for leak in ("jbk_live", "asha@example.com", "98765"):
            self.assertNotIn(leak, text)


@tagged("post_install", "-at_install", "jobiba")
class TestReadableDate(BaseCase):
    def test_applied_date_reads_like_a_date(self):
        from ..models.jobiba_application_mapping import readable_utc
        self.assertEqual(readable_utc("2026-11-24T09:59:00.000Z"), "24 Nov 2026, 09:59 UTC")
        self.assertEqual(readable_utc("2026-11-24T15:29:00+05:30"), "24 Nov 2026, 09:59 UTC")
        self.assertEqual(readable_utc("yesterday"), "yesterday")     # shown as it came, never guessed
        self.assertEqual(readable_utc("2026-11-24T09:59:00"), "2026-11-24T09:59:00")   # no zone = not converted
        self.assertFalse(readable_utc(None))
