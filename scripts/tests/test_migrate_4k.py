import contextlib
import io
import os
import re
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate_4k  # noqa: E402

HD, UHD = 11, 22


class FakeArr:
    """In-memory Radarr: movies by id, records every write, calls on_write after each one."""

    def __init__(self, movies=(), on_write=None, name="arr", log=None):
        self.name = name
        self.log = log if log is not None else []
        self.movies = {m["id"]: dict(m) for m in movies}
        self.tags = []
        self.calls = []
        self.on_write = on_write or (lambda: None)
        self._next = 1000

    def get(self, path):
        if path == "/api/v3/movie":
            return [dict(m) for m in self.movies.values()]
        if m := re.fullmatch(r"/api/v3/movie/(\d+)", path):
            return dict(self.movies[int(m.group(1))])
        if path == "/api/v3/tag":
            return list(self.tags)
        if path == "/api/v3/qualityprofile":
            return [{"id": HD, "name": migrate_4k.HD_PROFILE},
                    {"id": UHD, "name": migrate_4k.UHD_PROFILE}]
        raise KeyError(path)

    def _write(self, *call):
        self.calls.append(call)
        method, path, body = call
        self.log.append((self.name, method, path.split("?")[0], (body or {}).get("name")))
        self.on_write()

    def put(self, path, body):
        assert path == "/api/v3/movie/editor"
        for mid in body["movieIds"]:
            m = self.movies[mid]
            m["path"] = body["rootFolderPath"] + "/" + m["path"].rsplit("/", 1)[1]
        self._write("PUT", path, body)

    def post(self, path, body):
        result = None
        if path == "/api/v3/movie":
            self._next += 1
            path_ = body.get("path") or f'{body["rootFolderPath"]}/{body["title"]} ({body["year"]})'
            result = {**body, "id": self._next, "path": path_, "hasFile": False}
            self.movies[self._next] = result
        elif path == "/api/v3/command" and body["name"] == "RescanMovie":
            self.movies[body["movieId"]]["hasFile"] = True
        elif path == "/api/v3/tag":
            result = {"id": len(self.tags) + 1, "label": body["label"]}
            self.tags.append(result)
        self._write("POST", path, body)
        return result

    def delete(self, path):
        mid = int(re.match(r"/api/v3/movie/(\d+)", path).group(1))
        del self.movies[mid]
        self._write("DELETE", path, None)

    def writes(self):
        return [c for c in self.calls if c[0] in ("PUT", "POST", "DELETE")]


def movie(mid, tmdb, res=2160, folder=None, root="/media/movies", monitored=True, title=None):
    title = title or f"SECRET-TITLE-{mid}"
    m = {"id": mid, "tmdbId": tmdb, "title": title, "year": 2020, "monitored": monitored,
         "minimumAvailability": "released", "path": f"{root}/{folder or title}",
         "hasFile": res is not None, "tags": []}
    if res is not None:
        m["movieFile"] = {"quality": {"quality": {"resolution": res}}}
    return m


def run(radarr, radarr4k, tmdb, apply=True):
    return migrate_4k.migrate_one(radarr, radarr4k, tmdb, HD, UHD, apply, wait=lambda s: None)


class StateTest(unittest.TestCase):
    def test_selects_only_2160p(self):
        self.assertEqual(migrate_4k.state(movie(1, 101, 2160), None), "todo")
        self.assertEqual(migrate_4k.state(movie(2, 102, 1080), None), "skip")
        self.assertEqual(migrate_4k.state(movie(3, 103, None), None), "skip")


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.radarr = FakeArr([movie(1, 101)], name="radarr", log=self.log)
        self.radarr4k = FakeArr(name="radarr4k", log=self.log)

    def test_dry_run_makes_no_writes(self):
        self.assertEqual(run(self.radarr, self.radarr4k, 101, apply=False), "todo")
        self.assertEqual(self.radarr.writes() + self.radarr4k.writes(), [])

    def test_happy_path_order(self):
        self.assertEqual(run(self.radarr, self.radarr4k, 101), "done")
        self.assertEqual([c for c in self.log if c[2] != "/api/v3/tag"], [
            ("radarr", "PUT", "/api/v3/movie/editor", None),
            ("radarr4k", "POST", "/api/v3/movie", None),
            ("radarr4k", "POST", "/api/v3/command", "RescanMovie"),
            ("radarr", "DELETE", "/api/v3/movie/1", None),
            ("radarr", "POST", "/api/v3/movie", None),
        ])
        delete = next(c for c in self.radarr.calls if c[0] == "DELETE")
        self.assertIn("deleteFiles=false", delete[1])
        self.assertIn("addImportExclusion=false", delete[1])
        uhd = next(m for m in self.radarr4k.movies.values())
        self.assertEqual(uhd["qualityProfileId"], UHD)
        self.assertTrue(uhd["path"].startswith("/media/movies-4k/"))
        self.assertTrue(uhd["hasFile"])
        hd = next(m for m in self.radarr.movies.values())
        self.assertEqual((hd["qualityProfileId"], hd["rootFolderPath"]), (HD, "/media/movies"))
        self.assertFalse(hd["addOptions"]["searchForMovie"])

    def test_preserves_monitored_flag(self):
        radarr = FakeArr([movie(1, 101, monitored=False)])
        radarr4k = FakeArr()
        run(radarr, radarr4k, 101)
        posts = [c[2] for c in radarr.calls + radarr4k.calls
                 if c[0] == "POST" and c[1] == "/api/v3/movie"]
        self.assertEqual([p["monitored"] for p in posts], [False, False])

    def test_skips_folder_collision(self):
        title = "SECRET-TITLE-1"
        radarr4k = FakeArr([movie(50, 999, root="/media/movies-4k", folder=title)])
        self.assertEqual(run(self.radarr, radarr4k, 101), "collision")
        self.assertEqual(self.radarr.writes() + radarr4k.writes(), [])


