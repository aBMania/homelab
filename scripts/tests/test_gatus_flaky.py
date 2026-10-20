import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "clusters", "main", "kubernetes", "core", "gatus", "app", "flaky"))

import flaky  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
WINDOW = timedelta(hours=6)


def series(fails, n=360, step=MIN, end=NOW):
    """n results ending at `end`, one per step; indexes in `fails` (0 = oldest) failed."""
    start = end - step * (n - 1)
    return [{"success": i not in fails,
             "timestamp": (start + step * i).isoformat().replace("+00:00", "Z")}
            for i in range(n)]


def run(a, length):
    return set(range(a, a + length))


class TestOutages(unittest.TestCase):
    def test_single_failure_is_not_an_outage(self):
        self.assertEqual(flaky.outages(series({100}), NOW, WINDOW, MIN), [])

    def test_two_consecutive_failures_is_one_outage(self):
        self.assertEqual(len(flaky.outages(series({100, 101}), NOW, WINDOW, MIN)), 1)

    def test_gap_in_results_is_not_an_outage(self):
        # successes, then 2 h with no results at all (node:down), then successes
        before = series(set(), n=60, end=NOW - timedelta(hours=3))
        after = series(set(), n=60, end=NOW)
        v = flaky.assess(before + after, NOW, MIN)
        self.assertEqual(v.outage_count, 0)
        self.assertFalse(v.flaky)

    def test_gap_ends_a_failure_run(self):
        # one failure before a 2 h gap, one after: not one 2-failure outage
        before = series({59}, n=60, end=NOW - timedelta(hours=3))
        after = series({0}, n=60, end=NOW)
        self.assertEqual(flaky.outages(before + after, NOW, WINDOW, MIN), [])


class TestAssess(unittest.TestCase):
    def test_three_outages_is_flaky(self):
        v = flaky.assess(series(run(50, 2) | run(150, 2) | run(250, 2)), NOW, MIN)
        self.assertEqual(v.outage_count, 3)
        self.assertTrue(v.flaky)

    def test_ratio_over_5_percent_is_flaky(self):
        v = flaky.assess(series(run(100, 12) | run(200, 10)), NOW, MIN)
        self.assertEqual(v.outage_count, 2)
        self.assertAlmostEqual(v.failed_ratio, 22 / 360, places=3)
        self.assertTrue(v.flaky)

    def test_single_long_outage_not_flaky(self):
        # one 25-min incident is not "repeated short outages"
        self.assertFalse(flaky.assess(series(run(100, 25)), NOW, MIN).flaky)

    def test_open_outage_not_counted(self):
        # failing for the last 30 min (not yet down for 1 h): Gatus's own alert handles it
        v = flaky.assess(series(run(50, 3) | run(150, 3) | run(330, 30)), NOW, MIN)
        self.assertEqual(v.outage_count, 2)
        self.assertFalse(v.flaky)

    def test_recovered_long_outage_not_counted(self):
        # a 70-min outage already alerted by Gatus ("down for 1 h") doesn't also make it flaky
        v = flaky.assess(series(run(100, 70) | run(250, 3)), NOW, MIN)
        self.assertEqual(v.outage_count, 1)
        self.assertFalse(v.flaky)

    def test_under_thresholds_not_flaky(self):
        v = flaky.assess(series(run(50, 5) | run(200, 5)), NOW, MIN)
        self.assertEqual((v.outage_count, v.failed_minutes), (2, 10))
        self.assertFalse(v.flaky)

    def test_single_misses_do_not_count(self):
        v = flaky.assess(series(set(range(0, 360, 10))), NOW, MIN)  # 36 isolated misses
        self.assertEqual((v.outage_count, v.failed_minutes), (0, 0))
        self.assertFalse(v.flaky)

    def test_few_results_after_restart_not_flaky(self):
        # fresh Gatus: 10 results, one 2-failure outage = 2 min, far from 5 % of 6 h
        v = flaky.assess(series(run(3, 2), n=10), NOW, MIN)
        self.assertFalse(v.flaky)

    def test_down_now_is_not_flaky(self):
        v = flaky.assess(series(run(290, 70)), NOW, MIN)
        self.assertTrue(v.down_now)
        self.assertFalse(v.flaky)

    def test_window_uses_timestamps_not_count(self):
        old = series(run(0, 30), n=100, end=NOW - timedelta(hours=7))  # outside the window
        recent = series(set(), n=100, end=NOW)
        v = flaky.assess(old + recent, NOW, MIN)
        self.assertEqual(v.outage_count, 0)
        self.assertFalse(v.flaky)

    def test_five_minute_interval(self):
        five = timedelta(minutes=5)
        v = flaky.assess(series(run(10, 2) | run(30, 2) | run(50, 2), n=72, step=five), NOW, five)
        self.assertEqual((v.outage_count, v.failed_minutes), (3, 30))
        self.assertTrue(v.flaky)


