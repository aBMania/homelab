"""move-volume.sh against fake kubectl/flux/gh binaries: checks the safety gates, not the copy."""
import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "move-volume.sh")

# A fake CLI: logs its argv to $FAKE_LOG, then answers from $FAKE_RULES (first rule whose
# substring is in the joined argv wins): {"match": str, "out": str, "rc": int}.
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
argv = [os.path.basename(sys.argv[0])] + sys.argv[1:]
line = " ".join(argv)
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(line + "\n")
for r in json.load(open(os.environ["FAKE_RULES"])):
    if r["match"] in line:
        sys.stdout.write(r.get("out", ""))
        sys.exit(r.get("rc", 0))
"""

OK_RULES = [
    {"match": "gh pr view 42 --json state", "out": "MERGED"},
    {"match": "mergeStateStatus", "out": "CLEAN"},
    {"match": "gh pr view 42 --json statusCheckRollup", "out": "0"},
    {"match": "get pvc app-data", "out": "app-data"},
    {"match": "logs app-data-rsync-pass2", "out": "files src=10 dst=10\n"},
    {"match": "logs app-data-rsync-pass1", "out": "files src=10 dst=9\n"},
    {"match": "get pods -l", "out": ""},
    {"match": "matchLabels", "out": "app.kubernetes.io/instance=app,app.kubernetes.io/name=app,"},
    {"match": "-o jsonpath={.status.phase}", "out": "Succeeded"},
    {"match": "get deploy app -o jsonpath", "out": "app-data"},
    {"match": "get secret cluster-config", "out": "MTAuMC4wLjE="},  # 10.0.0.1
]


class MoveVolume(unittest.TestCase):
    def run_move(self, rules, *extra, idle="true", env_extra=None):
        d = tempfile.mkdtemp()
        for b in ("kubectl", "flux", "gh"):
            p = os.path.join(d, b)
            open(p, "w").write(FAKE)
            os.chmod(p, 0o755)
        log, rf = os.path.join(d, "log"), os.path.join(d, "rules.json")
        json.dump(rules, open(rf, "w"))
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log, FAKE_RULES=rf,
                   EMBY_IDLE_CMD=idle, MOVE_POLL="0", MOVE_STATE_DIR=d, **(env_extra or {}))
        r = subprocess.run(["bash", SCRIPT, "media", "app", "app", "pvc:app-config", "app-data", "42", *extra],
                           env=env, capture_output=True, text=True, timeout=60)
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        return r, calls

    def test_happy_path_resumes_on_new_claim(self):
        r, calls = self.run_move(OK_RULES)
        self.assertEqual(r.returncode, 0, r.stderr)
        order = [c for c in calls if c.startswith(("flux suspend", "gh pr merge", "kubectl -n media scale", "flux resume"))]
        self.assertEqual([c.split()[0] + " " + c.split()[1] for c in order],
                         ["flux suspend", "gh pr", "kubectl -n", "flux resume"])
        self.assertTrue(any("--replicas=0" in c for c in calls))

    def test_emby_busy_at_start_changes_nothing(self):
        r, calls = self.run_move(OK_RULES, idle="false")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Emby busy", r.stderr)
        self.assertFalse(any(c.startswith(("flux suspend", "gh pr merge")) or "--replicas=0" in c for c in calls))

    def test_emby_busy_after_merge_keeps_hr_suspended(self):
        # idle at start, busy at the stop: the HR already points at the new claim, so resuming
        # would start the app on the pass-1 copy. It must stay suspended (app on its old volume).
        d = tempfile.mkdtemp(); flag = os.path.join(d, "n"); idle = os.path.join(d, "idle")
        open(idle, "w").write(f"#!/bin/bash\nn=$(cat {flag} 2>/dev/null || echo 0); echo $((n+1)) > {flag}; [ $n = 0 ]\n")
        os.chmod(idle, 0o755)
        r, calls = self.run_move(OK_RULES, idle=idle)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rerun", r.stderr.lower())
        self.assertFalse(any("--replicas=0" in c for c in calls))
        self.assertFalse(any(c.startswith("flux resume") for c in calls))

    def test_failed_pass1_stops_before_the_app(self):
        rules = [{"match": "logs app-data-rsync-pass1", "out": "rsync error: some files (code 23)\n"}] + OK_RULES
        r, calls = self.run_move(rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pass1 failed", r.stderr)
        self.assertFalse(any("--replicas=0" in c for c in calls))
        self.assertFalse(any(c.startswith("flux resume") for c in calls))

    def test_pods_that_never_stop_block_pass2(self):
        rules = [{"match": "get pods -l", "out": "pod/app-x"}] + OK_RULES
        r, calls = self.run_move(rules, env_extra={"MOVE_STOP_TIMEOUT": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("still running", r.stderr)
        self.assertFalse(any("rsync-pass2" in c for c in calls))
        self.assertFalse(any(c.startswith("flux resume") for c in calls))

    def test_empty_selector_blocks_pass2(self):
        rules = [{"match": "matchLabels", "out": ""}] + OK_RULES
        r, calls = self.run_move(rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any("rsync-pass2" in c for c in calls))

    def test_exclude_option_is_gone(self):
        r, _ = self.run_move(OK_RULES, "--exclude", "cache")
        self.assertEqual(r.returncode, 2)

    def test_count_mismatch_stops_without_resume(self):
        rules = [{"match": "logs app-data-rsync-pass2", "out": "files src=10 dst=9\n"}] + OK_RULES
        r, calls = self.run_move(rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("counts differ", r.stderr)
        self.assertIn("SUSPENDED", r.stderr)
        self.assertFalse(any(c.startswith("flux resume") for c in calls))

    def test_red_checks_abort_before_merge(self):
        rules = [{"match": "select(.conclusion", "out": "1"}] + OK_RULES  # checks done, one failed
        r, calls = self.run_move(rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any(c.startswith("gh pr merge") for c in calls))
        self.assertTrue(any(c.startswith("flux resume hr -n media app") for c in calls))

    def test_refused_merge_aborts_and_resumes(self):
        rules = [{"match": "gh pr merge", "rc": 1}, {"match": "gh pr view 42 --json state", "out": "OPEN"}] + OK_RULES
        r, calls = self.run_move(rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not merged", r.stderr)
        self.assertTrue(any(c.startswith("flux resume hr -n media app") for c in calls))
        self.assertFalse(any("--replicas=0" in c for c in calls))

    def test_waits_until_mergeable(self):
        # required checks not done (or not registered yet): must not merge; times out, aborts, resumes
        rules = [{"match": "mergeStateStatus", "out": "BLOCKED"}] + OK_RULES
        r, calls = self.run_move(rules, env_extra={"MOVE_CHECKS_TIMEOUT": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any(c.startswith("gh pr merge") for c in calls))
        self.assertTrue(any(c.startswith("flux resume hr -n media app") for c in calls))

    def test_already_merged_pr_is_resumable(self):
        rules = [{"match": "mergeStateStatus", "out": "UNKNOWN"}, {"match": "--json mergedAt", "out": "2026-10-18T18:00:00Z"}] + OK_RULES
        r, calls = self.run_move(rules, env_extra={"MOVE_CHECKS_TIMEOUT": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any(c.startswith("gh pr merge") for c in calls))
        self.assertTrue(any(c.startswith("flux resume hr -n media app") for c in calls))

    def test_waits_only_for_the_deployments_pods(self):
        r, calls = self.run_move(OK_RULES)
        self.assertEqual(r.returncode, 0, r.stderr)
        waits = [c for c in calls if "get pods -l" in c]
        self.assertTrue(waits)
        self.assertTrue(all("app.kubernetes.io/name=app" in c for c in waits), waits)

    def test_dry_run_changes_nothing(self):
        r, calls = self.run_move(OK_RULES, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        writes = [c for c in calls if any(w in c for w in ("suspend", "resume", "pr merge", "scale", "apply", "delete"))]
        self.assertEqual(writes, [])

    def test_wrong_claim_after_rollout_fails(self):
        rules = [{"match": "get deploy app -o jsonpath", "out": "app-config"}] + OK_RULES
        r, _ = self.run_move(rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("app-config", r.stderr)

    def test_nfs_source_mounts_nfs_read_only(self):
        d_rules = OK_RULES
        r, calls = self.run_move(d_rules)  # baseline pvc source
        self.assertEqual(r.returncode, 0, r.stderr)
        # nfs: source
        dd = tempfile.mkdtemp()
        for b in ("kubectl", "flux", "gh"):
            p = os.path.join(dd, b); open(p, "w").write(FAKE); os.chmod(p, 0o755)
        log, rf = os.path.join(dd, "log"), os.path.join(dd, "rules.json")
        json.dump(OK_RULES, open(rf, "w"))
        manifests = os.path.join(dd, "manifests")
        env = dict(os.environ, PATH=dd + ":" + os.environ["PATH"], FAKE_LOG=log, FAKE_RULES=rf,
                   EMBY_IDLE_CMD="true", MOVE_POLL="0", MOVE_STATE_DIR=dd, MOVE_SAVE_MANIFESTS=manifests)
        r = subprocess.run(["bash", SCRIPT, "media", "app", "app", "nfs:/mnt/tank/apps/app", "app-data", "42"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = open(manifests).read()
        self.assertIn("server: 10.0.0.1", m)
        self.assertIn("path: /mnt/tank/apps/app", m)
        self.assertIn("runAsUser: 568", m)
        # as 568, rsync can't chgrp the new volume's root (owned by root): don't try
        # the new volume's root is root-owned: a root init container hands it to 568 first
        self.assertIn("chown 568:568 /dst", m)
        self.assertIn("--no-owner --no-group", m)


if __name__ == "__main__":
    unittest.main()
