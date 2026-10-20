import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media_usage  # noqa: E402

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
GB = 1e9
HOUR = 60 * media_usage.TICKS_PER_MIN


def ago(days):
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


class FakeArr:
    def __init__(self, responses):
        self.responses = responses

    def get(self, path):
        return self.responses[path]


class FakeEmby:
    """Two users; per-user item lists keyed by (user id, parent/item id)."""

    def __init__(self, series, movies, episodes, movie_data):
        self.series, self.movies, self.episodes, self.movie_data = series, movies, episodes, movie_data

    def get(self, path, **q):
        if path == "/Users":
            return [{"Id": "a", "Name": "Alice Secret"}, {"Id": "b", "Name": "Bob Secret"}]
        if path == "/Items":
            return {"Items": self.series if q["IncludeItemTypes"] == "Series" else self.movies}
        user = path.split("/")[2]
        if path.endswith("/Items"):
            return {"Items": self.episodes.get((user, q["ParentId"]), [])}
        item = path.split("/")[-1]
        return self.movie_data[(user, item)]


def ep(hours=1.0, played=False, pos_h=0.0, count=1, last=None):
    return {"SeriesId": "s1", "RunTimeTicks": int(hours * HOUR),
            "UserData": {"Played": played, "PlayCount": count,
                         "PlaybackPositionTicks": int(pos_h * HOUR), "LastPlayedDate": last}}


def fixture(viewer_last_days=400, added_days=400, twin_has_file=True):
    sonarr = FakeArr({
        "/api/v3/tag": [{"id": 1, "label": "7-somebody"}],
        "/api/v3/series": [
            {"id": 11, "title": "Long Show Title", "path": "/tv/Long Show", "tvdbId": 100, "added": ago(added_days),
             "ended": True, "monitored": True, "tags": [],
             "statistics": {"sizeOnDisk": 500 * GB, "episodeFileCount": 100},
             "seasons": [{"seasonNumber": 1, "statistics": {"episodeFileCount": 50}},
                         {"seasonNumber": 2, "statistics": {"episodeFileCount": 50}}]},
            {"id": 12, "title": "Small Show", "path": "/tv/Small", "tvdbId": 101, "added": ago(400),
             "ended": True, "monitored": True, "tags": [],
             "statistics": {"sizeOnDisk": 50 * GB, "episodeFileCount": 10}, "seasons": []},
        ]})
    radarr = FakeArr({
        "/api/v3/tag": [],
        "/api/v3/movie": [{"id": 21, "title": "Twin Movie", "tmdbId": 9, "path": "/movies/Twin", "added": ago(2),
                           "hasFile": twin_has_file, "sizeOnDisk": 10 * GB, "tags": []}]})
    radarr_4k = FakeArr({
        "/api/v3/tag": [{"id": 3, "label": "7-somebody"}],
        "/api/v3/movie": [{"id": 31, "title": "Twin Movie", "tmdbId": 9, "path": "/movies-4k/Twin", "added": ago(2),
                           "hasFile": True, "sizeOnDisk": 80 * GB, "tags": [3]}]})
    emby = FakeEmby(
        series=[{"Id": "s1", "ProviderIds": {"Tvdb": "100"}, "Path": "/media/tv/Long Show", "DateCreated": ago(900)}],
        movies=[{"Id": "m4k", "ProviderIds": {"Tmdb": "9"}, "Path": "/media/movies-4k/Twin/f.mkv", "DateCreated": ago(500)},
                {"Id": "m1080", "ProviderIds": {"Tmdb": "9"}, "Path": "/media/movies/Twin/f.mkv"}],
        episodes={("a", "s1"): [ep(played=True, count=3, last=ago(viewer_last_days))] * 2 + [ep()] * 98,
                  ("b", "s1"): [ep(pos_h=0.2, last=ago(30))] + [ep()] * 99},
        movie_data={("a", "m4k"): {"Id": "m4k", "RunTimeTicks": 2 * HOUR, "UserData": {}},
                    ("b", "m4k"): {"Id": "m4k", "RunTimeTicks": 2 * HOUR, "UserData": {}},
                    ("a", "m1080"): {"Id": "m1080", "RunTimeTicks": 2 * HOUR,
                                     "UserData": {"Played": True, "LastPlayedDate": ago(300)}},
                    ("b", "m1080"): {"Id": "m1080", "RunTimeTicks": 2 * HOUR, "UserData": {}}})
    return {"sonarr": sonarr, "radarr": radarr, "radarr_4k": radarr_4k}, emby


