import http.server
import json
import os
import subprocess
import threading
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "emby-idle.sh")
KEY = "good-key"


class FakeEmby:
    """Serves /emby/Sessions; `sessions` is the list returned to a request with the right key."""

    def __init__(self, sessions):
        self.sessions = sessions
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/emby/Sessions" or self.headers.get("X-Emby-Token") != KEY:
                    self.send_response(401); self.end_headers(); return
                body = json.dumps(outer.sessions).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def close(self):
        self.srv.shutdown()


def run(url, key=KEY, *args):
    env = dict(os.environ, EMBY_URL=url, EMBY_KEY_OVERRIDE=key, EMBY_IDLE_INTERVAL="1")
    return subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=30)


class EmbyIdle(unittest.TestCase):
    def setUp(self):
        self.emby = FakeEmby([{"Id": "a"}, {"Id": "b"}])

    def tearDown(self):
        self.emby.close()

    def test_idle_exits_0(self):
        r = run(self.emby.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("playing: 0", r.stdout + r.stderr)

    def test_playing_exits_1_without_titles(self):
        self.emby.sessions = [{"Id": "a", "NowPlayingItem": {"Name": "Some Secret Title"}}, {"Id": "b"}]
        r = run(self.emby.url)
        self.assertEqual(r.returncode, 1)
        self.assertIn("playing: 1", r.stdout + r.stderr)
        self.assertNotIn("Secret", r.stdout + r.stderr)

    def test_bad_key_exits_2(self):
        r = run(self.emby.url, "bad")
        self.assertEqual(r.returncode, 2)

    def test_unreachable_exits_2(self):
        r = run("http://127.0.0.1:9")
        self.assertEqual(r.returncode, 2)

    def test_wait_returns_0_once_playback_stops(self):
        self.emby.sessions = [{"Id": "a", "NowPlayingItem": {"Name": "x"}}]
        threading.Timer(1.5, lambda: setattr(self.emby, "sessions", [{"Id": "a"}])).start()
        r = run(self.emby.url, KEY, "--wait", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_wait_gives_up_with_1(self):
        self.emby.sessions = [{"Id": "a", "NowPlayingItem": {"Name": "x"}}]
        env = dict(os.environ, EMBY_URL=self.emby.url, EMBY_KEY_OVERRIDE=KEY, EMBY_IDLE_INTERVAL="1", EMBY_IDLE_MINUTE="2")
        r = subprocess.run(["bash", SCRIPT, "--wait", "1"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
