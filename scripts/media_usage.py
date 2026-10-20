#!/usr/bin/env python3
"""Removal candidates: big series/movies ranked by how little they are watched.

For every series over --min-series-gb (default 100) and movie over --min-movie-gb (default 50),
sums each Emby user's watch time (unique minutes covered, plus total minutes with rewatches) and
scores the item. Higher score = better candidate: lots of GB that few people watch, nobody recently.

    score = GB x viewers x recency x newness x request
      viewers  (users with >= 60 min AND >= 5% of the runtime): 0 -> 1.0, 1 -> 0.7, 2 -> 0.3, 3+ -> 0.1
      recency  (days since anyone last played it): <14 -> 0.05, <60 -> 0.3, <180 -> 0.6, else/never -> 1.0
      newness  (days since added; a 4K copy uses its 1080p twin's date): <30 -> 0.2, <90 -> 0.5, else 1.0
      request  (carries a Jellyseerr request tag): 0.7

A 4K movie whose 1080p twin (same TMDb id in radarr) has a file is scored on the watch time of
both copies, and its action is "drop 4K" (the movie stays in 1080p).

Output is anonymised: items by app:id (S = sonarr, R = radarr, K = radarr-4k), users as U1..Un.
--names prints titles and user names: for the owner only, never for an agent.

Run through `mise run media:usage [--names] [--json] [--min-series-gb N] [--min-movie-gb N]`
(scripts/arr.sh + scripts/emby.sh set up port-forwards and keys).
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from media_stats import URLS, Arr

GB = 1e9
TICKS_PER_MIN = 600_000_000
EMBY_URL = "http://localhost:18096"
REQUEST_TAG = re.compile(r"^\d+-")  # Jellyseerr tags requests "<user id>-<user name>"
PREFIX = {"sonarr": "S", "radarr": "R", "radarr_4k": "K"}


class Emby:
    """Minimal Emby API client (api_key query parameter)."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def get(self, path, **query):
        query["api_key"] = self.api_key
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(url, timeout=120) as resp:
            return json.loads(resp.read())


def _days(iso, now):
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")[:26] if iso.endswith("Z") else iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def _tail(path):
    return (path or "").rstrip("/").split("/")[-1].lower()


def watch_by_user(items):
    """{'uniq': covered minutes, 'total': minutes incl. rewatches, 'last': latest LastPlayedDate}."""
    uniq = total = 0.0
    last = None
    for it in items:
        ud = it.get("UserData") or {}
        runtime = (it.get("RunTimeTicks") or 0) / TICKS_PER_MIN
        if ud.get("Played"):
            uniq += runtime
            total += runtime * max(1, ud.get("PlayCount") or 1)
        elif ud.get("PlaybackPositionTicks"):
            uniq += ud["PlaybackPositionTicks"] / TICKS_PER_MIN
            total += ud["PlaybackPositionTicks"] / TICKS_PER_MIN
        lp = ud.get("LastPlayedDate")
        if lp and (last is None or lp > last):
            last = lp
    return {"uniq": uniq, "total": total, "last": last}


def score(row):
    viewers = sum(1 for u in row["users"] if u["uniq"] >= 60 and u["uniq"] >= 0.05 * max(row["runtime"], 1))
    w_view = {0: 1.0, 1: 0.7, 2: 0.3}.get(viewers, 0.1)
    last = row["last_days"]
    w_recent = 1.0 if last is None or last >= 180 else 0.6 if last >= 60 else 0.3 if last >= 14 else 0.05
    age = row["age_days"]
    w_new = 0.2 if age is not None and age < 30 else 0.5 if age is not None and age < 90 else 1.0
    w_req = 0.7 if row["requested"] else 1.0
    return viewers, round(row["size"] / GB * w_view * w_recent * w_new * w_req)


def _emby_items(emby, kind):
    items = emby.get("/Items", Recursive="true", IncludeItemTypes=kind, Fields="ProviderIds,Path,DateCreated")
    return items.get("Items", [])


def _match_series(series, emby_series):
    tvdb = str(series.get("tvdbId") or "")
    hits = [i for i in emby_series if tvdb and (i.get("ProviderIds") or {}).get("Tvdb") == tvdb]
    return hits or [i for i in emby_series if _tail(i.get("Path")) == _tail(series.get("path"))]


