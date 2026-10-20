"""Computes the checks Gatus can't express and pushes them to its external endpoints.

  probe.py vpn  every minute: downloads_vpn-connected
  probe.py all  every 5 min:  downloads_stalled, downloads_<arr>-download-client, requests_backends

Messages and logs carry counts only, never torrent or media names.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GATUS = "http://gatus.monitoring.svc:8080"
QBIT = "http://qbittorrent.media.svc:10095"
ARRS = {
    "radarr": ("http://radarr.media.svc:7878", "RADARR_API_KEY"),
    "radarr-4k": ("http://radarr-4k.media.svc:7878", "RADARR_4K_API_KEY"),
    "sonarr": ("http://sonarr.media.svc:8989", "SONARR_API_KEY"),
}
JELLYSEERR_STATUS = "http://jellyseerr.media.svc:10241/api/v1/status"

ACTIVE_STATES = {"downloading", "stalledDL", "metaDL", "forcedDL"}
DOWNLOAD_CLIENT_SOURCES = {"DownloadClientCheck", "DownloadClientStatusCheck"}


class Http:
    def request(self, method, url, data=None, headers=None, timeout=10):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()


def vpn_connected(transfer_info):
    status = transfer_info.get("connection_status", "unknown")
    return status == "connected", f"qBittorrent connection: {status}"


def stalled(torrents, transfer_info):
    """Broken = downloads that have connected seeds, yet nothing comes in.

    Seedless torrents are ignored: one dead torrent isn't "downloading broken" (Decluttarr
    removes it), and the VPN check covers a tunnel that can't reach anyone.
    """
    seeded = sum(1 for t in torrents
                 if t.get("state") in ACTIVE_STATES and t.get("num_seeds", 0) > 0)
    speed = transfer_info.get("dl_info_speed", 0)
    if seeded and speed == 0:
        return False, f"{seeded} downloads with seeds, 0 B/s"
    return True, f"{seeded} downloads with seeds, {speed} B/s"


def download_client_ok(health, app):
    if any(h.get("source") in DOWNLOAD_CLIENT_SOURCES for h in health):
        return False, f"{app}: download client unavailable"
    return True, f"{app}: download client ok"


def backends_ok(results):
    failing = [name for name in ("jellyseerr", "radarr", "sonarr") if not results.get(name)]
    if failing:
        return False, f"{', '.join(failing)} unreachable"
    return True, "jellyseerr, radarr, sonarr reachable"


def push(key, ok, error, http):
    """Returns False when Gatus never took the result (the Job then exits 1)."""
    query = urllib.parse.urlencode({"success": str(ok).lower(), "error": "" if ok else error})
    url = f"{GATUS}/api/v1/endpoints/{key}/external?{query}"
    headers = {"Authorization": f"Bearer {os.environ['GATUS_PUSH_TOKEN']}"}
    for attempt in range(3):
        try:
            http.request("POST", url, headers=headers)
            return True
        except Exception as e:  # Gatus restarting: retry, then give up quietly
            if attempt == 2:
                print(f"{key} push failed: {type(e).__name__}", file=sys.stderr)
            else:
                time.sleep(2)
    return False


def get_json(http, url, headers=None):
    _, body = http.request("GET", url, headers=headers)
    return json.loads(body)


def qbit_get(http, path):
    # qBittorrent >= 5.2 API key: no login, no session cookie
    return get_json(http, f"{QBIT}{path}", headers={"Authorization": f"Bearer {os.environ['QBIT_API_KEY']}"})


def arr_get(http, app, path):
    base, key_env = ARRS[app]
    return get_json(http, f"{base}{path}", headers={"X-Api-Key": os.environ[key_env]})


def reachable(fn):
    try:
        fn()
        return True
    except Exception:
        return False


def run(key, check, http):
    """Runs one check; any exception becomes a failed result with a short reason."""
    try:
        ok, msg = check()
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"[:200]
    print(f"{key} {'ok' if ok else 'fail'} {msg}")
    return push(key, ok, msg, http)


def main(mode, http=None):
    http = http or Http()
    qbit = {}

    def qbit_state():
        if not qbit:
            qbit["transfer"] = qbit_get(http, "/api/v2/transfer/info")
        return qbit

    if mode == "vpn":
        pushed = run("downloads_vpn-connected", lambda: vpn_connected(qbit_state()["transfer"]), http)
        return 0 if pushed else 1

    def stalled_check():
        state = qbit_state()
        torrents = qbit_get(http, "/api/v2/torrents/info?filter=all")
        return stalled(torrents, state["transfer"])

    pushed = [run("downloads_stalled", stalled_check, http)]
    for app in ARRS:
        pushed.append(run(f"downloads_{app}-download-client",
            lambda app=app: download_client_ok(arr_get(http, app, "/api/v3/health"), app), http))
    pushed.append(run("requests_backends", lambda: backends_ok({
        "jellyseerr": reachable(lambda: get_json(http, JELLYSEERR_STATUS)),
        "radarr": reachable(lambda: arr_get(http, "radarr", "/api/v3/system/status")),
        "sonarr": reachable(lambda: arr_get(http, "sonarr", "/api/v3/system/status")),
    }), http))
    return 0 if all(pushed) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))
