import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media_prioritize as mp  # noqa: E402

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=60)).isoformat()


class FakeArr:
    def __init__(self, responses):
        self.responses = responses

    def get(self, path):
        for prefix, value in self.responses.items():
            if path.startswith(prefix):
                return value
        raise KeyError(path)


class FakeQBit:
    """Queue positions are the order of self.queue; topPrio moves hashes to the top."""

    def __init__(self, hashes, finished=()):
        self.queue = list(hashes)
        self.finished = list(finished)
        self.calls = []

    def torrents(self):
        waiting = [{"hash": h, "priority": i + 1, "state": "queuedDL", "name": "SECRET-" + h}
                   for i, h in enumerate(self.queue)]
        done = [{"hash": h, "priority": 0, "state": "uploading", "name": "SECRET-" + h}
                for h in self.finished]
        return waiting + done

    def top_prio(self, hashes):
        self.calls.append(list(hashes))
        moved = [h for h in self.queue if h in hashes]
        self.queue = moved + [h for h in self.queue if h not in hashes]


def movie_rec(h, has_file=False, tags=(), tmdb=1):
    return {"downloadId": h.upper(), "status": "queued",
            "movie": {"title": "SECRET-TITLE", "hasFile": has_file, "tags": list(tags),
                      "tmdbId": tmdb, "added": OLD}}


def episode_rec(h, has_file=False, tags=()):
    return {"downloadId": h.upper(), "status": "queued",
            "series": {"title": "SECRET-TITLE", "tags": list(tags), "added": OLD},
            "episode": {"hasFile": has_file}}


TAGS = [{"id": 1, "label": "2-someone"}, {"id": 2, "label": "migrated-4k"}]


class ClassifyTest(unittest.TestCase):
    def test_classes(self):
        tags = {1: "2-someone", 2: "migrated-4k"}
        c = mp.classify_record
        self.assertEqual(c("radarr", movie_rec("a", tags=[1]), tags, set()), "request")
        # a request wins even when the movie already has a file
        self.assertEqual(c("radarr", movie_rec("a", True, [1]), tags, set()), "request")
        self.assertEqual(c("radarr", movie_rec("a", tmdb=9), tags, {9}), "twin_1080p")
        self.assertEqual(c("radarr", movie_rec("a", True), tags, set()), "upgrade")
        self.assertEqual(c("radarr", movie_rec("a", tags=[2]), tags, set()), "newly_available")
        self.assertEqual(c("sonarr", episode_rec("a", True), tags, set()), "upgrade")
        self.assertEqual(c("sonarr", episode_rec("a"), tags, set()), "newly_available")
        # twins only exist in the 1080p radarr
        self.assertEqual(c("radarr_4k", movie_rec("a", tmdb=9), tags, {9}), "newly_available")

    def test_torrent_takes_best_class_of_its_records(self):
        sonarr = FakeArr({"/api/v3/tag": TAGS, "/api/v3/queue": {"records": [
            episode_rec("pack", True), episode_rec("pack"), episode_rec("pack", True)]}})
        self.assertEqual(mp.classify_queues({"sonarr": sonarr}), {"pack": "newly_available"})


class PlanTest(unittest.TestCase):
    def test_groups_in_priority_order_and_ignores_finished(self):
        qb = FakeQBit(["u", "n", "o", "r", "t"], finished=["r2"])
        classes = {"u": "upgrade", "n": "newly_available", "r": "request",
                   "t": "twin_1080p", "r2": "request"}
        groups = mp.plan(qb.torrents(), classes)
        self.assertEqual(groups, {"request": ["r"], "newly_available": ["n"],
                                  "twin_1080p": ["t"], "upgrade": ["u"], "other": ["o"]})

    def test_apply_orders_queue(self):
        qb = FakeQBit(["u", "n", "o", "r", "t"])
        classes = {"u": "upgrade", "n": "newly_available", "r": "request", "t": "twin_1080p"}
        mp.apply(qb, mp.plan(qb.torrents(), classes))
        self.assertEqual(qb.queue, ["r", "n", "t", "u", "o"])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.radarr = FakeArr({"/api/v3/tag": TAGS, "/api/v3/movie": [], "/api/v3/queue": {
            "records": [movie_rec("a", tags=[1]), movie_rec("b", True)]}})

    def run_main(self, qb, apply):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mp.main({"radarr": self.radarr}, qb, apply_changes=apply, wait=0)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        qb = FakeQBit(["b", "a"])
        out = self.run_main(qb, apply=False)
        self.assertEqual(qb.calls, [])
        self.assertIn("dry run", out)
        self.assertNotIn("SECRET", out)

    def test_apply(self):
        qb = FakeQBit(["b", "a"])
        out = self.run_main(qb, apply=True)
        self.assertEqual(qb.queue, ["a", "b"])
        self.assertNotIn("SECRET", out)


if __name__ == "__main__":
    unittest.main()