class ResumeTest(unittest.TestCase):
    def test_resume_after_partial_move(self):
        # Interrupted after step 1: radarr already points at /media/movies-4k, not in radarr-4k yet.
        world = {}
        watched = {"tmdb": 101}

        def present_somewhere():
            tmdbs = {m["tmdbId"] for a in world.values() for m in a.movies.values()}
            self.assertIn(watched["tmdb"], tmdbs, "movie vanished from both instances")

        radarr = FakeArr([movie(1, 101, root="/media/movies-4k")], on_write=present_somewhere)
        radarr4k = FakeArr(on_write=present_somewhere)
        world.update(r=radarr, k=radarr4k)
        self.assertEqual(migrate_4k.state(radarr.get("/api/v3/movie/1"), None), "moved")
        self.assertEqual(run(radarr, radarr4k, 101), "done")
        self.assertNotIn("PUT", [c[0] for c in radarr.calls])  # step 1 not redone

        # Interrupted after step 3: tagged in radarr-4k, gone from radarr -> only step 4 runs.
        radarr2 = FakeArr(on_write=present_somewhere)
        radarr4k2 = FakeArr([movie(7, 102, root="/media/movies-4k")], on_write=present_somewhere)
        tag = radarr4k2.post("/api/v3/tag", {"label": migrate_4k.TAG})
        radarr4k2.movies[7]["tags"] = [tag["id"]]
        radarr4k2.calls.clear()
        world.update(r=radarr2, k=radarr4k2)
        watched["tmdb"] = 102
        self.assertEqual(migrate_4k.migrate_one(radarr2, radarr4k2, 102, HD, UHD, True,
                                                wait=lambda s: None), "done")
        self.assertEqual(radarr4k2.writes(), [])
        self.assertEqual([c[:2] for c in radarr2.writes()], [("POST", "/api/v3/movie")])

    def test_untagged_4k_only_movie_is_left_alone(self):
        # A fresh 4K request (not a migration) must not get an HD twin added silently.
        radarr = FakeArr()
        radarr4k = FakeArr([movie(7, 102, root="/media/movies-4k")])
        self.assertEqual(run(radarr, radarr4k, 102), "skip")
        self.assertEqual(radarr.writes(), [])


class MainTest(unittest.TestCase):
    def test_search_twins_limit(self):
        radarr = FakeArr([movie(i, 100 + i, res=None) for i in range(1, 16)])
        radarr4k = FakeArr([movie(100 + i, 100 + i, root="/media/movies-4k") for i in range(1, 16)])
        with contextlib.redirect_stdout(io.StringIO()):
            migrate_4k.main(["search-twins", "--limit", "10", "--apply"],
                            clients=(radarr, radarr4k))
        searches = [c[2] for c in radarr.calls if c[0] == "POST" and c[1] == "/api/v3/command"]
        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0]["name"], "MoviesSearch")
        self.assertEqual(len(searches[0]["movieIds"]), 10)

    def test_missing_4k_key_exits_with_hint(self):
        env = {k: v for k, v in os.environ.items() if k != "RADARR_4K_API_KEY"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                migrate_4k.main([])
        self.assertIn("radarr-4k", str(cm.exception.code))

    def test_output_has_no_titles(self):
        radarr = FakeArr([movie(1, 101), movie(2, 102, res=1080)])
        radarr4k = FakeArr()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            migrate_4k.main(["--apply", "--limit", "5"], clients=(radarr, radarr4k),
                            wait=lambda s: None)
        self.assertNotIn("SECRET-TITLE", out.getvalue() + err.getvalue())
        self.assertIn("done", out.getvalue())


if __name__ == "__main__":
    unittest.main()
