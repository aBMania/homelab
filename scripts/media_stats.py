#!/usr/bin/env python3
"""Aggregate stats of the Radarr/Sonarr libraries: counts only, never titles.

Run through `mise run media:stats [--json]` (scripts/arr.sh sets up port-forwards and API keys).
"""
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

URLS = {
    "radarr": ("http://localhost:17878", "RADARR_API_KEY"),
    "sonarr": ("http://localhost:18989", "SONARR_API_KEY"),
    "radarr_4k": ("http://localhost:17879", "RADARR_4K_API_KEY"),
}


class Arr:
    """Minimal Servarr v3 API client."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"X-Api-Key": self.api_key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)

    def put(self, path, body):
        return self._call("PUT", path, body)

    def delete(self, path):
        self._call("DELETE", path)


def audio_class(langs, original):
    """Classify a file's audio languages against the title's original language."""
    ls = set(langs)
    if not ls or ls == {"Unknown"}:
        return "unknown"
    if original in ls:
        if original == "French":
            return "french-original"
        return "multi" if "French" in ls else "vo"
    return "no-vo"


def _resolution(file):
    return file.get("quality", {}).get("quality", {}).get("resolution")


def _names(langs):
    return [l["name"] for l in langs]


def summarize_movies(movies, profiles):
    files = [(m, m["movieFile"]) for m in movies if m.get("hasFile") and m.get("movieFile")]
    return {
        "items": len(movies),
        "with_file": len(files),
        "size_tb": round(sum(f.get("size", 0) for _, f in files) / 1e12, 2),
        "profiles": dict(Counter(profiles.get(m["qualityProfileId"]) for m in movies)),
        "resolution": dict(Counter(_resolution(f) for _, f in files)),
        "audio": dict(Counter(
            audio_class(_names(f.get("languages", [])),
                        (m.get("originalLanguage") or {}).get("name"))
            for m, f in files)),
    }


def summarize_episodes(series, files_by_series, profiles):
    files = [(s, f) for s in series for f in files_by_series.get(s["id"], [])]
    return {
        "items": len(series),
        "with_file": len(files),
        "profiles": dict(Counter(profiles.get(s["qualityProfileId"]) for s in series)),
        "resolution": dict(Counter(_resolution(f) for _, f in files)),
        "audio": dict(Counter(
            audio_class(_names(f.get("languages", [])),
                        (s.get("originalLanguage") or {}).get("name"))
            for s, f in files)),
    }


def grabs_last_24h(history, now):
    since = now - timedelta(hours=24)
    return sum(1 for h in history
               if h.get("eventType") == "grabbed"
               and datetime.fromisoformat(h["date"].replace("Z", "+00:00")) > since)


def _common(arr, now):
    return {
        "queue": arr.get("/api/v3/queue?pageSize=1").get("totalRecords", 0),
        "grabs_24h": grabs_last_24h(
            arr.get("/api/v3/history?eventType=1&pageSize=200&sortKey=date&sortDirection=descending")
            .get("records", []), now),
    }


def collect(name, arr, now):
    profiles = {p["id"]: p["name"] for p in arr.get("/api/v3/qualityprofile")}
    if name == "sonarr":
        series = arr.get("/api/v3/series")
        files = {s["id"]: arr.get(f"/api/v3/episodefile?seriesId={s['id']}")
                 for s in series if s.get("statistics", {}).get("episodeFileCount")}
        summary = summarize_episodes(series, files, profiles)
    else:
        summary = summarize_movies(arr.get("/api/v3/movie"), profiles)
    return {**summary, **_common(arr, now)}


def clients_from_env():
    return {name: Arr(url, os.environ[key])
            for name, (url, key) in URLS.items() if os.environ.get(key)}


def _label(key, kind):
    if key is None:
        return "none"
    return f"{key}p" if kind == "resolution" else str(key)


def _counts(counts, kind):
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], _label(kv[0], kind)))
    return " · ".join(f"{_label(k, kind)} {v}" for k, v in ordered) or "-"


def format_text(stats):
    blocks = []
    for name, s in stats.items():
        head = [f"{s['items']} items", f"{s['with_file']} with file"]
        if "size_tb" in s:
            head.append(f"{s['size_tb']} TB")
        head += [f"queue {s['queue']}", f"{s['grabs_24h']} grabs/24h"]
        lines = [f"{name:<10} " + " · ".join(head)]
        for kind in ("profiles", "resolution", "audio"):
            lines.append(f"  {kind:<11} {_counts(s[kind], kind)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main(clients=None, fmt="text"):
    clients = clients if clients is not None else clients_from_env()
    now = datetime.now(timezone.utc)
    stats = {name: collect(name, arr, now) for name, arr in clients.items()}
    if fmt == "json":
        print(json.dumps(stats, indent=2, default=str))
    else:
        print(format_text(stats))


if __name__ == "__main__":
    main(fmt="json" if "--json" in sys.argv[1:] else "text")
