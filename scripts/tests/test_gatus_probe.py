import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "clusters", "main", "kubernetes", "core", "gatus", "app", "probe"))

import probe  # noqa: E402

TITLE = "SENTINEL-TITLE-Do-Not-Print"


class FakeHttp:
    """Answers (method, path) from a dict; records pushes to Gatus."""

    def __init__(self, routes):
        self.routes = routes
        self.pushes = []

    def request(self, method, url, data=None, headers=None, timeout=10):
        u = urlparse(url)
        if "/external" in u.path:
            q = parse_qs(u.query)
            key = u.path.split("/")[-2]
            self.pushes.append((key, q["success"][0], q.get("error", [""])[0], headers))
            return 200, b""
        body = self.routes.get((method, u.netloc.split(".")[0], u.path))
        if body is None:
            raise OSError(f"no route {method} {url}")
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        return 200, body.encode()


def torrent(state, seeds=0):
    return {"name": TITLE, "state": state, "save_path": f"/downloads/{TITLE}", "num_seeds": seeds}


class TestChecks(unittest.TestCase):
    def test_vpn_connected_ok(self):
        ok, _ = probe.vpn_connected({"connection_status": "connected"})
        self.assertTrue(ok)

    def test_vpn_firewalled_fails(self):
        self.assertEqual(probe.vpn_connected({"connection_status": "firewalled"}),
                         (False, "qBittorrent connection: firewalled"))

    def test_vpn_disconnected_fails(self):
        ok, msg = probe.vpn_connected({"connection_status": "disconnected"})
        self.assertFalse(ok)
        self.assertIn("disconnected", msg)

    def test_stalled_when_active_and_zero_speed(self):
        ts = [torrent("downloading", seeds=3), torrent("stalledDL", seeds=1), torrent("stalledDL")]
        self.assertEqual(probe.stalled(ts, {"dl_info_speed": 0}),
                         (False, "2 downloads with seeds, 0 B/s"))

    def test_seedless_torrent_not_stalled(self):
        # spec: one seedless torrent is not alerted (Decluttarr handles it)
        ok, _ = probe.stalled([torrent("stalledDL"), torrent("metaDL")], {"dl_info_speed": 0})
        self.assertTrue(ok)

    def test_not_stalled_when_moving(self):
        ok, _ = probe.stalled([torrent("downloading", seeds=2)], {"dl_info_speed": 1200})
        self.assertTrue(ok)

    def test_not_stalled_when_nothing_active(self):
        ts = [torrent("pausedDL", seeds=5), torrent("queuedDL", seeds=5), torrent("uploading", seeds=5)]
        ok, _ = probe.stalled(ts, {"dl_info_speed": 0})
        self.assertTrue(ok)

    def test_download_client_unavailable(self):
        health = [{"source": "DownloadClientStatusCheck", "type": "error", "message": "x"}]
        self.assertEqual(probe.download_client_ok(health, "radarr"),
                         (False, "radarr: download client unavailable"))

    def test_other_health_issues_ignored(self):
        health = [{"source": "IndexerStatusCheck", "type": "warning", "message": "x"}]
        ok, _ = probe.download_client_ok(health, "sonarr")
        self.assertTrue(ok)

    def test_backends_lists_failures(self):
        self.assertEqual(probe.backends_ok({"jellyseerr": True, "radarr": False, "sonarr": False}),
                         (False, "radarr, sonarr unreachable"))

    def test_backends_all_ok(self):
        ok, _ = probe.backends_ok({"jellyseerr": True, "radarr": True, "sonarr": True})
        self.assertTrue(ok)


