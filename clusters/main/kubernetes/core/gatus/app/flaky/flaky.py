"""Sends "flaky" ntfy messages from Gatus's history (Gatus itself only alerts on 1 h outages).

Flaky = over the last 6 h, >= 3 outages, or >= 2 outages adding up to > 5 % of the time; an
outage is >= 2 consecutive failed checks, finished and shorter than 1 h (longer ones are
Gatus's own "down" alert). At most one message per check per day,
and a "back to stable" message once the window has no outage. FLAKY_NOTIFY=false only logs.
"""
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GATUS = "http://gatus.monitoring.svc:8080"
STATE_PATH = "/state/flaky.json"
WINDOW = timedelta(hours=6)
DOWN = timedelta(hours=1)
MAX_RATIO = 0.05
MAX_OUTAGES = 3
INTERVALS = {"downloads_vpn-connected": timedelta(minutes=1)}
DEFAULT_INTERVAL = timedelta(minutes=5)


@dataclass
class Verdict:
    flaky: bool
    down_now: bool
    outage_count: int
    failed_minutes: int
    failed_ratio: float


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _runs(results, now, window, interval):
    """Failure runs (lists of timestamps) inside the window; a gap > 3 intervals ends a run."""
    points = sorted((parse_ts(r["timestamp"]), r["success"]) for r in results)
    points = [(t, ok) for t, ok in points if now - window <= t <= now]
    runs, current, previous = [], [], None
    for t, ok in points:
        if current and (ok or t - previous > 3 * interval):
            runs.append(current)
            current = []
        if not ok:
            current.append(t)
        previous = t
    if current:
        runs.append(current)
    return points, runs


def outages(results, now, window, interval):
    _, runs = _runs(results, now, window, interval)
    return [(r[0], r[-1]) for r in runs if len(r) >= 2]


def assess(results, now, interval):
    points, runs = _runs(results, now, WINDOW, interval)
    down_now = bool(runs) and bool(points) and not points[-1][1] and len(runs[-1]) * interval >= DOWN
    # Only short, finished outages count: one still open, or one that lasted 1 h (Gatus's own
    # "down" alert), is a single incident, not repeated short outages
    open_run = runs[-1] if runs and points and not points[-1][1] else None
    counted = [r for r in runs
               if len(r) >= 2 and r is not open_run and len(r) * interval < DOWN]
    failed = sum(len(r) for r in counted)
    # Share of the whole window, not of the results seen: a fresh Gatus with few results
    # isn't flaky after one short outage (5 % of 6 h = 18 min)
    ratio = failed * interval / WINDOW
    flaky = (len(counted) >= MAX_OUTAGES or (len(counted) >= 2 and ratio > MAX_RATIO)) and not down_now
    return Verdict(flaky=flaky, down_now=down_now, outage_count=len(counted),
                   failed_minutes=int(failed * interval / timedelta(minutes=1)),
                   failed_ratio=ratio)


def decide(key, verdict, state, today):
    state = dict(state)
    entry = dict(state.get(key, {}))
    kind = None
    if verdict.flaky:
        if entry.get("sent") != today.isoformat():
            kind = "flaky"
            entry["sent"] = today.isoformat()
        entry["active"] = True
    elif entry.get("active") and verdict.outage_count == 0 and not verdict.down_now:
        kind = "stable"
        entry["active"] = False
    state[key] = entry
    return kind, state


def label(key):
    return key.replace("_", "/", 1)


def message(key, verdict, kind="flaky"):
    if kind == "stable":
        return f"{label(key)} stable again: no outage in the last 6 h"
    return (f"{label(key)} flaky: down {verdict.outage_count} times / "
            f"{verdict.failed_minutes} min in the last 6 h")


def load_state(path):
    try:
        with open(path) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


class Gatus:
    def get_json(self, url):
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.load(resp)


def ntfy_sender(title, text, priority, tags):
    req = urllib.request.Request(
        f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", data=text.encode(), method="POST",
        headers={"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}", "Title": title,
                 "Priority": str(priority), "Tags": tags})
    urllib.request.urlopen(req, timeout=20).close()


def main(gatus=None, sender=None, state_path=STATE_PATH, now=None, notify=None):
    gatus = gatus or Gatus()
    sender = sender or ntfy_sender
    now = now or datetime.now(timezone.utc)
    if notify is None:
        notify = os.environ.get("FLAKY_NOTIFY", "false").lower() == "true"
    state = load_state(state_path)
    for endpoint in gatus.get_json(f"{GATUS}/api/v1/endpoints/statuses"):
        key = endpoint["key"]
        try:
            data = gatus.get_json(f"{GATUS}/api/v1/endpoints/{key}/statuses?page=1&pageSize=500")
            v = assess(data.get("results") or [], now, INTERVALS.get(key, DEFAULT_INTERVAL))
        except Exception as e:
            print(f"{key} error {type(e).__name__}", file=sys.stderr)
            continue
        kind, state = decide(key, v, state, now.date())
        print(f"{key} flaky={v.flaky} down_now={v.down_now} outages={v.outage_count} "
              f"failed_min={v.failed_minutes} ratio={v.failed_ratio:.3f} send={kind}")
        if kind is None:
            continue
        text = message(key, v, kind)
        priority, tags = (3, "warning") if kind == "flaky" else (2, "white_check_mark")
        if notify:
            try:
                sender(f"Flaky: {label(key)}" if kind == "flaky" else f"Stable: {label(key)}",
                       text, priority, tags)
            except Exception as e:  # retried at the next run
                print(f"{key} send failed: {type(e).__name__}", file=sys.stderr)
                state[key] = {**state[key], "sent": None} if kind == "flaky" else {**state[key], "active": True}
        else:
            print(f"{key} would send: {text}")
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
