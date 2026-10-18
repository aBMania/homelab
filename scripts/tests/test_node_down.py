"""node-down.sh --dry-run against a fake kubectl serving a small cluster: who gets stopped."""
import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "node-down.sh")

# fake kubectl/talosctl: logs argv, answers "get <what> -A -o json" from fixtures, else empty
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
line = " ".join([os.path.basename(sys.argv[0])] + sys.argv[1:])
open(os.environ["FAKE_LOG"], "a").write(line + "\n")
fx = json.load(open(os.environ["FAKE_FIXTURES"]))
scaled = " scale " in open(os.environ["FAKE_LOG"]).read()
for key, val in fx.items():
    if key.endswith("|after") and not scaled:
        continue
    if key.split("|")[0] in line:
        if isinstance(val, dict) and "__rc__" in val:
            sys.exit(val["__rc__"])
        sys.stdout.write(val if isinstance(val, str) else json.dumps(val)); sys.exit(0)
sys.stdout.write("")
"""


def pod(ns, name, owner_kind=None, owner=None, pvcs=(), labels=None, phase="Running"):
    p = {"metadata": {"namespace": ns, "name": name, "labels": labels or {}},
         "spec": {"volumes": [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in pvcs]},
         "status": {"phase": phase}}
    if owner_kind:
        p["metadata"]["ownerReferences"] = [{"kind": owner_kind, "name": owner}]
    return p


CLUSTER = {
    "get pods -A -o json|after": {"items": [pod("kube-system", "coredns-x", "ReplicaSet", "coredns-rs")]},
    "get crd volumes.longhorn.io": {"__rc__": 1},  # Longhorn gone
    "get pvc -A -o json": {"items": [
        {"metadata": {"namespace": "media", "name": "radarr-data"}, "spec": {"storageClassName": "local-data"}},
        {"metadata": {"namespace": "auth", "name": "pg-1"}, "spec": {"storageClassName": "local-data"}}]},
    "get pods -A -o json": {"items": [
        pod("media", "radarr-x", "ReplicaSet", "radarr-rs", ["radarr-data"]),
        pod("media", "proxy-x", "ReplicaSet", "proxy-rs"),                        # no PVC, app namespace
        pod("auth", "pg-1", "Cluster", "pg", ["pg-1"], {"cnpg.io/cluster": "pg"}),
        pod("kube-system", "coredns-x", "ReplicaSet", "coredns-rs")]},            # infra: left alone
    "get deploy -A -o json": {"items": [
        {"metadata": {"namespace": "media", "name": "radarr"}, "spec": {"replicas": 1}},
        {"metadata": {"namespace": "media", "name": "proxy"}, "spec": {"replicas": 1}},
        {"metadata": {"namespace": "media", "name": "idle"}, "spec": {"replicas": 0}},
        {"metadata": {"namespace": "kube-system", "name": "coredns"}, "spec": {"replicas": 2}}]},
    "get sts -A -o json": {"items": []},
    "get cronjobs -A -o json": {"items": []},
    "get rs radarr-rs": "radarr", "get rs proxy-rs": "proxy",
    "get deploy radarr -o jsonpath": "1", "get deploy proxy -o jsonpath": "1",
    "get ks -o json": {"items": []}, "get hr -A -o json": {"items": []},
}


class NodeDown(unittest.TestCase):
    def run_down(self, *args, idle="true", fixtures=CLUSTER):
        d = tempfile.mkdtemp()
        for b in ("kubectl", "talosctl", "jq-check"):
            p = os.path.join(d, b); open(p, "w").write(FAKE); os.chmod(p, 0o755)
        fx, log = os.path.join(d, "fx.json"), os.path.join(d, "log")
        json.dump(fixtures, open(fx, "w"))
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log, FAKE_FIXTURES=fx,
                   EMBY_IDLE_CMD=idle, NODE_DOWN_STATE=os.path.join(d, "quiesce.json"), NODE_DOWN_POLL="0")
        r = subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=60)
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        self.state = os.path.join(d, "quiesce.json")
        return r, calls

    def test_dry_run_stops_apps_with_and_without_volumes(self):
        r, _ = self.run_down("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Deployment media/radarr", r.stderr)
        self.assertIn("Deployment media/proxy", r.stderr)      # no PVC: stopped too (drain stalls)
        self.assertIn("cnpg auth/pg", r.stderr)
        self.assertNotIn("coredns", r.stderr)                    # infra namespace
        self.assertNotIn("media/idle", r.stderr)                 # already at 0

    def test_dry_run_changes_nothing(self):
        _, calls = self.run_down("--dry-run")
        writes = [c for c in calls if any(w in c for w in (" scale ", " patch ", " annotate ", "shutdown", "reboot"))]
        self.assertEqual(writes, [])

    def test_emby_busy_aborts_before_any_change(self):
        r, calls = self.run_down(idle="false")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Emby", r.stderr)
        self.assertFalse(any(" scale " in c or " patch " in c or " annotate " in c for c in calls))

    def test_unmanaged_pods_dont_block_the_wait(self):
        fx = dict(CLUSTER)
        ds = pod("media", "agent-x", "DaemonSet", "agent")
        fx["get pods -A -o json|after"] = {"items": [pod("kube-system", "coredns-x", "ReplicaSet", "coredns-rs"), ds]}
        fx["get pods -A -o json"] = {"items": CLUSTER["get pods -A -o json"]["items"] + [ds]}
        r, _ = self.run_down(fixtures=fx)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_timeout_names_the_pods_left(self):
        fx = dict(CLUSTER)
        fx["get pods -A -o json|after"] = {"items": [pod("media", "proxy-x", "ReplicaSet", "proxy-rs")]}
        r, _ = self.run_down(fixtures=fx)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("media/proxy-x", r.stderr)

    def test_no_longhorn_left(self):
        src = open(SCRIPT).read().lower()
        self.assertNotIn("longhorn", src)

    def test_quiesce_scales_apps_and_saves_replicas(self):
        r, calls = self.run_down()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("scale deploy proxy --replicas=0" in c for c in calls))
        self.assertTrue(any("scale deploy radarr --replicas=0" in c for c in calls))
        self.assertFalse(any("coredns" in c and " scale " in c for c in calls))
        targets = {(x["kind"], x["ns"], x["name"]): x for x in json.load(open(self.state))["targets"]}
        self.assertEqual(targets[("Deployment", "media", "proxy")]["replicas"], 1)  # node-up restores it


if __name__ == "__main__":
    unittest.main()