ENV = {"GATUS_PUSH_TOKEN": "pt", "QBIT_USERNAME": "u", "QBIT_PASSWORD": "p",
       "RADARR_API_KEY": "r", "RADARR_4K_API_KEY": "r4", "SONARR_API_KEY": "s"}


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update(ENV)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def healthy_routes(self):
        return {
            ("POST", "qbittorrent", "/api/v2/auth/login"): "Ok.",
            ("GET", "qbittorrent", "/api/v2/transfer/info"): {"connection_status": "connected",
                                                               "dl_info_speed": 0},
            ("GET", "qbittorrent", "/api/v2/torrents/info"): [torrent("downloading", seeds=2)],
            ("GET", "radarr", "/api/v3/health"): [],
            ("GET", "radarr-4k", "/api/v3/health"): [],
            ("GET", "sonarr", "/api/v3/health"): [],
            ("GET", "radarr", "/api/v3/system/status"): {"version": "5"},
            ("GET", "sonarr", "/api/v3/system/status"): {"version": "4"},
            ("GET", "jellyseerr", "/api/v1/status"): {"version": "2"},
        }

    def test_login_failure_reports_failure(self):
        routes = self.healthy_routes()
        routes[("POST", "qbittorrent", "/api/v2/auth/login")] = "Fails."
        http = FakeHttp(routes)
        self.assertEqual(probe.main("vpn", http=http), 0)
        self.assertEqual(len(http.pushes), 1)
        key, success, error, headers = http.pushes[0]
        self.assertEqual((key, success), ("downloads_vpn-connected", "false"))
        self.assertIn("login", error)
        self.assertEqual(headers["Authorization"], "Bearer pt")

    def test_all_mode_pushes_five_keys(self):
        http = FakeHttp(self.healthy_routes())
        self.assertEqual(probe.main("all", http=http), 0)
        self.assertEqual(sorted(k for k, *_ in http.pushes), sorted([
            "downloads_stalled", "downloads_radarr-download-client",
            "downloads_radarr-4k-download-client", "downloads_sonarr-download-client",
            "requests_backends"]))
        by_key = {k: s for k, s, *_ in http.pushes}
        self.assertEqual(by_key["downloads_stalled"], "false")  # 1 downloading, 0 B/s
        self.assertEqual(by_key["requests_backends"], "true")

    def test_one_failing_service_does_not_stop_others(self):
        routes = self.healthy_routes()
        del routes[("GET", "radarr-4k", "/api/v3/health")]
        http = FakeHttp(routes)
        probe.main("all", http=http)
        by_key = {k: s for k, s, *_ in http.pushes}
        self.assertEqual(len(by_key), 5)
        self.assertEqual(by_key["downloads_radarr-4k-download-client"], "false")
        self.assertEqual(by_key["downloads_radarr-download-client"], "true")

    def test_login_ok_with_empty_body(self):
        # qBittorrent >= 5.2 answers a successful login with an empty body (older: "Ok.")
        routes = self.healthy_routes()
        routes[("POST", "qbittorrent", "/api/v2/auth/login")] = ""
        http = FakeHttp(routes)
        probe.main("vpn", http=http)
        self.assertEqual(http.pushes[0][:2], ("downloads_vpn-connected", "true"))

    def test_push_failure_exits_nonzero(self):
        # Gatus refuses the token: the Job must fail visibly (and the heartbeat goes red)
        class Refusing(FakeHttp):
            def request(self, method, url, data=None, headers=None, timeout=10):
                if "/external" in url:
                    raise OSError("HTTP Error 401")
                return super().request(method, url, data, headers, timeout)
        orig, probe.time.sleep = probe.time.sleep, lambda s: None
        try:
            self.assertEqual(probe.main("vpn", http=Refusing(self.healthy_routes())), 1)
        finally:
            probe.time.sleep = orig

    def test_messages_have_no_titles(self):
        import contextlib
        import io
        out = io.StringIO()
        http = FakeHttp(self.healthy_routes())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            probe.main("all", http=http)
            probe.main("vpn", http=http)
        everything = out.getvalue() + repr(http.pushes)
        self.assertNotIn(TITLE, everything)
        msg = probe.stalled([torrent("downloading", seeds=1)] * 3, {"dl_info_speed": 0})[1]
        self.assertNotIn(TITLE, msg)


if __name__ == "__main__":
    unittest.main()
