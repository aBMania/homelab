import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media_stats  # noqa: E402


def movie(mid, title, res=None, langs=None, original="English", profile=1):
    m = {"id": mid, "title": title, "qualityProfileId": profile,
         "originalLanguage": {"name": original}, "hasFile": res is not None}
    if res is not None:
        m["movieFile"] = {"size": 10, "quality": {"quality": {"resolution": res}},
                          "languages": [{"name": l} for l in langs]}
    return m


class FakeArr:
    """Answers GET paths from a dict; records writes (there should be none here)."""

    def __init__(self, responses):
        self.responses = responses
        self.writes = []

    def get(self, path):
        for prefix, value in self.responses.items():
            if path.startswith(prefix):
                return value
        raise KeyError(path)


class AudioClassTest(unittest.TestCase):
    def test_audio_class(self):
        c = media_stats.audio_class
        self.assertEqual(c(["English", "French"], "English"), "multi")
        self.assertEqual(c(["English"], "English"), "vo")
        self.assertEqual(c(["French"], "French"), "french-original")
        self.assertEqual(c(["French"], "English"), "no-vo")
        self.assertEqual(c([], "English"), "unknown")
        self.assertEqual(c(["Unknown"], "English"), "unknown")


class SummarizeTest(unittest.TestCase):
    def test_summarize_movies(self):
        movies = [movie(1, "a", 2160, ["English"]),
                  movie(2, "b", 1080, ["French"], profile=2),
                  movie(3, "c")]
        s = media_stats.summarize_movies(movies, {1: "HD", 2: "Any"})
        self.assertEqual(s["items"], 3)
        self.assertEqual(s["with_file"], 2)
        self.assertEqual(s["resolution"], {2160: 1, 1080: 1})
        self.assertEqual(s["audio"], {"vo": 1, "no-vo": 1})
        self.assertEqual(s["profiles"], {"HD": 2, "Any": 1})

    def test_grabs_last_24h(self):
        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")
        history = [{"eventType": "grabbed", "date": iso(1)},
                   {"eventType": "grabbed", "date": iso(23)},
                   {"eventType": "grabbed", "date": iso(25)},
                   {"eventType": "downloadFolderImported", "date": iso(2)}]
        self.assertEqual(media_stats.grabs_last_24h(history, now), 2)


class OutputTest(unittest.TestCase):
    def test_output_has_no_titles(self):
        radarr = FakeArr({
            "/api/v3/movie": [movie(1, "SECRET-TITLE-1", 2160, ["English"]),
                              movie(2, "SECRET-TITLE-2", 1080, ["French"])],
            "/api/v3/qualityprofile": [{"id": 1, "name": "HD"}],
            "/api/v3/queue": {"totalRecords": 0},
            "/api/v3/history": {"records": []},
        })
        sonarr = FakeArr({
            "/api/v3/series": [{"id": 7, "title": "SECRET-TITLE-3", "qualityProfileId": 1,
                                "originalLanguage": {"name": "English"},
                                "statistics": {"episodeFileCount": 1}}],
            "/api/v3/episodefile": [{"relativePath": "SECRET-TITLE-3/S01E01.mkv",
                                     "quality": {"quality": {"resolution": 1080}},
                                     "languages": [{"name": "English"}]}],
            "/api/v3/qualityprofile": [{"id": 1, "name": "HD"}],
            "/api/v3/queue": {"totalRecords": 2},
            "/api/v3/history": {"records": []},
        })
        for fmt in ("json", "text"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                media_stats.main({"radarr": radarr, "sonarr": sonarr}, fmt=fmt)
            self.assertNotIn("SECRET-TITLE", out.getvalue())
        self.assertIn("queue 2", out.getvalue())


class FormatTest(unittest.TestCase):
    STATS = {"radarr": {"items": 3, "with_file": 2, "size_tb": 1.5,
                        "profiles": {"HD": 2, "Any": 1},
                        "resolution": {2160: 1, 1080: 1, None: 1},
                        "audio": {"vo": 1, "multi": 3},
                        "queue": 4, "grabs_24h": 5}}

    def test_text_has_one_block_per_app(self):
        text = media_stats.format_text(self.STATS)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("radarr"))
        self.assertIn("3 items", lines[0])
        self.assertIn("2 with file", lines[0])
        self.assertIn("1.5 TB", lines[0])
        self.assertIn("queue 4", lines[0])
        self.assertIn("5 grabs/24h", lines[0])

    def test_counts_sorted_descending_with_readable_labels(self):
        text = media_stats.format_text(self.STATS)
        self.assertIn("audio       multi 3 · vo 1", text)
        self.assertIn("2160p 1", text)
        self.assertIn("none 1", text)


if __name__ == "__main__":
    unittest.main()
