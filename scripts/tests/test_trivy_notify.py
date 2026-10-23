import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "clusters", "main", "kubernetes", "core", "gatus", "app", "trivy"))

import trivy_notify  # noqa: E402


def vuln(cve, sev="CRITICAL", fixed="1.1", pkg="libfoo"):
    return {"vulnerabilityID": cve, "severity": sev, "fixedVersion": fixed, "resource": pkg,
            "installedVersion": "1.0"}


def report(vulns, repo="org/app", tag="1.0", ns="media", name="app", server="ghcr.io"):
    return {"metadata": {"labels": {"trivy-operator.resource.namespace": ns,
                                    "trivy-operator.resource.name": name}},
            "report": {"registry": {"server": server},
                       "artifact": {"repository": repo, "tag": tag},
                       "vulnerabilities": vulns}}


class TestCriticals(unittest.TestCase):
    def test_only_critical_with_fix(self):
        found = trivy_notify.criticals([report([
            vuln("CVE-1"), vuln("CVE-2", sev="HIGH"), vuln("CVE-3", fixed="")])])
        self.assertEqual([f["id"] for f in found.values()], ["CVE-1"])
        f = next(iter(found.values()))
        self.assertEqual(f["image"], "ghcr.io/org/app:1.0")
        self.assertEqual(f["workload"], "media/app")

    def test_same_cve_two_images_two_keys(self):
        found = trivy_notify.criticals([report([vuln("CVE-1")]),
                                        report([vuln("CVE-1")], repo="org/other")])
        self.assertEqual(len(found), 2)

    def test_null_vulnerabilities(self):
        self.assertEqual(trivy_notify.criticals([report(None)]), {})


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.cur = trivy_notify.criticals([report([vuln("CVE-1"), vuln("CVE-2")])])

    def test_first_run_is_baseline(self):
        msg, state = trivy_notify.decide(self.cur, None, "2026-10-23")
        self.assertTrue(msg[0].startswith("Trivy baseline: 2 critical"))
        self.assertEqual(set(state), set(self.cur))

    def test_nothing_new_sends_nothing(self):
        state = {k: "2026-10-22" for k in self.cur}
        msg, new = trivy_notify.decide(self.cur, state, "2026-10-23")
        self.assertIsNone(msg)
        self.assertEqual(new, state)

    def test_new_cve_sent_and_fixed_one_dropped(self):
        old = {k: "2026-10-22" for k in self.cur}
        old["ghcr.io/org/app:0.9|CVE-0|libfoo"] = "2026-10-01"
        cur = dict(self.cur, **trivy_notify.criticals([report([vuln("CVE-9")])]))
        msg, new = trivy_notify.decide(cur, old, "2026-10-23")
        self.assertEqual(msg[0], "Trivy: 1 new critical CVE")
        self.assertIn("CVE-9 libfoo 1.0 -> 1.1", msg[1])
        self.assertNotIn("CVE-1", msg[1])
        self.assertNotIn("ghcr.io/org/app:0.9|CVE-0|libfoo", new)
        self.assertEqual(new["ghcr.io/org/app:1.0|CVE-9|libfoo"], "2026-10-23")

    def test_long_message_clipped(self):
        cur = trivy_notify.criticals([report([vuln(f"CVE-{i}") for i in range(500)])])
        msg, _ = trivy_notify.decide(cur, {}, "2026-10-23")
        self.assertLessEqual(len(msg[1]), trivy_notify.MAX_TEXT + 1)
        self.assertTrue(msg[1].endswith("…"))


class TestMain(unittest.TestCase):
    def run_main(self, reports, state=None, sender=None):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "trivy.json")
        if state is not None:
            with open(path, "w") as f:
                json.dump(state, f)
        sent = []
        rc = trivy_notify.main(reports=reports, state_path=path, today="2026-10-23", notify=True,
                               sender=sender or (lambda *a: sent.append(a)))
        saved = json.load(open(path)) if os.path.exists(path) else None
        return rc, sent, saved

    def test_no_reports_no_state(self):
        rc, sent, saved = self.run_main([])
        self.assertEqual((rc, sent, saved), (0, [], None))

    def test_baseline_then_quiet(self):
        reps = [report([vuln("CVE-1")])]
        _, sent, saved = self.run_main(reps)
        self.assertEqual(len(sent), 1)
        _, sent2, _ = self.run_main(reps, state=saved)
        self.assertEqual(sent2, [])

    def test_send_failure_keeps_old_state(self):
        def boom(*a):
            raise OSError("down")
        rc, _, saved = self.run_main([report([vuln("CVE-1")])], state={}, sender=boom)
        self.assertEqual(rc, 1)
        self.assertEqual(saved, {})


if __name__ == "__main__":
    unittest.main()
