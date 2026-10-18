"""node-up.sh against fake kubectl/flux/talosctl: state file override and the data-disk check."""
import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "node-up.sh")
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
line = " ".join([os.path.basename(sys.argv[0])] + sys.argv[1:])
open(os.environ["FAKE_LOG"], "a").write(line + "\n")
for key, val in json.load(open(os.environ["FAKE_FIXTURES"])).items():
    if key in line:
        if isinstance(val, dict) and "__rc__" in val: sys.exit(val["__rc__"])
        sys.stdout.write(val if isinstance(val, str) else json.dumps(val)); sys.exit(0)
"""
BASE = {"get clusters.postgresql.cnpg.io": {"items": []}, "get hr -A -o json": {"items": []},
        "get volumestatus u-data": "ready"}


class NodeUp(unittest.TestCase):
    def run_up(self, fixtures=BASE):
        d = tempfile.mkdtemp()
        for b in ("kubectl", "flux", "talosctl"):
            p = os.path.join(d, b); open(p, "w").write(FAKE); os.chmod(p, 0o755)
        fx, log, st = os.path.join(d, "fx.json"), os.path.join(d, "log"), os.path.join(d, "q.json")
        json.dump(fixtures, open(fx, "w"))
        json.dump({"targets": [{"kind": "Deployment", "ns": "media", "name": "radarr", "replicas": 1}],
                   "already_suspended_ks": [], "already_suspended_hr": []}, open(st, "w"))
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log, FAKE_FIXTURES=fx,
                   NODE_DOWN_STATE=st, NODE_UP_POLL="0")
        r = subprocess.run(["bash", SCRIPT, "--no-wait-node"], env=env, capture_output=True, text=True, timeout=60)
        return r, open(log).read().splitlines()

    def test_reads_the_overridden_state_file(self):
        r, calls = self.run_up()
        self.assertTrue(any("scale deploy radarr --replicas=1" in c for c in calls), r.stderr)

    def test_data_disk_ready(self):
        r, _ = self.run_up()
        self.assertIn("OK   data disk", r.stderr)

    def test_data_disk_not_ready_fails(self):
        r, _ = self.run_up(dict(BASE, **{"get volumestatus u-data": "waiting"}))
        self.assertIn("FAIL data disk", r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_no_longhorn_left(self):
        r, calls = self.run_up()
        self.assertFalse(any("longhorn" in c for c in calls))
        self.assertNotIn("longhorn", open(SCRIPT).read().lower())


if __name__ == "__main__":
    unittest.main()
