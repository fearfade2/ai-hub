import asyncio
import time
import unittest
from pathlib import Path

from app import config
from app.gateway import _estimate_tokens, _quota_block
from app.pricing import Pricing
from app.store import Store, billing_day, hash_key


class QuotaTests(unittest.TestCase):
    def test_billing_day_flips_at_reset_hour(self):
        # 2026-11-17 04:00 UTC is already the next billing day (reset at 03:00).
        moment = time.mktime(time.gmtime(0)) + 0
        from calendar import timegm
        base = timegm((2026, 11, 17, 4, 0, 0))
        self.assertEqual(billing_day(base), "2026-11-17")
        just_before = timegm((2026, 11, 17, 2, 59, 0))
        self.assertEqual(billing_day(just_before), "2026-11-16")

    def test_reset_in_seconds_is_within_a_day(self):
        store = Store(daily_requests=5, daily_usd=1)
        try:
            for shift in range(0, 24 * 3600, 3600):
                remaining = store.reset_in_seconds(time.time() + shift)
                self.assertTrue(0 <= remaining <= 86400, remaining)
        finally:
            store.close()

    def test_quota_block_triggers_on_requests_and_usd(self):
        class Row(dict):
            def __getitem__(self, item):
                return self.get(item)
        key = Row(daily_requests=3, daily_usd=0.5)
        self.assertIsNone(_quota_block(key, {"requests": 2, "usd": 0.1}))
        self.assertIsNotNone(_quota_block(key, {"requests": 3, "usd": 0.1}))
        self.assertIsNotNone(_quota_block(key, {"requests": 1, "usd": 0.5}))
        unlimited = Row(daily_requests=3, daily_usd=0)
        self.assertIsNone(_quota_block(unlimited, {"requests": 2, "usd": 99}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.sqlite",
                           daily_requests=2, daily_usd=1.0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_key_issue_verify_revoke(self):
        self.store.ensure_user(10, "alice")
        secret, prefix = self.store.issue_key(10, "laptop")
        self.assertTrue(secret.startswith("hub_"))
        row = self.store.verify_key(secret)
        self.assertIsNotNone(row)
        self.assertEqual(row["telegram_id"], 10)
        self.assertEqual(self.store.revoke_by_prefix(prefix), 1)
        self.assertIsNone(self.store.verify_key(secret))
        self.assertEqual(self.store.revoke_by_prefix(prefix), 0)

    def test_usage_rollup_and_quota_view(self):
        self.store.ensure_user(20, "bob")
        secret, _ = self.store.issue_key(20)
        key = self.store.verify_key(secret)
        self.store.record_usage(key["id"], "glm-5.3", prompt_tokens=100,
                                completion_tokens=50, usd=0.01)
        self.store.record_usage(key["id"], "glm-5.3", prompt_tokens=10,
                                completion_tokens=5, usd=0.001)
        totals = self.store.today_totals(key["id"])
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["pt"], 110)
        self.assertEqual(totals["ct"], 55)
        self.assertAlmostEqual(totals["usd"], 0.011, places=6)

    def test_bad_key_rejected(self):
        self.assertIsNone(self.store.verify_key("hub_nope"))
        self.assertIsNone(self.store.verify_key(""))

    def test_hash_key_stable(self):
        self.assertEqual(hash_key("abc"), hash_key("abc"))
        self.assertNotEqual(hash_key("abc"), hash_key("abd"))


class PricingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "pricing.json"
        path.write_text('{"default": {"in": 3, "out": 9},'
                        ' "glm-*": {"in": 0.5, "out": 1.5}}')
        self.pricing = Pricing(path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pattern_and_default_rates(self):
        self.assertEqual(self.pricing.rates("glm-5.3"), (0.5, 1.5))
        self.assertEqual(self.pricing.rates("unknown-model"), (3.0, 9.0))

    def test_cost_math(self):
        self.assertAlmostEqual(self.pricing.cost("glm-x", 1_000_000, 0), 0.5)
        self.assertAlmostEqual(self.pricing.cost("glm-x", 0, 2_000_000), 3.0)
        self.assertAlmostEqual(self.pricing.cost("m", 500_000, 500_000), 1.5 + 4.5)

    def test_corrupt_file_keeps_defaults(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        bad = Path(tmp.name) / "p.json"
        bad.write_text("{not json")
        pricing = Pricing(bad)
        self.assertEqual(pricing.rates("any"), (3.0, 9.0))
        tmp.cleanup()


class EstimateTests(unittest.TestCase):
    def test_estimate_tokens_positive(self):
        messages = [{"role": "user", "content": "a" * 400}]
        self.assertGreater(_estimate_tokens(messages), 0)
        self.assertEqual(_estimate_tokens([]), 1)


if __name__ == "__main__":
    unittest.main()
