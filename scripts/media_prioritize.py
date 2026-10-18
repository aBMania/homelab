#!/usr/bin/env python3
"""Reorder the qBittorrent download queue: request > newly available > 4K twin (1080p) > upgrade.

Each torrent is classified from the Radarr/Sonarr(/radarr-4k) queues:
- request: the movie/series carries a Jellyseerr user tag ("<id>-<user>")
- twin_1080p: a radarr movie that also lives in radarr-4k (re-added by media:migrate-4k)
- upgrade: the movie/episode already has a file
- newly_available: anything else (monitored, no file, a release finally showed up)
Torrents the *arrs don't know about go last. Counts only, never titles.

Run through `mise run media:prioritize [--apply]` (scripts/arr.sh sets up port-forwards).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

from media_stats import URLS, Arr

QBIT_URL = "http://localhost:18080/api/v2"
ORDER = ["request", "newly_available", "twin_1080p", "upgrade"]
RANK = {c: i for i, c in enumerate(ORDER)}
REQUEST_TAG = re.compile(r"^\d+-")


class QBit:
    """Minimal qBittorrent Web API client (localhost auth bypass through the port-forward)."""

    def __init__(self, base_url=QBIT_URL):
        self.base_url = base_url

    def torrents(self):
        with urllib.request.urlopen(self.base_url + "/torrents/info") as resp:
            return json.loads(resp.read())

    def top_prio(self, hashes):
        body = urllib.parse.urlencode({"hashes": "|".join(hashes)}).encode()
        with urllib.request.urlopen(urllib.request.Request(
                self.base_url + "/torrents/topPrio", data=body)) as resp:
            resp.read()


def classify_record(app, record, tags, twins):
    if app.startswith("radarr"):
        item = record.get("movie") or {}
        has_file = item.get("hasFile", False)
    else:
        item = record.get("series") or {}
        has_file = (record.get("episode") or {}).get("hasFile", False)
    if any(REQUEST_TAG.match(tags.get(t, "")) for t in item.get("tags", [])):
        return "request"
    if app == "radarr" and item.get("tmdbId") in twins:
        return "twin_1080p"
    return "upgrade" if has_file else "newly_available"


def classify_queues(clients):
    """{torrent hash: class}; a torrent (e.g. a season pack) takes the best class of its records."""
    twins = set()
    if "radarr_4k" in clients and "radarr" in clients:
        twins = {m["tmdbId"] for m in clients["radarr_4k"].get("/api/v3/movie")}
    classes = {}
    for app, arr in clients.items():
        tags = {t["id"]: t["label"] for t in arr.get("/api/v3/tag")}
        include = "includeMovie=true" if app.startswith("radarr") else \
            "includeSeries=true&includeEpisode=true"
        for r in arr.get(f"/api/v3/queue?pageSize=2000&{include}")["records"]:
            h = (r.get("downloadId") or "").lower()
            if not h:
                continue
            c = classify_record(app, r, tags, twins)
            if h not in classes or RANK[c] < RANK[classes[h]]:
                classes[h] = c
    return classes


def plan(torrents, classes):
    """Hashes of the torrents in the download queue, grouped by class in queue order."""
    groups = {c: [] for c in ORDER + ["other"]}
    queued = sorted((t for t in torrents if t.get("priority", 0) > 0), key=lambda t: t["priority"])
    for t in queued:
        groups[classes.get(t["hash"].lower(), "other")].append(t["hash"])
    return groups


def apply(qbit, groups):
    # Each topPrio moves a group above everything: go from lowest to highest priority
    for c in reversed(ORDER):
        if groups[c]:
            qbit.top_prio(groups[c])


def _summary(groups):
    return " · ".join(f"{c} {len(groups[c])}" for c in ORDER + ["other"])


def _queue_order(torrents):
    return [t["hash"] for t in sorted((t for t in torrents if t.get("priority", 0) > 0),
                                      key=lambda t: t["priority"])]


def main(clients, qbit, apply_changes=False, wait=15):
    groups = plan(qbit.torrents(), classify_queues(clients))
    print(f"download queue: {sum(len(v) for v in groups.values())} torrents")
    print(f"  {_summary(groups)}")
    if not apply_changes:
        print("dry run: nothing changed (add --apply)")
        return
    apply(qbit, groups)
    # qBittorrent applies queue moves asynchronously: wait until the new order shows up
    top = [h for c in ORDER for h in groups[c]]
    deadline = time.monotonic() + wait
    while (done := _queue_order(qbit.torrents())[:len(top)] == top) is False \
            and time.monotonic() < deadline:
        time.sleep(0.5)
    print("queue reordered" if done
          else "queue reorder sent; qBittorrent hasn't shown the new order yet")


def clients_from_env():
    return {name: Arr(url, os.environ[key])
            for name, (url, key) in URLS.items() if os.environ.get(key)}


if __name__ == "__main__":
    main(clients_from_env(), QBit(), apply_changes="--apply" in sys.argv[1:])