def _match_movie(movie, emby_movies):
    """Emby items for this copy of the movie (same TMDb/IMDb id and same folder)."""
    tmdb, imdb, folder = str(movie.get("tmdbId") or ""), movie.get("imdbId") or "", _tail(movie.get("path"))
    same_id = [i for i in emby_movies
               if (tmdb and (i.get("ProviderIds") or {}).get("Tmdb") == tmdb)
               or (imdb and (i.get("ProviderIds") or {}).get("Imdb") == imdb)]
    same_folder = [i for i in same_id if _tail(os.path.dirname(i.get("Path") or "")) == folder]
    return same_folder or [i for i in emby_movies if _tail(os.path.dirname(i.get("Path") or "")) == folder]


def _requested(item, tags):
    return any(REQUEST_TAG.match(tags.get(t, "")) for t in item.get("tags", []))


def collect(arrs, emby, min_series_gb=100, min_movie_gb=50, now=None):
    now = now or datetime.now(timezone.utc)
    users = emby.get("/Users")
    rows = []
    sonarr = arrs.get("sonarr")
    if sonarr:
        tags = {t["id"]: t["label"] for t in sonarr.get("/api/v3/tag")}
        emby_series = _emby_items(emby, "Series")
        for s in sonarr.get("/api/v3/series"):
            st = s.get("statistics", {})
            if st.get("sizeOnDisk", 0) <= min_series_gb * GB:
                continue
            rows.append({
                "kind": "series", "app": "sonarr", "id": s["id"], "title": s.get("title"),
                "size": st["sizeOnDisk"], "added": s.get("added"), "requested": _requested(s, tags),
                "seasons": sum(1 for x in s.get("seasons", [])
                               if x["seasonNumber"] > 0 and x.get("statistics", {}).get("episodeFileCount")),
                "episodes": st.get("episodeFileCount", 0), "ended": s.get("ended"),
                "monitored": s.get("monitored"), "emby": _match_series(s, emby_series),
                "action": "delete series"})
    radarr = {m.get("tmdbId"): m for m in arrs["radarr"].get("/api/v3/movie")} if arrs.get("radarr") else {}
    emby_movies = None
    for app in ("radarr", "radarr_4k"):
        arr = arrs.get(app)
        if not arr:
            continue
        tags = {t["id"]: t["label"] for t in arr.get("/api/v3/tag")}
        movies = radarr.values() if app == "radarr" else arr.get("/api/v3/movie")
        for m in movies:
            if m.get("sizeOnDisk", 0) <= min_movie_gb * GB:
                continue
            if emby_movies is None:
                emby_movies = _emby_items(emby, "Movie")
            row = {"kind": "movie", "app": app, "id": m["id"], "title": m.get("title"),
                   "size": m["sizeOnDisk"], "added": m.get("added"), "requested": _requested(m, tags),
                   "emby": _match_movie(m, emby_movies), "action": "delete movie"}
            twin = radarr.get(m.get("tmdbId")) if app == "radarr_4k" else None
            if twin:
                row["emby"] += [i for i in _match_movie(twin, emby_movies) if i not in row["emby"]]
                row["action"] = "drop 4K (1080p kept)" if twin.get("hasFile") else "drop 4K (1080p twin to download)"
            rows.append(row)
    for row in rows:
        # age: the earliest of the app's date and Emby's: radarr-4k movies and their re-added 1080p
        # twins carry the 2026-10-17 migration date, Emby remembers when the file first showed up
        dates = [d for d in [row["added"]] + [i.get("DateCreated") for i in row["emby"]] if d]
        row["added"] = max(dates, key=lambda d: _days(d, now)) if dates else None
        row["emby"] = [i["Id"] for i in row["emby"]]
        row["users"], row["runtime"] = [], 0.0
        for n, u in enumerate(users, 1):
            items = []
            for eid in row["emby"]:
                if row["kind"] == "series":
                    items += emby.get(f"/Users/{u['Id']}/Items", ParentId=eid, Recursive="true",
                                      IncludeItemTypes="Episode", EnableUserData="true",
                                      Fields="UserDataPlayCount,UserDataLastPlayedDate").get("Items", [])
                else:
                    items.append(emby.get(f"/Users/{u['Id']}/Items/{eid}"))
            if n == 1:
                # runtime of one copy: a movie in two copies isn't twice as long
                per_copy = {}
                for it in items:
                    per_copy.setdefault(it.get("SeriesId") or it.get("Id"), 0)
                    per_copy[it.get("SeriesId") or it.get("Id")] += (it.get("RunTimeTicks") or 0) / TICKS_PER_MIN
                row["runtime"] = max(per_copy.values(), default=0.0)
            w = watch_by_user(items)
            if w["uniq"] > 0:
                row["users"].append({"user": f"U{n}", "name": u.get("Name"), "uniq": min(w["uniq"], row["runtime"] or w["uniq"]),
                                     "total": w["total"], "last_days": _days(w["last"], now),
                                     "last_played": (w["last"] or "")[:10] or None})
        lasts = [u["last_days"] for u in row["users"] if u["last_days"] is not None]
        row["last_days"] = min(lasts) if lasts else None
        played = [u["last_played"] for u in row["users"] if u["last_played"]]
        row["last_played"] = max(played) if played else None
        row["age_days"] = _days(row["added"], now)
        row["viewers"], row["score"] = score(row)
    rows.sort(key=lambda r: (-r["score"], -r["size"]))
    return rows