class CollectTest(unittest.TestCase):
    def test_thresholds_and_watch_time(self):
        rows = media_usage.collect(*fixture(), now=NOW)
        self.assertEqual(sorted((r["app"], r["id"]) for r in rows), [("radarr_4k", 31), ("sonarr", 11)])
        show = next(r for r in rows if r["app"] == "sonarr")
        self.assertEqual(show["runtime"], 100 * 60)
        alice = next(u for u in show["users"] if u["user"] == "U1")
        self.assertEqual(alice["uniq"], 120)          # 2 played episodes, rewatches not counted
        self.assertEqual(alice["total"], 360)         # PlayCount 3 each
        self.assertEqual(show["viewers"], 0)          # 2 % of the runtime: below the 5 % bar
        self.assertEqual(show["last_days"], 30)       # the most recent play by anyone
        self.assertEqual(show["last_played"], ago(30)[:10])
        self.assertEqual(alice["last_played"], ago(400)[:10])
        self.assertIn(ago(30)[:10], media_usage.render(rows))
        self.assertEqual(show["seasons"], 2)

    def test_4k_twin_uses_both_copies_and_twin_age(self):
        rows = media_usage.collect(*fixture(), now=NOW)
        k = next(r for r in rows if r["app"] == "radarr_4k")
        self.assertEqual(k["action"], "drop 4K (1080p kept)")
        self.assertEqual(k["age_days"], 500)          # Emby's date, not the 4K migration
        self.assertEqual(k["runtime"], 120)           # one copy, not both
        self.assertEqual([u["user"] for u in k["users"]], ["U1"])  # watched in 1080p
        self.assertTrue(k["requested"])

    def test_4k_whose_twin_has_no_file_yet(self):
        rows = media_usage.collect(*fixture(twin_has_file=False), now=NOW)
        self.assertEqual(next(r for r in rows if r["app"] == "radarr_4k")["action"], "drop 4K (1080p twin to download)")

    def test_age_is_the_earliest_known_date(self):
        rows = media_usage.collect(*fixture(added_days=40), now=NOW)
        self.assertEqual(next(r for r in rows if r["app"] == "sonarr")["age_days"], 900)


class ScoreTest(unittest.TestCase):
    def row(self, **kw):
        r = {"size": 100 * GB, "runtime": 600, "users": [], "last_days": None, "age_days": 400, "requested": False}
        r.update(kw)
        return r

    def test_weights(self):
        self.assertEqual(media_usage.score(self.row()), (0, 100))
        one = [{"uniq": 300}]
        self.assertEqual(media_usage.score(self.row(users=one, last_days=200)), (1, 70))
        self.assertEqual(media_usage.score(self.row(users=one, last_days=5)), (1, 4))       # being watched
        self.assertEqual(media_usage.score(self.row(users=one * 3, last_days=200)), (3, 10))
        self.assertEqual(media_usage.score(self.row(age_days=10)), (0, 20))                 # just added
        self.assertEqual(media_usage.score(self.row(requested=True)), (0, 70))

    def test_viewer_needs_an_hour_and_five_percent(self):
        self.assertEqual(media_usage.score(self.row(users=[{"uniq": 59}]))[0], 0)
        self.assertEqual(media_usage.score(self.row(runtime=6000, users=[{"uniq": 200}]))[0], 0)


class OutputTest(unittest.TestCase):
    SECRETS = ("Long Show", "Twin Movie", "Alice", "Bob", "somebody")

    def test_default_output_is_anonymous(self):
        rows = media_usage.collect(*fixture(), now=NOW)
        for out in (media_usage.render(rows), media_usage.to_json(rows)):
            for s in self.SECRETS:
                self.assertNotIn(s, out)
        self.assertIn("S11", media_usage.render(rows))
        self.assertIn("K31", media_usage.render(rows))

    def test_names_flag_shows_titles_and_users(self):
        rows = media_usage.collect(*fixture(), now=NOW)
        out = media_usage.render(rows, names=True)
        for s in ("Long Show Title", "Twin Movie", "Alice Secret"):
            self.assertIn(s, out)
        self.assertIn("Long Show Title", media_usage.to_json(rows, names=True))


if __name__ == "__main__":
    unittest.main()
