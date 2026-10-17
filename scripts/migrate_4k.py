#!/usr/bin/env python3
"""Move movies whose file is 2160p from radarr (1080p library) into radarr-4k, without
re-downloading, then re-add them to radarr so a 1080p twin can be searched later.

Per movie, each step idempotent (state() decides where to resume after an interruption):
  1. radarr: move the folder to /media/movies-4k (editor, moveFiles)
  2. radarr-4k: add it at that path (UHD profile, tag migrated-4k, no search) + rescan
  3. radarr: remove it, keeping files
  4. radarr: re-add it under /media/movies (HD profile, no search)
A movie is always in at least one instance. Output: counts and tmdb ids only, never titles.

  mise run media:migrate-4k [-- --apply] [--limit N]
  mise run media:search-twins [-- --apply] [--limit N]
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

from media_stats import Arr

HD_PROFILE = "[French MULTi.VO] HD Bluray + WEB"
UHD_PROFILE = "[French MULTi.VO] UHD Bluray + WEB"
TAG = "migrated-4k"
ROOT_HD = "/media/movies"
ROOT_4K = "/media/movies-4k"
TERMINAL = {"done", "skip", "collision"}
MOVE_TIMEOUT, RESCAN_TIMEOUT, POLL = 120, 300, 2


def _resolution(m):
    return (m.get("movieFile") or {}).get("quality", {}).get("quality", {}).get("resolution")


def _in_4k_root(path):
    return path.startswith(ROOT_4K + "/")


def state(r, k, taken_paths=frozenset(), tag_id=None):
    """Where a movie stands. r: its radarr record, k: its radarr-4k record (or None)."""
    k_migrated = k is not None and tag_id is not None and tag_id in k.get("tags", [])
    if r is None:
        return "in-4k" if k_migrated else "skip"
    if _in_4k_root(r["path"]):
        return "in-4k" if k is not None and k.get("hasFile") else "moved"
    if k is not None:
        return "done" if k_migrated else "skip"
    if r.get("hasFile") and _resolution(r) == 2160:
        target = f'{ROOT_4K}/{r["path"].rstrip("/").rsplit("/", 1)[1]}'
        return "collision" if target in taken_paths else "todo"
    return "skip"


def _by_tmdb(movies, tmdb_id):
    return next((m for m in movies if m["tmdbId"] == tmdb_id), None)


def _tag_id(arr, create=False):
    tag = next((t for t in arr.get("/api/v3/tag") if t["label"] == TAG), None)
    if tag is None and create:
        tag = arr.post("/api/v3/tag", {"label": TAG})
    return tag["id"] if tag else None


def _poll(fetch, done, timeout, wait, what):
    for _ in range(max(1, timeout // POLL)):
        if done(fetch()):
            return
        wait(POLL)
    raise TimeoutError(what)


def _add_body(src, profile, root, path=None, tags=()):
    body = {"tmdbId": src["tmdbId"], "title": src["title"], "year": src["year"],
            "qualityProfileId": profile, "rootFolderPath": root,
            "monitored": src["monitored"],
            "minimumAvailability": src.get("minimumAvailability", "released"),
            "tags": list(tags), "addOptions": {"searchForMovie": False}}
    if path:
        body["path"] = path
    return body


def migrate_one(radarr, radarr4k, tmdb_id, hd_profile, uhd_profile, apply, wait=time.sleep):
    """Advance one movie as far as possible; returns its final state."""
    for _ in range(6):
        k_movies = radarr4k.get("/api/v3/movie")
        r, k = _by_tmdb(radarr.get("/api/v3/movie"), tmdb_id), _by_tmdb(k_movies, tmdb_id)
        taken = {m["path"] for m in k_movies if m["tmdbId"] != tmdb_id}
        st = state(r, k, taken, _tag_id(radarr4k))
        if not apply or st in TERMINAL:
            return st
        if st == "todo":  # step 1
            radarr.put("/api/v3/movie/editor", {"movieIds": [r["id"]], "rootFolderPath": ROOT_4K,
                                                "moveFiles": True})
            _poll(lambda: radarr.get(f'/api/v3/movie/{r["id"]}'),
                  lambda m: _in_4k_root(m["path"]), MOVE_TIMEOUT, wait, "move")
        elif st == "moved":  # step 2
            tag = _tag_id(radarr4k, create=True)
            added = radarr4k.post("/api/v3/movie",
                                  _add_body(r, uhd_profile, ROOT_4K, path=r["path"], tags=[tag]))
            radarr4k.post("/api/v3/command", {"name": "RescanMovie", "movieId": added["id"]})
            _poll(lambda: radarr4k.get(f'/api/v3/movie/{added["id"]}'),
                  lambda m: m.get("hasFile"), RESCAN_TIMEOUT, wait, "rescan")
        elif st == "in-4k":  # steps 3 + 4
            if r is not None:
                radarr.delete(f'/api/v3/movie/{r["id"]}?deleteFiles=false&addImportExclusion=false')
            radarr.post("/api/v3/movie", _add_body(k, hd_profile, ROOT_HD))
    raise RuntimeError("no progress")


def _profile_id(arr, name):
    pid = next((p["id"] for p in arr.get("/api/v3/qualityprofile") if p["name"] == name), None)
    if pid is None:
        sys.exit(f"quality profile not found: {name}")
    return pid


def _clients_from_env():
    for key in ("RADARR_API_KEY", "RADARR_4K_API_KEY"):
        if not os.environ.get(key):
            sys.exit(f"{key} not set: run through mise (scripts/arr.sh); "
                     "radarr-4k must be deployed (svc + radarr-4k-secret)")
    return (Arr("http://localhost:17878", os.environ["RADARR_API_KEY"]),
            Arr("http://localhost:17879", os.environ["RADARR_4K_API_KEY"]))


def migrate(radarr, radarr4k, apply, limit, wait):
    hd, uhd = _profile_id(radarr, HD_PROFILE), _profile_id(radarr4k, UHD_PROFILE)
    r_movies, k_movies = radarr.get("/api/v3/movie"), radarr4k.get("/api/v3/movie")
    tag_id = _tag_id(radarr4k)
    taken = {m["path"] for m in k_movies}
    before = {}
    for t in {m["tmdbId"] for m in r_movies} | {m["tmdbId"] for m in k_movies}:
        r, k = _by_tmdb(r_movies, t), _by_tmdb(k_movies, t)
        before[t] = state(r, k, taken - ({k["path"]} if k else set()), tag_id)
    # Finish interrupted movies first, then new ones
    queue = sorted((t for t, s in before.items() if s not in TERMINAL),
                   key=lambda t: before[t] == "todo")[:limit]
    result = Counter(s for s in before.values())
    if apply:
        for t in queue:
            result[before[t]] -= 1
            try:
                result[migrate_one(radarr, radarr4k, t, hd, uhd, True, wait)] += 1
            except Exception as e:  # keep going; report the id only
                result["error"] += 1
                print(f"error tmdb={t}: {type(e).__name__}", file=sys.stderr)
    print(json.dumps({"apply": apply, "batch": len(queue), "states": dict(+result)}))


def search_twins(radarr, radarr4k, apply, limit):
    k_tmdbs = {m["tmdbId"] for m in radarr4k.get("/api/v3/movie")}
    missing = [m["id"] for m in radarr.get("/api/v3/movie")
               if m["tmdbId"] in k_tmdbs and not m.get("hasFile") and m.get("monitored")]
    batch = missing[:limit]
    if apply and batch:
        radarr.post("/api/v3/command", {"name": "MoviesSearch", "movieIds": batch})
    print(json.dumps({"apply": apply, "missing_twins": len(missing),
                      "searched": len(batch) if apply else 0}))


def main(argv=None, clients=None, wait=time.sleep):
    argv = sys.argv[1:] if argv is None else argv
    cmd = "search-twins" if argv[:1] == ["search-twins"] else "migrate"
    parser = argparse.ArgumentParser(prog=f"migrate_4k {cmd}")
    parser.add_argument("--apply", action="store_true", help="make changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv[1:] if cmd == "search-twins" else argv)
    radarr, radarr4k = clients or _clients_from_env()
    if cmd == "search-twins":
        search_twins(radarr, radarr4k, args.apply, args.limit)
    else:
        migrate(radarr, radarr4k, args.apply, args.limit, wait)


if __name__ == "__main__":
    main()