def render(rows, names=False):
    lines = [f"{'rank':>4} {'item':<8} {'score':>5} {'size':>7} {'runtime':>7} {'viewers':>7} "
             f"{'last played':>16} {'age':>5}  action / details | per user: coverage% (hours incl. rewatches) / last played"]
    for n, r in enumerate(rows, 1):
        item = f"{PREFIX[r['app']]}{r['id']}"
        details = (f"{r['seasons']} seasons, {r['episodes']} eps, {'ended' if r['ended'] else 'continuing'}"
                   if r["kind"] == "series" else r["app"])
        if r["requested"]:
            details += ", requested"
        users = " ".join(
            f"{u['name'] if names else u['user']}:{int(100 * u['uniq'] / max(r['runtime'], 1))}%"
            f"({int(u['total'] // 60)}h)/{u['last_played'] or '?'}"
            for u in sorted(r["users"], key=lambda u: -u["uniq"])) or "-"
        last = f"{r['last_played']} {r['last_days']}d" if r["last_played"] else "never"
        age = f"{r['age_days']}d" if r["age_days"] is not None else "?"
        line = (f"{n:>4} {item:<8} {r['score']:>5} {r['size'] / GB:>5.0f}GB {r['runtime'] / 60:>6.0f}h "
                f"{r['viewers']:>7} {last:>16} {age:>5}  {r['action']}: {details} | {users}")
        if names:
            line += f" | {r['title']}"
        lines.append(line)
    total = sum(r["size"] for r in rows)
    lines.append(f"{len(rows)} items, {total / 1e12:.2f} TB")
    return "\n".join(lines)


def to_json(rows, names=False):
    keep = ("kind", "app", "id", "size", "runtime", "seasons", "episodes", "ended", "monitored",
            "requested", "action", "viewers", "last_played", "last_days", "age_days", "score")
    out = []
    for r in rows:
        o = {k: r[k] for k in keep if k in r}
        o["users"] = [{k: v for k, v in u.items() if names or k != "name"} for u in r["users"]]
        if names:
            o["title"] = r["title"]
        out.append(o)
    return json.dumps(out, indent=1)


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--names", action="store_true", help="show titles and user names (owner only)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--min-series-gb", type=float, default=100)
    p.add_argument("--min-movie-gb", type=float, default=50)
    a = p.parse_args(argv)
    arrs = {name: Arr(url, os.environ[key]) for name, (url, key) in URLS.items() if os.environ.get(key)}
    emby = Emby(EMBY_URL, os.environ["EMBY_API_KEY"])
    rows = collect(arrs, emby, a.min_series_gb, a.min_movie_gb)
    print(to_json(rows, a.names) if a.json else render(rows, a.names))


if __name__ == "__main__":
    main(sys.argv[1:])