FLAKY = flaky.Verdict(flaky=True, down_now=False, outage_count=3, failed_minutes=12, failed_ratio=0.03)
CLEAN = flaky.Verdict(flaky=False, down_now=False, outage_count=0, failed_minutes=0, failed_ratio=0.0)
ONE = flaky.Verdict(flaky=False, down_now=False, outage_count=1, failed_minutes=2, failed_ratio=0.005)
D = date(2026, 10, 20)


class TestDecide(unittest.TestCase):
    def test_flaky_sent_once_per_day(self):
        kind, state = flaky.decide("k", FLAKY, {}, D)
        self.assertEqual(kind, "flaky")
        kind, state = flaky.decide("k", FLAKY, state, D)
        self.assertIsNone(kind)
        kind, _ = flaky.decide("k", FLAKY, state, D + timedelta(days=1))
        self.assertEqual(kind, "flaky")

    def test_stable_after_flaky_when_window_clean(self):
        _, state = flaky.decide("k", FLAKY, {}, D)
        kind, state = flaky.decide("k", ONE, state, D)
        self.assertIsNone(kind)  # not flaky any more, but the window isn't clean yet
        kind, state = flaky.decide("k", CLEAN, state, D)
        self.assertEqual(kind, "stable")
        kind, _ = flaky.decide("k", CLEAN, state, D)
        self.assertIsNone(kind)

    def test_no_stable_without_prior_flaky(self):
        kind, _ = flaky.decide("k", CLEAN, {}, D)
        self.assertIsNone(kind)

    def test_parses_gatus_nanosecond_timestamps(self):
        # Gatus returns 9 fractional digits (seen on v5.37.0)
        results = [{"success": False, "timestamp": "2026-10-20T11:58:00.741792696Z"},
                   {"success": False, "timestamp": "2026-10-20T11:59:00.759303475Z"}]
        self.assertEqual(len(flaky.outages(results, NOW, WINDOW, MIN)), 1)

    def test_message(self):
        v = flaky.Verdict(flaky=True, down_now=False, outage_count=9, failed_minutes=34, failed_ratio=0.09)
        self.assertEqual(flaky.message("downloads_vpn-connected", v),
                         "downloads/vpn-connected flaky: down 9 times / 34 min in the last 6 h")


class FakeGatus:
    def __init__(self, results_by_key):
        self.results = results_by_key

    def get_json(self, url):
        if url.endswith("/api/v1/endpoints/statuses"):
            return [{"key": k} for k in self.results]
        key = url.split("/api/v1/endpoints/")[1].split("/")[0]
        return {"key": key, "results": self.results[key]}


class TestMain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "flaky.json")
        self.sent = []

    def sender(self, title, text, priority, tags):
        self.sent.append((title, text, priority))

    def test_missing_state_starts_fresh(self):
        self.assertEqual(flaky.load_state(self.state), {})
        with open(self.state, "w") as f:
            f.write("{not json")
        self.assertEqual(flaky.load_state(self.state), {})

    def test_silent_mode_logs_but_does_not_send(self):
        gatus = FakeGatus({"downloads_vpn-connected": series(run(50, 2) | run(150, 2) | run(250, 2))})
        rc = flaky.main(gatus=gatus, sender=self.sender, state_path=self.state, now=NOW, notify=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent, [])
        with open(self.state) as f:
            self.assertIn("downloads_vpn-connected", json.load(f))

    def test_notify_mode_sends_once(self):
        gatus = FakeGatus({"downloads_vpn-connected": series(run(50, 2) | run(150, 2) | run(250, 2)),
                           "login_authelia": series(set(), n=72, step=timedelta(minutes=5))})
        for _ in range(2):
            flaky.main(gatus=gatus, sender=self.sender, state_path=self.state, now=NOW, notify=True)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][2], 3)
        self.assertIn("downloads/vpn-connected flaky", self.sent[0][1])


if __name__ == "__main__":
    unittest.main()
